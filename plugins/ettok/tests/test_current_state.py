"""What the agent is doing right now, in one place.

AGENT-01: the panel had six tabs and the four facts an operator opens it for --
which case, whether anything is running, whether anything has reached the
platform, and whether a person is needed -- were spread across four of them, each
phrased in the vocabulary of the table it came from.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.ettok.dashboard import plugin_api
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


def _iso(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _run(conn, case_id, ended=None, reason=None, scanned=0, flagged=0):
    conn.execute(
        'INSERT INTO case_run (platform_case_id, target_group_slug, started_at, '
        'ended_at, stop_reason, posts_scanned, items_flagged) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (case_id, 'yazidi', _iso(30), ended, reason, scanned, flagged))
    conn.commit()


class TestItSaysWhatIsHappening:
    def test_a_run_in_progress_names_its_case(self, home):
        conn = schema.connect()
        _run(conn, 7, scanned=12, flagged=3)

        now = plugin_api.status()['now']

        assert now['doing'] == 'Collecting now'
        assert now['case_id'] == 7
        assert '12 posts read' in now['detail']

    def test_a_finished_run_says_why_it_stopped_in_words(self, home):
        conn = schema.connect()
        _run(conn, 7, ended=_iso(5), reason='budget', scanned=40)

        now = plugin_api.status()['now']

        assert now['doing'] == 'Idle'
        assert 'spending limit' in now['detail']

    def test_never_having_run_is_not_the_same_as_idle(self, home):
        now = plugin_api.status()['now']
        assert now['doing'] == 'Has never run'


class TestDeliveryIsSeparateFromDoing:
    def test_it_reports_when_something_last_reached_the_platform(self, home):
        conn = schema.connect()
        conn.execute(
            "INSERT INTO outbox (endpoint, payload, idempotency_key, state, "
            "created_at, delivered_at) VALUES ('/x', '{}', 'k1', 'delivered', ?, ?)",
            (_iso(90), _iso(60)))
        conn.commit()

        assert plugin_api.status()['now']['last_delivery_at']

    def test_nothing_delivered_is_stated_rather_than_left_blank(self, home):
        assert plugin_api.status()['now']['last_delivery_at'] is None


class TestNextActionIsSometimesEmpty:
    """A panel that always carries an instruction trains people to skip it."""

    def test_a_signed_out_account_asks_a_person_to_sign_in(self, home):
        conn = schema.connect()
        conn.execute(
            "INSERT INTO account_health (account_id, state) VALUES ('fb-1', 'auth_lost')")
        conn.commit()

        action = plugin_api.status()['now']['next_action']
        assert 'Sign in again, yourself' in action
        # It must never offer to do it, and never to clear a challenge.
        assert 'captcha' not in action.lower()

    def test_a_healthy_run_in_progress_asks_for_nothing(self, home):
        conn = schema.connect()
        conn.execute(
            "INSERT INTO account_health (account_id, state) VALUES ('fb-1', 'healthy')")
        _run(conn, 7, scanned=3)

        assert plugin_api.status()['now']['next_action'] == ''


class TestPerCaseActivity:
    def test_a_case_carries_what_this_machine_did_on_it(self, home):
        conn = schema.connect()
        _run(conn, 7, ended=_iso(10), reason='exhausted', scanned=40, flagged=5)
        _run(conn, 7, ended=_iso(5), reason='exhausted', scanned=10, flagged=1)
        _run(conn, 9, ended=_iso(20), reason='exhausted', scanned=2)

        by_case = {row['case_id']: row for row in plugin_api.status()['by_case']}

        assert by_case[7]['runs'] == 2
        assert by_case[7]['scanned'] == 50
        assert by_case[7]['flagged'] == 6
        assert by_case[9]['scanned'] == 2

    def test_judgements_made_while_unpaired_still_show_against_the_case(self, home):
        conn = schema.connect()
        conn.execute(
            "INSERT INTO classification (content_hash, case_id, is_hate_speech, "
            "created_at) VALUES ('h1', '7', 1, ?)", (_iso(3),))
        conn.execute(
            "INSERT INTO classification (content_hash, case_id, is_hate_speech, "
            "created_at) VALUES ('h2', '7', 0, ?)", (_iso(2),))
        conn.commit()

        by_case = plugin_api.status()['by_case']
        row = [r for r in by_case if str(r['case_id']) == '7'][0]

        assert row['judged'] == 2
        assert row['hate'] == 1


class TestItDoesNotClaimTheCuratorsAreFinished:
    """The panel said "Knowledge looks complete", which this agent cannot know.

    It sees whether the required fields are populated. Whether the lexicon
    covers what people are actually saying is a curator's judgement, and an
    agent asserting otherwise is how an under-curated lexicon passes for a
    quiet week -- the same confusion the three-empty-runs alert exists to catch.
    """

    def test_the_completeness_claim_is_gone(self):
        from pathlib import Path
        bundle = Path(__file__).resolve().parents[1] / 'dashboard' / 'dist' / 'index.js'
        text = bundle.read_text(encoding='utf-8')
        assert 'Every required field is filled in' in text
        # Present only inside the comment explaining what it replaced.
        assert text.count('Knowledge looks complete') <= 1

    def test_the_release_is_stamped_for_the_reader(self):
        from pathlib import Path
        bundle = Path(__file__).resolve().parents[1] / 'dashboard' / 'dist' / 'index.js'
        text = bundle.read_text(encoding='utf-8')
        assert 'Release: ' in text
        assert 'Fetched from the platform' in text
