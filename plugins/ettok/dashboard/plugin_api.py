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

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)

router = APIRouter()

# The platform is a network hop away and this panel polls. Local state is read
# every time; the platform snapshot is cached, because an operator watching a
# dashboard should not be generating a request storm against the VPS.
# Turns still running. Held strongly: asyncio keeps only a weak reference to a
# task, and a turn nobody is watching is precisely the one that would otherwise
# be garbage collected mid-sentence.
_running_turns: set = set()

# session id -> when its turn started. A turn is only written into the session
# when it finishes: while it runs, the rows are tool calls and assistant entries
# with no text, which the chat page correctly drops. So an operator who comes
# back mid-turn sees their own message and nothing under it, and reasonably
# concludes the agent stopped when they left. It had not; there was simply
# nothing yet to show and no way to say so.
_active_sessions: dict = {}

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


@router.get('/threads')
def threads(limit: int = 40, case_id: str = '') -> dict:
    """What this agent read, grouped the way the platform shows it.

    Case, then post, then the comments under it -- because that is the unit a
    person judges: a comment is read against what it replies to, and the same
    words under a different post are a different finding. A flat list ordered by
    arrival scatters one pile-on across forty unrelated rows.

    Built from this machine's own records, so it works with no network and shows
    what THIS agent saw, which is the question an operator brings to the agent's
    own dashboard rather than to the platform's.
    """
    conn = _db()
    params = []
    clause = ''
    if case_id:
        clause = ' WHERE case_id = ?'
        params.append(case_id)

    rows = conn.execute(
        'SELECT * FROM classification' + clause + ' ORDER BY created_at DESC LIMIT ?',
        (*params, max(1, min(int(limit), 500)) * 40),
    ).fetchall()

    # Grouped on the comment's own page rather than the post text: the same post
    # read again next week comes back edited or truncated.
    posts = {}
    for row in rows:
        key = (row['case_id'], row['url'] or '(no url)')
        post = posts.setdefault(key, {
            'case_id': row['case_id'],
            'case': row['case_title'],
            'url': row['url'],
            'post': row['parent_excerpt'],
            'collected': 0,
            'matched': 0,
            'judged_hate': 0,
            'last_seen': row['created_at'],
            'comments': [],
        })
        post['collected'] += 1
        if row['why_flagged']:
            post['matched'] += 1
        if row['is_hate_speech']:
            post['judged_hate'] += 1
        if not post['post'] and row['parent_excerpt']:
            post['post'] = row['parent_excerpt']
        post['comments'].append({
            'at': row['created_at'],
            'text': row['excerpt'],
            'is_hate_speech': bool(row['is_hate_speech']),
            'why_flagged': row['why_flagged'],
            'reason': row['reason'],
            'category': row['category'],
            'severity': row['severity'],
            'tier': row['tier'],
        })

    # Newest first, then the busiest thread to the top. Two passes because a
    # timestamp cannot be negated inside one sort key, and a single tuple sort
    # would have put the OLDEST post first among equally busy ones.
    grouped = sorted(posts.values(), key=lambda p: p['last_seen'], reverse=True)
    grouped.sort(key=lambda p: -p['matched'])
    for post in grouped:
        post['share'] = (
            round(post['matched'] * 100 / post['collected']) if post['collected'] else 0
        )

    cases = {}
    for post in grouped:
        case = cases.setdefault(post['case_id'], {
            'id': post['case_id'], 'title': post['case'] or 'No case',
            'collected': 0, 'matched': 0, 'judged_hate': 0, 'posts': [],
        })
        case['posts'].append(post)
        case['collected'] += post['collected']
        case['matched'] += post['matched']
        case['judged_hate'] += post['judged_hate']

    return {'cases': list(cases.values())}


