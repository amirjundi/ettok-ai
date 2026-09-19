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

    # The real Knowledge carries this; a run refuses to use one that has
    # gone stale. A stub without it does not model the thing it stands in
    # for, which is how it went unnoticed that nothing enforced freshness.
    age_seconds = 0.0

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


# ---------------------------------------------------------------------------
# Identity and durability
#
# Three defects the product review found, each asserted by the thing that used
# to go wrong rather than by the shape of the fix.
# ---------------------------------------------------------------------------

def test_two_authors_posting_the_same_words_stay_two_observations(home):
    """The hash was text + parent post, and nothing else.

    Two people replying "عبدة الشيطان" under one post collapsed into one
    observation and the second was dropped as a duplicate -- undercounting a
    pile-on exactly in proportion to how coordinated it is.
    """
    shared = {
        'text': 'عبدة الشيطان', 'parent_post_text': 'منشور عن سنجار',
        'parent_post_url': 'https://facebook.com/p/1', 'platform': 'facebook',
    }
    a = {**shared, 'author_id': 'user-1', 'url': 'https://facebook.com/c/1'}
    b = {**shared, 'author_id': 'user-2', 'url': 'https://facebook.com/c/2'}

    assert scan_mod.content_hash(a) != scan_mod.content_hash(b)

    result = scan_mod.run(Ctx(), items=[a, b], classify=False, submit=False)
    assert result['flagged'] == 2
    assert result['duplicates'] == 0


def test_a_page_with_no_per_comment_link_still_deduplicates(home):
    """Collectors hand back the post URL when the page offers no permalink.

    That is the post's identity, not the comment's, so it must not make one
    comment look like two on the next run.
    """
    item = {
        'text': 'عبدة الشيطان', 'parent_post_text': 'منشور عن سنجار',
        'url': 'https://facebook.com/p/1', 'parent_post_url': 'https://facebook.com/p/1',
        'platform': 'facebook', 'author_id': 'user-1',
    }
    first = scan_mod.run(Ctx(), items=[item], classify=False, submit=False)
    second = scan_mod.run(Ctx(), items=[item], classify=False, submit=False)

    assert first['flagged'] == 1
    assert second['duplicates'] == 1


def test_the_post_and_author_links_reach_the_submission(home):
    """They were dropped by a seven-key copy of the row, so every submission
    carried them empty: no way to group comments by post, and no way to open the
    profile behind a repeat account."""
    item = {
        'text': 'عبدة الشيطان', 'parent_post_text': 'منشور عن سنجار',
        'url': 'https://facebook.com/c/1',
        'parent_post_url': 'https://facebook.com/p/1',
        'author_url': 'https://facebook.com/u/1',
        'author_id': 'user-1', 'author_name': 'someone', 'platform': 'facebook',
    }
    scan_mod.run(Ctx(), items=[item], classify=False, submit=False)

    from plugins.ettok.store import schema
    conn = schema.connect()
    row = conn.execute(
        "SELECT payload FROM outbox WHERE endpoint = 'flagged-items/'"
    ).fetchone()
    submitted = json.loads(row['payload'])['items'][0]

    assert submitted['parent_post_url'] == 'https://facebook.com/p/1'
    assert submitted['author_url'] == 'https://facebook.com/u/1'


def test_one_comment_can_belong_to_two_cases(home):
    """Two watches read the same thread. Each needs its own copy of the
    evidence; keyed on the hash alone, whichever ran first starved the other."""
    def case(case_id, title):
        return {
            'id': case_id, 'title': title, 'state': 'active',
            'target_groups': [{'slug': 'yazidi', 'topic_markers': ['سنجار']}],
            'seed_sources': [], 'limits': {}, 'suggests_closing': False,
        }

    know = FakeKnowledge(cases=[case(1, 'Anniversary'), case(2, 'Standing watch')])
    items = _items(1)

    first = scan_mod.run(Ctx(know), items=items, case_id=1, classify=False, submit=False)
    second = scan_mod.run(Ctx(know), items=items, case_id=2, classify=False, submit=False)

    assert first['flagged'] == 1
    assert second['flagged'] == 1, 'the second case saw nothing at all'


