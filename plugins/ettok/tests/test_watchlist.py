"""The agent can see who is being watched, and says what it did about them.

From the architecture review of 19 September 2026 (ARCH-11). The platform had a
"Watching" status on an observed account and nothing turned it into work: the
endpoint this agent asks for accounts serves the organisation's own login
credentials -- the accounts it signs in *as* -- and the two were confused for
long enough that marking somebody as watched produced no collection at all.

The half that lives here is a watchlist the agent can read and a sweep it has
to report. Reporting the failures matters as much as the successes: an account
whose sweep is never recorded stays due forever and takes every later run,
which starves the accounts that can still be collected -- the same starvation
the case rota prevents one level up.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

DUE = {
    'accounts': [{
        'id': 7, 'platform': 'facebook', 'handle': 'fa:1',
        'display_name': 'Someone', 'profile_url': 'https://x.test/a',
        'cases': [{'id': 3, 'title': 'Sinjar watch'}],
        'due': True, 'coverage_state': 'never_swept',
    }],
    'total': 1,
    'watched_total': 4,
}


@pytest.fixture
def ctx():
    holder = mock.MagicMock()
    holder._ettok_knowledge = None
    return holder


@pytest.fixture
def client():
    return mock.MagicMock()


@pytest.fixture
def tools(ctx, client, monkeypatch):
    import plugins.ettok as plugin

    monkeypatch.setattr(plugin, '_services',
                        lambda _ctx: (mock.MagicMock(), mock.MagicMock(), client))
    return {spec['name']: handler
            for _key, spec, handler, _icon in plugin._make_tools(ctx)}


class TestSeeingWhoIsDue:
    def test_it_lists_them(self, tools, client):
        client.watchlist.return_value = DUE
        out = json.loads(tools['ettok_watchlist']({}))
        assert out['due'] == 1
        assert out['accounts'][0]['profile_url'] == 'https://x.test/a'

    def test_it_carries_the_case_so_collection_is_attributed(self, tools, client):
        client.watchlist.return_value = DUE
        out = json.loads(tools['ettok_watchlist']({}))
        assert out['accounts'][0]['cases'] == [{'id': 3, 'title': 'Sinjar watch'}]

    def test_nothing_due_is_a_real_answer(self, tools, client):
        """Distinguishable from "nothing is watched", which is a different
        problem and the one this finding is about."""
        client.watchlist.return_value = {'accounts': [], 'total': 0, 'watched_total': 4}
        out = json.loads(tools['ettok_watchlist']({}))
        assert out['due'] == 0
        assert out['watched_total'] == 4
        assert 'inside its sweep interval' in out['note']

    def test_the_whole_list_can_be_asked_for(self, tools, client):
        client.watchlist.return_value = DUE
        tools['ettok_watchlist']({'all': True})
        assert client.watchlist.call_args.kwargs['everything'] is True


class TestReportingWhatHappened:
    def test_a_completed_sweep(self, tools, client):
        out = json.loads(tools['ettok_record_sweep'](
            {'account_id': 7, 'state': 'completed'}))
        assert out['recorded'] is True
        assert client.record_sweep.call_args.args[:2] == (7, 'completed')

    def test_a_block_is_reported_rather_than_worked_around(self, tools, client):
        """A challenge is a detection. The sweep is recorded as blocked and a
        person decides what happens next; nothing here tries to get past it."""
        out = json.loads(tools['ettok_record_sweep'](
            {'account_id': 7, 'state': 'blocked', 'note': 'checkpoint page'}))
        assert out['state'] == 'blocked'
        assert client.record_sweep.call_args.kwargs['note'] == 'checkpoint page'

    def test_an_invented_state_is_refused(self, tools, client):
        out = json.loads(tools['ettok_record_sweep']({'account_id': 7, 'state': 'fine'}))
        assert 'error' in out
        assert client.record_sweep.call_count == 0

    def test_the_refusal_explains_what_blocked_means(self, tools, client):
        """The operating rule, where somebody would meet it: a challenge is a
        detection to report, never an obstacle to solve."""
        out = json.loads(tools['ettok_record_sweep']({'account_id': 7, 'state': 'fine'}))
        assert 'never an obstacle to work around' in out['error']

    def test_a_non_numeric_account_is_refused(self, tools, client):
        out = json.loads(tools['ettok_record_sweep']({'account_id': 'seven',
                                                      'state': 'completed'}))
        assert 'error' in out
        assert client.record_sweep.call_count == 0


def test_the_watchlist_is_not_the_login_credentials(tools, client):
    """The confusion the finding is about, pinned so it cannot come back: these
    tools must ask for the observed accounts, not the ones the agent signs in
    with."""
    client.watchlist.return_value = DUE
    tools['ettok_watchlist']({})
    assert client.watchlist.call_count == 1
    assert client.accounts.call_count == 0