@router.get('/judgements')
def judgements(limit: int = 100, only: str = '', case_id: str = '') -> dict:
    """What this agent decided, and why, held on this machine.

    Separate from /reports, which asks the platform what became of a submission.
    This is the other half of the same question and the half nobody could see:
    an operator could read the platform's verdict and never their own agent's,
    so "the agent is judging badly" was a claim that could only be checked by
    logging into somebody else's database -- and a run made while unpaired left
    no trace of its reasoning at all.
    """
    conn = _db()
    where, params = [], []
    if only == 'hate':
        where.append('is_hate_speech = 1')
    elif only == 'clear':
        where.append('is_hate_speech = 0')
    elif only == 'matched':
        # The agent read it and a rule fired, whatever the verdict was.
        where.append("why_flagged != ''")
    if case_id:
        where.append('case_id = ?')
        params.append(case_id)

    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    rows = conn.execute(
        'SELECT * FROM classification' + clause + ' ORDER BY created_at DESC LIMIT ?',
        (*params, max(1, min(int(limit), 500))),
    ).fetchall()

    totals = conn.execute(
        'SELECT COUNT(*) AS read, '
        'SUM(CASE WHEN why_flagged != '' THEN 1 ELSE 0 END) AS matched, '
        'SUM(is_hate_speech) AS judged_hate FROM classification'
    ).fetchone()

    return {
        'judgements': [
            {
                'id': row['id'],
                'at': row['created_at'],
                'case': row['case_title'],
                'case_id': row['case_id'],
                'platform': row['platform'],
                'url': row['url'],
                'excerpt': row['excerpt'],
                'parent_excerpt': row['parent_excerpt'],
                'is_hate_speech': bool(row['is_hate_speech']),
                'why_flagged': row['why_flagged'],
                'category': row['category'],
                'severity': row['severity'],
                'reason': row['reason'],
                'terms': json.loads(row['fired_terms'] or '[]'),
                'tropes': json.loads(row['fired_tropes'] or '[]'),
                'exemption_applied': row['exemption_applied'],
                # `context` means the agent read it and nothing fired;
                # `matched_only` means no model was affordable on that run.
                'tier': row['tier'],
                'versions': json.loads(row['versions'] or '{}'),
            }
            for row in rows
        ],
        'totals': {
            'read': totals['read'] or 0,
            'matched': totals['matched'] or 0,
            'judged_hate': totals['judged_hate'] or 0,
        },
        'cases': [
            {'id': r['case_id'], 'title': r['case_title'], 'count': r['n']}
            for r in conn.execute(
                'SELECT case_id, case_title, COUNT(*) AS n FROM classification '
                "WHERE case_id != '' GROUP BY case_id, case_title ORDER BY n DESC"
            ).fetchall()
        ],
    }


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
# A chat turn is a stream, not a request/response, so the useful limit is how
# long to wait for the NEXT chunk rather than for the whole thing. httpx applies
# one number to connect, read, write and pool, so a flat 300.0 meant a turn died
# after five minutes of quiet -- which is an ordinary length for one browser
# navigation or a scan, and the symptom is a reply that stops mid-sentence with
# no error anywhere.
#
# Connect stays short: a gateway that is not listening should fail immediately,
# not hang the page. Read is generous but finite -- unbounded would leak a task
# against a wedged gateway, and the operator can always press Stop.
_CHAT_CONNECT_SECONDS = 10.0
_CHAT_READ_SECONDS = 900.0


def _gateway_url() -> str:
    import os
    port = os.environ.get('API_SERVER_PORT', str(GATEWAY_DEFAULT_PORT))
    host = os.environ.get('API_SERVER_HOST', '127.0.0.1')
    return f'http://{host}:{port}'


def _gateway_headers() -> dict:
    """Bearer auth for the gateway, when a key is configured.

    The gateway refuses to start without ``API_SERVER_KEY`` and 401s every
    request that does not carry it, so a proxy that forwards no Authorization
    header reaches a running gateway and is turned away -- which reads to the
    user as "the chat is broken", not "a key is missing".
    """
    try:
        from agent.secret_scope import get_secret
        key = (get_secret('API_SERVER_KEY', '') or '').strip()
    except Exception:                                 # noqa: BLE001
        key = ''
    return {'Authorization': f'Bearer {key}'} if key else {}


