"""Backend for the Ettok dashboard tab, mounted at /api/plugins/ettok/.

An agent that runs unattended on a machine in another room is only observable
through something like this. The questions it has to answer are the ones an
operator actually asks, in the order they ask them:

    Is it connected to anything?
    Is anything stuck?
    Are the accounts still alive?
    What has it been doing?
    And -- the one nobody thinks to ask -- what can it not detect?

That last one is here because the failure this project has already lived through
once looks exactly like success: a system that runs on schedule, reports no
errors, and finds nothing, because the knowledge it needs was never curated.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter()

# The platform is a network hop away and this panel polls. Local state is read
# every time; the platform snapshot is cached, because an operator watching a
# dashboard should not be generating a request storm against the VPS.
_PLATFORM_TTL_SECONDS = 60
_platform_cache: Dict[str, Any] = {}
_platform_cache_at: float = 0.0


def _db():
    from plugins.ettok.store import schema
    return schema.connect()


def _config():
    from plugins.ettok import config as config_mod
    return config_mod.load(None)


@router.get('/status')
def status() -> dict:
    """Everything local, read fresh. Cheap enough to poll."""
    from plugins.ettok.platform import outbox as outbox_mod

    cfg = _config()
    conn = _db()

    queue = outbox_mod.status(conn)

    accounts = [
        {
            'account_id': row['account_id'],
            'state': row['state'],
            'last_success_at': row['last_success_at'],
            'last_block_at': row['last_block_at'],
            'block_reason': row['block_reason'],
            'cooldown_until': row['cooldown_until'],
        }
        for row in conn.execute(
            'SELECT * FROM account_health ORDER BY account_id'
        ).fetchall()
    ]

    runs = [
        {
            'id': row['id'],
            'case_id': row['platform_case_id'],
            'group': row['target_group_slug'],
            'started_at': row['started_at'],
            'ended_at': row['ended_at'],
            'stop_reason': row['stop_reason'],
            'scanned': row['posts_scanned'],
            'flagged': row['items_flagged'],
            'spend': row['spend'],
            'errors': json.loads(row['errors'] or '[]'),
        }
        for row in conn.execute(
            'SELECT * FROM case_run ORDER BY id DESC LIMIT 10'
        ).fetchall()
    ]

    evidence_pending = conn.execute(
        'SELECT COUNT(*) FROM evidence_artifact WHERE delivered_at IS NULL'
    ).fetchone()[0]

    return {
        'paired': cfg.is_paired,
        'platform_url': cfg.platform_url,
        'agent_id': cfg.agent_id or None,
        'queue': queue,
        'accounts': accounts,
        'runs': runs,
        'evidence_pending': evidence_pending,
        'alerts': _alerts(queue, accounts, runs, cfg),
    }


def _alerts(queue: dict, accounts: list, runs: list, cfg) -> list:
    """Things an operator should act on, phrased as what to do about them.

    A dashboard that shows numbers and leaves the reader to work out which ones
    are bad is a dashboard nobody checks twice.
    """
    out = []

    if not cfg.is_paired:
        out.append({
            'level': 'critical',
            'text': 'This agent is not paired with a platform. Run `ettok connect`.',
        })

    if queue.get('failed_permanent'):
        out.append({
            'level': 'critical',
            'text': (f"{queue['failed_permanent']} submission(s) failed permanently. "
                     f"Usually a revoked key or a rejected payload -- `ettok outbox failed`."),
        })

    if queue.get('pending', 0) > 50:
        out.append({
            'level': 'warning',
            'text': (f"{queue['pending']} submissions are waiting. If this keeps rising the "
                     f"platform is unreachable, not merely slow."),
        })

    quarantined = [a for a in accounts if a['state'] in ('quarantined', 'auth_lost')]
    if quarantined and len(quarantined) == len(accounts) and accounts:
        out.append({
            'level': 'critical',
            'text': ('Every monitoring account is unavailable. Collection cannot proceed '
                     'until one is restored.'),
        })
    elif quarantined:
        names = ', '.join(a['account_id'] for a in quarantined)
        out.append({'level': 'warning', 'text': f'Accounts unavailable: {names}.'})

    if any(a['state'] == 'auth_lost' for a in accounts):
        out.append({
            'level': 'warning',
            'text': 'A session was signed out. That one needs a person to log in again, '
                    'not a cooldown.',
        })

    recent = [r for r in runs if r['ended_at']]
    if len(recent) >= 3 and all(r['flagged'] == 0 for r in recent[:3]):
        out.append({
            'level': 'info',
            'text': ('The last three runs found nothing. Check the knowledge panel before '
                     'assuming the feed is quiet -- uncurated tropes look exactly like this.'),
        })

    return out


@router.get('/knowledge')
def knowledge() -> dict:
    """What the agent can and cannot currently detect.

    Cached, because this reaches the platform and the panel polls.
    """
    global _platform_cache, _platform_cache_at

    if _platform_cache and (time.time() - _platform_cache_at) < _PLATFORM_TTL_SECONDS:
        return _platform_cache

    cfg = _config()
    if not cfg.is_paired:
        return {'available': False, 'reason': 'not paired with a platform'}

    try:
        from plugins.ettok.platform import knowledge as knowledge_mod
        from plugins.ettok.platform.client import PlatformClient
        from plugins.ettok.setup_wizard import _knowledge_gaps

        know = knowledge_mod.fetch(PlatformClient(cfg))
        payload = {
            'available': True,
            'terms': len(know.terms),
            'tropes': len(know.tropes),
            'cases': len(know.cases),
            'gaps': _knowledge_gaps(know),
            'cases_detail': [
                {
                    'id': c.get('id'),
                    'title': c.get('title'),
                    'state': c.get('state'),
                    'groups': [g.get('slug') for g in c.get('target_groups', [])],
                    'limits': c.get('limits', {}),
                    'suggests_closing': c.get('suggests_closing'),
                }
                for c in know.cases
            ],
        }
    except Exception as exc:                          # noqa: BLE001
        payload = {'available': False, 'reason': str(exc)}

    _platform_cache, _platform_cache_at = payload, time.time()
    return payload


@router.get('/reports')
def reports() -> dict:
    """The far end of the loop: what the platform made of what was sent.

    Separate from /status because it crosses the network, and separate from
    /knowledge because an operator checks it on a different rhythm -- knowledge
    changes when a curator works, reports change when the agent does.
    """
    cfg = _config()
    if not cfg.is_paired:
        return {'available': False, 'reason': 'not paired with a platform'}
    try:
        from plugins.ettok.platform.client import PlatformClient
        data = PlatformClient(cfg).reports(limit=20)
        return {'available': True, **data}
    except Exception as exc:                          # noqa: BLE001
        # An older platform has no reports/ endpoint. That is a missing feature,
        # not a broken agent, and the panel should say so rather than look failed.
        return {'available': False, 'reason': str(exc)}


# ---------------------------------------------------------------------------
# Chat
#
# The dashboard's own chat page is an xterm terminal streamed over a PTY, which
# is fine for an operator and wrong for anyone else: it renders ANSI, not
# markdown, and a research team handed a terminal will not use it.
#
# Rather than reimplement the agent loop, this proxies the OpenAI-compatible
# endpoint the gateway already serves -- same agent, same tools, same session
# handling -- and lets the browser render the stream as structured text.
#
# Proxied rather than called directly from the page because the gateway listens
# on its own port, and a cross-origin fetch from the dashboard would need CORS
# on a local API server that has no business allowing it.
# ---------------------------------------------------------------------------

GATEWAY_DEFAULT_PORT = 8642
_CHAT_TIMEOUT_SECONDS = 300.0


def _gateway_url() -> str:
    import os
    port = os.environ.get('API_SERVER_PORT', str(GATEWAY_DEFAULT_PORT))
    host = os.environ.get('API_SERVER_HOST', '127.0.0.1')
    return f'http://{host}:{port}'


@router.get('/chat/health')
def chat_health() -> dict:
    """Whether there is anything to chat to.

    Checked separately so the page can say "start the gateway" rather than
    failing on the first message, which is when a user decides the thing is
    broken.
    """
    import httpx

    url = _gateway_url()
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f'{url}/v1/models')
        return {
            'available': response.status_code < 400,
            'url': url,
            'status': response.status_code,
        }
    except Exception as exc:                          # noqa: BLE001
        return {
            'available': False,
            'url': url,
            'reason': str(exc),
            'hint': 'Start it with `ettok gateway run` (api_server platform enabled).',
        }


@router.post('/chat')
async def chat(payload: dict) -> Any:
    """Stream one exchange through the gateway, unchanged.

    Deliberately a pass-through. Anything this rewrote would be a second place
    where the agent's behaviour is defined, and the whole reason for proxying
    rather than reimplementing is to avoid exactly that.
    """
    import httpx
    from fastapi.responses import StreamingResponse

    body = {
        'model': payload.get('model') or 'hermes',
        'messages': payload.get('messages') or [],
        'stream': True,
    }
    if payload.get('session_id'):
        body['user'] = payload['session_id']

    url = f'{_gateway_url()}/v1/chat/completions'

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT_SECONDS) as client:
                async with client.stream('POST', url, json=body) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode('utf-8', 'replace')[:400]
                        yield _sse({'error': f'gateway returned {response.status_code}: {detail}'})
                        return
                    # Raw bytes, not lines. The gateway announces tool activity as
                    # an `event: hermes.tool.progress` line followed by its `data:`
                    # line, and re-framing line by line would split that pair --
                    # the browser would then see a data frame with no event name
                    # and quietly treat a tool call as an empty completion chunk.
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except Exception as exc:                      # noqa: BLE001
            # Surfaced into the stream rather than raised: the page is already
            # reading a stream, and an error it can render beats a dead socket.
            yield _sse({'error': f'{type(exc).__name__}: {exc}',
                        'hint': 'Is the gateway running? `ettok gateway run`'})

    return StreamingResponse(relay(), media_type='text/event-stream')


def _sse(obj: dict) -> bytes:
    return f'data: {json.dumps(obj)}\n\n'.encode('utf-8')
