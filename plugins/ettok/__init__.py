"""Ettok AI — hate speech monitoring for minority communities in Iraq.

Everything this plugin does lives under this directory. It integrates through the
documented `register(ctx)` surface and touches no runtime core file, which is both
the runtime's own rule for plugins and what keeps `git merge upstream/main` working
on a fork of a repository with 33,000 commits.

The division of labour, which explains most of the design:

  The platform owns the knowledge and the verdict. It holds the lexicon, the
  tropes, the exemptions, the rubric and the cases; it re-evaluates everything this
  agent submits, and its conclusion is the one that stands.

  This agent owns collection and delivery. It fetches knowledge per run and keeps
  none of it, captures evidence before an item counts as collected, forms an
  advisory opinion when there is budget for one, and gets findings onto the
  platform without losing or duplicating any of them.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

TOOLSET = 'ettok'


def _tool_error(message: str, **extra) -> str:
    from tools.registry import tool_error
    return tool_error(message, **extra)


def _tool_result(**kwargs) -> str:
    from tools.registry import tool_result
    return tool_result(**kwargs)


def _guard(fn):
    """Tool handlers return JSON and never raise.

    A handler that raises takes the turn down with it. Every failure here has to
    come back as something the agent can read and act on, including the one that
    matters most -- this machine is not paired with a platform yet.
    """
    def wrapper(args: dict, **kwargs) -> str:
        try:
            return fn(args or {}, **kwargs)
        except Exception as exc:                      # noqa: BLE001 - deliberate
            log.exception('ettok: %s failed', getattr(fn, '__name__', 'tool'))
            return _tool_error(f'{type(exc).__name__}: {exc}')
    wrapper.__name__ = getattr(fn, '__name__', 'ettok_tool')
    return wrapper


def _learned_selectors(ctx, platform: str):
    """Selectors the agent worked out on a previous run, if any."""
    try:
        return (ctx.get_config('selectors', {}) or {}).get(platform)
    except Exception:
        return None


def _services(ctx):
    """Config, database and client, assembled the same way everywhere."""
    from . import config as config_mod
    from .platform.client import PlatformClient
    from .store import schema

    cfg = config_mod.load(ctx)
    return cfg, schema.connect(), PlatformClient(cfg)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _make_tools(ctx):
    from .platform import knowledge as knowledge_mod
    from .platform import outbox as outbox_mod

    @_guard
    def sync_knowledge(args: dict, **_) -> str:
        """Fetch the lexicon, tropes, cases and accounts for this run."""
        cfg, conn, client = _services(ctx)
        know = knowledge_mod.fetch(client)
        ctx._ettok_knowledge = know          # in memory, for this process only
        return _tool_result(
            terms=len(know.terms),
            tropes=len(know.tropes),
            cases=len(know.cases),
            accounts=len(know.accounts),
            versions=know.versions,
            topic_markers={k: len(v) for k, v in know.group_markers().items()},
            note='Held in memory for this run only and never written to disk.',
        )

    @_guard
    def submit(args: dict, **_) -> str:
        """Drain everything waiting for the platform."""
        cfg, conn, client = _services(ctx)
        reclaimed = outbox_mod.reclaim_in_flight(conn)
        result = outbox_mod.drain(conn, client, limit=int(args.get('limit') or 50))
        return _tool_result(reclaimed=reclaimed, **result, queue=outbox_mod.status(conn))

    @_guard
    def case_status(args: dict, **_) -> str:
        """What the agent is working on, and what it is waiting for."""
        cfg, conn, client = _services(ctx)
        know = getattr(ctx, '_ettok_knowledge', None)
        return _tool_result(
            paired=cfg.is_paired,
            platform=cfg.platform_url,
            agent_id=cfg.agent_id or None,
            cases=[
                {
                    'id': c.get('id'),
                    'title': c.get('title'),
                    'state': c.get('state'),
                    'limits': c.get('limits'),
                    'suggests_closing': c.get('suggests_closing'),
                }
                for c in (know.cases if know else [])
            ],
            queue=outbox_mod.status(conn),
            knowledge_loaded=bool(know),
        )

    @_guard
    def match_item(args: dict, **_) -> str:
        """Deterministic matching. Free, and always available."""
        from .detect import match as match_mod

        know = getattr(ctx, '_ettok_knowledge', None)
        if know is None:
            return _tool_error('No knowledge loaded. Run ettok_sync_knowledge first.')

        item = {
            'text': args.get('text', ''),
            'parent_post_text': args.get('parent_post_text', ''),
            'parent_media_text': args.get('parent_media_text', ''),
        }
        result = match_mod.evaluate(item, know)
        return _tool_result(
            matched=result.matched,
            explanation=result.explain(),
            fired_terms=[t['term'] for t in result.fired_terms],
            fired_tropes=[{'name': t['name'], 'why': t['activation_reason']}
                          for t in result.fired_tropes],
            post_concerns=result.topic_groups,
            exemptions_to_respect=result.exemption_hints,
            skipped_terms=result.skipped_terms,
        )

    @_guard
    def classify_item(args: dict, **_) -> str:
        """Advisory verdict. The platform re-evaluates and its verdict stands."""
        from .detect import classify as classify_mod
        from .detect import match as match_mod

        know = getattr(ctx, '_ettok_knowledge', None)
        if know is None:
            return _tool_error('No knowledge loaded. Run ettok_sync_knowledge first.')

        item = {
            'text': args.get('text', ''),
            'parent_post_text': args.get('parent_post_text', ''),
            'parent_media_text': args.get('parent_media_text', ''),
        }
        result = match_mod.evaluate(item, know)

        background = ''
        for case in know.cases:
            for group in case.get('target_groups', []):
                if group.get('slug') in result.topic_groups and group.get('background'):
                    background = group['background']
                    break

        if args.get('match_only'):
            verdict = classify_mod.from_match_only(result, know.versions)
        else:
            verdict = classify_mod.classify(
                ctx, item, result, versions=know.versions, group_background=background,
            )

        return _tool_result(**verdict.as_payload(result), explanation=result.explain())

    @_guard
    def explain_item(args: dict, **_) -> str:
        """Why an item was flagged, or why it was not.

        Distinguishes the three answers a reviewer actually needs apart: nothing
        matched, the gate was not satisfied, or an exemption applies.
        """
        from .detect import match as match_mod

        know = getattr(ctx, '_ettok_knowledge', None)
        if know is None:
            return _tool_error('No knowledge loaded. Run ettok_sync_knowledge first.')

        item = {
            'text': args.get('text', ''),
            'parent_post_text': args.get('parent_post_text', ''),
        }
        result = match_mod.evaluate(item, know)

        if result.matched:
            verdict = 'flagged'
        elif result.topic_groups:
            verdict = 'not flagged: the post concerns a monitored community, but no term or gate fired'
        else:
            verdict = 'not flagged: the post concerns no monitored community'

        return _tool_result(
            verdict=verdict,
            detail=result.explain(),
            post_concerns=result.topic_groups,
            exemptions_that_would_apply=result.exemption_hints,
        )

    @_guard
    def collect(args: dict, **_) -> str:
        """Open a page and take the comments on it, with evidence."""
        from .collect import base as collect_mod
        from .collect import session as session_mod
        from .store import schema

        url = (args.get('url') or '').strip()
        if not url:
            return _tool_error('A url is required.')

        platform = (args.get('platform') or 'facebook').strip().lower()
        if platform not in collect_mod.COLLECTORS:
            return _tool_error(
                f'No collector for "{platform}". Supported: '
                + ', '.join(sorted(collect_mod.COLLECTORS))
            )

        conn = schema.connect()
        account_id = (args.get('account_id') or '').strip()
        if account_id and session_mod.account_state(conn, account_id) != session_mod.HEALTHY:
            return _tool_error(
                f'Account "{account_id}" is not available -- it was quarantined after a '
                f'block. Use another account or wait for its cooldown.'
            )

        learned = _learned_selectors(ctx, platform)
        collector = collect_mod.for_platform(
            ctx, platform, selectors=args.get('selectors') or learned,
        )
        result = collector.collect(url, capture_evidence=args.get('capture_evidence', True))

        if not result.ok:
            # Quarantine, report, and stop. Never work around it.
            if account_id:
                session_mod.quarantine(conn, account_id, result.blocked,
                                       auth_lost=result.auth_lost)
            return _tool_result(
                blocked=True,
                reason=result.blocked,
                auth_lost=result.auth_lost,
                account_quarantined=bool(account_id),
                guidance=(
                    'This is the platform saying it has noticed. Do not solve it and do '
                    'not retry: solving a challenge removes the only warning and leaves '
                    'the detection, so the account escalates to a permanent ban instead '
                    'of backing off while it is still recoverable. Report this to an '
                    'operator and move on.'
                ),
            )

        if account_id:
            session_mod.record_success(conn, account_id)

        evidence = result.evidence
        return _tool_result(
            url=url,
            items=result.items,
            count=len(result.items),
            evidence_captured=bool(evidence and evidence.is_complete),
            evidence_hash=(evidence.content_hash if evidence else ''),
            selectors_in_use=('learned' if learned else 'default'),
            note=('Each item carries the post it replies to. Pass them to ettok_scan.'
                  if result.items else
                  'Nothing was extracted, which usually means this page uses a layout the '
                  'current selectors do not match -- these sites change their markup '
                  'without notice. Read the page_outline below, work out selectors for '
                  'the post container, the comment containers and the author element, '
                  'test them with ettok_try_selectors, and save the working set with '
                  'ettok_learn_selectors. Then collect again.'),
            page_outline=('' if result.items else collector.page_outline()),
        )

    @_guard
    def try_selectors(args: dict, **_) -> str:
        """Test a guess at the page's layout before committing to it."""
        from .collect import base as collect_mod

        selectors = args.get('selectors') or {}
        missing = [k for k in ('post', 'comment', 'author') if not selectors.get(k)]
        if missing:
            return _tool_error(f'selectors must include: {", ".join(missing)}')

        collector = collect_mod.for_platform(
            ctx, (args.get('platform') or 'facebook').lower(), selectors=selectors,
        )
        if collector is None:
            return _tool_error('No collector for that platform.')

        outcome = collector.probe(selectors)
        return _tool_result(
            **outcome,
            note=('These work. Save them with ettok_learn_selectors so later runs '
                  'do not have to work them out again.' if outcome.get('ok') else
                  'These found nothing. Try a different container selector.'),
        )

    @_guard
    def learn_selectors(args: dict, **_) -> str:
        """Remember a working layout, so the next run starts from it.

        Saved to plugin config rather than code: these change when the site
        changes, and an agent that re-derives them every run pays for the same
        discovery repeatedly.
        """
        selectors = args.get('selectors') or {}
        platform = (args.get('platform') or 'facebook').strip().lower()
        missing = [k for k in ('post', 'comment', 'author') if not selectors.get(k)]
        if missing:
            return _tool_error(f'selectors must include: {", ".join(missing)}')

        try:
            store = dict(ctx.get_config('selectors', {}) or {})
            store[platform] = selectors
            ctx.set_config('selectors', store)
        except Exception as exc:                      # noqa: BLE001
            return _tool_error(f'could not save selectors: {exc}')

        return _tool_result(
            saved=True, platform=platform, selectors=selectors,
            note='Later runs will use these without re-deriving them.',
        )

    @_guard
    def scan(args: dict, **_) -> str:
        """Work a batch of items end to end: match, classify, queue, deliver."""
        from . import scan as scan_mod

        items = args.get('items') or []
        if not isinstance(items, list) or not items:
            return _tool_error(
                'No items supplied. Pass items: [{text, parent_post_text, url, platform}]. '
                'Each comment needs the post it replies to, or context-dependent hate '
                'cannot be judged.'
            )
        return _tool_result(**scan_mod.run(
            ctx,
            items=items,
            case_id=args.get('case_id'),
            classify=args.get('classify', True),
            submit=args.get('submit', True),
        ))

    _TEXT_ARGS = {
        'text': {'type': 'string', 'description': 'The comment being judged.'},
        'parent_post_text': {
            'type': 'string',
            'description': 'The post it replies to. Context-dependent hate is invisible without this.',
        },
        'parent_media_text': {
            'type': 'string',
            'description': "Text read out of the parent post's image or video.",
        },
    }

    return [
        (
            'ettok_collect',
            {
                'name': 'ettok_collect',
                'description': (
                    'Open a post and collect the comments under it, capturing evidence '
                    'before anything is extracted. Returns items ready for ettok_scan, '
                    'each carrying the post it replies to. If the platform presents a '
                    'CAPTCHA or block this reports it and stops -- never solve one.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'url': {'type': 'string', 'description': 'The post to open.'},
                        'platform': {'type': 'string', 'description': 'Defaults to facebook.'},
                        'account_id': {
                            'type': 'string',
                            'description': 'Monitoring account in use, so its health is tracked.',
                        },
                        'capture_evidence': {'type': 'boolean'},
                    },
                    'required': ['url'],
                },
            },
            collect,
            '🧰',
        ),
        (
            'ettok_try_selectors',
            {
                'name': 'ettok_try_selectors',
                'description': (
                    'Test CSS selectors against the page currently open, and report how '
                    'many comments they would find. Use this after reading a page_outline '
                    'when collection found nothing.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'selectors': {
                            'type': 'object',
                            'description': 'Keys: post, comment, author.',
                            'properties': {
                                'post': {'type': 'string'},
                                'comment': {'type': 'string'},
                                'author': {'type': 'string'},
                            },
                        },
                        'platform': {'type': 'string'},
                    },
                    'required': ['selectors'],
                },
            },
            try_selectors,
            '🧪',
        ),
        (
            'ettok_learn_selectors',
            {
                'name': 'ettok_learn_selectors',
                'description': (
                    'Save a working set of selectors so later runs use them directly. Do '
                    'this once ettok_try_selectors confirms they find comments.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'selectors': {'type': 'object'},
                        'platform': {'type': 'string'},
                    },
                    'required': ['selectors'],
                },
            },
            learn_selectors,
            '📝',
        ),
        (
            'ettok_scan',
            {
                'name': 'ettok_scan',
                'description': (
                    'Work a batch of collected comments end to end for the current case: '
                    'deduplicate, match against the synced lexicon and tropes, classify '
                    'what matched if there is budget, queue the findings and deliver them. '
                    'Each item needs the post it replies to. Safe to re-run: items already '
                    'seen are skipped and delivery never duplicates.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'items': {
                            'type': 'array',
                            'description': 'Collected comments, each with its parent post.',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'text': {'type': 'string'},
                                    'parent_post_text': {'type': 'string'},
                                    'parent_media_text': {'type': 'string'},
                                    'url': {'type': 'string'},
                                    'platform': {'type': 'string'},
                                    'author_name': {'type': 'string'},
                                    'author_id': {'type': 'string'},
                                },
                                'required': ['text'],
                            },
                        },
                        'case_id': {'type': 'integer', 'description': 'Case to attribute this to.'},
                        'classify': {'type': 'boolean', 'description': 'Skip the model pass if false.'},
                        'submit': {'type': 'boolean', 'description': 'Queue only, do not deliver.'},
                    },
                    'required': ['items'],
                },
            },
            scan,
            '🛰',
        ),
        (
            'ettok_match',
            {
                'name': 'ettok_match',
                'description': (
                    'Match a comment against the synced lexicon and tropes. Deterministic '
                    'and free -- no model call -- so it works with no budget at all. Use it '
                    'to decide whether an item is worth classifying.'
                ),
                'parameters': {
                    'type': 'object', 'properties': dict(_TEXT_ARGS), 'required': ['text'],
                },
            },
            match_item,
            '🔎',
        ),
        (
            'ettok_classify',
            {
                'name': 'ettok_classify',
                'description': (
                    'Form an advisory verdict on a comment, judged together with the post '
                    'it replies to. The Ettok platform re-evaluates every submission and a '
                    'human reviews it, so this opinion informs triage rather than deciding '
                    'anything. Pass match_only to skip the model call entirely.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        **_TEXT_ARGS,
                        'match_only': {
                            'type': 'boolean',
                            'description': 'Skip the model and report only what matched.',
                        },
                    },
                    'required': ['text'],
                },
            },
            classify_item,
            '🧭',
        ),
        (
            'ettok_explain',
            {
                'name': 'ettok_explain',
                'description': (
                    'Explain why a comment was or was not flagged: whether nothing matched, '
                    'the activation gate was unmet, or an exemption applies.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {k: _TEXT_ARGS[k] for k in ('text', 'parent_post_text')},
                    'required': ['text'],
                },
            },
            explain_item,
            '💡',
        ),
        (
            'ettok_sync_knowledge',
            {
                'name': 'ettok_sync_knowledge',
                'description': (
                    'Fetch the current lexicon, tropes, open cases and monitoring accounts '
                    'from the Ettok platform. Run this before collecting or classifying '
                    'anything; the knowledge is held in memory for this run only.'
                ),
                'parameters': {'type': 'object', 'properties': {}, 'required': []},
            },
            sync_knowledge,
            '\U0001f4da',
        ),
        (
            'ettok_submit',
            {
                'name': 'ettok_submit',
                'description': (
                    'Deliver everything queued for the Ettok platform: findings, vocabulary '
                    'proposals and scan logs. Safe to call at any time and repeatedly; '
                    'nothing is lost and nothing is delivered twice.'
                ),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'limit': {'type': 'integer', 'description': 'Maximum rows to send.'},
                    },
                    'required': [],
                },
            },
            submit,
            '\U0001f4e4',
        ),
        (
            'ettok_case_status',
            {
                'name': 'ettok_case_status',
                'description': (
                    'Report what this agent is paired to, which cases are open, what the '
                    'delivery queue holds, and whether knowledge has been synced.'
                ),
                'parameters': {'type': 'object', 'properties': {}, 'required': []},
            },
            case_status,
            '\U0001f4cb',
        ),
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Wire the plugin into the runtime.

    Kept deliberately thin: anything that can fail at import time takes the plugin
    down with it, and a monitoring agent that silently did not load is exactly the
    failure mode this project has already lived through once.
    """
    for name, schema, handler, emoji in _make_tools(ctx):
        ctx.register_tool(
            name=name, toolset=TOOLSET, schema=schema, handler=handler,
            description=schema['description'], emoji=emoji,
        )

    # The classifier gets its own routable slot, so an operator can point
    # classification at Gemini without moving the model the agent chats on.
    try:
        ctx.register_auxiliary_task(
            'ettok_classify',
            display_name='Ettok classifier',
            description='Advisory hate-speech classification, separate from the chat model.',
        )
    except Exception:
        log.debug('ettok: auxiliary task slot unavailable', exc_info=True)

    try:
        from .cli import register_cli, handle_cli
        ctx.register_cli_command(
            name='ettok',
            help='Pair with a platform, run a scan, inspect the queue',
            setup_fn=register_cli,
            handler_fn=handle_cli,
            description='Operate the Ettok AI monitoring agent.',
        )
    except Exception:
        log.exception('ettok: CLI registration failed; tools are still available')

    try:
        ctx.register_system_prompt_section('ettok.operating-rules', _OPERATING_RULES)
    except Exception:
        log.debug('ettok: system prompt section unavailable', exc_info=True)

    # Guidance the agent loads when it works a case. Deliberately a skill rather
    # than more system prompt: it is long, it is only relevant during a run, and
    # the system prompt is charged on every turn whether or not it is used.
    try:
        from pathlib import Path
        skills_dir = Path(__file__).parent / 'skills'
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / 'SKILL.md'
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
    except Exception:
        log.debug('ettok: skill registration unavailable', exc_info=True)


_OPERATING_RULES = (
    'You are operating Ettok AI, a hate speech monitoring agent. Four rules govern '
    'everything you do with it. Your classification is advisory: the Ettok platform '
    're-evaluates every item and a human reviews it before anything is reported, so '
    'never claim more confidence than the evidence supports. Judge a comment together '
    'with the post it replies to, never alone -- the same words are ordinary piety '
    'under one post and a libel under another. Never circumvent an access control: a '
    'CAPTCHA or a block is the platform telling you it has noticed, so stop and report '
    'it rather than working around it. And the people in this content are real, often '
    'from persecuted communities, and sometimes the targets of organised campaigns -- '
    'handle what you collect accordingly.'
)
