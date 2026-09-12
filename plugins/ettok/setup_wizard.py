"""`ettok setup` — from a fresh clone to a working agent, in one command.

Every step here was something a person had to already know. Which platform to
point at, that pairing needs an administrator, that a model has to be configured
before classification does anything, that the plugin is opt-in. None of that is
discoverable from an error message, and the first one a new operator hits --
`ettok: not recognized` -- reads like a failed install rather than an unactivated
environment.

So this asks, checks, and says what is still missing. It is safe to re-run: every
step detects what is already done and offers to keep it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DIVIDER = '─' * 62

# What this agent actually uses. The runtime's default is the `hermes-cli`
# composite, which means every tool it ships -- desktop control, Spotify, Kanban,
# video generation, Home Assistant. Each one's schema is re-read by the model on
# every single turn, so an agent that will never open Spotify still pays for
# knowing how, in latency and in tokens, on every message.
#
# Measured on this install: 46 tools, ~10,300 tokens of definitions per request.
# Bundled plugins this agent never uses. Every one of them is imported and
# registered at startup whether or not it is ever called: measured here, 52
# plugins load in 1.90s, and disabling these leaves 18 loading in 0.70s.
#
# Disabled rather than deleted. A deny-list is one config line to undo, while
# deleting the directories would conflict on every `git merge upstream/main`
# forever -- and the cost being paid here is startup time, not disk.
UNUSED_PLUGINS = [
    # Messaging platforms. The agent reports to the Ettok platform over HTTPS and
    # talks to nobody else.
    'a2a-platform', 'buzz-platform', 'dingtalk-platform', 'discord-platform',
    'email-platform', 'feishu-platform', 'google_chat-platform',
    'homeassistant-platform', 'irc-platform', 'line-platform', 'matrix-platform',
    'mattermost-platform', 'ntfy-platform', 'photon-platform', 'raft-platform',
    'simplex-platform', 'slack-platform', 'sms-platform', 'teams-platform',
    'telegram-platform', 'wecom-platform', 'whatsapp-platform',
    # Generation providers. This agent reads; it does not produce media.
    'image_gen/deepinfra', 'image_gen/fal', 'image_gen/krea', 'image_gen/meta-ai',
    'image_gen/openai', 'image_gen/openai-codex', 'image_gen/openrouter', 'image_gen/xai',
    'video_gen/deepinfra', 'video_gen/fal', 'video_gen/xai',
    'spotify',
]

MONITORING_TOOLSETS = [
    'ettok',        # this plugin
    'browser',      # collection
    'web',          # finding posts worth opening
    'file',         # reading and writing locally
    'terminal',     # occasional operator work
    'skills',       # working-a-case
    'todo',         # tracking a long run
    'cronjob',      # scheduling itself
    'clarify',      # asking an operator rather than guessing
]


def _say(text: str = '') -> None:
    print(text)


def _step(number: int, total: int, title: str) -> None:
    _say()
    _say(f'{DIVIDER}\n  Step {number} of {total}   {title}\n{DIVIDER}')


def _ask(prompt: str, default: str = '') -> str:
    """Ask, unless nobody is there to answer.

    The wizard runs in a terminal, but it may also be reached from a script or a
    provisioning step, and blocking forever on a prompt nobody sees is worse than
    taking the default.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return default
    suffix = f' [{default}]' if default else ''
    try:
        answer = input(f'  {prompt}{suffix}: ').strip()
    except (EOFError, KeyboardInterrupt):
        _say()
        return default
    return answer or default


def _interactive() -> bool:
    return bool(sys.stdin) and sys.stdin.isatty()


def _confirm(prompt: str, default: bool = True, *, blocking: bool = False) -> bool:
    """Ask, or decide for a shell nobody is watching.

    `blocking` marks a step that waits on a human somewhere else -- pairing waits
    for an administrator to approve. Those must never start unattended: a
    provisioning script that silently hangs for ten minutes looks like a broken
    install, and the person who would have approved it does not know to.
    """
    if not _interactive():
        return False if blocking else default
    answer = _ask(f'{prompt} (y/n)', 'y' if default else 'n').lower()
    return answer.startswith('y')


