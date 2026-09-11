"""The run loop: the budget ladder, deduplication, and not losing anything.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from plugins.ettok import cases as cases_mod
from plugins.ettok import scan as scan_mod

REFUGE = 'اعوذ بالله من الشيطان الرجيم'


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


class FakeKnowledge:
    versions = {'lexicon': 'test'}

    def __init__(self, cases=None):
        self.terms = [{
            'id': 12, 'term': 'عبدة الشيطان', 'is_explicit': True, 'is_regex': False,
            'target_group_slug': 'yazidi', 'severity_weight': 8,
            'category': 'dehumanization', 'variants': [], 'never_flag_when': [],
        }]
        self.tropes = []
        self.cases = cases if cases is not None else [{
            'id': 1, 'title': 'Sinjar anniversary backlash', 'state': 'active',
            'target_groups': [{'slug': 'yazidi', 'topic_markers': ['سنجار'], 'background': 'b'}],
            'seed_sources': [{'kind': 'page', 'value': 'X'}],
            'limits': {'items_remaining': None, 'cost_remaining_usd': None},
            'suggests_closing': False,
        }]

    def group_markers(self):
        return {'yazidi': ['سنجار', 'الايزيدية']}

    def tropes_for(self, slug):
        return self.tropes


class Ctx:
    """Minimal stand-in. No network, no model."""

    def __init__(self, knowledge=None):
        self._ettok_knowledge = knowledge or FakeKnowledge()

    def get_config(self, key, default=None):
        return default


def _items(n=1):
    return [{
        'text': 'عبدة الشيطان', 'parent_post_text': f'منشور عن سنجار رقم {i}',
        'url': f'https://facebook.com/{i}', 'platform': 'facebook',
    } for i in range(n)]


# ---------------------------------------------------------------------------
# The budget ladder
# ---------------------------------------------------------------------------

def test_a_zero_budget_month_still_produces_findings(home):
    """The property the whole ladder exists for.

    Model spend is donation-funded and some months there is none. Collection is
    the time-sensitive half -- a deleted post cannot be collected next month when
    a donation arrives -- so a run without budget must still deliver evidence.
    """
    result = scan_mod.run(Ctx(), items=_items(2), classify=False, submit=False)

    assert result['flagged'] == 2
    assert result['classified'] == 0
    assert result['match_only'] == 2
    assert result['spend_usd'] == 0.0
    assert result['queue']['pending'] >= 1, 'findings were not queued for delivery'


def test_budget_allows_free_work_even_when_exhausted():
    budget = cases_mod.Budget(remaining_usd=0.0)
    assert budget.exhausted
    assert budget.allows(cases_mod.TIER_FREE), 'the free tier must never be gated'
    assert not budget.allows(cases_mod.TIER_CLASSIFY)


def test_an_unset_budget_is_unlimited_not_zero():
    """Null means "no limit on this axis", which is a choice, not a default."""
    assert not cases_mod.Budget(remaining_usd=None).exhausted


# ---------------------------------------------------------------------------
# Not losing, not duplicating
# ---------------------------------------------------------------------------

def test_the_same_comment_is_not_processed_twice(home):
    """Otherwise every run resubmits everything it has ever seen."""
    ctx = Ctx()
    first = scan_mod.run(ctx, items=_items(1), classify=False, submit=False)
    second = scan_mod.run(ctx, items=_items(1), classify=False, submit=False)

    assert first['flagged'] == 1
    assert second['duplicates'] == 1
    assert second['flagged'] == 0


def test_the_same_words_under_a_different_post_are_a_different_finding(home):
    """Context-dependent detection means the parent post is part of the identity."""
    a = {'text': 'عبدة الشيطان', 'parent_post_text': 'منشور عن سنجار', 'platform': 'facebook'}
    b = {'text': 'عبدة الشيطان', 'parent_post_text': 'منشور عن لالش', 'platform': 'facebook'}
    assert scan_mod.content_hash(a) != scan_mod.content_hash(b)


def test_everything_is_queued_before_anything_is_sent(home):
    """The previous attempt ran a year and stored nothing. Queue first, always."""
    result = scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)
    assert result['queue']['pending'] >= 2, 'expected the findings and the scan log queued'
    assert 'delivery' not in result


def test_a_scan_log_is_recorded_even_when_nothing_matched(home):
    """"Ran and found nothing" and "did not run" must not look the same."""
    quiet = [{'text': 'تعليق عادي', 'parent_post_text': 'منشور عادي', 'platform': 'facebook'}]
    result = scan_mod.run(Ctx(), items=quiet, classify=False, submit=False)
    assert result['flagged'] == 0
    assert result['queue']['pending'] >= 1


# ---------------------------------------------------------------------------
# Case limits and honesty about capability
# ---------------------------------------------------------------------------

def test_the_item_budget_stops_the_run_and_records_why(home):
    """Every ending records which of six reasons it was."""
    know = FakeKnowledge(cases=[{
        'id': 1, 'title': 'Capped', 'state': 'active',
        'target_groups': [{'slug': 'yazidi', 'topic_markers': ['سنجار']}],
        'seed_sources': [], 'limits': {'items_remaining': 1}, 'suggests_closing': False,
    }])
    result = scan_mod.run(Ctx(know), items=_items(5), classify=False, submit=False)

    assert result['stop_reason'] == 'item_budget'
    assert result['flagged'] == 1


def test_readiness_reports_groups_that_cannot_be_recognised(home):
    """A group with no topic markers cannot be found in a feed at all.

    Its tropes then never get a chance to fire, however well curated. An operator
    should learn that from the agent rather than from a month of empty reports.
    """
    know = FakeKnowledge(cases=[{
        'id': 1, 'title': 'Unmarked', 'state': 'active',
        'target_groups': [{'slug': 'shabak', 'topic_markers': []}],
        'seed_sources': [], 'limits': {}, 'suggests_closing': False,
    }])
    result = scan_mod.run(Ctx(know), items=_items(1), classify=False, submit=False)

    readiness = result['readiness']
    assert readiness['groups_without_topic_markers'] == ['shabak']
    assert readiness['can_find_content'] is False


def test_a_case_suggesting_closure_is_reported_not_acted_on(home):
    """The agent proposes; a human closes."""
    know = FakeKnowledge(cases=[{
        'id': 1, 'title': 'Quiet', 'state': 'active',
        'target_groups': [{'slug': 'yazidi', 'topic_markers': ['سنجار']}],
        'seed_sources': [], 'limits': {}, 'suggests_closing': True,
    }])
    result = scan_mod.run(Ctx(know), items=_items(1), classify=False, submit=False)
    assert 'note' in result and 'cannot close a case itself' in result['note']


def test_no_open_case_is_not_an_error(home):
    """A deployment with no case is valid; it simply has nothing to attribute to."""
    result = scan_mod.run(Ctx(FakeKnowledge(cases=[])), items=_items(1),
                          classify=False, submit=False)
    assert result['case'] is None
    assert result['flagged'] == 1
