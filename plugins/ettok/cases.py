"""Working a case: what to do, how much to spend, and when to stop.

The stop conditions are enforced on the platform, not here. An agent that decides
for itself when it has spent enough and looked long enough is not a control, so
this module reads limits and respects them -- it never relaxes one, and a case past
its limits is not returned by `tasks/` at all rather than returned and refused.

The budget ladder is the other half, and its order is deliberately the opposite of
what it looks like it should be:

    always, free   collect, evidence, normalise, match, dedup, submit
    first spend    discovery of unknown vocabulary
    last spend     the agent's own advisory classification

Classification goes first because the platform reproduces it authoritatively, so
paying for it twice buys the least. Discovery goes last because nothing anywhere
else performs it, and without it the term list decays as campaigns adopt new
language specifically to evade it. A month with no budget at all is a valid
operating state: collection still runs, and collection is the time-sensitive part
-- a deleted post cannot be collected next month when a donation arrives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

TIER_FREE = 'free'
TIER_DISCOVERY = 'discovery'
TIER_CLASSIFY = 'classify'


@dataclass
class Budget:
    """What this run may spend, and on what.

    Model spend is donation-funded and varies month to month, including months at
    or near zero, so this is read from the platform every run rather than fixed.
    """

    remaining_usd: Optional[float] = None
    spent_usd: float = 0.0

    @property
    def exhausted(self) -> bool:
        return self.remaining_usd is not None and self.remaining_usd <= 0

    def allows(self, tier: str) -> bool:
        if tier == TIER_FREE:
            return True                      # never gated; this is the essential output
        return not self.exhausted

    def charge(self, amount: float) -> None:
        self.spent_usd += amount
        if self.remaining_usd is not None:
            self.remaining_usd = max(0.0, self.remaining_usd - amount)


@dataclass
class CaseWork:
    """One case, as the agent sees it."""

    case_id: int
    title: str
    state: str
    target_groups: list = field(default_factory=list)
    seed_sources: list = field(default_factory=list)
    items_remaining: Optional[int] = None
    budget: Budget = field(default_factory=Budget)
    suggests_closing: bool = False
    trigger: str = ''
    # Whether the platform says this case is ready for another run, and when it
    # next will be. Defaults to True so a platform too old to send the field
    # behaves as it always did rather than going silent.
    due: bool = True
    next_scan_at: str = ''

    @classmethod
    def from_payload(cls, payload: dict) -> 'CaseWork':
        limits = payload.get('limits') or {}
        return cls(
            case_id=payload.get('id'),
            title=payload.get('title', ''),
            state=payload.get('state', ''),
            target_groups=payload.get('target_groups', []) or [],
            seed_sources=payload.get('seed_sources', []) or [],
            items_remaining=limits.get('items_remaining'),
            budget=Budget(remaining_usd=limits.get('cost_remaining_usd')),
            suggests_closing=bool(payload.get('suggests_closing')),
            trigger=payload.get('trigger', ''),
            due=bool(payload.get('due', True)),
            next_scan_at=payload.get('next_scan_at') or '',
        )

    @property
    def group_slugs(self) -> list:
        return [g.get('slug') for g in self.target_groups if g.get('slug')]

    def background_for(self, slug: str) -> str:
        for group in self.target_groups:
            if group.get('slug') == slug:
                return group.get('background', '')
        return ''

    def may_collect(self, collected_so_far: int) -> bool:
        """Whether there is room for one more item under this case's item budget."""
        if self.items_remaining is None:
            return True
        return collected_so_far < self.items_remaining

    def readiness(self) -> dict:
        """What this case can actually detect right now.

        Surfaced because the honest answer is usually "less than you think", and
        an operator should learn that from the agent rather than from a month of
        empty reports. A group with no topic markers cannot be recognised in a
        feed at all, so its tropes never get a chance to fire.
        """
        without_markers = [
            g.get('slug') for g in self.target_groups if not (g.get('topic_markers') or [])
        ]
        return {
            'groups': self.group_slugs,
            'groups_without_topic_markers': without_markers,
            'seed_sources': len(self.seed_sources),
            'can_find_content': bool(self.seed_sources) and len(without_markers) < len(self.target_groups),
        }


def pick(knowledge, *, case_id: Optional[int] = None) -> Optional[CaseWork]:
    """Choose the case to work, and let the others have a turn.

    Only runnable cases reach the agent -- the platform filters out anything past
    its deadline or budget -- so this picks among cases that are already allowed
    to run rather than re-deciding whether they should.

    Cases not yet due are skipped. Without that, sorting by state alone meant the
    same case won every single run: two active cases, and the second was never
    scanned once, while the dashboard showed both as monitored. A case is due
    when it has never been scanned, or when its own interval has elapsed since
    the last finished run -- the platform decides which, and says so.

    Among the cases that are due, a live campaign still outranks a watch on a
    quiet one; the tiebreak is whichever has waited longest, so equals rotate
    instead of one of them starving.
    """
    cases = [CaseWork.from_payload(c) for c in (knowledge.cases or [])]
    if not cases:
        return None
    if case_id is not None:
        # An explicit request is an operator asking for this case now, which
        # outranks the rota.
        return next((c for c in cases if c.case_id == case_id), None)

    due = [c for c in cases if c.due]
    if not due:
        return None

    order = {'active': 0, 'reactivated': 0, 'cooling': 1, 'dormant': 2}
    # An empty next_scan_at means never scanned, which should go first; the
    # empty string sorts before any ISO timestamp, so it does.
    return sorted(due, key=lambda c: (order.get(c.state, 3), c.next_scan_at))[0]


def start_run(conn, case: Optional[CaseWork], knowledge) -> int:
    """Record the attempt before any collection happens.

    Written first so a crash mid-run is visible rather than invisible. The
    previous attempt at this system had no way to tell "ran and found nothing"
    from "died on the second page".
    """
    import json

    cursor = conn.execute(
        'INSERT INTO case_run(platform_case_id, target_group_slug, started_at, '
        'knowledge_versions) VALUES (?, ?, ?, ?)',
        (
            case.case_id if case else None,
            (case.group_slugs[0] if case and case.group_slugs else ''),
            datetime.now(timezone.utc).isoformat(),
            json.dumps(knowledge.versions),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def finish_run(conn, run_id: int, *, stop_reason: str, scanned: int = 0,
               flagged: int = 0, spend: float = 0.0, errors: Optional[list] = None) -> None:
    """Every ending records why.

    "The run stopped" is not something anyone can act on: a run that hit its item
    budget and a run whose account was banned look identical from outside and need
    opposite responses.
    """
    import json

    conn.execute(
        'UPDATE case_run SET ended_at = ?, stop_reason = ?, posts_scanned = ?, '
        'items_flagged = ?, spend = ?, errors = ? WHERE id = ?',
        (
            datetime.now(timezone.utc).isoformat(), stop_reason, scanned, flagged,
            spend, json.dumps(errors or []), run_id,
        ),
    )
    conn.commit()