def run(args) -> int:
    from . import config as config_mod
    from .platform import knowledge as knowledge_mod
    from .platform import pairing
    from .platform.client import PlatformClient, PlatformError
    from .store import schema

    total = 6
    _say()
    _say('  Ettok AI — hate speech monitoring for minority communities in Iraq')
    _say('  This sets the agent up on this machine. It is safe to run again.')

    # -- 1. where the platform is -----------------------------------------
    _step(1, total, 'Which platform does this agent report to?')
    cfg = config_mod.load(None)
    _say('  The platform owns the lexicon, the tropes and the review queue.')
    _say('  This agent collects and reports; it decides nothing on its own.')
    _say()

    url = getattr(args, 'platform', None) or _ask('Platform URL', cfg.platform_url)
    if url != cfg.platform_url:
        os.environ['ETTOK_PLATFORM_URL'] = url
        cfg = config_mod.load(None)

    reachable = False
    try:
        import httpx
        with httpx.Client(timeout=10.0) as client:
            client.get(url, follow_redirects=True)
        reachable = True
        _say(f'  Reached {url}.')
    except Exception as exc:                          # noqa: BLE001
        _say(f'  Could not reach {url} — {type(exc).__name__}.')
        _say('  Check the platform is running and that this machine can see it.')
        _say('  On a second machine it must be bound to 0.0.0.0, not 127.0.0.1,')
        _say('  and its host must appear in the platform\'s ALLOWED_HOSTS.')

    # -- 2. pairing --------------------------------------------------------
    _step(2, total, 'Let this machine in')
    if cfg.is_paired:
        _say(f'  Already paired as "{cfg.agent_id}".')
        if not _confirm('Pair again with a new key?', default=False, blocking=True):
            _say('  Keeping the existing key.')
        else:
            cfg = _pair(cfg, pairing, args)
    elif not reachable:
        _say('  Skipped — the platform has to be reachable before it can approve.')
    else:
        _say('  Ettok AI is open source, so anyone can run this agent. An')
        _say('  administrator has to approve this specific machine before it can')
        _say('  see anything. You will get a short code to read out; the key')
        _say('  itself never travels through a human.')
        _say()
        if _confirm('Request access now?', blocking=True):
            cfg = _pair(cfg, pairing, args)
        elif _interactive():
            _say('  Skipped. Run `ettok connect` when you are ready.')
        else:
            _say('  Skipped -- pairing needs someone present to read out the code.')
            _say('  Run `ettok connect` from a terminal when you are ready.')

    # -- 3. the model ------------------------------------------------------
    _step(3, total, 'Which model forms the agent\'s opinion?')
    _say('  Classification is advisory — the platform re-evaluates everything and')
    _say('  a human reviews it. The agent works without a model configured; it')
    _say('  simply reports what matched rather than forming a view.')
    _say()
    if _model_configured():
        _say('  A model provider is configured.')
    else:
        _say('  No model provider is configured yet.')
        _say('  Run `ettok model` to choose one (Gemini, OpenAI, OpenRouter, ...).')
        _say('  Collection, matching and delivery all work without this.')

    # -- 4. what it can actually detect ------------------------------------
    _step(4, total, 'What can it detect right now?')
    if not cfg.is_paired:
        _say('  Unknown until this machine is paired.')
    else:
        try:
            know = knowledge_mod.fetch(PlatformClient(cfg))
            gaps = _knowledge_gaps(know)
            _say(f'  Synced {len(know.terms)} terms, {len(know.tropes)} tropes, '
                 f'{len(know.cases)} open case(s).')
            if gaps:
                _say()
                _say('  Detection is limited until curators fill these in. None of')
                _say('  it is a code problem, and none of it blocks installing:')
                for gap in gaps:
                    _say(f'    - {gap}')
            else:
                _say('  Knowledge looks complete.')
        except (PlatformError, Exception) as exc:     # noqa: BLE001
            _say(f'  Could not sync — {exc}')

    # -- 5. trimming the tool list ------------------------------------------
    _step(5, total, 'Trim the tools it carries?')
    current = _current_toolsets()
    if current == MONITORING_TOOLSETS:
        _say('  Already trimmed to the monitoring set.')
    else:
        _say('  The runtime offers every tool it ships -- desktop control, Spotify,')
        _say('  Kanban, video generation. The model re-reads all of their')
        _say('  definitions on every turn, so an agent that will never open')
        _say('  Spotify still pays for knowing how, on every message.')
        _say()
        _say('  Keeping: ' + ', '.join(MONITORING_TOOLSETS))
        _say()
        _say('  It also disables ' + str(len(UNUSED_PLUGINS)) + ' bundled plugins the agent never')
        _say('  uses -- messaging platforms, image and video generators. Measured:')
        _say('  52 plugins loading in 1.90s becomes 18 in 0.70s.')
        _say()
        _say('  And it stops the runtime hiding plugin tools behind a search.')
        _say('  Left on, it hides all ten Ettok tools -- an agent whose whole job')
        _say('  is ettok_scan cannot see ettok_scan without looking for it first.')
        _say()
        if _confirm('Trim to the monitoring set?', default=True):
            if _apply_toolsets():
                _say('  Trimmed. Restart a session for it to take effect.')
            else:
                _say('  Could not write the config; run `ettok tools` to set it by hand.')
        else:
            _say('  Left as-is. `ettok tools` changes it later.')

    # -- 6. running by itself ----------------------------------------------
    _step(6, total, 'Run unattended?')
    _say('  A scheduled run survives reboots and a closed laptop, which an')
    _say('  in-process timer does not.')
    _say()
    if cfg.is_paired and _confirm('Schedule a run every 6 hours?', default=False):
        from .cli import _schedule
        import argparse
        _schedule(argparse.Namespace(every='6h', name='ettok-scan', remove=False,
                                     platform=None))
    else:
        _say('  Skipped. `ettok schedule --every 6h` sets it up later.')

    # -- done --------------------------------------------------------------
    _say()
    _say(DIVIDER)
    _say('  Setup finished. Next:')
    _say()
    _say('    ettok doctor     check everything end to end')
    _say('    ettok status     pairing, open cases, delivery queue')
    if not cfg.is_paired:
        _say('    ettok connect    request access to a platform')
    _say(DIVIDER)
    _say()
    return 0