_autostart_attempted = False


def _ensure_chat_backend() -> dict:
    """Configure the api_server platform and start the gateway if it is not up.

    "The dashboard is running, so the chat should work" is a reasonable thing to
    expect, and it was not true: the tab needs a second process, configured in a
    third place, and told you none of that beyond "not running". Two machines and
    several rounds of the same error later, the honest conclusion is that the
    setup step was the bug.

    Attempted once per dashboard process. Idempotent: an already-running gateway
    is left alone, and configuration only fills what is empty, so an operator who
    chose a port or a key keeps them.

    This is convenience, not supervision. A gateway started here dies with the
    dashboard, which is why setup also offers `ettok gateway install` -- that is
    what makes the agent work in the background with no dashboard at all.
    """
    global _autostart_attempted
    if _autostart_attempted:
        return {'attempted': False, 'reason': 'already attempted this process'}
    _autostart_attempted = True

    if _gateway_is_up():
        return {'attempted': False, 'reason': 'gateway already running'}

    configured, detail = _configure_api_server()
    if not configured:
        log.warning('ettok: could not configure the chat gateway: %s', detail)
        return {'attempted': False, 'reason': detail}

    started = _spawn_gateway()
    log.info('ettok: chat gateway %s', 'started' if started else 'could not be started')

    # A gateway that refuses to start does so within a second or two, so a short
    # wait is the difference between reporting "starting..." forever and
    # reporting the actual reason. Long enough to catch a refusal, short enough
    # that a page load does not feel stalled.
    if started:
        import time
        for _ in range(12):
            time.sleep(0.5)
            if _gateway_is_up():
                break
        else:
            reason = _gateway_failure_reason()
            if reason:
                return {'attempted': True, 'configured': detail,
                        'started': False, 'failure': reason}

    return {'attempted': True, 'configured': detail, 'started': started}


def _gateway_log_path():
    """Where a gateway started by the dashboard writes its startup output."""
    from hermes_constants import get_hermes_home
    from pathlib import Path

    return Path(get_hermes_home()) / 'logs' / 'ettok-chat-gateway.log'


def _gateway_failure_reason() -> str:
    """The line that explains why the gateway is not up, or ''.

    Only the lines that say something: the gateway logs a page of "dependent
    tools will be unavailable this turn" warnings on every start, and burying
    the one real error in those is how it went unread the first time.
    """
    try:
        text = _gateway_log_path().read_text(encoding='utf-8', errors='replace')
    except Exception:                                 # noqa: BLE001
        return ''

    interesting = [
        line.strip() for line in text.splitlines()
        if ('ERROR' in line or 'Refusing to start' in line
            or 'non-retryable' in line or 'failed to connect' in line)
        and 'tools.registry' not in line
    ]
    if not interesting:
        return ''
    # The first error is the cause; the ones after it are usually consequences.
    reason = interesting[0]
    # Strip the logger preamble, which is noise to a reader in a browser.
    for marker in (': ', ' - '):
        if marker in reason and reason.index(marker) < 80:
            reason = reason.split(marker, 1)[1]
            break
    return reason[:400]


def _has_api_server_key() -> bool:
    """Whether a usable key exists, from the secret scope or straight off disk.

    Read from the file as a fallback because the key may have been written by
    this very process, after the secret scope cached its answer.
    """
    try:
        from agent.secret_scope import get_secret
        if (get_secret('API_SERVER_KEY', '') or '').strip():
            return True
    except Exception:                                 # noqa: BLE001
        pass
    try:
        from plugins.ettok.cli import _env_path
        for line in _env_path().read_text(encoding='utf-8').splitlines():
            if line.startswith('API_SERVER_KEY='):
                # The startup guard rejects short or placeholder keys, so an
                # empty or token value here is the same as having none.
                return len(line.split('=', 1)[1].strip()) >= 16
    except Exception:                                 # noqa: BLE001
        pass
    return False


