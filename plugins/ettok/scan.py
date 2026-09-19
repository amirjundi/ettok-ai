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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import cases as cases_mod
from .detect import classify as classify_mod
from .detect import match as match_mod
from .platform import outbox as outbox_mod

log = logging.getLogger(__name__)

# A classification is one short structured call. Deliberately pessimistic: an
# estimate that runs out early downgrades a run, one that runs out late overspends.
ESTIMATED_CLASSIFY_COST_USD = 0.002

# Field separator inside the identity digest. A control character, so no comment
# text can contain it and shift the boundary between two fields.
UNIT_SEPARATOR = '\x1f'

# Every field the rest of the run reads by name, so a collector that omits one
# does not raise a KeyError halfway through a batch. Anything else the collector
# supplies travels through untouched -- the platform stores what it recognises.
_ITEM_FIELDS = {
    'text': '', 'parent_post_text': '', 'parent_media_text': '',
    'url': '', 'parent_post_url': '', 'platform': '',
    'author_name': '', 'author_id': '', 'author_url': '',
}


# Query parameters that identify the visitor or the click rather than the post.
_TRACKING_EXACT = {
    'fbclid', 'igshid', 'gclid', 'mibextid', 'rdid', 'ref', 'refsrc', 'refid',
    'si', 'share_url', 'source', '__tn__',
}
_TRACKING_PREFIX = ('utm_', '__cft__')


def post_identity(url: str) -> str:
    """A post URL reduced to the part that identifies the post.

    The query is filtered rather than dropped. Facebook puts the post id *in*
    the query -- `permalink.php?story_fbid=...` -- so a bare path would merge
    every post on that page into one. But the same link copied twice carries a
    different fbclid or mibextid each time, and an identity that moves between
    scans turns one comment into a fresh finding on every run, which now also
    spends the case's item budget.

    Host prefixes go too: m.facebook.com and www.facebook.com are the same page
    reached from a phone and a desktop, and the collector may see either.
    """
    if not url:
        return ''
    parts = urlsplit(url.strip())
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_EXACT
        and not k.lower().startswith(_TRACKING_PREFIX)
    )
    host = parts.netloc.lower()
    for prefix in ('m.', 'www.', 'mobile.'):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return urlunsplit(('', host, parts.path.rstrip('/'), urlencode(kept), ''))


def content_hash(item: dict) -> str:
    """Identity of a comment, for deduplication.

    Hashes the comment together with what it replies to -- both the post's text
    and which post it is -- because the same words under a different post are a
    different finding, and that is the entire premise of context-dependent
    detection.

    Author and platform are in it too. Without them, two people posting the same
    short phrase under one post collapse into a single observation and the
    second is dropped as a duplicate: exactly the pile-on the count exists to
    measure, undercounted in proportion to how coordinated it is. The comment
    permalink joins them when the page gave one, because it is the only identity
    on the page that is genuinely stable.
    """
    parent = post_identity(item.get('parent_post_url', '') or '')
    permalink = post_identity(item.get('url', '') or '')
    if permalink == parent:
        # A page with no per-comment link hands back the post URL. That is the
        # post's identity, not the comment's, so it adds nothing here.
        permalink = ''
    parts = (
        item.get('text', '') or '',
        item.get('parent_post_text', '') or '',
        # Which post, not just what it said. Two posts can carry identical text
        # -- a caption reshared, or no caption at all on a photo -- and without
        # this the same reply under each of them collapses into one finding.
        parent,
        item.get('platform', '') or '',
        item.get('author_id', '') or '',
        permalink,
    )
    return hashlib.sha256(UNIT_SEPARATOR.join(parts).encode('utf-8')).hexdigest()


def already_seen(conn, digest: str, case_key: str = '') -> bool:
    """Seen before *in this case*.

    One comment can legitimately belong to two cases -- an anniversary watch and
    a standing watch read the same thread, and each needs its own copy of the
    evidence. Keyed on the hash alone, whichever case scanned first silently
    starved the other.
    """
    return conn.execute(
        'SELECT 1 FROM seen_item WHERE content_hash = ? AND case_id = ?',
        (digest, case_key),
    ).fetchone() is not None


