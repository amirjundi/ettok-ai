"""A provider that never answered did not clear anybody.

From the contextual implementation guide of 20 September 2026, sections 8 and
10: "Provider failure is not semantic uncertainty", and a provider outage "must
remain replayable work".

`classify()` caught its own provider errors and returned the rule-only verdict.
The caller could not tell that apart from a real answer, so it marked the job
**completed** -- and a completed job is not replayable. An outage therefore
produced, for every item in the run: a stored observation, a verdict of
`rule_candidate` or `not_assessed` that no model had ever produced, a job table
reporting no backlog, and nothing anywhere saying a model had failed to run.
Everything reported success. Nothing had happened.

The evidence still goes out -- it must not wait on a provider -- but the job
behind it stays failed, which is the only thing that will fetch the verdict
later.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import jobs as jobs_mod
from plugins.ettok import scan as scan_mod
from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.match import Match
from plugins.ettok.platform.knowledge import Knowledge


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


def matched():
    return Match(fired_terms=[{'id': 1, 'term': 'x', 'category': 'slur',
                               'severity_weight': 9}],
                 fired_tropes=[], ungated_tropes=[], topic_groups=['yazidi'])


class TestClassifyRefusesToInventAnAnswer:
    def test_a_dead_provider_raises(self):
        ctx = mock.MagicMock()
        ctx.llm.complete_structured.side_effect = RuntimeError('502 upstream')

        with pytest.raises(classify_mod.ProviderUnavailable):
            classify_mod.classify(ctx, {'text': 't'}, matched(), versions={})

    def test_the_cause_survives(self):
        """"It failed" and "it failed because no provider is configured" need
        different responses, and the second is a five-second fix."""
        ctx = mock.MagicMock()
        ctx.llm.complete_structured.side_effect = RuntimeError(
            'No enabled LLM provider for task')

        with pytest.raises(classify_mod.ProviderUnavailable) as raised:
            classify_mod.classify(ctx, {'text': 't'}, matched(), versions={})

        assert 'No enabled LLM provider' in str(raised.value)


def knowledge():
    """The real dataclass: one explicit term, one case about Yazidis.

    Not a stand-in. A stub that does not model what it replaces drifts the
    moment a field is added, and hides the bug that field was added for.
    """
    return Knowledge(
        terms=[{
            'id': 12, 'term': 'devil worshippers', 'is_explicit': True,
            'is_regex': False, 'target_group_slug': 'yazidi', 'severity_weight': 8,
            'category': 'dehumanization', 'variants': [], 'never_flag_when': [],
        }],
        tropes=[],
        cases=[{
            'id': 1, 'title': 'Sinjar anniversary', 'state': 'active',
            'target_groups': [{'slug': 'yazidi', 'name_en': 'Yazidis',
                               'topic_markers': ['sinjar'], 'background': 'b'}],
            'seed_sources': [{'kind': 'page', 'value': 'X'}],
            'limits': {'items_remaining': None, 'cost_remaining_usd': None},
            'suggests_closing': False,
        }],
        platform_release_id='rel-1',
    )


class DeadCtx:
    def __init__(self):
        self._ettok_knowledge = knowledge()
        self.llm = mock.MagicMock()
        self.llm.complete_structured.side_effect = RuntimeError('502 upstream')

    def get_config(self, key, default=None):
        return default


ITEM = [{'text': 'they are devil worshippers',
         'parent_post_text': 'remembering sinjar',
         'url': 'https://facebook.com/1', 'platform': 'facebook'}]


class TestTheRunKeepsTheWork:
    def test_the_job_stays_replayable(self, home):
        """The whole point. A completed job is never retried, so an outage that
        completed its jobs lost every verdict in the run silently."""
        scan_mod.run(DeadCtx(), items=ITEM, submit=False)

        from plugins.ettok.store import schema
        conn = schema.connect()
        assert len(jobs_mod.replayable(conn)) == 1

    def test_the_failure_is_written_down(self, home):
        scan_mod.run(DeadCtx(), items=ITEM, submit=False)

        from plugins.ettok.store import schema
        row = jobs_mod.replayable(schema.connect())[0]
        assert '502 upstream' in jobs_mod.last_error_of(row)

    def test_the_run_says_so_rather_than_reporting_success(self, home):
        result = scan_mod.run(DeadCtx(), items=ITEM, submit=False)

        assert result['classified'] == 0
        assert any('classification failed' in e for e in result['errors'])

    def test_the_evidence_still_goes_out(self, home):
        """Collection is the half that cannot be redone later: the post may be
        deleted before the provider comes back."""
        scan_mod.run(DeadCtx(), items=ITEM, submit=False)

        from plugins.ettok.store import schema
        conn = schema.connect()
        stored = conn.execute('SELECT state, excerpt FROM classification').fetchall()
        assert len(stored) == 1
        assert stored[0]['state'] == classify_mod.STATE_RULE_CANDIDATE, (
            'a rule fired and no model answered -- not a verdict either way')

    def test_nobody_was_cleared(self, home):
        """The direction that costs something. A false clearance is never
        looked at again."""
        scan_mod.run(DeadCtx(), items=ITEM, submit=False)

        from plugins.ettok.store import schema
        row = schema.connect().execute(
            'SELECT is_hate_speech, state FROM classification').fetchone()
        assert row['is_hate_speech'] is None
        assert row['state'] != classify_mod.STATE_MODEL_NEGATIVE
