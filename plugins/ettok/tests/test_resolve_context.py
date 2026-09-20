"""Going back for what was missing, within limits, and saying what was not.

From the contextual implementation guide of 20 September 2026, section 9.

`needs_context` was a state nothing acted on. An item entered it and stayed
there: the verdict said the post it sat under had not been read, and nobody
went and read the post. In practice it meant "a person will look at this
eventually", which is the same as not having the state at all.

Three properties, and the third is the one that keeps this honest.

It is bounded. Every page visit costs a little money, costs time, and is one
more request from an account that would rather not be noticed -- so this is a
tool somebody runs, never a loop the scan enters on its own, and it stops where
it is told to.

It produces a new assessment, not a retry. The evidence changed; the earlier
verdict was not wrong, it was right about what it could see. Both are kept.

And when the context cannot be recovered, the item stays undecided and the
limitation is reported. No invented referent, no forced negative, no automatic
positive. A retrieval pass that resolved everything it touched would be lying
about something.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import jobs as jobs_mod
from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.platform.knowledge import Knowledge
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


@pytest.fixture
def conn(home):
    return schema.connect()


PAGE = 'https://facebook.com/p/1'

ITEM = {
    'content_hash': 'h1',
    'text': 'Exactly.',
    'parent_post_text': '',
    'parent_post_url': PAGE,
    'platform': 'facebook',
    'author_id': 'fb:9',
    'author_name': 'Somebody',
    'is_reply': True,
    'reply_to_text': '',
}


def knowledge():
    return Knowledge(
        terms=[], tropes=[],
        cases=[{'id': 1, 'title': 'Sinjar', 'state': 'active',
                'target_groups': [{'slug': 'yazidi', 'name_en': 'Yazidis',
                                   'topic_markers': ['sinjar'], 'background': 'b'}],
                'seed_sources': [], 'limits': {}, 'suggests_closing': False}],
    )


def waiting(conn, missing=('parent_post',), item=None):
    """A job that finished with a request outstanding."""
    job_id = jobs_mod.enqueue(conn, item=item or ITEM, case_key='1',
                              knowledge_id='rel')
    jobs_mod.complete(conn, job_id, {
        'state': classify_mod.STATE_NEEDS_CONTEXT,
        'is_hate_speech': None,
        'missing': list(missing),
    })
    return job_id


class Collected:
    def __init__(self, items):
        self.items = items


def tools(conn, monkeypatch, *, collected=None, collector=True,
          reply={'is_hate_speech': True, 'reason': 'calls for expulsion'},
          collect_raises=None):
    import plugins.ettok as plugin
    from plugins.ettok.collect import base as collect_mod

    ctx = mock.MagicMock()
    ctx._ettok_knowledge = knowledge()
    ctx.llm.complete_structured.return_value = mock.MagicMock(
        parsed=reply, usage=mock.MagicMock(cost_usd=0.002, total_tokens=80))

    fake = mock.MagicMock()
    if collect_raises is not None:
        fake.collect.side_effect = collect_raises
    else:
        fake.collect.return_value = Collected(collected if collected is not None else [])

    monkeypatch.setattr(collect_mod, 'for_platform',
                        lambda _ctx, _p, **kw: fake if collector else None)
    monkeypatch.setattr(plugin, '_services',
                        lambda _c: (mock.MagicMock(), conn, mock.MagicMock()))
    built = {spec['name']: handler
             for _k, spec, handler, _i in plugin._make_tools(ctx)}
    return built, ctx, fake


RECOVERED = dict(ITEM, parent_post_text='a post about Sinjar',
                 reply_to_text='they should all be driven out')


# ---------------------------------------------------------------------------
# Finding the work
# ---------------------------------------------------------------------------

class TestWhatIsWaiting:
    def test_a_fetchable_gap_is_picked_up(self, conn):
        waiting(conn, missing=['parent_post'])

        found = jobs_mod.awaiting_context(
            conn, kinds=classify_mod.RECOVERABLE)
        assert len(found) == 1

    def test_an_unfetchable_one_is_not(self, conn):
        """An unresolved event is a note a curator has to write. Sending a
        browser after it spends money and finds nothing."""
        waiting(conn, missing=['event', 'referent'])

        assert jobs_mod.awaiting_context(conn, kinds=classify_mod.RECOVERABLE) == []

    def test_a_decided_verdict_is_not_picked_up(self, conn):
        job_id = jobs_mod.enqueue(conn, item=ITEM, case_key='1', knowledge_id='r')
        jobs_mod.complete(conn, job_id, {'state': 'model_positive', 'missing': []})

        assert jobs_mod.awaiting_context(conn, kinds=classify_mod.RECOVERABLE) == []

    def test_a_job_that_never_ran_is_not_picked_up(self, conn):
        """A provider outage and a missing reply are different problems, and
        one pass reporting both as the same thing helps nobody. `replayable`
        owns the first."""
        jobs_mod.start(conn, jobs_mod.enqueue(
            conn, item=ITEM, case_key='1', knowledge_id='r'))

        assert jobs_mod.awaiting_context(conn, kinds=classify_mod.RECOVERABLE) == []
        assert len(jobs_mod.replayable(conn)) == 1


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

class TestTheRetrieval:
    def test_recovered_context_decides_the_item(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[RECOVERED])

        out = json.loads(built['ettok_resolve_context']({}))

        assert out['resolved'] == 1
        assert out['pages_visited'] == 1

    def test_the_new_context_reaches_the_model(self, conn, monkeypatch):
        waiting(conn)
        built, ctx, _fake = tools(conn, monkeypatch, collected=[RECOVERED])

        built['ettok_resolve_context']({})

        prompt = ctx.llm.complete_structured.call_args.kwargs['input'][0]['text']
        assert 'a post about Sinjar' in prompt
        assert 'they should all be driven out' in prompt

    def test_it_is_a_new_assessment_not_a_retry(self, conn, monkeypatch):
        """The evidence changed. The earlier verdict was right about what it
        could see, and overwriting it would destroy the only record of what
        the agent knew at the time."""
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[RECOVERED])

        built['ettok_resolve_context']({})

        rows = conn.execute(
            'SELECT verdict_json FROM classification_job ORDER BY id').fetchall()
        assert len(rows) == 2
        assert json.loads(rows[0]['verdict_json'])['state'] == \
            classify_mod.STATE_NEEDS_CONTEXT

    def test_the_context_revision_moves(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[RECOVERED])

        built['ettok_resolve_context']({})

        latest = conn.execute(
            'SELECT item_json FROM classification_job ORDER BY id DESC LIMIT 1'
        ).fetchone()
        assert json.loads(latest['item_json'])['context_revision'] == 1

    def test_the_result_is_queued_for_the_platform(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[RECOVERED])

        built['ettok_resolve_context']({})

        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE endpoint = 'flagged-items/'"
        ).fetchone()['n']
        assert queued == 1


# ---------------------------------------------------------------------------
# When it cannot be got
# ---------------------------------------------------------------------------

class TestNothingIsForced:
    def test_a_comment_that_is_gone_stays_undecided(self, conn, monkeypatch):
        """Deleted, collapsed, or edited since. Undecided is the truthful
        outcome and the state exists to hold it."""
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[])

        out = json.loads(built['ettok_resolve_context']({}))

        assert out['resolved'] == 0 and out['still_unresolved'] == 1

    def test_a_page_that_still_shows_nothing_new_costs_no_model_call(
            self, conn, monkeypatch):
        """Identical evidence produces an identical answer. Paying for it
        again buys nothing."""
        waiting(conn)
        built, ctx, _fake = tools(conn, monkeypatch, collected=[dict(ITEM)])

        out = json.loads(built['ettok_resolve_context']({}))

        assert ctx.llm.complete_structured.call_count == 0
        assert out['still_unresolved'] == 1

    def test_a_page_that_will_not_load_is_reported(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(
            conn, monkeypatch, collect_raises=RuntimeError('login wall'))

        out = json.loads(built['ettok_resolve_context']({}))

        assert out['still_unresolved'] == 1
        assert 'login wall' in out['note']

    def test_still_undecided_with_better_evidence_is_an_outcome(
            self, conn, monkeypatch):
        """Forcing it either way is exactly what this state exists to stop."""
        waiting(conn)
        built, _ctx, _fake = tools(
            conn, monkeypatch, collected=[RECOVERED],
            reply={'is_hate_speech': None, 'reason': 'still cannot tell',
                   'needs_context': True})

        out = json.loads(built['ettok_resolve_context']({}))

        assert out['resolved'] == 0 and out['still_unresolved'] == 1

    def test_an_item_with_no_page_says_so(self, conn, monkeypatch):
        waiting(conn, item=dict(ITEM, parent_post_url=''))
        built, _ctx, _fake = tools(conn, monkeypatch)

        out = json.loads(built['ettok_resolve_context']({}))

        assert 'no post URL' in out['note']

    def test_no_collector_for_the_platform_is_reported(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collector=False)

        out = json.loads(built['ettok_resolve_context']({}))

        assert out['still_unresolved'] == 1
        assert 'No collector' in out['note']


# ---------------------------------------------------------------------------
# The bounds
# ---------------------------------------------------------------------------

class TestItStaysWithinLimits:
    def pages(self, conn, n):
        for i in range(n):
            waiting(conn, item=dict(ITEM, content_hash=f'h{i}',
                                    text=f'comment {i}',
                                    parent_post_url=f'https://facebook.com/p/{i}'))

    def test_it_stops_at_the_page_limit(self, conn, monkeypatch):
        """Every visit is one more request from an account that would rather
        not be noticed."""
        self.pages(conn, 5)
        built, _ctx, fake = tools(conn, monkeypatch, collected=[])

        out = json.loads(built['ettok_resolve_context']({'max_pages': 2}))

        assert out['pages_visited'] == 2
        assert fake.collect.call_count == 2

    def test_it_says_what_it_did_not_reach(self, conn, monkeypatch):
        self.pages(conn, 5)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[])

        out = json.loads(built['ettok_resolve_context']({'max_pages': 2}))

        assert 'Run it again' in out['note']

    def test_the_limits_are_recorded(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[])

        out = json.loads(built['ettok_resolve_context'](
            {'max_pages': 4, 'max_spend_usd': 0.1}))

        assert out['limits'] == {'max_pages': 4, 'max_spend_usd': 0.1}

    def test_an_absurd_limit_is_clamped(self, conn, monkeypatch):
        waiting(conn)
        built, _ctx, _fake = tools(conn, monkeypatch, collected=[])

        out = json.loads(built['ettok_resolve_context']({'max_pages': 9999}))

        assert out['limits']['max_pages'] == 20

    def test_it_never_runs_without_knowledge(self, conn, monkeypatch):
        import plugins.ettok as plugin

        ctx = mock.MagicMock()
        ctx._ettok_knowledge = None
        monkeypatch.setattr(plugin, '_services',
                            lambda _c: (mock.MagicMock(), conn, mock.MagicMock()))
        built = {spec['name']: handler
                 for _k, spec, handler, _i in plugin._make_tools(ctx)}

        out = json.loads(built['ettok_resolve_context']({}))
        assert 'error' in out

    def test_nothing_waiting_is_the_healthy_answer(self, conn, monkeypatch):
        built, _ctx, fake = tools(conn, monkeypatch)

        out = json.loads(built['ettok_resolve_context']({}))

        assert out['resolved'] == 0
        assert fake.collect.call_count == 0
        assert 'need a curator' in out['note']
