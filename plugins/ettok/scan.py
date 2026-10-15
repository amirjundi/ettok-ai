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
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import cases as cases_mod
from . import jobs as jobs_mod
from .detect import classify as classify_mod
from .detect import guidance as guidance_mod
from .detect import match as match_mod
from .detect import scope as scope_mod
from .platform import outbox as outbox_mod

log = logging.getLogger(__name__)

# A classification is one short structured call. Deliberately pessimistic: an
# estimate that runs out early downgrades a run, one that runs out late overspends.
ESTIMATED_CLASSIFY_COST_USD = 0.002

# One scope check reads a post and answers a yes-or-no, so it is shorter than a
# classification both ways. It is also charged once per post rather than once
# per comment, which is the point of it: the reservation for a two-hundred
# comment thread is this, not two hundred of the figure above.
ESTIMATED_SCOPE_COST_USD = 0.001

# How old the detection knowledge may be before a run refuses to use it.
#
# Fifteen minutes is short enough that a curator's edit reaches the next run and
# that the case rota cannot have moved far, and long enough that a burst of runs
# does not re-fetch the whole lexicon each time. A re-fetch costs one round trip;
# scanning against knowledge of unknown age costs findings attributed to a
# release that was not the one applied.
MAX_KNOWLEDGE_AGE_SECONDS = 15 * 60

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
    # The conversation this comment sits in. A reply judged against the post
    # instead of against the comment above it is judged against the wrong
    # thing: "Exactly" means opposite things under an attack and under a
    # defence, and a boolean `is_reply` cannot say which.
    'reply_to_text': '', 'reply_to_author': '', 'reply_depth': 0,
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



def remember_posts(conn, entries: list, case_key: str) -> None:
    """Note which posts this run read, and how much was new in each.

    The counts are what make a revisit decision possible later. A thread that
    yielded eighty new comments an hour ago is worth opening again; one that
    yielded nothing twice has gone quiet, and a third visit spends a page load
    to confirm it.
    """
    now = datetime.now(timezone.utc).isoformat()
    totals = {}
    for entry in entries:
        item = entry['item']
        identity = post_identity(item.get('parent_post_url') or '')
        if not identity:
            continue
        bucket = totals.setdefault(identity, {
            'url': item.get('parent_post_url') or '',
            'platform': item.get('platform') or '',
            'seen': 0, 'new': 0,
        })
        bucket['seen'] += 1
        if entry.get('fresh'):
            bucket['new'] += 1

    for identity, bucket in totals.items():
        conn.execute(
            'INSERT INTO seen_post(post_identity, url, platform, case_id, '
            'first_seen_at, last_seen_at, last_comment_count, last_new_count) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(post_identity) DO UPDATE SET '
            'last_seen_at = excluded.last_seen_at, '
            'times_seen = times_seen + 1, '
            'last_comment_count = excluded.last_comment_count, '
            'last_new_count = excluded.last_new_count',
            (identity, bucket['url'], bucket['platform'], case_key, now, now,
             bucket['seen'], bucket['new']),
        )
    conn.commit()