def test_the_budget_stop_does_not_swallow_the_item_that_tripped_it(home):
    """`remember()` ran before the budget check, so the item the stop fired on
    was marked seen without ever having been collected. The next run skipped it
    as a duplicate, and it was gone."""
    know = FakeKnowledge(cases=[{
        'id': 1, 'title': 'Capped', 'state': 'active',
        'target_groups': [{'slug': 'yazidi', 'topic_markers': ['سنجار']}],
        'seed_sources': [], 'limits': {'items_remaining': 1}, 'suggests_closing': False,
    }])
    items = _items(2)

    stopped = scan_mod.run(Ctx(know), items=items, classify=False, submit=False)
    assert stopped['stop_reason'] == 'item_budget'
    assert stopped['flagged'] == 1

    # The cap is per run, so the next run picks up where this one stopped.
    resumed = scan_mod.run(Ctx(know), items=items, classify=False, submit=False)
    assert resumed['flagged'] == 1, 'the item the budget stopped on was lost'


def test_nothing_is_marked_seen_until_it_is_queued(home):
    """A crash between hashing and queuing used to lose the batch permanently."""
    from plugins.ettok.store import schema

    conn = schema.connect()
    assert conn.execute('SELECT COUNT(*) c FROM seen_item').fetchone()['c'] == 0

    scan_mod.run(Ctx(), items=_items(3), classify=False, submit=False)

    seen = conn.execute('SELECT COUNT(*) c FROM seen_item').fetchone()['c']
    queued = conn.execute(
        "SELECT payload FROM outbox WHERE endpoint = 'flagged-items/'"
    ).fetchone()
    assert seen == 3
    assert len(json.loads(queued['payload'])['items']) == 3


def test_a_context_row_records_which_knowledge_read_it(home):
    """A row saying "examined, matched nothing" carried an empty verdict, so a
    curator adding a term later could not tell whether this comment had been
    read by a lexicon that already had it."""
    quiet = [{'text': 'تعليق عادي', 'parent_post_text': 'منشور عادي', 'platform': 'facebook'}]
    scan_mod.run(Ctx(), items=quiet, classify=False, submit=False)

    from plugins.ettok.store import schema
    conn = schema.connect()
    row = conn.execute(
        "SELECT payload FROM outbox WHERE endpoint = 'flagged-items/'"
    ).fetchone()
    submitted = json.loads(row['payload'])['items'][0]

    assert submitted['why_flagged'] == '', 'this is a context row, not a finding'
    assert submitted['agent_verdict']['versions']['lexicon']
    assert submitted['agent_verdict']['tier'] == 'context'


def test_a_case_that_is_not_due_collects_nothing(home):
    """`pick` returns None for two different situations and only one of them
    means "go ahead".

    Found by the first live collection pass. A second run, minutes after the
    first, found no case due -- correctly, the rota had set the next scan an
    hour out -- and then collected anyway, submitting five observations attached
    to no case. Orphaned evidence is invisible on the case screen and has nothing
    to group it under anywhere else, and running regardless also defeats the
    rota, whose whole purpose is stopping one case from taking every run.
    """
    not_due = FakeKnowledge().cases
    not_due[0]['due'] = False

    result = scan_mod.run(Ctx(FakeKnowledge(cases=not_due)), items=_items(3),
                          classify=False, submit=False)

    assert result['stop_reason'] == 'not_due'
    assert result['scanned'] == 0
    assert result['flagged'] == 0
    assert 'belong to no case' in result['note']


def test_an_operator_naming_a_case_overrides_the_rota(home):
    """Asking for a case by name is a person deciding, not the schedule."""
    not_due = FakeKnowledge().cases
    not_due[0]['due'] = False

    result = scan_mod.run(Ctx(FakeKnowledge(cases=not_due)), items=_items(1),
                          case_id=1, classify=False, submit=False)

    assert result['case'] == 'Sinjar anniversary backlash'
    assert result['scanned'] == 1
