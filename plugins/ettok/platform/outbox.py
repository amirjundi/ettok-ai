"""Nothing collected is lost, and nothing is delivered twice.

The previous attempt at this system ran for a year and its database held zero
collected items. Whatever else changes, that must not happen again, which is why
delivery is a durable queue rather than an HTTP call at the end of a run.

Two rules do the work. Everything is written to disk before any network call, so a
crash, a power cut or a closed laptop costs a retry rather than a day's collection.
And every submission carries an idempotency key that is stable across retries, so
the platform replays its original response instead of performing the work again --
without it, a timeout on a bad line turns one finding into several and every rate
computed from them is quietly wrong.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from .client import PermanentError, TransientError, new_idempotency_key

log = logging.getLogger(__name__)

PENDING = 'pending'
IN_FLIGHT = 'in_flight'
DELIVERED = 'delivered'
FAILED_PERMANENT = 'failed_permanent'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(conn, endpoint: str, payload: Mapping[str, Any]) -> str:
    """Persist a submission before anything tries to send it."""
    key = new_idempotency_key()
    conn.execute(
        'INSERT INTO outbox(endpoint, payload, idempotency_key, state, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (endpoint, json.dumps(payload, ensure_ascii=False), key, PENDING, _now()),
    )
    conn.commit()
    return key


def reclaim_in_flight(conn) -> int:
    """Return abandoned rows to the queue at startup.

    A row left `in_flight` means the process died between sending and recording the
    outcome, so whether the platform received it is unknown. Retrying under the same
    idempotency key is exactly the case that key exists for: if it did arrive, the
    platform replays its answer and nothing is written twice.
    """
    cursor = conn.execute(
        'UPDATE outbox SET state = ? WHERE state = ?', (PENDING, IN_FLIGHT),
    )
    conn.commit()
    return cursor.rowcount or 0


def pending(conn, *, limit: int = 50) -> list:
    now = _now()
    rows = conn.execute(
        'SELECT * FROM outbox WHERE state = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?) '
        'ORDER BY id LIMIT ?',
        (PENDING, now, limit),
    ).fetchall()
    return list(rows)


def drain(conn, client, *, limit: int = 50, on_delivered=None) -> dict:
    """Send what is waiting. Safe to call repeatedly and at any time.

    Returns counts rather than raising, because a drain that stops at the first
    failure leaves later rows stranded behind one bad payload.
    """
    delivered = failed = deferred = 0

    for row in pending(conn, limit=limit):
        conn.execute('UPDATE outbox SET state = ?, attempts = attempts + 1 WHERE id = ?',
                     (IN_FLIGHT, row['id']))
        conn.commit()

        payload = json.loads(row['payload'])
        try:
            response = client.request(
                'POST', row['endpoint'],
                payload=payload,
                idempotency_key=row['idempotency_key'],
                attempts=1,          # the queue owns retrying, not the client
            )
        except PermanentError as exc:
            # A revoked key or a malformed body will fail identically forever.
            log.error('ettok: outbox %s permanently failed: %s', row['id'], exc)
            conn.execute(
                'UPDATE outbox SET state = ?, last_status = ?, last_error = ? WHERE id = ?',
                (FAILED_PERMANENT, exc.status, str(exc), row['id']),
            )
            conn.commit()
            failed += 1
            continue
        except TransientError as exc:
            retry_at = (datetime.now(timezone.utc) + timedelta(
                seconds=min(60 * (row['attempts'] + 1), 3600))).isoformat()
            conn.execute(
                'UPDATE outbox SET state = ?, next_attempt_at = ?, last_status = ?, '
                'last_error = ? WHERE id = ?',
                (PENDING, retry_at, exc.status, str(exc), row['id']),
            )
            conn.commit()
            deferred += 1
            continue

        conn.execute(
            'UPDATE outbox SET state = ?, last_status = ?, delivered_at = ? WHERE id = ?',
            (DELIVERED, response.status, _now(), row['id']),
        )
        conn.commit()
        delivered += 1

        if on_delivered is not None:
            # Evidence is deleted from this machine only once delivery is
            # confirmed. An unconfirmed delivery counts as undelivered, because
            # there is no second copy anywhere.
            try:
                on_delivered(conn, row, response)
            except Exception:
                log.exception('ettok: post-delivery cleanup failed for outbox %s', row['id'])

    return {'delivered': delivered, 'failed': failed, 'deferred': deferred}


def status(conn) -> dict:
    rows = conn.execute('SELECT state, COUNT(*) AS n FROM outbox GROUP BY state').fetchall()
    counts = {row['state']: row['n'] for row in rows}
    return {
        'pending': counts.get(PENDING, 0),
        'in_flight': counts.get(IN_FLIGHT, 0),
        'delivered': counts.get(DELIVERED, 0),
        'failed_permanent': counts.get(FAILED_PERMANENT, 0),
    }