def posts_to_revisit(conn, *, within_hours: int = 48, limit: int = 20,
                     case_key: str = None) -> list:
    """Threads read recently that are still producing comments.

    Two filters, and the second is what keeps this from becoming a treadmill.

    Recent, because an old thread is finished and its comments are not coming
    back -- revisiting it forever would spend every run re-reading history.

    And still moving: a post whose last read found nothing new has gone quiet.
    Dropping it is not a guess about the future, it is declining to pay a page
    load to confirm what the last page load already said. A post nobody has
    read twice yet has no such evidence and is always offered.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    sql = ('SELECT * FROM seen_post WHERE last_seen_at >= ? '
           'AND (times_seen < 2 OR last_new_count > 0)')
    params = [cutoff]
    if case_key:
        sql += ' AND case_id = ?'
        params.append(case_key)
    sql += ' ORDER BY last_new_count DESC, last_seen_at DESC LIMIT ?'
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def record_judgement(conn, item: dict, result, verdict, case, digest: str,
                     scope=None) -> None:
    """Keep this agent's own decision, on this machine.

    The operator could see what the platform made of a submission and never what
    their own agent decided or why -- the local table for it existed and nothing
    wrote to it. So "the agent is judging badly" was a claim nobody could check
    without a network round trip to somebody else's database, and a run made
    while unpaired left no trace of its reasoning at all.

    No longer advisory. It was, when the platform ran a classifier of its own
    and its verdict was the one that stood; this was kept so a disagreement
    between the two was visible. The platform stopped classifying -- one
    rulebook instead of two that had already drifted -- so this row and the one
    the platform stores are the same judgement, and this is the copy that
    survives on the operator's own machine when the network does not.

    Which makes `state` worth its column. A rule firing, a comment nobody
    examined and a model clearing something all wrote 0 or 1 into
    `is_hate_speech` and were indistinguishable afterwards.
    """
    payload = verdict.as_payload(result) if verdict is not None else {}
    conn.execute(
        'INSERT INTO classification(content_hash, case_id, case_title, platform, url, '
        'excerpt, parent_excerpt, is_hate_speech, why_flagged, category, severity, '
        'reason, fired_terms, fired_tropes, exemption_applied, tier, state, '
        'scope_status, scope_reason, versions, '
        'created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
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
            # 1, 0, or NULL where the model gave no yes-or-no. Storing 0 for
            # "nobody looked" made an unexamined comment read as a cleared one.
            (None if payload.get('is_hate_speech') is None
             else (1 if payload['is_hate_speech'] else 0)),
            result.explain() if result.matched else '',
            payload.get('category', '') or '',
            payload.get('severity'),
            payload.get('reason', '') or '',
            json.dumps([t.get('term') for t in result.fired_terms], ensure_ascii=False),
            json.dumps([t.get('name') for t in result.fired_tropes], ensure_ascii=False),
            payload.get('exemption_applied') or '',
            payload.get('tier', 'context'),
            payload.get('state', classify_mod.STATE_NOT_ASSESSED),
            # Kept beside the verdict rather than folded into it. "Nobody
            # monitors this community" and "this is not an attack" are
            # different answers, and the row has to be able to say the first
            # without implying the second.
            scope.status if scope is not None else '',
            scope.reason if scope is not None else '',
            json.dumps(payload.get('versions') or {}, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _finding(item: dict, result, verdict, case, digest: str, versions=None,
             scope=None) -> dict:
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
        # Whether this product monitors the conversation at all, and why the
        # agent thinks so. Sent so the platform can show a day's coverage
        # honestly: an out-of-scope comment section is not a quiet one, and an
        # item nobody assessed is not an item nobody needed to.
        'scope_status': scope.status if scope is not None else '',
        'scope_reason': scope.reason if scope is not None else '',
        'scope_decided_by': scope.decided_by if scope is not None else '',
        # A context row carried an empty verdict, so nothing recorded which
        # knowledge had read it. That is the row that says "this comment was
        # examined and matched nothing" -- and without a version stamp, a later
        # curator cannot tell whether it was examined by the lexicon that has
        # the term they just added, or by one from six weeks earlier.
        'agent_verdict': (
            verdict.as_payload(result) if verdict is not None
            # Nothing examined this comment, which is not the same as something
            # examining it and finding nothing. It reported is_hate_speech
            # False, and downstream that reads as a clearance by a classifier
            # that never ran.
            else {'tier': 'context', 'state': classify_mod.STATE_NOT_ASSESSED,
                  'is_hate_speech': None, 'versions': versions or {}}
        ),
    }


def _note_gap(gaps: dict, item: dict, verdict, groups, allowed_categories) -> None:
    """Propose the words that carried an attack no curated entry caught.

    Merged by spelling within a run, so a phrase repeated down a thread is one
    proposal with its occurrences counted rather than many.
    """
    from .detect.normalize import normalize

    key = normalize(verdict.key_phrase)
    gap = gaps.get(key)
    if gap is None:
        gaps[key] = {
            'suggested_term': verdict.key_phrase[:500],
            'content_text': item.get('text', ''),
            'language': item.get('language') or '',
            'suggested_target_group': (groups or [''])[0],
            # The platform refuses a category it does not know, and refuses the
            # whole proposal with it.
            'suggested_category': (verdict.category
                                   if verdict.category in (allowed_categories or [])
                                   else ''),
            'evidence_urls': [item['url']] if item.get('url') else [],
            'occurrences': 1,
            'rationale': verdict.reason,
        }
        return
    gap['occurrences'] += 1
    if item.get('url') and item['url'] not in gap['evidence_urls']:
        gap['evidence_urls'] = (gap['evidence_urls'] + [item['url']])[:20]


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

    # Re-fetched when it has gone stale, rather than trusted because it is
    # there.
    #
    # ARCH-03. This held whatever was fetched the first time the agent synced,
    # for as long as the conversation lasted -- and these conversations last
    # days. A skill instruction to sync first is not enforcement: a chat opened
    # on Monday would scan on Thursday against Monday's cases, Monday's
    # lexicon, and a case rota that had moved on without it. Nothing said so,
    # because stale knowledge produces confident findings.
    know = getattr(ctx, '_ettok_knowledge', None)
    if know is None or know.age_seconds > MAX_KNOWLEDGE_AGE_SECONDS:
        know = knowledge_mod.fetch(client)
        ctx._ettok_knowledge = know

    case = cases_mod.pick(know, case_id=case_id)
    run_id = cases_mod.start_run(conn, case, know)

    summary = {
        'run_id': run_id,
        'case': case.title if case else None,
        'scanned': 0, 'duplicates': 0, 'flagged': 0,
        'classified': 0, 'match_only': 0,
        # How much was looked at, and how much was set aside as somebody
        # else's subject. A day of monitoring that assessed nothing and a
        # day that found nothing are different days.
        'posts': 0, 'scope_checks': 0, 'out_of_scope': 0,
        # Items a provider never answered for. Replayable work, not
        # finished work, and never a clearance.
        'failed_assessments': 0,
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

    # Claimed before any collecting, released when the run ends.
    #
    # Two agents asking for work both got the same due case and both collected
    # it. A refusal here is an ordinary outcome, not a fault: it means somebody
    # else got there first, and the right response is to stop rather than do
    # the work twice.
    #
    # Tolerated when the platform does not offer claiming, because the two are
    # released separately and refusing to run against a server one version
    # behind loses posts that are deleted within hours.
    #
    # Only when the run will actually submit. A dry run collects nothing anyone
    # else could duplicate, and requiring a network round trip to do one would
    # make the offline path -- the one an operator uses to check selectors or
    # try a comment by hand -- depend on the platform being reachable.
    claimed = False
    if case is not None and submit:
        try:
            client.claim_case(case.case_id)
            claimed = True
        except Exception as exc:                              # noqa: BLE001
            if '409' in str(exc):
                cases_mod.finish_run(conn, run_id, stop_reason='claimed_elsewhere',
                                     spend=0.0)
                summary['stop_reason'] = 'claimed_elsewhere'
                summary['note'] = (
                    f'Another agent is already working "{case.title}". Nothing was '
                    f'collected. Ask for the case list again and take a different '
                    f'one; the claim expires on its own if that agent has stopped.'
                )
                return summary
            log.info('ettok: case claiming unavailable (%s); continuing', exc)

    budget = case.budget if case else cases_mod.Budget()

    # What died last time, before anything new is taken on. Reported rather
    # than acted on automatically: replaying costs provider calls, and a run
    # that silently spent a case's budget on yesterday's backlog would be a
    # surprise. `ettok_replay` does the work when somebody asks for it.
    summary['jobs'] = jobs_mod.status(conn)

    findings = []
    # Hashes waiting on a durable delivery record. Nothing is marked seen until
    # the outbox row for it is committed. A dict, so a comment repeated inside
    # one batch is still caught as a duplicate without a scan of the list.
    collected_digests = {}
    case_key = str(case.case_id) if case is not None else ''

    # --- pass one: free, and unconditional --------------------------------
    #
    # Match everything, keep everything, spend nothing. What comes out is the
    # batch as collected, annotated -- not filtered. Deciding here which items
    # deserve a model was the whole bug: a lexicon of slurs cannot recognise
    # "they should go back where they came from", which is the attack, and does
    # recognise a journalist quoting a slur, which is not.
    candidates = []
    # Items seen before, kept only long enough to note that their post was
    # read again and produced nothing. They go no further than that.
    revisited = []
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
            # Noted against its post anyway. A revisit that finds every
            # comment already seen is the evidence that the thread has gone
            # quiet -- and without recording it, the post keeps whatever
            # `last_new_count` it had and is offered for revisiting for ever.
            revisited.append({'item': item, 'fresh': False})
            continue

        # Everything kept is submitted and stored, matched or not, so the
        # budget counts all of it. Counting only flagged items let a case with
        # one item of budget left collect a whole page of context -- which is
        # real work and real storage, and is now charged as such on the
        # platform side too.
        if case is not None and not case.may_collect(len(candidates)):
            summary['stop_reason'] = 'item_budget'
            break

        try:
            result = match_mod.evaluate(
                item, know, case_id=case.case_id if case is not None else None,
            )
        except Exception as exc:                      # noqa: BLE001
            log.exception('ettok: matching failed')
            summary['errors'].append(f'match failed: {exc}')
            continue

        if result.matched:
            summary['flagged'] += 1
        collected_digests[digest] = None
        candidates.append({'item': item, 'digest': digest, 'result': result,
                           'verdict': None,
                           # Everything reaching here passed deduplication, so
                           # it is new to this agent. Recorded per post, to
                           # answer "is this thread still producing" later.
                           'fresh': True})

    # --- pass two: is this conversation even ours? ------------------------
    #
    # Once per post, not once per comment. This is what keeps contextual
    # assessment affordable: a thread of two hundred comments asks the question
    # once, and a post about football takes its whole comment section out of
    # the paid queue in a single cheap call.
    #
    # A curated topic marker answers it for free. Only a post no marker
    # recognised costs anything, and a post nobody could decide about is
    # `uncertain`, which is assessed -- never skipped.
    communities = case.communities() if case is not None else {}
    scopes = {}
    for entry in candidates:
        item = entry['item']
        key = (post_identity(item.get('parent_post_url') or '')
               or item['parent_post_text'][:200])
        if key not in scopes:
            if entry['result'].topic_groups:
                scopes[key] = scope_mod.from_markers(entry['result'].topic_groups)
            elif classify and budget.allows(cases_mod.TIER_CLASSIFY):
                decided = scope_mod.assess(
                    ctx, post_text=item['parent_post_text'],
                    media_text=item['parent_media_text'], communities=communities,
                )
                spent = (decided.cost_usd if decided.cost_usd is not None
                         else ESTIMATED_SCOPE_COST_USD)
                budget.charge(spent)
                summary['spend_usd'] += spent
                summary['tokens'] = summary.get('tokens', 0) + decided.total_tokens
                summary['scope_checks'] = summary.get('scope_checks', 0) + 1
                scopes[key] = decided
            else:
                scopes[key] = scope_mod.Scope(
                    status=scope_mod.STATUS_UNCERTAIN,
                    reason='no budget to check whether this post is in scope')
        entry['scope'] = scopes[key]

    # Matched again where the scope check named a community no topic marker
    # did. Pass one could only know what the markers said, so a context-only
    # term or a community's trope under a post the model placed was never
    # tested, and the prompt then said nothing curated had matched.
    for entry in candidates:
        if set(entry['scope'].groups) - set(entry['result'].topic_groups):
            was_matched = entry['result'].matched
            entry['result'] = match_mod.evaluate(
                entry['item'], know,
                case_id=case.case_id if case is not None else None,
                groups=entry['scope'].groups,
            )
            if entry['result'].matched and not was_matched:
                summary['flagged'] += 1

    summary['posts'] = len(scopes)
    summary['out_of_scope'] = sum(
        1 for e in candidates if not e['scope'].assessable and not e['result'].matched)

    # --- pass three: paid, in priority order ------------------------------
    gaps = {}
    #
    # Matched first. The lexicon stopped being the gate and became the queue:
    # a hit is the one direction a word list is reliable in, so a run that runs
    # out of money runs out of it on the least likely comments rather than the
    # most likely.
    for entry in sorted(candidates, key=lambda e: not e['result'].matched):
        item, result, digest = entry['item'], entry['result'], entry['digest']

        # An out-of-scope post excuses its comments -- unless one of them
        # matched. Somebody raising Yazidis in a comment under a football post
        # is exactly what per-post scoping would otherwise miss, and a curated
        # term firing is the evidence that it happened.
        if not entry['scope'].assessable and not result.matched:
            continue

        if not (classify and budget.allows(cases_mod.TIER_CLASSIFY)):
            # Out of money, or never asked. Whatever is left keeps
            # `not_assessed`, which is not a clearance.
            summary['match_only'] += 1
            entry['verdict'] = classify_mod.from_match_only(result, know.versions)
            continue

        # Every community the post concerns. Keeping only the last one gave a
        # post about Yazidis and Assyrians half its background.
        background = ''
        if case is not None:
            background = '\n\n'.join(dict.fromkeys(
                b for b in (case.background_for(slug) for slug in
                            (result.topic_groups or entry['scope'].groups)) if b))

        # Written down before the attempt, not after it.
        #
        # A provider outage or a closed laptop used to lose the verdict for
        # these items with no trace: the observation was already stored for
        # submission, the attempt left nothing behind, and an item nobody
        # judged looks exactly like one the agent has not reached yet. The
        # job is the trace, and it is created first so that a crash leaves a
        # row saying what was in flight.
        item['content_hash'] = digest
        job_id = jobs_mod.enqueue(
            conn, item=item, case_key=case_key,
            # Every semantic input, not the lexicon digest alone. Keyed on
            # the lexicon, a job for the same comment looked already done
            # after a curator fixed the trope gate that had misread it.
            knowledge_id=knowledge_mod.assessment_id(know.versions),
        )
        jobs_mod.start(conn, job_id)
        try:
            # Chosen per comment, not per run: which examples help depends
            # on what matched and which community the post concerns.
            picked = guidance_mod.select(
                getattr(know, 'examples', None) or [],
                case_id=case.case_id if case is not None else None,
                match=result,
                groups=result.topic_groups or entry['scope'].groups,
            )
            if picked.get('note'):
                summary.setdefault('guidance_notes', [])
                if picked['note'] not in summary['guidance_notes']:
                    summary['guidance_notes'].append(picked['note'])

            verdict = classify_mod.classify(
                ctx, item, result, versions=know.versions,
                group_background=background,
                policy=know.policy_text,
                severity_guidance=know.severity_guidance,
                allowed_categories=know.allowed_categories,
                guidance=picked,
                case_context=case.trigger if case is not None else '',
            )
        except Exception as exc:                          # noqa: BLE001
            # Not swallowed into a match-only verdict here. `classify`
            # already degrades to that for a provider it cannot reach; an
            # exception reaching this far is something worse, and the item
            # deserves to be replayable rather than quietly downgraded.
            jobs_mod.fail(conn, job_id, f'{type(exc).__name__}: {exc}')
            summary['errors'].append(f'classification failed: {exc}')
            # Submitted with what the rules found and nothing more, so the
            # evidence does not wait on a provider -- but not counted or
            # charged. The accounting below used to run for these too, so an
            # outage reported a full run of classifications and billed the
            # case for calls that never happened.
            entry['verdict'] = classify_mod.from_match_only(result, know.versions)
            summary['failed_assessments'] = summary.get('failed_assessments', 0) + 1
            continue
        jobs_mod.complete(conn, job_id, verdict.as_payload(result))
        entry['verdict'] = verdict
        if verdict.key_phrase and not result.matched:
            _note_gap(gaps, item, verdict, result.topic_groups or entry['scope'].groups,
                      know.allowed_categories)

        # Reserve the estimate, settle on what it cost. The estimate is
        # what the budget check above had to work from -- you cannot know
        # the price before making the call -- but charging it afterwards
        # made a case's reported spend a count of calls wearing a dollar
        # sign. Arabic costs several times the tokens of the equivalent
        # English, so the error was not random: it understated exactly the
        # cases this system exists for.
        spent = (verdict.cost_usd if verdict.cost_usd is not None
                 else ESTIMATED_CLASSIFY_COST_USD)
        budget.charge(spent)
        summary['spend_usd'] += spent
        summary['tokens'] = summary.get('tokens', 0) + verdict.total_tokens
        summary['classified'] += 1
        if verdict.cost_usd is None:
            # Worth knowing that a figure is an estimate rather than a
            # measurement, because a budget built on estimates drifts.
            summary['estimated_costs'] = summary.get('estimated_costs', 0) + 1

    # --- and now, in the order they were collected ------------------------
    for entry in candidates:
        findings.append(_finding(entry['item'], entry['result'], entry['verdict'],
                                 case, entry['digest'], know.versions,
                                 scope=entry['scope']))
        record_judgement(conn, entry['item'], entry['result'], entry['verdict'],
                         case, entry['digest'], scope=entry['scope'])

    remember_posts(conn, candidates + revisited, case_key)

    if summary.get('stop_reason') == 'item_budget':
        cases_mod.finish_run(conn, run_id, stop_reason='item_budget',
                             scanned=summary['scanned'], flagged=summary['flagged'],
                             spend=summary['spend_usd'])


    # --- delivery: queued first, sent second ------------------------------
    if findings:
        outbox_mod.enqueue(conn, 'flagged-items/', {'items': findings})
        # Only now. The outbox row is committed, so the work survives a crash
        # here, and marking these seen can no longer lose an item that was never
        # queued.
        remember(conn, collected_digests, case_key)

    # What the lexicon missed: hate the model found where no curated term or
    # trope matched. Proposals for a curator, never added on their own.
    if gaps:
        outbox_mod.enqueue(conn, 'lexicon-gaps/', {'gaps': list(gaps.values())})
        summary['lexicon_gaps'] = len(gaps)

    outbox_mod.enqueue(conn, 'scan-log/', {
        # The case this run worked. It is what lets the platform stamp the case
        # as scanned and move it to the back of the queue; without it every case
        # stays permanently due and the highest-priority one takes every run.
        'case_id': case.case_id if case is not None else None,
        'platforms_browsed': sorted({f['platform'] for f in findings}) or [],
        'posts_scanned': summary['scanned'],
        'items_flagged': summary['flagged'],
        # Which release this run judged under. Without it a reviewer asking why
        # two runs disagreed has to guess whether the knowledge changed.
        'knowledge_versions': know.versions,
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
    # Given back rather than left to time out, so the next run does not have to
    # wait out a lease nobody is using.
    if claimed and case is not None:
        try:
            client.claim_case(case.case_id, release=True)
        except Exception:                                     # noqa: BLE001
            log.debug('ettok: could not release the case claim', exc_info=True)

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
                       'and delivery continue; nothing collected this run has been judged.')
    elif summary['match_only'] and not summary['classified']:
        budget_note = 'Ran without classification; findings were submitted on matching alone.'
    if budget_note:
        summary['budget_note'] = budget_note

    return summary