def _gateway_is_up() -> bool:
    import httpx

    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(f'{_gateway_url()}/v1/models', headers=_gateway_headers())
        # 401 still means something is listening and serving the API; the key is
        # a separate problem and starting a second gateway would not fix it.
        return response.status_code < 500
    except Exception:                                 # noqa: BLE001
        return False


def _configure_api_server() -> tuple:
    """Enable the platform the chat proxies to, and give it a key."""
    import secrets

    try:
        from hermes_cli import config as hermes_config
        cfg = hermes_config.load_config() or {}
        gateway_cfg = cfg.setdefault('gateway', {})
        if not isinstance(gateway_cfg, dict):
            return False, 'gateway config is not a mapping'
        platforms = gateway_cfg.setdefault('platforms', {})
        if not isinstance(platforms, dict):
            return False, 'gateway.platforms is not a mapping'

        # Key first, and no enabling at all if it cannot be written. Enabling
        # the platform without a key is worse than leaving it alone: a missing
        # key is a non-retryable startup conflict, so the gateway refuses to
        # start entirely and the cron scheduler and messaging go down with it.
        # That is exactly what this code did on a machine where the gateway had
        # been running fine.
        from plugins.ettok.setup_wizard import _ensure_api_server_key
        _ensure_api_server_key(secrets.token_urlsafe(32))
        if not _has_api_server_key():
            return False, 'no API_SERVER_KEY could be written; leaving the gateway alone'

        api = platforms.setdefault('api_server', {})
        api['enabled'] = True
        api.setdefault('port', 8642)
        api.setdefault('host', '127.0.0.1')
        hermes_config.save_config(cfg)
        return True, f'api_server on port {api.get("port")}'
    except Exception as exc:                          # noqa: BLE001
        return False, f'{type(exc).__name__}: {exc}'


def _spawn_gateway() -> bool:
    """Start the gateway detached, so it outlives the request that started it.

    Not a supervised service: this dies with the dashboard. `ettok gateway
    install` is the durable form and setup offers it.
    """
    import subprocess
    import sys

    try:
        # Into a log, not into DEVNULL. The gateway explains itself perfectly
        # well when it refuses to start -- "API_SERVER_KEY is required", "port
        # already in use" -- and discarding that left the operator with "the
        # gateway is not running" and nowhere to look. The reason is the whole
        # value of the message.
        log_path = _gateway_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, 'w', encoding='utf-8', errors='replace')
        kwargs = {'stdin': subprocess.DEVNULL,
                  'stdout': handle,
                  'stderr': subprocess.STDOUT}
        if sys.platform == 'win32':
            # Detached, and without a console window appearing over the user's
            # browser.
            kwargs['creationflags'] = (
                getattr(subprocess, 'DETACHED_PROCESS', 0)
                | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            )
        else:
            kwargs['start_new_session'] = True

        subprocess.Popen(
            [sys.executable, '-m', 'hermes_cli.main', 'gateway', 'run'], **kwargs,
        )
        return True
    except Exception:                                 # noqa: BLE001
        log.warning('ettok: could not spawn the chat gateway', exc_info=True)
        return False


def _api_server_configured() -> bool:
    """Whether the gateway has an api_server platform to serve at all.

    A gateway with no platforms starts cleanly, logs nothing alarming, and
    listens on nothing -- so "start the gateway" is advice that cannot work, and
    the operator runs the right command twice and concludes the product is
    broken. That happened on a fresh install here.
    """
    try:
        from hermes_cli import config as hermes_config
        platforms = ((hermes_config.load_config() or {})
                     .get('gateway', {}) or {}).get('platforms', {}) or {}
        return bool((platforms.get('api_server') or {}).get('enabled'))
    except Exception:                                 # noqa: BLE001
        # Unknown is not the same as absent; fall back to the generic advice
        # rather than telling somebody to configure what may already be there.
        return True


