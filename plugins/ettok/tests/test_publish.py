# -*- coding: utf-8 -*-
"""Posting to social accounts from the machine that is signed in to them.

The platform has queued `social_publish` jobs since the day it shipped and
nothing ever collected them: an account set to "a computer you run posts it from
a signed-in browser" created a job and waited for an agent that did not exist.

Most of what is asserted here is what must happen when posting does NOT work,
because those are the paths that lose an article or an account:

* a claimed job is always reported, or the platform believes a machine still has
  it and nobody else picks it up;
* a challenge from the platform quarantines the account and stops, rather than
  being cleared or retried elsewhere;
* a failure hands the post back rather than dropping it, because the platform
  turns a failed agent job into a manual one for a person.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from plugins.ettok import publish as publish_mod
from plugins.ettok.collect import session as session_mod
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


class FakeClient:
    """Records what was reported, which is the contract under test."""

    def __init__(self, jobs=None):
        self._jobs = jobs or []
        self.reported = []

    def claim_jobs(self, kind, limit=5):
        return {'jobs': self._jobs}

    def report_job(self, job_id, status, result=None):
        self.reported.append((job_id, status, result or {}))
        return {'status': 'recorded'}


def _job(job_id=1, account_id='fb-1', caption='A headline'):
    return {
        'id': job_id,
        'kind': 'social_publish',
        'payload': {
            'platform': 'facebook', 'account_id': account_id,
            'account_name': 'Ettok page', 'caption': caption,
            'media_url': '', 'link': 'https://ettok.net/a',
        },
    }


class TestAPostThatWorks:
    def test_it_reports_done_with_the_url(self, home):
        conn = schema.connect()
        client = FakeClient()

        out = publish_mod.handle(
            conn, client, _job(),
            post=lambda payload: {'posted': True, 'url': 'https://fb.test/1'})

        assert out['outcome'] == 'posted'
        assert client.reported == [(1, 'done', {'url': 'https://fb.test/1',
                                                'posted': True})]

    def test_a_success_keeps_the_account_healthy(self, home):
        conn = schema.connect()
        publish_mod.handle(conn, FakeClient(), _job(),
                           post=lambda payload: {'posted': True, 'url': 'x'})
        assert session_mod.account_state(conn, 'fb-1') == 'healthy'


class TestTheChallengeRule:
    """A checkpoint is the platform noticing the automation. It is a detection
    signal, not an obstacle, and the response is to stop."""

    CHECKPOINT = 'Please complete this security check to continue'

    def test_a_challenge_quarantines_the_account(self, home):
        conn = schema.connect()
        client = FakeClient()

        out = publish_mod.handle(
            conn, client, _job(),
            post=lambda payload: {'posted': False, 'page_text': self.CHECKPOINT})

        assert out['outcome'] == 'blocked'
        assert session_mod.account_state(conn, 'fb-1') != 'healthy'

    def test_it_does_not_retry_the_same_post(self, home):
        conn = schema.connect()
        client = FakeClient()
        calls = []

        def post(payload):
            calls.append(payload)
            return {'posted': False, 'page_text': self.CHECKPOINT}

        publish_mod.handle(conn, client, _job(), post=post)
        assert len(calls) == 1, 'a blocked attempt is not tried again'

    def test_the_job_goes_back_rather_than_being_dropped(self, home):
        conn = schema.connect()
        client = FakeClient()
        publish_mod.handle(
            conn, client, _job(),
            post=lambda payload: {'posted': False, 'page_text': self.CHECKPOINT})

        job_id, status, result = client.reported[0]
        assert status == 'failed'
        assert 'blocked' in result['reason']

    def test_the_run_says_plainly_not_to_work_around_it(self, home, monkeypatch):
        """The instruction has to survive into whatever a person reads next.

        Whoever sees this summary is the person most likely to be tempted to go
        and clear the challenge by hand, which is the one thing that must not
        happen: a human clearing a checkpoint that this agent's own request
        triggered is the same automation with an extra step.
        """
        client = FakeClient([_job()])
        monkeypatch.setattr(publish_mod, 'PlatformClient', lambda cfg: client,
                            raising=False)
        monkeypatch.setattr('plugins.ettok.platform.client.PlatformClient',
                            lambda cfg: client)

        class Ctx:
            def get_config(self, key, default=None):
                return default

        summary = publish_mod.run(
            Ctx(),
            post=lambda payload: {'posted': False, 'page_text': self.CHECKPOINT})

        assert summary['blocked'] == 1
        assert 'do not clear it' in summary['note']
        assert 'not retry from another machine' in summary['note']


class TestAnAccountAlreadyOut:
    def test_a_quarantined_account_is_not_used(self, home):
        conn = schema.connect()
        session_mod.quarantine(conn, 'fb-1', 'checkpoint earlier today')
        client = FakeClient()
        calls = []

        publish_mod.handle(conn, client, _job(),
                           post=lambda p: calls.append(p) or {'posted': True})

        assert calls == [], 'the browser is not opened for an account in quarantine'
        assert client.reported[0][1] == 'failed'


class TestNothingIsEverLeftClaimed:
    """The platform turns a failed agent job into a manual one. A job that is
    never reported is the only way an article disappears entirely."""

    def test_an_exception_is_still_reported(self, home):
        conn = schema.connect()
        client = FakeClient()

        def post(payload):
            raise RuntimeError('the composer never loaded')

        out = publish_mod.handle(conn, client, _job(), post=post)

        assert out['outcome'] == 'failed'
        assert client.reported[0][1] == 'failed'
        assert 'composer' in client.reported[0][2]['reason']

    def test_an_unconfirmed_post_is_reported_rather_than_assumed(self, home):
        conn = schema.connect()
        client = FakeClient()

        out = publish_mod.handle(conn, client, _job(),
                                 post=lambda p: {'posted': False})

        assert out['outcome'] == 'not_confirmed'
        assert client.reported[0][1] == 'failed'

    def test_one_bad_job_does_not_strand_the_next(self, home):
        conn = schema.connect()
        client = FakeClient()
        seen = []

        def post(payload):
            seen.append(payload['account_id'])
            if payload['account_id'] == 'fb-1':
                raise RuntimeError('boom')
            return {'posted': True, 'url': 'ok'}

        for job in (_job(1, 'fb-1'), _job(2, 'fb-2')):
            publish_mod.handle(conn, client, job, post=post)

        assert seen == ['fb-1', 'fb-2']
        assert [r[1] for r in client.reported] == ['failed', 'done']
