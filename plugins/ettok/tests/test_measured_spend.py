"""A case's spend is what it spent, not how many calls it made.

From the architecture review of 19 September 2026 (ARCH-08, the accounting
half). Every classification was charged at a flat $0.002 whatever it actually
cost, so the number a case reported as money spent was a count of calls wearing
a dollar sign.

The error was not random. Arabic costs several times the tokens of the
equivalent English, and Kurdish and Syriac more again -- so a flat rate
understates precisely the cases this system exists for, and a budget set in
dollars would be exhausted long after it said it was.

The estimate still has a job: the budget check happens before the call, and you
cannot know the price of a call you have not made. It is a reservation now, and
the measured figure settles it.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from unittest import mock

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.match import Match


def matched():
    return Match(
        fired_terms=[{'id': 1, 'term': 'x', 'category': 'slur', 'severity_weight': 9}],
        fired_tropes=[], ungated_tropes=[], topic_groups=['yazidi'],
    )


def classify(*, cost, tokens=1200):
    ctx = mock.MagicMock()
    ctx.llm.complete_structured.return_value = mock.MagicMock(
        parsed={'is_hate_speech': True, 'reason': 'r', 'severity': 9},
        usage=mock.MagicMock(cost_usd=cost, total_tokens=tokens),
    )
    return classify_mod.classify(ctx, {'text': 't', 'parent_post_text': 'p'},
                                 matched(), versions={})


def test_the_measured_cost_is_carried_back():
    assert classify(cost=0.0137).cost_usd == 0.0137


def test_the_tokens_come_back_too():
    """The figure behind the figure. A cost with no token count cannot be
    checked against a provider's own bill."""
    assert classify(cost=0.01, tokens=1200).total_tokens == 1200


def test_an_unmeasured_call_says_so_rather_than_reporting_zero():
    """None and 0.0 are different claims: one is "this was free", the other is
    "nobody knows". Charging zero for the second would let a case classify
    forever inside a spent budget."""
    assert classify(cost=None).cost_usd is None


def test_a_call_that_never_happened_costs_nothing():
    """A match-only verdict runs no model, so there is nothing to settle."""
    verdict = classify_mod.from_match_only(matched(), {})
    assert verdict.cost_usd is None
    assert verdict.total_tokens == 0


def test_the_estimate_is_still_there_for_the_reservation():
    """Deliberately pessimistic, because it is spent before it is known: an
    estimate that runs out early downgrades a run, one that runs out late
    overspends."""
    from plugins.ettok.scan import ESTIMATED_CLASSIFY_COST_USD

    assert ESTIMATED_CLASSIFY_COST_USD > 0


def test_the_run_settles_on_the_measured_figure():
    """The whole point, read off the code that does the charging."""
    import inspect

    from plugins.ettok import scan

    source = inspect.getsource(scan.run)
    assert 'verdict.cost_usd if verdict.cost_usd is not None' in source
    assert 'budget.charge(spent)' in source