def _not_running_hint() -> str:
    if not _api_server_configured():
        return ('The gateway has no api_server platform configured, so starting it '
                'would serve nothing. Run `ettok setup` to configure it, or '
                '`ettok gateway install` to have it start automatically.')
    return ('Start it with `ettok gateway run`, or `ettok gateway install` to '
            'have it start on login and survive reboots.')


@router.get('/chat/active')
def chat_active() -> dict:
    """Sessions with a turn still being written.

    The page asks this when it reopens a conversation. Without it, returning
    mid-turn is indistinguishable from returning to a turn that failed: both
    show the question and nothing else.
    """
    now = time.time()
    return {
        'sessions': [
            {'session_id': session_id, 'running_for': round(now - started, 1)}
            for session_id, started in _active_sessions.items()
        ],
    }


@router.get('/chat/health')
def chat_health() -> dict:
    """Whether there is anything to chat to.

    Checked separately so the page can say "start the gateway" rather than
    failing on the first message, which is when a user decides the thing is
    broken.
    """
    import httpx

    url = _gateway_url()

    # Opening the chat tab is what starts the backend. Doing it here rather than
    # at plugin load keeps it off the path of operators who never open chat, and
    # means the page that needs it is the page that asks for it.
    _ensure_chat_backend()

    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f'{url}/v1/models', headers=_gateway_headers())
        if response.status_code == 401:
            # Running, but it does not accept us. Say which of the two it is.
            return {
                'available': False, 'url': url, 'status': 401,
                'reason': 'the gateway rejected the API_SERVER_KEY this agent sent',
                'hint': 'Set API_SERVER_KEY in .env to the same value the gateway uses.',
            }
        return {
            'available': response.status_code < 400,
            'url': url,
            'status': response.status_code,
        }
    except Exception as exc:                          # noqa: BLE001
        # If it was tried and refused, its own words beat anything written here.
        failure = _gateway_failure_reason()
        return {
            'available': False,
            'url': url,
            'reason': failure or str(exc),
            'configured': _api_server_configured(),
            'hint': (f'The gateway refused to start: {failure}'
                     if failure else _not_running_hint()),
        }


# ---------------------------------------------------------------------------
# The credential vault
# ---------------------------------------------------------------------------
#
# Monitoring needs accounts, and an operator with no other route types the
# password into the chat -- which is the one place it must never go, because a
# transcript keeps it for good. The vault already existed as a CLI command and
# nothing in the dashboard mentioned it, so the CLI was the only way to know it
# was there.
#
# What crosses the wire here is a password, so writes are refused unless the
# request came over loopback. On a tunnelled or non-loopback dashboard the form
# would put a credential on the network to save it from a transcript, which is
# not a trade worth making. Reads never return secrets at all: the store keeps
# the identifier as metadata by design (the agent types it itself) and only the
# password is encrypted, so there is nothing secret to leak through the list.


def _vault():
    from agent.vault_store import VaultStore
    return VaultStore()


def _is_loopback(request) -> bool:
    client = getattr(request, 'client', None)
    host = (getattr(client, 'host', '') or '').strip()
    return host in ('127.0.0.1', '::1', 'localhost', '')


@router.get('/vault')
def vault_list() -> dict:
    """Handles, kinds and identifiers. Never a password."""
    try:
        store = _vault()
        return {
            'items': [meta.to_dict() for meta in store.list_items()],
            'kinds': ['login', 'payment', 'address'],
        }
    except Exception as exc:                          # noqa: BLE001
        log.warning('ettok: could not read the vault', exc_info=True)
        return {'items': [], 'error': f'{type(exc).__name__}: {exc}'}


