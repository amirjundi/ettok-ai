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

    total = 5
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

    # -- 5. running by itself ----------------------------------------------
    _step(5, total, 'Run unattended?')
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
