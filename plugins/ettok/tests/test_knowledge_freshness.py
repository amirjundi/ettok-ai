"""A run does not scan against knowledge of unknown age.

From the architecture review of 19 September 2026 (ARCH-03). `scan.run` reused
whatever was cached on the context, for as long as the conversation lasted --
and these conversations last days. The skill instructs the agent to sync before
a run, but an instruction is not enforcement: a chat opened on Monday would
scan on Thursday against Monday's cases, Monday's lexicon and a case rota that
had moved on without it.

Nothing would have said so. Stale knowledge does not produce errors; it
produces confident findings, attributed to a knowledge release that was not the
one applied -- which is the same hole in the audit chain as a version ID that
does not move, reached from the other side.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import time
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


def context(knowledge):
    holder = mock.MagicMock()
    holder._ettok_knowledge = knowledge
    return holder


def run(ctx, fresh):
    """Run a scan with `fetch` stubbed, and report whether it re-fetched."""
    with mock.patch('plugins.ettok.config.load', return_value={}), \
         mock.patch('plugins.ettok.platform.client.PlatformClient'), \
         mock.patch('plugins.ettok.platform.knowledge.fetch',
                    return_value=fresh) as fetch:
        scan_mod.run(ctx, items=[], classify=False, submit=False)
    return fetch.call_count


class TestStaleKnowledgeIsRefetched:
    def test_knowledge_older_than_the_limit_is_replaced(self):
        old = Knowledge(cases=[CASE])
        old.fetched_at = time.time() - (scan_mod.MAX_KNOWLEDGE_AGE_SECONDS + 60)
        ctx = context(old)

        assert run(ctx, Knowledge(cases=[CASE])) == 1

    def test_the_run_then_uses_the_new_one(self):
        old = Knowledge(cases=[CASE])
        old.fetched_at = time.time() - (scan_mod.MAX_KNOWLEDGE_AGE_SECONDS + 60)
        ctx = context(old)
        fresh = Knowledge(cases=[CASE])

        run(ctx, fresh)
        assert ctx._ettok_knowledge is fresh

    def test_fresh_knowledge_is_kept(self):
        """A burst of runs must not re-fetch the whole lexicon each time."""
        assert run(context(Knowledge(cases=[CASE])), Knowledge(cases=[CASE])) == 0

    def test_nothing_cached_is_fetched(self):
        assert run(context(None), Knowledge(cases=[CASE])) == 1


class TestTheAgeIsHonestAboutWhereItCameFrom:
    def test_a_fresh_bundle_is_young(self):
        assert Knowledge().age_seconds < 5

    def test_a_hand_built_bundle_ages_like_any_other(self):
        """Not exempted for being hand-built. One cached for three days is
        exactly as stale as a fetched one, and the limit is about how long it
        has been held rather than how it was made."""
        held = Knowledge()
        held.fetched_at = time.time() - 3600
        assert held.age_seconds > 3500

    def test_the_limit_is_short_enough_for_a_curators_edit_to_land(self):
        """The point of the number. A curator adding a term should see it
        applied on the next run, not the next conversation."""
        assert scan_mod.MAX_KNOWLEDGE_AGE_SECONDS <= 30 * 60


def test_the_run_records_which_release_it_used():
    """A reviewer asking why two runs disagreed should not have to guess
    whether the knowledge changed between them."""
    know = Knowledge(cases=[CASE])
    ctx = context(know)
    queued = []

    with mock.patch('plugins.ettok.config.load', return_value={}), \
         mock.patch('plugins.ettok.platform.client.PlatformClient'), \
         mock.patch('plugins.ettok.platform.outbox.enqueue',
                    side_effect=lambda conn, path, payload: queued.append((path, payload))):
        scan_mod.run(ctx, items=[], classify=False, submit=False)

    logs = [payload for path, payload in queued if path == 'scan-log/']
    assert logs and logs[0]['knowledge_versions'] == know.versions
