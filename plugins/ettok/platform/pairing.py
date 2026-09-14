"""Getting a credential onto a machine without a human carrying it.

Ettok AI is open source, so anyone can run this agent. Holding a key is what
separates an agent the platform chose to trust from one that merely exists, and
this is how a key is obtained.

The handover is the problem. In practice an operator gets a credential from an
admin over WhatsApp or a phone call, which puts a key that never expires into a
chat log on two phones, a backup, and possibly a cloud sync -- where it stays long
after the conversation is forgotten.

So nothing secret travels through a human channel. The agent asks the platform to
open a pairing request, shows the operator a short code and a link, and waits. An
admin signed into the platform opens the link, sees which machine is asking, and
approves. The key is then handed to the polling agent directly, once. What a human
carries is a code that is single-use, expires in minutes, and is worthless to
anyone who reads it afterwards.

Shaped after the OAuth device authorization grant, for the same reason it exists:
the device asking for access has no browser and no way to authenticate a person.
"""

from __future__ import annotations

import logging
import platform as platform_mod
import socket
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Slow enough not to hammer a VPS while an admin walks to their laptop, fast
# enough that approval feels immediate.
POLL_INTERVAL_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 600.0


class PairingError(RuntimeError):
    """The request was refused, expired, already used, or unreachable."""


@dataclass
class PairingRequest:
    pairing_code: str
    verification_url: str
    poll_token: str
    expires_at: str


@dataclass
class PairingResult:
    agent_id: str
    agent_key: str


def machine_name() -> str:
    """What the admin sees when deciding whether to approve.

    An approval screen showing an opaque identifier tells the admin nothing, and
    an admin who cannot tell which machine is asking will approve whatever appears.
    """
    try:
        host = socket.gethostname()
    except Exception:
        host = 'unknown-host'
    return f'{host} ({platform_mod.system().lower()})'


def start(config, *, name: Optional[str] = None, transport=None) -> PairingRequest:
    """Open a pairing request. Deliberately unauthenticated -- there is no key yet."""
    url = config.api('pair/start/')
    payload = {'machine_name': name or machine_name()}
    try:
        with httpx.Client(timeout=config.timeout_seconds, transport=transport) as client:
            response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise PairingError(f'could not reach the platform at {config.platform_url}: {exc}') from exc

    if response.is_redirect:
        # httpx does not follow redirects, and it should not: this POST carries
        # the machine name to an address the operator typed, and a production
        # platform sets SECURE_SSL_REDIRECT, so an `http://` URL answers 301 to
        # the `https://` one. Following it silently would mean the first attempt
        # went out in the clear. Say what happened instead.
        target = response.headers.get('location', '')
        raise PairingError(
            f'the platform redirected {url} to {target or "another address"}. '
            f'It is most likely served over HTTPS -- re-run with '
            f'--platform https://{config.platform_url.split("://", 1)[-1]}')
    if response.status_code == 429:
        raise PairingError('too many pairing attempts from this address; wait and try again')
    if response.status_code >= 400:
        raise PairingError(f'the platform refused the pairing request ({response.status_code})')

    try:
        data = response.json()
        return PairingRequest(
            pairing_code=data['pairing_code'],
            verification_url=data['verification_url'],
            poll_token=data['poll_token'],
            expires_at=data.get('expires_at', ''),
        )
    except (ValueError, KeyError) as exc:
        raise PairingError(f'the platform returned an unusable pairing response: {exc}') from exc


def poll(
    config,
    request: PairingRequest,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    sleep=time.sleep,
    transport=None,
) -> PairingResult:
    """Wait for an admin to approve, then take the key exactly once.

    The key is returned to whoever holds the poll token, which never leaves this
    machine. The code an operator reads aloud cannot be used to collect it.
    """
    url = config.api('pair/poll/')
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=config.timeout_seconds, transport=transport) as client:
                response = client.post(url, json={'poll_token': request.poll_token})
        except httpx.HTTPError as exc:
            log.warning('ettok: pairing poll failed, retrying: %s', exc)
            sleep(POLL_INTERVAL_SECONDS)
            continue

        if response.status_code == 404:
            raise PairingError('this pairing request expired or was already used')
        if response.status_code >= 400:
            raise PairingError(f'pairing failed ({response.status_code})')

        data = response.json() if response.content else {}
        state = data.get('state')

        if state == 'approved':
            try:
                return PairingResult(agent_id=data['agent_id'], agent_key=data['agent_key'])
            except KeyError as exc:
                raise PairingError(f'approval response was missing {exc}') from exc
        if state == 'denied':
            raise PairingError('an administrator denied this request')

        sleep(POLL_INTERVAL_SECONDS)

    raise PairingError('timed out waiting for approval')


def write_credentials(result: PairingResult, env_path, platform_url: str = '') -> None:
    """Persist the key where secrets belong: `.env`, not plugin storage.

    Plugin storage is wiped by a plugin update and is documented as the wrong place
    for secrets. Existing values are replaced in place rather than appended, so
    re-pairing a machine does not leave a stale key above the live one.

    The platform address is written alongside the key, because a machine that
    paired with a platform belongs to that platform. Without it, `--platform`
    lasted only as long as the process: pairing succeeded, the key landed, and
    the next run fell back to the built-in default and refused to connect to a
    platform nobody had pointed it at. Paired, keyed, and unreachable -- the
    agent on the machine this happened to called itself "half connected".
    """
    from pathlib import Path

    path = Path(env_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    wanted = {'ETTOK_AGENT_ID': result.agent_id, 'ETTOK_AGENT_KEY': result.agent_key}
    if platform_url:
        wanted['ETTOK_PLATFORM_URL'] = platform_url.rstrip('/')
    kept = [ln for ln in lines if ln.split('=', 1)[0].strip() not in wanted]
    kept.extend(f'{key}={value}' for key, value in wanted.items())

    path.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    try:
        path.chmod(0o600)   # no-op on Windows, correct everywhere else
    except OSError:
        pass