@router.post('/vault')
async def vault_add(request: Request) -> dict:
    """Store one login. The password is encrypted at rest and never read back.

    Bound to an origin, because that is what makes the fill safe: the page the
    agent is on must match exactly, or nothing is typed.
    """
    if not _is_loopback(request):
        return {'ok': False,
                'error': 'Adding a credential is only allowed from this machine. '
                         'Open the dashboard on 127.0.0.1 rather than over the '
                         'network -- otherwise the password crosses the wire to '
                         'be saved from a transcript, which is not a trade worth '
                         'making.'}

    body = await request.json()
    kind = (body.get('kind') or 'login').strip()
    label = (body.get('label') or '').strip()
    origin = (body.get('origin') or '').strip()
    identifier = (body.get('identifier') or '').strip()
    identifier_type = (body.get('identifier_type') or 'email').strip()
    password = body.get('password') or ''
    otp = (body.get('otp_secret') or '').strip()

    if kind != 'login':
        return {'ok': False, 'error': 'Only logins can be added here for now.'}
    for field, value in (('name', label), ('site', origin),
                         ('username', identifier), ('password', password)):
        if not value:
            return {'ok': False, 'error': f'{field} is required.'}

    secret = {
        'identifier_type': identifier_type,
        'identifier': identifier,
        'password': password,
    }
    if otp:
        secret['otp_secret'] = otp

    try:
        meta = _vault().add_item(kind='login', label=label, secret=secret, origin=origin)
    except Exception as exc:                          # noqa: BLE001
        # Deliberately not logging the exception object with exc_info: a
        # validation error can carry the value that failed validation.
        log.warning('ettok: vault add refused (%s)', type(exc).__name__)
        return {'ok': False, 'error': str(exc)}

    return {'ok': True, 'item': meta.to_dict()}


@router.delete('/vault/{item_id}')
def vault_remove(item_id: str) -> dict:
    try:
        return {'ok': bool(_vault().remove_item(item_id))}
    except Exception as exc:                          # noqa: BLE001
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


# Fields the page may set on a turn. An allow-list rather than a spread of the
# payload: this body reaches the agent, and a field nobody vetted arriving from
# the browser is how a chat box turns into a config surface.
_PASSTHROUGH_FIELDS = ('reasoning_effort', 'temperature', 'max_tokens')


@router.post('/chat/clarify')
async def chat_clarify(payload: dict) -> dict:
    """Answer a question the agent asked mid-turn.

    A separate request on purpose: the turn that asked is still streaming, and
    the thread that asked is blocked waiting for exactly this.
    """
    import httpx
    from fastapi import HTTPException

    clarify_id = str(payload.get('clarify_id') or '').strip()
    answer = payload.get('response')
    if isinstance(answer, list):
        answer = ', '.join(str(a) for a in answer)
    answer = str(answer or '').strip()
    if not clarify_id or not answer:
        raise HTTPException(status_code=400, detail='clarify_id and response are required')

    url = f'{_gateway_url()}/v1/clarify/{clarify_id}'
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(url, json={'response': answer},
                                         headers=_gateway_headers())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'the agent could not be reached: {exc}')
    if response.status_code == 404:
        raise HTTPException(
            status_code=409,
            detail='That question is no longer waiting — it was answered or it timed out.')
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail='the agent refused the answer')
    return {'ok': True}


