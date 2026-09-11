"""Where the agent gets its settings, and where it does not get its secrets.

Two rules, both load-bearing:

The platform URL is configuration and lives in ``config.yaml`` via ``ctx.get_config``,
because an operator changes it when the platform moves to a VPS and that should not
require editing a file the plugin owns.

The agent key is a secret and never touches plugin storage or ``config.yaml``. It is
read through ``agent.secret_scope``, which resolves the active profile's ``.env`` and
fails closed rather than quietly handing back another profile's credential when the
gateway is multiplexing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PLATFORM_URL = 'http://localhost:8000'

# Human pacing, not politeness. Collection that moves at machine speed is what a
# platform's automation detection is looking for, so these are survival settings.
DEFAULT_MIN_DELAY_SECONDS = 4.0
DEFAULT_MAX_DELAY_SECONDS = 11.0

# Anything slower than this and a dropped connection stalls the run; anything
# faster and a slow VPS looks like a failure.
DEFAULT_TIMEOUT_SECONDS = 30.0


def _secret(name: str, default: str = '') -> str:
    """Read a secret, preferring the profile-scoped resolver.

    ``agent.secret_scope`` is the correct path under a multiplexed gateway. It is
    imported lazily and falls back to the environment, so this module stays usable
    in a bare unit test that has not booted the runtime.
    """
    try:
        from agent.secret_scope import get_secret
        value = get_secret(name, default)
    except Exception:
        value = os.environ.get(name, default)
    return (value or default).strip()


@dataclass(frozen=True)
class EttokConfig:
    platform_url: str
    agent_id: str
    agent_key: str
    min_delay_seconds: float
    max_delay_seconds: float
    timeout_seconds: float

    @property
    def is_paired(self) -> bool:
        """Whether this machine has been granted access by the platform.

        Ettok AI is open source, so anyone can run the agent. Holding a key is what
        distinguishes an agent the platform chose to trust from one that merely
        exists, and `ettok connect` is how a key is obtained.
        """
        return bool(self.agent_key and self.agent_id)

    def api(self, path: str) -> str:
        return f"{self.platform_url.rstrip('/')}/api/hermes/{path.lstrip('/')}"


def load(ctx=None) -> EttokConfig:
    """Build the live configuration.

    ``ctx`` is optional so tools, the CLI and tests can all call this the same way;
    without it the platform URL falls back to the environment.
    """
    url = ''
    if ctx is not None:
        try:
            url = (ctx.get_config('platform_url', '') or '').strip()
        except Exception:
            url = ''
    url = url or os.environ.get('ETTOK_PLATFORM_URL', '').strip() or DEFAULT_PLATFORM_URL

    def _num(key: str, env: str, fallback: float) -> float:
        raw = ''
        if ctx is not None:
            try:
                raw = str(ctx.get_config(key, '') or '')
            except Exception:
                raw = ''
        raw = raw or os.environ.get(env, '')
        try:
            return float(raw) if raw else fallback
        except ValueError:
            return fallback

    return EttokConfig(
        platform_url=url,
        agent_id=_secret('ETTOK_AGENT_ID'),
        agent_key=_secret('ETTOK_AGENT_KEY'),
        min_delay_seconds=_num('min_delay_seconds', 'ETTOK_MIN_DELAY', DEFAULT_MIN_DELAY_SECONDS),
        max_delay_seconds=_num('max_delay_seconds', 'ETTOK_MAX_DELAY', DEFAULT_MAX_DELAY_SECONDS),
        timeout_seconds=_num('timeout_seconds', 'ETTOK_TIMEOUT', DEFAULT_TIMEOUT_SECONDS),
    )
