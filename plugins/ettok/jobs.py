"""Classification work that outlives the process doing it.

From the architecture review of 19 September 2026 (ARCH-10). A run stored its
observations first and judged them second, which is the right order -- storage
is cheap and local, judgement needs a provider that may be down. But the
judging half left no trace. A model outage, or a laptop closed mid-run, and
those items simply had no verdict: on every screen indistinguishable from items
the agent had not reached yet, with nothing anywhere recording that they had
been given up on.

The fix is not a retry loop. A retry inside the run still dies with the run.
What is needed is that the intention to classify something is written down
before the attempt, so that after a crash there is a list of what was in
flight.

Three properties worth stating, because each was a real failure somewhere:

* The item payload is stored, not a reference to it. Replay happens hours or
  days later and the post is usually deleted by then -- a job holding only a
  URL is a job that cannot be replayed.
* Jobs are keyed by observation, case and knowledge release together. The same
  comment re-judged under an edited lexicon is a new job, so an earlier verdict
  is a version rather than something overwritten.
* A job that fails keeps its error. "It failed" and "it failed because no
  provider is configured" need different responses, and the second is a
  five-second fix that somebody has to be told about.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

QUEUED = 'queued'
RUNNING = 'running'
COMPLETED = 'completed'
FAILED = 'failed'

# Attempts before a job stops being retried automatically. It stays in the
# table, visibly failed, because a job that disappears after three tries is the
# silent loss this module exists to stop -- it just stops costing a provider
# call on every run.
MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(conn, *, item: dict, case_key: str, knowledge_id: str) -> int:
    """Record the intention to classify one observation. Returns the job id.

    Idempotent on (observation, case, knowledge release): re-submitting the same
    comment under the same knowledge finds the existing job rather than making a
    second one, so a re-run does not pay twice for the same verdict.
    """
    digest = item.get('content_hash') or ''
    existing = conn.execute(
        'SELECT id, state FROM classification_job '
        'WHERE content_hash = ? AND case_key = ? AND knowledge_id = ?',
        (digest, case_key, knowledge_id),
    ).fetchone()
    if existing is not None:
        return existing['id']

    cursor = conn.execute(
        'INSERT INTO classification_job(content_hash, case_key, knowledge_id, '
        'item_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (digest, case_key, knowledge_id,
         json.dumps(item, ensure_ascii=False), QUEUED, _now(), _now()),
    )
    conn.commit()
    return cursor.lastrowid


def start(conn, job_id: int) -> None:
    """Mark a job as being attempted, before the attempt.

    Written first on purpose. A job left `running` by a crash is the signal that
    something died holding it; if the state were only written afterwards, a
    crash would leave it looking untouched and indistinguishable from work that
    had not started.
    """
    conn.execute(
        'UPDATE classification_job SET state = ?, attempts = attempts + 1, '
        'updated_at = ? WHERE id = ?',
        (RUNNING, _now(), job_id),
    )
    conn.commit()


def complete(conn, job_id: int, verdict_payload: dict) -> None:
    conn.execute(
        'UPDATE classification_job SET state = ?, verdict_json = ?, last_error = ?, '
        'updated_at = ? WHERE id = ?',
        (COMPLETED, json.dumps(verdict_payload, ensure_ascii=False), '', _now(), job_id),
    )
    conn.commit()


def fail(conn, job_id: int, error: str) -> None:
    conn.execute(
        'UPDATE classification_job SET state = ?, last_error = ?, updated_at = ? '
        'WHERE id = ?',
        (FAILED, str(error)[:1000], _now(), job_id),
    )
    conn.commit()


def replayable(conn, limit: int = 50) -> list:
    """Jobs worth attempting again, oldest first.

    `running` is included and is the important one: nothing is running once the
    process that owned it is gone, so a row still in that state is work that
    died mid-flight. Attempted oldest first so a burst of failures cannot push
    an older item out of reach indefinitely.
    """
    return conn.execute(
        'SELECT * FROM classification_job WHERE state IN (?, ?, ?) '
        'AND attempts < ? ORDER BY created_at LIMIT ?',
        (QUEUED, RUNNING, FAILED, MAX_ATTEMPTS, limit),
    ).fetchall()


def status(conn) -> dict:
    """What the local queue holds, for an operator asking whether anything is stuck."""
    rows = conn.execute(
        'SELECT state, COUNT(*) AS n FROM classification_job GROUP BY state'
    ).fetchall()
    counts = {row['state']: row['n'] for row in rows}
    exhausted = conn.execute(
        'SELECT COUNT(*) AS n FROM classification_job WHERE state = ? AND attempts >= ?',
        (FAILED, MAX_ATTEMPTS),
    ).fetchone()['n']
    return {
        'queued': counts.get(QUEUED, 0),
        'running': counts.get(RUNNING, 0),
        'completed': counts.get(COMPLETED, 0),
        'failed': counts.get(FAILED, 0),
        # Failed and no longer retried on their own. These need a person, and
        # are the number worth putting in front of one.
        'needs_attention': exhausted,
    }


def item_of(row) -> dict:
    """The observation a job was created for."""
    try:
        return json.loads(row['item_json'])
    except (TypeError, ValueError):
        return {}


def last_error_of(row) -> str:
    return row['last_error'] or ''


def forget_completed(conn, *, keep_last: int = 500) -> int:
    """Trim finished jobs, keeping a recent tail.

    The tail is kept because a completed job is the only local record of what
    was sent and under which knowledge release, and this machine is where an
    operator looks when they want to answer that without asking the platform.
    """
    cursor = conn.execute(
        'DELETE FROM classification_job WHERE state = ? AND id NOT IN ('
        '  SELECT id FROM classification_job WHERE state = ? '
        '  ORDER BY id DESC LIMIT ?)',
        (COMPLETED, COMPLETED, keep_last),
    )
    conn.commit()
    return cursor.rowcount
