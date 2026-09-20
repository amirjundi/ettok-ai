"""The two reports the agent owns: what was covered, and what was found.

From the contextual implementation guide of 20 September 2026, section 12.

These are built here, on this machine, from what this machine actually did.
The platform stores and displays the snapshot; it does not recompute it. That
division exists because the two sides can only disagree if both of them count,
and the side that knows what happened is the one that was there.

Almost all of the care here is about denominators.

"Nine findings" is not a result. "Nine out of four hundred comments read" is,
and "nine out of four hundred, with a hundred and ten never assessed because
the budget ran out" is the truthful version of the same day. A rate computed
over what was assessed, presented as a rate over what was posted, overstates
by exactly the amount nobody looked at -- and the direction of that error is
always reassuring.

The rule that follows from it, and the one easiest to break by accident: an
item nobody assessed is never a negative. Not in a count, not in a percentage,
not in a chart. A day the laptop was closed is not a day with no hate speech.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .detect import classify as classify_mod

log = logging.getLogger(__name__)

# Bumped when the shape of either report changes, so a stored snapshot can be
# read by whatever is looking at it later.
REPORT_VERSION = '2026-09-20'

# The states that are an answer, and the states that are the absence of one.
DECIDED = set(classify_mod.DECIDED_STATES)
UNRESOLVED = {
    classify_mod.STATE_NEEDS_CONTEXT,
    classify_mod.STATE_NEEDS_VISUAL,
    classify_mod.STATE_UNCERTAIN,
}
UNASSESSED = {classify_mod.STATE_NOT_ASSESSED, classify_mod.STATE_RULE_CANDIDATE}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _rows(conn, days: int, case_key: str = None) -> list:
    sql = 'SELECT * FROM classification WHERE created_at >= ?'
    params = [_since(days)]
    if case_key:
        sql += ' AND case_id = ?'
        params.append(case_key)
    return conn.execute(sql + ' ORDER BY id', params).fetchall()


def coverage(conn, *, days: int = 1, case_key: str = None) -> dict:
    """What was looked at, what was not, and what is still owed.

    Every count here has its denominator beside it, because the single most
    misleading number this system can produce is a hate-speech rate over the
    comments it managed to assess.
    """
    rows = _rows(conn, days, case_key)
    states = {}
    for row in rows:
        states[row['state'] or ''] = states.get(row['state'] or '', 0) + 1

    assessed = sum(n for state, n in states.items() if state in DECIDED)
    unresolved = sum(n for state, n in states.items() if state in UNRESOLVED)
    unassessed = sum(n for state, n in states.items() if state in UNASSESSED)
    positive = states.get(classify_mod.STATE_MODEL_POSITIVE, 0)

    out_of_scope = sum(1 for row in rows if row['scope_status'] == 'out_of_scope')

    runs = conn.execute(
        'SELECT * FROM case_run WHERE started_at >= ? ORDER BY id',
        (_since(days),),
    ).fetchall()

    # Collection outcomes, kept apart. "Completed", "stopped on budget" and
    # "died holding the case" are three different days and were one number.
    outcomes = {}
    for run in runs:
        key = run['stop_reason'] or 'interrupted'
        outcomes[key] = outcomes.get(key, 0) + 1

    backlog = conn.execute(
        "SELECT state, COUNT(*) AS n FROM classification_job "
        "WHERE state IN ('queued', 'running', 'failed') GROUP BY state"
    ).fetchall()

    pending_delivery = conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE state != 'sent'"
    ).fetchone()['n']

    errors = []
    for run in runs:
        try:
            errors.extend(json.loads(run['errors'] or '[]'))
        except (TypeError, ValueError):
            continue

    return {
        'report': 'coverage',
        'report_version': REPORT_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'covers_days': days,
        'case_id': int(case_key) if (case_key or '').isdigit() else None,

        # The denominator, named as such so nothing downstream has to guess
        # which number the others are out of.
        'observations_collected': len(rows),
        'assessed': assessed,
        'unresolved': unresolved,
        # Never folded into `assessed`, and never into a negative. An item the
        # budget did not reach and an item a model cleared are opposite facts.
        'not_assessed': unassessed,
        'out_of_scope': out_of_scope,
        'by_state': states,

        'positive': positive,
        # Spelled out rather than given as a bare percentage, because the
        # sentence is the safeguard: a reader who sees only "2.4%" will read
        # it as prevalence on the platform, which it is not.
        'positive_rate_note': (
            f'{positive} of {assessed} assessed observation(s) were judged hate '
            f'speech. {unassessed} more were collected and never assessed, and '
            f'{unresolved} could not be decided. This is a rate over what this '
            f'agent read, not over what was posted.'
            if assessed else
            f'Nothing was assessed. {len(rows)} observation(s) were collected. '
            f'A day with no assessment is not a day with no hate speech.'
        ),

        'runs': len(runs),
        'run_outcomes': outcomes,
        'posts_scanned': sum(run['posts_scanned'] for run in runs),
        'spend_usd': round(sum(run['spend'] for run in runs), 4),

        'backlog': {row['state']: row['n'] for row in backlog},
        'pending_delivery': pending_delivery,
        'errors': errors[:50],

        # Said out loud. A report generated on a laptop that was shut for four
        # days covers four days of nothing, and it has to say so rather than
        # report a quiet week.
        'gaps': _gaps(runs, days),
    }


def _gaps(runs, days: int) -> list:
    """Days inside the window with no run at all.

    No powered-off PC provides uninterrupted collection, and the failure mode
    is not that somebody notices the machine was off -- it is that they do
    not, and read the empty report as good news.
    """
    seen = set()
    for run in runs:
        try:
            seen.add(run['started_at'][:10])
        except (TypeError, IndexError):
            continue
    today = datetime.now(timezone.utc).date()
    return [
        str(today - timedelta(days=offset))
        for offset in range(days)
        if str(today - timedelta(days=offset)) not in seen
    ]


def findings(conn, *, days: int = 1, case_key: str = None,
             limit: int = 500) -> dict:
    """What was found, with the evidence and the reasoning attached.

    Only the items somebody should look at: a decided positive, a rule that
    fired with nobody behind it, and anything left unresolved. A model
    negative is in the coverage count and not here -- it is the denominator,
    and putting four hundred clearances in the findings report buries the
    nine things that matter.
    """
    rows = _rows(conn, days, case_key)
    wanted = ({classify_mod.STATE_MODEL_POSITIVE} | UNRESOLVED
              | {classify_mod.STATE_RULE_CANDIDATE})

    items = []
    for row in rows:
        if (row['state'] or '') not in wanted:
            continue
        try:
            versions = json.loads(row['versions'] or '{}')
        except (TypeError, ValueError):
            versions = {}
        items.append({
            'content_hash': row['content_hash'],
            'case_id': row['case_id'],
            'platform': row['platform'],
            'url': row['url'],
            'excerpt': row['excerpt'],
            'parent_excerpt': row['parent_excerpt'],
            'state': row['state'],
            # Null outside the two decided states, and that is the point: a
            # rule candidate is a reason to look, not a finding that somebody
            # wrote hate speech.
            'is_hate_speech': row['is_hate_speech'],
            'category': row['category'],
            'severity': row['severity'],
            'reason': row['reason'],
            'why_flagged': row['why_flagged'],
            'scope_status': row['scope_status'],
            'scope_reason': row['scope_reason'],
            # Which knowledge judged it. A finding whose release cannot be
            # named cannot be defended by anybody later.
            'versions': versions,
            'assessed_at': row['created_at'],
        })
        if len(items) >= limit:
            break

    return {
        'report': 'findings',
        'report_version': REPORT_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'covers_days': days,
        'case_id': int(case_key) if (case_key or '').isdigit() else None,
        # Carried with the findings so the two can never be read apart. A
        # findings list without the number of comments it came out of is the
        # thing this whole report exists to stop.
        'observations_collected': len(rows),
        'items': items,
        'truncated': len(items) >= limit,
        'note': (
            'Machine assessments. A human review decision, where one exists, '
            'is recorded on the platform against the same observation and is '
            'not merged into these. An AI verdict alone is not a finding of '
            'fact about anybody.'
        ),
    }
