"""The day's two reports, and the denominators that make them honest.

From the contextual implementation guide of 20 September 2026, section 12.

"Nine findings" is not a result. "Nine out of four hundred comments read" is,
and "nine out of four hundred, with a hundred and ten never assessed because
the budget ran out" is the truthful version of the same day.

Two rules follow, and both are easy to break by accident because breaking them
always makes the numbers look better.

An item nobody assessed is never a negative. Not in a count, not in a
percentage. A day the laptop was shut is not a day with no hate speech, and
the failure mode is not that somebody notices the machine was off -- it is
that nobody does, and reads the empty report as good news.

And coverage and findings stay apart. Merged, they produce the one document
this system must never produce: a list of findings with no denominator, which
reads as a measurement of a platform rather than a sample of one agent's day.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.ettok import report as report_mod
from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(Path(tempfile.mkdtemp())))


@pytest.fixture
def conn(home):
    return schema.connect()


def judged(conn, state, *, case='1', scope='in_scope', text='a comment',
           when=None):
    conn.execute(
        'INSERT INTO classification(content_hash, case_id, platform, url, '
        'excerpt, state, scope_status, scope_reason, is_hate_speech, versions, '
        'created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (f'h-{text}-{state}', case, 'facebook', 'https://x.test/1', text, state,
         scope, 'r',
         1 if state == classify_mod.STATE_MODEL_POSITIVE else
         (0 if state == classify_mod.STATE_MODEL_NEGATIVE else None),
         json.dumps({'lexicon': '1@aaa', 'release': 'rel-1'}),
         when or datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def a_run(conn, *, when=None, stop='completed', spend=0.02, errors=()):
    conn.execute(
        'INSERT INTO case_run(platform_case_id, started_at, ended_at, '
        'stop_reason, posts_scanned, items_flagged, spend, errors) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (1, when or datetime.now(timezone.utc).isoformat(), None, stop, 3, 1,
         spend, json.dumps(list(errors))),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

class TestTheDenominator:
    def test_everything_collected_is_the_denominator(self, conn):
        for state in (classify_mod.STATE_MODEL_POSITIVE,
                      classify_mod.STATE_MODEL_NEGATIVE,
                      classify_mod.STATE_NOT_ASSESSED):
            judged(conn, state)

        out = report_mod.coverage(conn)
        assert out['observations_collected'] == 3

    def test_unassessed_items_are_not_negatives(self, conn):
        """The rule that is easiest to break by accident, because breaking it
        always makes the numbers look better."""
        judged(conn, classify_mod.STATE_NOT_ASSESSED, text='one')
        judged(conn, classify_mod.STATE_NOT_ASSESSED, text='two')

        out = report_mod.coverage(conn)
        assert out['assessed'] == 0
        assert out['not_assessed'] == 2

    def test_a_rule_candidate_is_not_assessed_either(self, conn):
        """A curated word fired and no model read it. That is a reason to
        look, not a finding and not a clearance."""
        judged(conn, classify_mod.STATE_RULE_CANDIDATE)

        out = report_mod.coverage(conn)
        assert out['not_assessed'] == 1 and out['assessed'] == 0

    def test_undecided_states_are_their_own_count(self, conn):
        for state in (classify_mod.STATE_NEEDS_CONTEXT,
                      classify_mod.STATE_NEEDS_VISUAL,
                      classify_mod.STATE_UNCERTAIN):
            judged(conn, state, text=state)

        out = report_mod.coverage(conn)
        assert out['unresolved'] == 3
        assert out['assessed'] == 0

    def test_the_rate_is_stated_as_a_sentence(self, conn):
        """A reader who sees only "2.4%" reads it as prevalence on the
        platform, which it is not. The sentence is the safeguard."""
        judged(conn, classify_mod.STATE_MODEL_POSITIVE)
        judged(conn, classify_mod.STATE_MODEL_NEGATIVE)
        judged(conn, classify_mod.STATE_NOT_ASSESSED)

        note = report_mod.coverage(conn)['positive_rate_note']
        assert '1 of 2 assessed' in note
        assert '1 more were collected and never assessed' in note
        assert 'not over what was posted' in note

    def test_a_day_with_no_assessment_says_so(self, conn):
        judged(conn, classify_mod.STATE_NOT_ASSESSED)

        note = report_mod.coverage(conn)['positive_rate_note']
        assert 'not a day with no hate speech' in note


class TestWhatTheRunsDid:
    def test_stop_reasons_are_kept_apart(self, conn):
        """"Completed", "stopped on budget" and "died holding the case" are
        three different days and were one number."""
        a_run(conn, stop='completed')
        a_run(conn, stop='item_budget')

        out = report_mod.coverage(conn)
        assert out['run_outcomes'] == {'completed': 1, 'item_budget': 1}

    def test_a_run_that_never_ended_is_not_a_completed_one(self, conn):
        conn.execute(
            'INSERT INTO case_run(platform_case_id, started_at, posts_scanned, '
            'items_flagged, spend) VALUES (1, ?, 0, 0, 0)',
            (datetime.now(timezone.utc).isoformat(),))
        conn.commit()

        assert report_mod.coverage(conn)['run_outcomes'] == {'interrupted': 1}

    def test_errors_are_carried_not_counted(self, conn):
        a_run(conn, errors=['classification failed: 502'])

        assert '502' in report_mod.coverage(conn)['errors'][0]

    def test_spend_is_summed_from_what_was_recorded(self, conn):
        a_run(conn, spend=0.02)
        a_run(conn, spend=0.03)

        assert report_mod.coverage(conn)['spend_usd'] == 0.05


class TestDaysWithNoRunAtAll:
    def test_a_missing_day_is_named(self, conn):
        """No powered-off PC provides uninterrupted collection, and the
        failure is that nobody notices."""
        out = report_mod.coverage(conn, days=3)

        assert len(out['gaps']) == 3

    def test_a_day_that_ran_is_not_in_the_gaps(self, conn):
        a_run(conn)

        out = report_mod.coverage(conn, days=1)
        assert out['gaps'] == []


class TestSourcesAreADenominatorToo:
    def test_due_and_watched_are_both_reported(self, conn):
        """Nine findings from one account out of twenty watched is a
        different day from nine out of twenty covered."""
        out = report_mod.coverage(conn, sources=[
            {'id': 1, 'is_due': True, 'coverage_state': 'due'},
            {'id': 2, 'is_due': False, 'coverage_state': 'covered'},
            {'id': 3, 'is_due': True, 'coverage_state': 'overdue'},
        ])

        assert out['sources_watched'] == 3
        assert out['sources_due'] == 2
        assert out['sources_overdue'] == 1

    def test_a_source_nobody_could_reach_is_counted(self, conn):
        out = report_mod.coverage(conn, sources=[
            {'id': 1, 'is_due': True, 'coverage_state': 'quarantined'}])

        assert out['sources_blocked'] == 1

    def test_an_unreadable_watchlist_says_so_rather_than_reporting_zero(self, conn):
        """Zero due and "could not ask" look identical as a number, and one
        of them reads as a thorough day."""
        out = report_mod.coverage(conn, sources=None)

        assert 'gap in the report, not a quiet day' in out['sources_note']

    def test_visiting_one_post_is_not_covering_a_source(self, conn):
        out = report_mod.coverage(conn, sources=[
            {'id': 1, 'is_due': True, 'coverage_state': 'due'}])

        assert 'not covered because one post under it was read' in out['sources_note']


class TestScope:
    def test_out_of_scope_is_counted_separately(self, conn):
        """A skipped football thread and a quiet one are different days."""
        judged(conn, classify_mod.STATE_NOT_ASSESSED, scope='out_of_scope')

        out = report_mod.coverage(conn)
        assert out['out_of_scope'] == 1

    def test_one_case_can_be_reported_alone(self, conn):
        judged(conn, classify_mod.STATE_MODEL_POSITIVE, case='1', text='a')
        judged(conn, classify_mod.STATE_MODEL_POSITIVE, case='2', text='b')

        assert report_mod.coverage(conn, case_key='1')['observations_collected'] == 1


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

class TestFindings:
    def test_a_positive_is_in_it(self, conn):
        judged(conn, classify_mod.STATE_MODEL_POSITIVE)

        assert len(report_mod.findings(conn)['items']) == 1

    def test_a_clearance_is_not(self, conn):
        """Four hundred clearances in the findings report bury the nine
        things that matter. They are the denominator, and they are in
        coverage."""
        judged(conn, classify_mod.STATE_MODEL_NEGATIVE)

        assert report_mod.findings(conn)['items'] == []

    def test_an_unresolved_item_is(self, conn):
        judged(conn, classify_mod.STATE_NEEDS_CONTEXT)

        assert len(report_mod.findings(conn)['items']) == 1

    def test_a_rule_candidate_is_carried_without_a_verdict(self, conn):
        """A reason to look, not a finding that somebody wrote hate speech."""
        judged(conn, classify_mod.STATE_RULE_CANDIDATE)

        item = report_mod.findings(conn)['items'][0]
        assert item['is_hate_speech'] is None
        assert item['state'] == classify_mod.STATE_RULE_CANDIDATE

    def test_the_denominator_travels_with_the_findings(self, conn):
        """A findings list without the number of comments it came out of is
        the thing this report exists to stop."""
        judged(conn, classify_mod.STATE_MODEL_POSITIVE, text='a')
        for i in range(5):
            judged(conn, classify_mod.STATE_MODEL_NEGATIVE, text=f'n{i}')

        out = report_mod.findings(conn)
        assert len(out['items']) == 1
        assert out['observations_collected'] == 6

    def test_each_finding_names_the_knowledge_that_judged_it(self, conn):
        """A finding whose release cannot be named cannot be defended later."""
        judged(conn, classify_mod.STATE_MODEL_POSITIVE)

        assert report_mod.findings(conn)['items'][0]['versions']['release'] == 'rel-1'

    def test_it_says_a_verdict_is_not_a_finding_of_fact(self, conn):
        judged(conn, classify_mod.STATE_MODEL_POSITIVE)

        assert 'not a finding of fact' in report_mod.findings(conn)['note']

    def test_truncation_is_declared(self, conn):
        for i in range(4):
            judged(conn, classify_mod.STATE_MODEL_POSITIVE, text=f'c{i}')

        out = report_mod.findings(conn, limit=2)
        assert len(out['items']) == 2 and out['truncated'] is True


class TestTheyStayApart:
    def test_coverage_carries_no_item_list(self, conn):
        judged(conn, classify_mod.STATE_MODEL_POSITIVE)

        assert 'items' not in report_mod.coverage(conn)

    def test_both_name_themselves(self, conn):
        assert report_mod.coverage(conn)['report'] == 'coverage'
        assert report_mod.findings(conn)['report'] == 'findings'

    def test_both_are_versioned(self, conn):
        """A stored snapshot has to be readable by whatever looks at it in a
        year, and the shape will have moved by then."""
        assert report_mod.coverage(conn)['report_version'] == report_mod.REPORT_VERSION
        assert report_mod.findings(conn)['report_version'] == report_mod.REPORT_VERSION


class TestTheWindow:
    def test_older_rows_are_outside_it(self, conn):
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        judged(conn, classify_mod.STATE_MODEL_POSITIVE, when=old, text='old')
        judged(conn, classify_mod.STATE_MODEL_POSITIVE, text='new')

        assert report_mod.coverage(conn, days=1)['observations_collected'] == 1
        assert report_mod.coverage(conn, days=30)['observations_collected'] == 2