def remember(conn, digests, case_key: str = '') -> None:
    """Mark items seen, once their delivery record is durable.

    Called after the outbox row is committed, never before. It used to run the
    moment an item was hashed, so a crash in between lost the item permanently
    -- it was marked seen, and no later run would look at it again -- and an
    item that tripped the budget stop was marked seen without ever having been
    collected at all.
    """
    if isinstance(digests, str):
        digests = [digests]
    digests = list(digests)
    if not digests:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        'INSERT INTO seen_item(content_hash, case_id, first_seen_at, last_seen_at) '
        'VALUES (?, ?, ?, ?) '
        'ON CONFLICT(content_hash, case_id) DO UPDATE SET '
        'last_seen_at = excluded.last_seen_at, times_seen = times_seen + 1',
        [(digest, case_key, now, now) for digest in digests],
    )
    conn.commit()



def record_judgement(conn, item: dict, result, verdict, case, digest: str) -> None:
    """Keep this agent's own decision, on this machine.

    The operator could see what the platform made of a submission and never what
    their own agent decided or why -- the local table for it existed and nothing
    wrote to it. So "the agent is judging badly" was a claim nobody could check
    without a network round trip to somebody else's database, and a run made
    while unpaired left no trace of its reasoning at all.

    Advisory, and stored as such: the platform re-judges every item and its
    verdict is the one that stands. This is kept so a disagreement between the
    two is visible rather than silent.
    """
    payload = verdict.as_payload(result) if verdict is not None else {}
    conn.execute(
        'INSERT INTO classification(content_hash, case_id, case_title, platform, url, '
        'excerpt, parent_excerpt, is_hate_speech, why_flagged, category, severity, '
        'reason, fired_terms, fired_tropes, exemption_applied, tier, versions, '
        'created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            digest,
            str(case.case_id) if case is not None else '',
            case.title if case is not None else '',
            item.get('platform', '') or '',
            item.get('url', '') or '',
            # An excerpt, not the comment. This is a laptop on a residential
            # connection; the full text of an attack on a named person does not
            # need a second permanent home here.
            (item.get('text', '') or '')[:400],
            (item.get('parent_post_text', '') or '')[:200],
            1 if payload.get('is_hate_speech') else 0,
            result.explain() if result.matched else '',
            payload.get('category', '') or '',
            payload.get('severity'),
            payload.get('reason', '') or '',
            json.dumps([t.get('term') for t in result.fired_terms], ensure_ascii=False),
            json.dumps([t.get('name') for t in result.fired_tropes], ensure_ascii=False),
            payload.get('exemption_applied') or '',
            payload.get('tier', 'context'),
            json.dumps(payload.get('versions') or {}, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _finding(item: dict, result, verdict, case, digest: str, versions=None) -> dict:
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
        # Which communities this post is about, as this agent worked it out.
        #
        # It decides whether a context-dependent term counts: "dirty people" is
        # ordinary abuse under most posts and a slur under one about Yazidis.
        # The platform re-runs the lexicon over the same comment and had no way
        # to know the answer, so it applied every context-dependent term
        # unconditionally -- the gate the curator asked for existed on one side
        # only. Sent rather than recomputed so both sides gate on the same
        # reading of the post.
        'topic_groups': list(result.topic_groups or []),
        # A context row carried an empty verdict, so nothing recorded which
        # knowledge had read it. That is the row that says "this comment was
        # examined and matched nothing" -- and without a version stamp, a later
        # curator cannot tell whether it was examined by the lexicon that has
        # the term they just added, or by one from six weeks earlier.
        'agent_verdict': (
            verdict.as_payload(result) if verdict is not None
            else {'tier': 'context', 'is_hate_speech': False, 'versions': versions or {}}
        ),
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

    # `pick` returns None for two situations that are not the same thing, and
    # collapsing them submitted evidence attached to no case at all -- invisible
    # on the case screen, and with nothing to group it under anywhere else.
    #
    #   no cases exist        -- a valid deployment with nothing to attribute to.
    #                            Collecting is fine; the operator asked for it.
    #   cases exist, none due -- the rota saying "not this one, not yet". Running
    #                            anyway both orphans the evidence and defeats the
    #                            rota, which exists so one case cannot starve the
    #                            others.
    #
    # Found by the first live pass: a second run, minutes after the first, went
    # through the whole pipeline and produced five orphaned observations.
    # An id that names no available case is the third situation, and it was
    # falling through both of the above: `case` is None, `case_id` is not, so
    # the guard below did not fire and the run collected everything under no
    # case at all. Reported as a stop rather than a quiet success, because a
    # typo in an id and a case that finished are both things an operator needs
    # told, and neither should cost a page of orphaned observations.
    if case is None and case_id is not None:
        cases_mod.finish_run(conn, run_id, stop_reason='unknown_case', spend=0.0)
        summary['stop_reason'] = 'unknown_case'
        summary['note'] = (
            f'No case {case_id} is available to work. Nothing was collected. '
            'Either the id is wrong, or that case is closed, past its deadline '
            'or out of budget -- the platform only offers cases that may run. '
            'Ask for the case list rather than trying another id.'
        )
        return summary

    if case is None and case_id is None and (know.cases or []):
        cases_mod.finish_run(conn, run_id, stop_reason='not_due', spend=0.0)
        summary['stop_reason'] = 'not_due'
        summary['note'] = (
            'No case is due yet. Nothing was collected: evidence gathered now '
            'would belong to no case, and the wait is what stops one case '
            'taking every run.'
        )
        return summary

    budget = case.budget if case else cases_mod.Budget()
    findings = []
    # Hashes waiting on a durable delivery record. Nothing is marked seen until
    # the outbox row for it is committed. A dict, so a comment repeated inside
    # one batch is still caught as a duplicate without a scan of the list.
    collected_digests = {}
    case_key = str(case.case_id) if case is not None else ''

    for raw in items:
        # The row as collected, not a seven-key copy of it. The copy dropped
        # parent_post_url and author_url -- the field that groups comments by
        # the post they hang under, and the one that makes a repeat account
        # visible -- so every submission carried both of them empty.
        item = {**_ITEM_FIELDS, **{k: v for k, v in raw.items() if v is not None}}
        if not item['text'].strip():
            continue

        summary['scanned'] += 1

        digest = content_hash(item)
        if digest in collected_digests or already_seen(conn, digest, case_key):
            summary['duplicates'] += 1
            continue

        # Everything appended to `findings` is submitted and stored, matched
        # or not, so the budget counts all of it. Counting only flagged items
        # let a case with one item of budget left collect a whole page of
        # context -- which is real work and real storage, and is now charged
        # as such on the platform side too.
        if case is not None and not case.may_collect(len(findings)):
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
            findings.append(_finding(item, result, None, case, digest, know.versions))
            record_judgement(conn, item, result, None, case, digest)
            collected_digests[digest] = None
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
        record_judgement(conn, item, result, verdict, case, digest)
        collected_digests[digest] = None

    # --- delivery: queued first, sent second ------------------------------
    if findings:
        outbox_mod.enqueue(conn, 'flagged-items/', {'items': findings})
        # Only now. The outbox row is committed, so the work survives a crash
        # here, and marking these seen can no longer lose an item that was never
        # queued.
        remember(conn, collected_digests, case_key)

    outbox_mod.enqueue(conn, 'scan-log/', {
        # The case this run worked. It is what lets the platform stamp the case
        # as scanned and move it to the back of the queue; without it every case
        # stays permanently due and the highest-priority one takes every run.
        'case_id': case.case_id if case is not None else None,
        'platforms_browsed': sorted({f['platform'] for f in findings}) or [],
        'posts_scanned': summary['scanned'],
        'items_flagged': summary['flagged'],
        'duration_seconds': 0,
        'errors': summary['errors'],
    })

    if submit:
        outbox_mod.reclaim_in_flight(conn)
        summary['delivery'] = outbox_mod.drain(conn, client)

        # The captures, after the findings. A page's artefact is worth nothing
        # without the item it supports, and the platform links the two by the
        # comment's own digest -- so the item has to arrive first.
        from .collect import evidence as evidence_mod

        item_hash_for = {}
        for finding in findings:
            page = finding.get('parent_post_url') or finding.get('url') or ''
            if page:
                item_hash_for.setdefault(page, finding['content_hash'])
        # No case passed. Each artefact knows which case it was captured for;
        # see the note in evidence.deliver.
        summary['evidence'] = evidence_mod.deliver(
            conn, client, item_hash_for=item_hash_for)
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
