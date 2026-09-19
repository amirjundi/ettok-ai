# -*- coding: utf-8 -*-
"""Cases take turns, instead of one of them taking every run.

The bug this pins: `pick()` sorted by state and returned the first result, so
with two active cases the second was never scanned once. Nothing errored,
nothing logged, and the dashboard listed both as monitored -- the only symptom
was a case whose corpus stayed empty while its neighbour filled up.
"""
from plugins.ettok import cases as cases_mod


class FakeKnowledge:
    # The real Knowledge carries this; a run refuses to use one that has
    # gone stale. A stub without it does not model the thing it stands in
    # for, which is how it went unnoticed that nothing enforced freshness.
    age_seconds = 0.0

    def __init__(self, cases):
        self.cases = cases


def case(case_id, state='active', due=True, next_scan_at=''):
    return {
        'id': case_id,
        'title': f'Case {case_id}',
        'state': state,
        'due': due,
        'next_scan_at': next_scan_at,
        'target_groups': [],
        'seed_sources': [],
        'limits': {},
    }


def test_two_equal_cases_do_not_starve_each_other():
    """The regression, stated directly.

    Both active, both due, and the one that has waited longer goes first.
    """
    know = FakeKnowledge([
        case(1, next_scan_at='2026-09-13T18:00:00+00:00'),
        case(2, next_scan_at='2026-09-13T06:00:00+00:00'),
    ])
    assert cases_mod.pick(know).case_id == 2


def test_a_case_that_is_not_due_is_skipped():
    know = FakeKnowledge([
        case(1, due=False),
        case(2, due=True),
    ])
    assert cases_mod.pick(know).case_id == 2


def test_nothing_due_means_no_case_rather_than_the_wrong_one():
    """Running a case early is worse than not running: it burns the budget that
    the interval exists to spread out."""
    know = FakeKnowledge([case(1, due=False), case(2, due=False)])
    assert cases_mod.pick(know) is None


def test_a_never_scanned_case_goes_first():
    """An empty next_scan_at means it has never run, which is the most overdue
    a case can be."""
    know = FakeKnowledge([
        case(1, next_scan_at='2026-09-13T06:00:00+00:00'),
        case(2, next_scan_at=''),
    ])
    assert cases_mod.pick(know).case_id == 2


def test_a_live_campaign_still_outranks_a_quiet_watch():
    """The rota must not flatten the priority that was there before: an active
    case beats a dormant one even if the dormant one has waited longer."""
    know = FakeKnowledge([
        case(1, state='dormant', next_scan_at='2026-01-01T00:00:00+00:00'),
        case(2, state='active', next_scan_at='2026-09-13T18:00:00+00:00'),
    ])
    assert cases_mod.pick(know).case_id == 2


def test_an_explicit_case_id_ignores_the_rota():
    """An operator asking for a case now outranks the schedule."""
    know = FakeKnowledge([case(1, due=True), case(2, due=False)])
    assert cases_mod.pick(know, case_id=2).case_id == 2


def test_a_platform_that_does_not_send_due_still_works():
    """Backward compatible: an older platform sends no `due`, and its cases must
    keep running rather than all going silent."""
    payload = case(1)
    del payload['due']
    assert cases_mod.pick(FakeKnowledge([payload])).case_id == 1


def test_no_cases_at_all():
    assert cases_mod.pick(FakeKnowledge([])) is None
