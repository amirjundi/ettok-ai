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

    return [
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
