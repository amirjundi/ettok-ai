"""One run, start to finish.

The order here is the budget ladder made concrete. Everything free happens first
and unconditionally, so a run in a month with no funding still produces
evidence-backed findings for review. Paid work happens afterwards, to whatever
the budget allows, and running out of it downgrades the run rather than stopping
it -- because collection is the time-sensitive half. A post deleted today cannot
be collected next month when a donation arrives, while an opinion about it can be
formed at any time, and the platform forms one anyway.

Items reach this from two places. Collection supplies them once that exists; until
then an operator or the agent supplies them directly, which is the same code path
and is how the loop was proven before a scraper existed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from . import cases as cases_mod
from .detect import classify as classify_mod
from .detect import match as match_mod
from .platform import outbox as outbox_mod

log = logging.getLogger(__name__)

# A classification is one short structured call. Deliberately pessimistic: an
# estimate that runs out early downgrades a run, one that runs out late overspends.
ESTIMATED_CLASSIFY_COST_USD = 0.002


def content_hash(item: dict) -> str:
    """Identity of a comment, for deduplication.

    Hashes the comment together with what it replies to, because the same words
    under a different post are a different finding -- that is the entire premise
    of context-dependent detection.
    """
    blob = (item.get('text', '') + '\x1f' + item.get('parent_post_text', '')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


def already_seen(conn, digest: str) -> bool:
    return conn.execute(
        'SELECT 1 FROM seen_item WHERE content_hash = ?', (digest,)
    ).fetchone() is not None


def remember(conn, digest: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        'INSERT INTO seen_item(content_hash, first_seen_at, last_seen_at) VALUES (?, ?, ?) '
        'ON CONFLICT(content_hash) DO UPDATE SET '
        'last_seen_at = excluded.last_seen_at, times_seen = times_seen + 1',
        (digest, now, now),
    )
    conn.commit()


def _finding(item: dict, result, verdict, case, digest: str) -> dict:
    """One submission row, for a finding or for the context around it.

    `verdict` is None for a comment that matched nothing: there is no judgement
    to report, and an empty `why_flagged` is how the platform recognises the row
    as context rather than a finding.

    The account fields are what make a repeat offender visible across posts and
    weeks, and `parent_post_url` is what groups comments by the post they hang
    under. A display name is not an identity -- names change, and two people
    share one -- so `author_id` carries the stable handle from the profile link
    and is empty rather than guessed when the page did not offer one.
    """
    return {
        # The case and the hash travel with every row so the platform can file
        # it against the right case and recognise it again on a later scan.
        'case_id': case.case_id if case is not None else None,
        'content_hash': digest,
        'platform': item['platform'] or 'unknown',
        'url': item['url'],
        'parent_post_url': item.get('parent_post_url', '') or '',
        'text': item['text'],
        'parent_post_text': item['parent_post_text'],
        'parent_media_text': item['parent_media_text'],
        'author_name': item['author_name'],
        'author_id': item.get('author_id', '') or '',
        'author_url': item.get('author_url', '') or '',
        'why_flagged': result.explain() if result.matched else '',
        'agent_verdict': verdict.as_payload(result) if verdict is not None else {},
    }


def run(ctx, *, items: list, case_id=None, classify: bool = True, submit: bool = True) -> dict:
    """Work one batch of items under one case.

    Returns a summary rather than raising: a run that stops at the first bad item
    strands everything behind it, and the whole point of the outbox is that partial
    progress is kept.
    """
    from . import config as config_mod
    from .platform import knowledge as knowledge_mod
    from .platform.client import PlatformClient
    from .store import schema

    cfg = config_mod.load(ctx)
    conn = schema.connect()
    client = PlatformClient(cfg)

    know = getattr(ctx, '_ettok_knowledge', None)
    if know is None:
        know = knowledge_mod.fetch(client)
        ctx._ettok_knowledge = know

    case = cases_mod.pick(know, case_id=case_id)
    run_id = cases_mod.start_run(conn, case, know)

    summary = {
        'run_id': run_id,
        'case': case.title if case else None,
        'scanned': 0, 'duplicates': 0, 'flagged': 0,
        'classified': 0, 'match_only': 0,
        'spend_usd': 0.0,
        'errors': [],
    }
    if case is not None:
        summary['readiness'] = case.readiness()

    budget = case.budget if case else cases_mod.Budget()
    findings = []

    for raw in items:
        item = {
            'text': raw.get('text', ''),
            'parent_post_text': raw.get('parent_post_text', ''),
            'parent_media_text': raw.get('parent_media_text', ''),
            'url': raw.get('url', ''),
            'platform': raw.get('platform', ''),
            'author_name': raw.get('author_name', ''),
            'author_id': raw.get('author_id', ''),
        }
        if not item['text'].strip():
            continue

        summary['scanned'] += 1

        digest = content_hash(item)
        if already_seen(conn, digest):
            summary['duplicates'] += 1
            continue
        remember(conn, digest)

        if case is not None and not case.may_collect(summary['flagged']):
            cases_mod.finish_run(conn, run_id, stop_reason='item_budget',
                                 scanned=summary['scanned'], flagged=summary['flagged'],
                                 spend=summary['spend_usd'])
            summary['stop_reason'] = 'item_budget'
            break

        # --- free tier: always, whatever the budget ------------------------
        try:
            result = match_mod.evaluate(
                item, know, case_id=case.case_id if case is not None else None,
            )
        except Exception as exc:                      # noqa: BLE001
            log.exception('ettok: matching failed')
            summary['errors'].append(f'match failed: {exc}')
            continue

        # Unmatched comments are kept and submitted too. They cost nothing to
        # collect -- they were already fetched, read and hashed -- and without
        # them the platform has findings but no denominator, so it can say "nine
        # findings" and never "nine out of four hundred comments", which is the
        # number that says whether a post is a pile-on or an ordinary thread.
        #
        # Only findings go on to classification, so the expensive tier is
        # unchanged.
        if not result.matched:
            findings.append(_finding(item, result, None, case, digest))
            continue
        summary['flagged'] += 1

        # --- paid tier: only if there is budget for it ---------------------
        background = ''
        if case is not None:
            for slug in result.topic_groups:
                background = case.background_for(slug) or background

        if classify and budget.allows(cases_mod.TIER_CLASSIFY):
            verdict = classify_mod.classify(
                ctx, item, result, versions=know.versions, group_background=background,
            )
            budget.charge(ESTIMATED_CLASSIFY_COST_USD)
            summary['spend_usd'] += ESTIMATED_CLASSIFY_COST_USD
            summary['classified'] += 1
        else:
            verdict = classify_mod.from_match_only(result, know.versions)
            summary['match_only'] += 1

        findings.append(_finding(item, result, verdict, case, digest))

    # --- delivery: queued first, sent second ------------------------------
    if findings:
        outbox_mod.enqueue(conn, 'flagged-items/', {'items': findings})

    outbox_mod.enqueue(conn, 'scan-log/', {
        'platforms_browsed': sorted({f['platform'] for f in findings}) or [],
        'posts_scanned': summary['scanned'],
        'items_flagged': summary['flagged'],
        'duration_seconds': 0,
        'errors': summary['errors'],
    })

    if submit:
        outbox_mod.reclaim_in_flight(conn)
        summary['delivery'] = outbox_mod.drain(conn, client)
    summary['queue'] = outbox_mod.status(conn)

    if 'stop_reason' not in summary:
        summary['stop_reason'] = 'completed'
        cases_mod.finish_run(conn, run_id, stop_reason='completed',
                             scanned=summary['scanned'], flagged=summary['flagged'],
                             spend=summary['spend_usd'], errors=summary['errors'])

    # An agent may raise this with an operator. It may not act on it.
    if case is not None and case.suggests_closing:
        summary['note'] = (
            f'This case has produced nothing confirmed for several runs. '
            f'Consider asking an administrator whether to close it -- the agent '
            f'cannot close a case itself.'
        )

    budget_note = None
    if budget.exhausted:
        budget_note = ('Model budget is exhausted for this month. Collection, matching '
                       'and delivery continue; advisory classification is suspended.')
    elif summary['match_only'] and not summary['classified']:
        budget_note = 'Ran without classification; findings were submitted on matching alone.'
    if budget_note:
        summary['budget_note'] = budget_note

    return summary