def _pair(cfg, pairing, args):
    from . import config as config_mod
    from .cli import _env_path

    try:
        request = pairing.start(cfg, name=getattr(args, 'name', None))
    except pairing.PairingError as exc:
        _say(f'  Could not start pairing: {exc}')
        return cfg

    _say()
    _say(f'    Pairing code:  {request.pairing_code}')
    _say(f'    Approve at:    {request.verification_url}')
    _say()
    _say('  Send an administrator that link. They will see this machine by name')
    _say('  and approve it. Waiting...')

    try:
        result = pairing.poll(cfg, request, timeout_seconds=600.0)
    except pairing.PairingError as exc:
        _say(f'  Pairing failed: {exc}')
        return cfg

    pairing.write_credentials(result, _env_path())
    os.environ['ETTOK_AGENT_ID'] = result.agent_id
    os.environ['ETTOK_AGENT_KEY'] = result.agent_key
    _say(f'  Approved. Paired as "{result.agent_id}".')
    return config_mod.load(None)


def _current_toolsets() -> list:
    try:
        from hermes_cli import config as hermes_config
        return list((hermes_config.load_config() or {}).get('toolsets') or [])
    except Exception:
        return []


def _apply_toolsets() -> bool:
    """Narrow the tool list, and stop the agent's own tools being hidden from it.

    Two changes, and the second matters more than the first.

    Trimming the toolsets drops what monitoring never uses -- desktop control,
    Spotify, Kanban, video generation -- whose schemas the model otherwise re-reads
    on every single turn.

    Turning tool_search off then keeps the remaining tools eager. Left on, the
    runtime defers plugin tools behind a search to save context, which for a
    general assistant is right and for this agent is backwards: measured here it
    hid all ten Ettok tools, so an agent whose entire job is ettok_scan could not
    see ettok_scan without going looking for it first.

    Measured on this install: 10,289 tokens per request with everything and the
    tools deferred, against 8,127 with this applied and all ten visible. Cheaper
    and more capable, which is rare enough to be worth the comment.

    Written to the runtime's config rather than forced in code, so an operator who
    wants the full set can put it back with `ettok tools`.
    """
    try:
        from hermes_cli import config as hermes_config
        cfg = hermes_config.load_config() or {}
        cfg['toolsets'] = list(MONITORING_TOOLSETS)

        # Startup cost, paid on every command. These load whether called or not.
        plugins_cfg = cfg.setdefault('plugins', {})
        if isinstance(plugins_cfg, dict):
            disabled = set(plugins_cfg.get('disabled') or [])
            plugins_cfg['disabled'] = sorted(disabled | set(UNUSED_PLUGINS))
        tools_cfg = cfg.setdefault('tools', {})
        if isinstance(tools_cfg, dict):
            search_cfg = tools_cfg.setdefault('tool_search', {})
            if isinstance(search_cfg, dict):
                search_cfg['enabled'] = 'off'
        hermes_config.save_config(cfg)
        return True
    except Exception:
        return False


def _model_configured() -> bool:
    """Whether the runtime has a provider it can actually call."""
    try:
        from hermes_cli import config as hermes_config
        cfg = hermes_config.load_config() or {}
        return bool(cfg.get('provider') or cfg.get('model'))
    except Exception:
        return bool(
            os.environ.get('GEMINI_API_KEY')
            or os.environ.get('GOOGLE_API_KEY')
            or os.environ.get('OPENAI_API_KEY')
            or os.environ.get('OPENROUTER_API_KEY')
        )


def _knowledge_gaps(know) -> list:
    """The honest answer to "will this find anything".

    Each of these looks exactly like a working system producing no results, which
    is the failure this project has already lived through once.
    """
    gaps = []

    formless = sum(1 for t in know.tropes if not (t.get('surface_forms') or []))
    if formless:
        gaps.append(f'{formless} of {len(know.tropes)} tropes have no surface forms, '
                    f'so they have no text to match and cannot fire at all')

    ungated = sum(1 for t in know.tropes
                  if t.get('requires_target_group') and not (t.get('activation_topics') or []))
    if ungated:
        gaps.append(f'{ungated} of {len(know.tropes)} tropes have no activation gate, '
                    f'so they cannot tell an attack from ordinary speech')

    unexempted = sum(1 for t in know.terms if not (t.get('never_flag_when') or []))
    if unexempted:
        gaps.append(f'{unexempted} of {len(know.terms)} terms have no exemptions, so '
                    f'journalists and survivors quoting them would be flagged')

    markers = know.group_markers()
    groups = {g.get('slug') for c in know.cases for g in c.get('target_groups', [])}
    unmarked = [g for g in groups if not markers.get(g)]
    if unmarked:
        gaps.append(f'{len(unmarked)} monitored group(s) have no topic markers, so posts '
                    f'about them cannot be recognised: {", ".join(sorted(unmarked))}')

    if not know.cases:
        gaps.append('no case is open, so there is nothing to scan yet')

    return gaps
