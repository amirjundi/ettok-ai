"""Pacing, and knowing when to stop.

Everything here exists to keep monitoring accounts alive, which is the scarce
resource in this system. Accounts are slow to obtain, tied to real people, and a
banned one takes its collection history with it.

The rule that matters most: **a challenge is a detection signal, not an obstacle.**
A CAPTCHA or a checkpoint is the platform saying it has already noticed. Solving
it removes the only warning while leaving the detection in place, so the account
escalates to a permanent ban instead of backing off while it is still recoverable.
The agent stops, quarantines the session, and reports.

Pacing is the other half, and it is a survival setting rather than politeness.
Collection at machine cadence is exactly the signature automation detection looks
for, and it is the cheapest thing to get right.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

HEALTHY = 'healthy'
RATE_LIMITED = 'rate_limited'
QUARANTINED = 'quarantined'
AUTH_LOST = 'auth_lost'

# How long an account rests after a block. Long enough that returning is not
# itself a signal; a quarantined account is not a broken one.
QUARANTINE_HOURS = 24

# Signals that the platform has noticed. Matched against page text, so they are
# deliberately broad -- a false positive costs one paused run, a false negative
# costs an account.
_BLOCK_PATTERNS = [
    (re.compile(r'\bcaptcha\b', re.I), 'a CAPTCHA was presented'),
    (re.compile(r'security check', re.I), 'a security check was presented'),
    (re.compile(r'confirm your identity', re.I), 'identity confirmation was demanded'),
    (re.compile(r'unusual activity', re.I), 'unusual activity was reported'),
    (re.compile(r'temporarily blocked|temporarily restricted', re.I), 'the account was temporarily blocked'),
    (re.compile(r'you.re temporarily', re.I), 'the account was temporarily restricted'),
    (re.compile(r'checkpoint', re.I), 'a checkpoint was triggered'),
    (re.compile(r'اثبت انك لست', re.I), 'a human-verification challenge was presented'),
    (re.compile(r'تم تقييد', re.I), 'the account was restricted'),
]

_AUTH_PATTERNS = [
    (re.compile(r'log in to continue|please log in|sign in to continue', re.I), 'the session is no longer signed in'),
    (re.compile(r'تسجيل الدخول للمتابعة', re.I), 'the session is no longer signed in'),
]


class CollectionBlocked(RuntimeError):
    """Stop. Do not retry, do not work around it, do not solve it."""

    def __init__(self, reason: str, *, account_id: str = '', auth_lost: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.account_id = account_id
        self.auth_lost = auth_lost


def detect_block(page_text: str) -> Optional[CollectionBlocked]:
    """Read a page for signs the platform has noticed.

    Checked before anything is extracted, because a challenge page contains no
    comments and treating it as an empty result would hide the problem behind a
    quiet run.
    """
    if not page_text:
        return None
    for pattern, reason in _AUTH_PATTERNS:
        if pattern.search(page_text):
            return CollectionBlocked(reason, auth_lost=True)
    for pattern, reason in _BLOCK_PATTERNS:
        if pattern.search(page_text):
            return CollectionBlocked(reason)
    return None


@dataclass
class Pacer:
    """Human-cadence delays between actions."""

    min_seconds: float = 4.0
    max_seconds: float = 11.0
    _sleep = staticmethod(time.sleep)

    def wait(self) -> float:
        """Sleep for a randomised interval and return how long it was.

        Randomised rather than fixed: a constant delay is as distinctive a
        signature as no delay at all, just a slower one.
        """
        delay = random.uniform(self.min_seconds, self.max_seconds)
        self._sleep(delay)
        return delay


def account_state(conn, account_id: str) -> str:
    row = conn.execute(
        'SELECT state, cooldown_until FROM account_health WHERE account_id = ?', (account_id,)
    ).fetchone()
    if row is None:
        return HEALTHY
    if row['state'] == QUARANTINED and row['cooldown_until']:
        if datetime.now(timezone.utc).isoformat() >= row['cooldown_until']:
            return HEALTHY          # the rest is over; it may be tried again
    return row['state']


def record_success(conn, account_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        'INSERT INTO account_health(account_id, state, last_success_at) VALUES (?, ?, ?) '
        'ON CONFLICT(account_id) DO UPDATE SET state = ?, last_success_at = excluded.last_success_at, '
        'block_reason = \'\', cooldown_until = NULL',
        (account_id, HEALTHY, now, HEALTHY),
    )
    conn.commit()


def quarantine(conn, account_id: str, reason: str, *, auth_lost: bool = False) -> None:
    """Take an account out of rotation and record why.

    "The account stopped working" is not actionable. A quarantine after a CAPTCHA
    and one after a lost session need different responses -- the first is a rest,
    the second needs someone to sign in again.
    """
    now = datetime.now(timezone.utc)
    conn.execute(
        'INSERT INTO account_health(account_id, state, last_block_at, block_reason, cooldown_until) '
        'VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET '
        'state = excluded.state, last_block_at = excluded.last_block_at, '
        'block_reason = excluded.block_reason, cooldown_until = excluded.cooldown_until',
        (
            account_id,
            AUTH_LOST if auth_lost else QUARANTINED,
            now.isoformat(),
            reason,
            (now + timedelta(hours=QUARANTINE_HOURS)).isoformat(),
        ),
    )
    conn.commit()
    log.warning('ettok: account %s quarantined -- %s', account_id, reason)


def release(conn, account_id: str, *, note: str = '') -> bool:
    """Return an account to rotation because a person says it is fine.

    The cooldown exists because an account that has been challenged needs to go
    quiet for a while. It does not exist to overrule the operator: someone who
    has signed in, cleared the challenge and watched the account behave knows
    something this agent cannot observe from a page of HTML.

    Distinct from `record_success`, which records that a collection worked. This
    records that a human intervened, which is a different fact and worth being
    able to tell apart afterwards.
    """
    now = datetime.now(timezone.utc).isoformat()
    reason = f'released by an operator{": " + note if note else ""}'
    cursor = conn.execute(
        'UPDATE account_health SET state = ?, cooldown_until = NULL, '
        'block_reason = ? WHERE account_id = ?',
        (HEALTHY, reason, account_id),
    )
    conn.commit()
    if cursor.rowcount:
        log.info('ettok: account %s released by an operator (%s)', account_id, now)
        return True
    return False


def account_health(conn) -> list:
    """Every account this agent has an opinion about, and why."""
    rows = conn.execute(
        'SELECT account_id, state, block_reason, cooldown_until, last_block_at, '
        'last_success_at FROM account_health ORDER BY account_id'
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item['effective_state'] = account_state(conn, row['account_id'])
        out.append(item)
    return out


def healthy_accounts(conn, accounts: list) -> list:
    """Which supplied accounts may be used right now."""
    usable = []
    for account in accounts:
        account_id = str(account.get('account_name') or account.get('id') or '')
        if account_id and account_state(conn, account_id) == HEALTHY:
            usable.append(account)
    return usable


def capacity_warning(conn, accounts: list) -> Optional[str]:
    """Tell an operator before collection quietly degrades to nothing."""
    total = len(accounts)
    usable = len(healthy_accounts(conn, accounts))
    if total and not usable:
        return ('No healthy monitoring accounts remain. Collection cannot proceed '
                'until an account is restored or a new one is added.')
    if total and usable < total:
        return f'{total - usable} of {total} monitoring accounts are unavailable.'
    return None
