"""A run takes the case before working it, and gives it back afterwards.

From the architecture review of 19 September 2026 (ARCH-04). Two agents asking
for work both received the same due case and both collected it: duplicate
effort, duplicate provider spend, and two runs writing one case's counters.

Three things worth pinning, because each was a choice rather than an obvious
consequence:

* A refusal is an ordinary outcome. Somebody else got there first, and the
  right response is to stop, not to retry or to collect anyway.
* A platform that does not offer claiming does not stop the run. The two are
  released separately and the post being collected is deleted within hours,
  while a version mismatch is fixed at somebody's convenience.
* A dry run does not claim. It collects nothing anyone could duplicate, and
  making it need a network round trip would tie the offline path -- checking
  selectors, trying a comment by hand -- to the platform being reachable.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.platform.knowledge import Knowledge

CASE = {
    'id': 5, 'title': 'Sinjar watch', 'state': 'active', 'due': True,
    'target_groups': [], 'seed_sources': [],
    'limits': {'deadline_at': None, 'items_remaining': None,
               'cost_remaining_usd': None},
}

ITEM = {'text': 'ordinary comment', 'parent_post_text': 'ordinary post',
        'platform': 'facebook', 'url': 'https://x.test/p/1#c1'}


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


@pytest.fixture
def ctx(home):
    holder = mock.MagicMock()
    holder._ettok_knowledge = Knowledge(terms=[], tropes=[], cases=[CASE])
    return holder


def run(ctx, client, **over):
    kwargs = {'items': [dict(ITEM)], 'classify': False, 'submit': True}
    kwargs.update(over)
    with mock.patch('plugins.ettok.config.load', return_value={}), \
         mock.patch('plugins.ettok.platform.client.PlatformClient',
                    return_value=client), \
         mock.patch('plugins.ettok.platform.outbox.drain', return_value={}), \
         mock.patch('plugins.ettok.platform.outbox.reclaim_in_flight'):
        return scan_mod.run(ctx, **kwargs)


class TestTakingTheCase:
    def test_the_case_is_claimed_before_collecting(self, ctx):
        client = mock.MagicMock()
        run(ctx, client)
        assert client.claim_case.call_args_list[0].args == (5,)

    def test_it_is_released_when_the_run_ends(self, ctx):
        """Given back rather than left to expire, so the next run does not wait
        out a lease nobody is using."""
        client = mock.MagicMock()
        run(ctx, client)
        assert client.claim_case.call_args_list[-1].kwargs == {'release': True}


class TestWhenSomebodyElseHasIt:
    def test_the_run_stops_and_collects_nothing(self, ctx):
        client = mock.MagicMock()
        client.claim_case.side_effect = RuntimeError('409 Conflict')

        summary = run(ctx, client)
        assert summary['stop_reason'] == 'claimed_elsewhere'
        assert summary['scanned'] == 0

    def test_it_says_which_case_and_what_to_do(self, ctx):
        client = mock.MagicMock()
        client.claim_case.side_effect = RuntimeError('409 Conflict')

        note = run(ctx, client)['note']
        assert 'Sinjar watch' in note
        assert 'take a different' in note

    def test_it_does_not_try_to_release_what_it_never_held(self, ctx):
        client = mock.MagicMock()
        client.claim_case.side_effect = RuntimeError('409 Conflict')
        run(ctx, client)
        assert all(call.kwargs.get('release') is not True
                   for call in client.claim_case.call_args_list)


class TestAnOlderPlatform:
    def test_a_server_without_claiming_does_not_stop_the_run(self, ctx):
        """Losing a post that is deleted within hours, to avoid a duplicate
        that costs a few provider calls, is the wrong trade."""
        client = mock.MagicMock()
        client.claim_case.side_effect = RuntimeError('404 Not Found')

        summary = run(ctx, client)
        assert summary['stop_reason'] != 'claimed_elsewhere'
        assert summary['scanned'] == 1


class TestADryRunNeedsNoNetwork:
    def test_nothing_is_claimed_when_nothing_is_submitted(self, ctx):
        client = mock.MagicMock()
        run(ctx, client, submit=False)
        assert client.claim_case.call_count == 0