@router.post('/chat')
async def chat(payload: dict) -> Any:
    """Stream one exchange through the gateway.

    Deliberately close to a pass-through. Anything this rewrote would be a
    second place where the agent's behaviour is defined, and the whole reason
    for proxying rather than reimplementing is to avoid exactly that.

    The session header is the exception worth explaining. `X-Hermes-Session-Id`
    is how the gateway resumes a conversation instead of starting a new one, and
    it comes back on the response naming the session the turn actually landed
    in. Both directions matter: without the request header every message is a
    fresh session, and without reading the response the page never learns the id
    it would need to send.
    """
    import httpx
    from fastapi.responses import StreamingResponse

    body = {
        'model': payload.get('model') or 'hermes',
        'messages': payload.get('messages') or [],
        'stream': True,
    }
    for field in _PASSTHROUGH_FIELDS:
        if payload.get(field) not in (None, ''):
            body[field] = payload[field]

    headers = _gateway_headers()
    resume = (payload.get('resume_session_id') or '').strip()
    if resume:
        headers['X-Hermes-Session-Id'] = resume

    url = f'{_gateway_url()}/v1/chat/completions'

    # The turn runs in a task of its own, and the browser reads from a queue it
    # fills. That separation is the whole point: relaying straight from the
    # gateway to the browser tied the agent's work to somebody looking at it, so
    # closing the tab or switching pages cancelled the response mid-sentence.
    # Measured before this change -- a forty-step answer, client disconnected
    # after four seconds, and the reply stored in the session was one character
    # long. The operator came back to their own question and nothing else.
    #
    # A queue rather than a shared buffer because the reader and the writer run
    # at different speeds, and because dropping the reader must not stall the
    # writer. `None` closes it.
    queue: 'asyncio.Queue' = asyncio.Queue()

    started_at = time.time()
    # Session ids this turn registered, so the cleanup removes its own and not
    # whatever happens to share a timestamp.
    mine: set = set()

    async def pump():
        """Read the gateway to the end, whether or not anyone is listening."""
        try:
            timeout = httpx.Timeout(
                connect=_CHAT_CONNECT_SECONDS, read=_CHAT_READ_SECONDS,
                write=60.0, pool=_CHAT_CONNECT_SECONDS,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream('POST', url, json=body,
                                          headers=headers) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode('utf-8', 'replace')[:400]
                        await queue.put(_sse({
                            'error': f'gateway returned {response.status_code}: {detail}'}))
                        return
                    # Announced in-band rather than as a response header: the
                    # header is written before the agent has resolved which
                    # session the turn belongs to, and a stream's headers are
                    # long flushed by the time it is known.
                    landed = response.headers.get('X-Hermes-Session-Id')
                    if landed:
                        _active_sessions[landed] = time.time()
                        mine.add(landed)
                        await queue.put(_sse({'session_id': landed}))
                    # Raw bytes, not lines. The gateway announces tool activity as
                    # an `event: hermes.tool.progress` line followed by its `data:`
                    # line, and re-framing line by line would split that pair --
                    # the browser would then see a data frame with no event name
                    # and quietly treat a tool call as an empty completion chunk.
                    async for chunk in response.aiter_bytes():
                        await queue.put(chunk)
        except Exception as exc:                      # noqa: BLE001
            # Surfaced into the stream rather than raised: the page is already
            # reading a stream, and an error it can render beats a dead socket.
            await queue.put(_sse({'error': f'{type(exc).__name__}: {exc}',
                                  'hint': _not_running_hint()}))
        finally:
            # Remove exactly what this turn registered. The previous version
            # matched on `v == started_at`, but the value stored is the time the
            # session id ARRIVED, which is always later than the time the turn
            # began -- so the comparison never matched and nothing was ever
            # removed. /chat/active then reported every session it had ever seen
            # as still running, for the life of the process.
            for session_id in mine:
                _active_sessions.pop(session_id, None)
            await queue.put(None)
            _running_turns.discard(asyncio.current_task())

    task = asyncio.create_task(pump())
    # Held, because asyncio keeps only a weak reference to a running task and a
    # turn nobody is watching is exactly the one that would be collected.
    _running_turns.add(task)

    async def relay():
        """Hand the browser whatever has arrived, and stop caring if it leaves."""
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk
        except (GeneratorExit, asyncio.CancelledError):
            # The browser navigated away or closed the tab. The turn keeps
            # going; the gateway writes it into the session as it completes, so
            # it is there when the operator comes back. Draining the queue in
            # the background stops the writer blocking once it fills.
            async def drain():
                while await queue.get() is not None:
                    pass
            drainer = asyncio.create_task(drain())
            _running_turns.add(drainer)
            drainer.add_done_callback(_running_turns.discard)
            raise

    return StreamingResponse(relay(), media_type='text/event-stream')


def _sse(obj: dict) -> bytes:
    return f'data: {json.dumps(obj)}\n\n'.encode('utf-8')
