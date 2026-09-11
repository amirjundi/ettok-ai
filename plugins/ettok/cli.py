"""`ettok` — the operator's side of the agent.

These commands exist because every failure in the previous attempt at this system
was silent. It reported success, collected nothing, and nobody could tell the
difference without opening the database. `ettok doctor` is the answer to "is this
actually working", and it is the first thing to run on a new machine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def register_cli(subparser) -> None:
    """Build the `ettok <command>` tree."""
    commands = subparser.add_subparsers(dest='ettok_command')

    connect = commands.add_parser(
        'connect', help='Pair this machine with an Ettok platform',
    )
    connect.add_argument('--platform', help='Platform base URL, e.g. https://ettok.example')
    connect.add_argument('--name', help='Name the administrator will see when approving')
    connect.add_argument('--timeout', type=float, default=600.0, help='Seconds to wait for approval')

    wizard = commands.add_parser(
        'setup', help='Set this machine up: platform, pairing, model, schedule',
    )
    wizard.add_argument('--platform', help='Platform base URL')
    wizard.add_argument('--name', help='Name the administrator will see when approving')

    commands.add_parser('doctor', help='Check everything this agent needs in order to work')
    commands.add_parser('status', help='Show pairing, open cases and the delivery queue')

    outbox = commands.add_parser('outbox', help='Inspect or drain the delivery queue')
    outbox.add_argument('action', nargs='?', default='status', choices=['status', 'drain', 'failed'])

    schedule = commands.add_parser(
        'schedule', help='Run unattended on a recurring schedule',
    )
    schedule.add_argument('--every', default='6h',
                          help='Interval, e.g. 30m, 6h, or a 5-field cron expression')
    schedule.add_argument('--name', default='ettok-scan')
    schedule.add_argument('--remove', action='store_true', help='Remove the scheduled run')

    subparser.set_defaults(func=handle_cli)


def handle_cli(args) -> int:
    command = getattr(args, 'ettok_command', None)
    handlers = {
        'setup': _setup,
        'connect': _connect,
        'doctor': _doctor,
        'status': _status,
        'outbox': _outbox,
        'schedule': _schedule,
    }
    handler = handlers.get(command)
    if handler is None:
        # No subcommand is how a new operator arrives here. Point at setup
        # rather than printing a list they have no basis for choosing from.
        print('Usage: ettok {setup|connect|doctor|status|outbox|schedule}')
        print()
        print('New here? Run:  ettok setup')
        return 1
    return handler(args)


def _load_config(args=None):
    from . import config as config_mod
    import os

    if args is not None and getattr(args, 'platform', None):
        os.environ['ETTOK_PLATFORM_URL'] = args.platform
    return config_mod.load(None)


def _env_path() -> Path:
    """Where the agent key is written.

    `.env` under the Hermes home, alongside every other credential this runtime
    holds -- not plugin storage, which a plugin update deletes.
    """
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / '.env'


# ---------------------------------------------------------------------------

def _connect(args) -> int:
    """Pair this machine, without a key passing through a human channel."""
    from .platform import pairing

    cfg = _load_config(args)
    print(f'Platform: {cfg.platform_url}')

    try:
        request = pairing.start(cfg, name=getattr(args, 'name', None))
    except pairing.PairingError as exc:
        print(f'\nCould not start pairing: {exc}', file=sys.stderr)
        return 1

    print('\n  Pairing code:  ' + request.pairing_code)
    print('  Approve at:    ' + request.verification_url)
    if request.expires_at:
        print('  Expires:       ' + request.expires_at)
    print('\nAsk an administrator to open that link and approve this machine.')
    print('Waiting...', flush=True)

    try:
        result = pairing.poll(cfg, request, timeout_seconds=getattr(args, 'timeout', 600.0))
    except pairing.PairingError as exc:
        print(f'\nPairing failed: {exc}', file=sys.stderr)
        return 1

    pairing.write_credentials(result, _env_path())
    print(f'\nPaired as "{result.agent_id}". The key is in {_env_path()} and was never')
    print('sent through a human channel. Run `ettok doctor` to confirm everything works.')
    return 0


def _doctor(args) -> int:
    """Answer "is this actually working" without reading logs or a database."""
    from .platform import knowledge as knowledge_mod
    from .platform.client import PlatformClient, PlatformError
    from .store import schema

    cfg = _load_config(args)
    checks: list = []

    def check(label: str, ok: bool, detail: str = '') -> None:
        checks.append((label, ok, detail))
        print(f'  {"PASS" if ok else "FAIL"}  {label}' + (f'  -- {detail}' if detail else ''))

    print(f'Ettok AI doctor\n  Platform: {cfg.platform_url}\n')

    check('paired with a platform', cfg.is_paired,
          '' if cfg.is_paired else 'run `ettok connect`')

    try:
        conn = schema.connect()
        conn.execute('SELECT 1 FROM outbox LIMIT 1')
        check('local database', True, str(schema.data_dir()))
    except Exception as exc:
        check('local database', False, str(exc))
        conn = None

    try:
        evidence = schema.evidence_dir()
        probe = evidence / '.write-probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        check('evidence directory writable', True, str(evidence))
    except Exception as exc:
        check('evidence directory writable', False, str(exc))

    if cfg.is_paired:
        client = PlatformClient(cfg)
        try:
            client.heartbeat({'status': 'doctor'})
            check('platform reachable and key accepted', True)
        except PlatformError as exc:
            check('platform reachable and key accepted', False, str(exc))
        else:
            try:
                know = knowledge_mod.fetch(client)
                ungated = sum(
                    1 for t in know.tropes
                    if t.get('requires_target_group') and not (t.get('activation_topics') or [])
                )
                check('knowledge fetch', True,
                      f'{len(know.terms)} terms, {len(know.tropes)} tropes, {len(know.cases)} cases')
                if ungated:
                    print(f'        note: {ungated} trope(s) have no activation gate yet, so '
                          f'context-dependent detection is limited until curators fill them')
                if not know.cases:
                    print('        note: no open cases -- nothing to scan until one is opened')
            except Exception as exc:
                check('knowledge fetch', False, str(exc))

    try:
        from tools import browser_tool  # noqa: F401
        check('browser tooling present', True)
    except Exception as exc:
        check('browser tooling present', False, str(exc))
    else:
        # Importing the module proves nothing about whether a browser exists.
        # Without one the agent asks permission to download 170MB mid-run, which
        # is the worst moment to ask -- an unattended run has nobody to answer,
        # and an attended one is interrupted by a question about npm.
        try:
            from tools.browser_tool_install import _chromium_installed
            if _chromium_installed():
                check('browser installed', True)
            else:
                check('browser installed', False,
                      'run: npm install -g agent-browser && agent-browser install')
                print('        Collection needs a real browser. Installing it now means the '
                      'agent never has to stop mid-run to ask.')
        except Exception:
            check('browser installed', False, 'could not determine; install it to be sure')

    try:
        import curses  # noqa: F401
        check('interactive menus available', True)
    except ImportError:
        # Windows ships no curses, and the setup wizard's arrow-key menus need it.
        check('interactive menus available', False,
              'reinstall to pick up windows-curses, or menus fall back to numbers')

    failed = [label for label, ok, _ in checks if not ok]
    print('\n' + ('All checks passed.' if not failed else f'{len(failed)} check(s) failed.'))
    return 0 if not failed else 1


def _status(args) -> int:
    from .platform import outbox as outbox_mod
    from .store import schema

    cfg = _load_config(args)
    conn = schema.connect()
    print(json.dumps({
        'paired': cfg.is_paired,
        'platform': cfg.platform_url,
        'agent_id': cfg.agent_id or None,
        'queue': outbox_mod.status(conn),
    }, indent=2))
    return 0


def _outbox(args) -> int:
    from .platform import outbox as outbox_mod
    from .platform.client import PlatformClient
    from .store import schema

    cfg = _load_config(args)
    conn = schema.connect()
    action = getattr(args, 'action', 'status')

    if action == 'status':
        print(json.dumps(outbox_mod.status(conn), indent=2))
        return 0

    if action == 'failed':
        rows = conn.execute(
            "SELECT id, endpoint, last_status, last_error, attempts FROM outbox "
            "WHERE state = 'failed_permanent' ORDER BY id"
        ).fetchall()
        if not rows:
            print('Nothing has permanently failed.')
            return 0
        for row in rows:
            print(f'  {row["id"]}  {row["endpoint"]}  status={row["last_status"]} '
                  f'attempts={row["attempts"]}  {row["last_error"]}')
        return 0

    if not cfg.is_paired:
        print('Not paired. Run `ettok connect` first.', file=sys.stderr)
        return 1

    reclaimed = outbox_mod.reclaim_in_flight(conn)
    if reclaimed:
        print(f'Recovered {reclaimed} submission(s) interrupted by an earlier shutdown.')
    result = outbox_mod.drain(conn, PlatformClient(cfg))
    print(json.dumps({**result, 'queue': outbox_mod.status(conn)}, indent=2))
    return 0


def _schedule(args) -> int:
    """Run unattended, through the runtime's cron rather than a loop of our own.

    cron survives restarts, reboots and a closed laptop; an in-process timer does
    not. For an agent that is supposed to work on its own machine while nobody is
    watching, that difference is the entire feature.
    """
    from cron import jobs as cron_jobs

    cfg = _load_config(args)
    name = getattr(args, 'name', 'ettok-scan')

    if getattr(args, 'remove', False):
        removed = 0
        for job in cron_jobs.load_jobs():
            if job.get('name') == name:
                cron_jobs.remove_job(job['id'])
                removed += 1
        print(f'Removed {removed} scheduled run(s) named "{name}".')
        return 0

    if not cfg.is_paired:
        print('Not paired. Run `ettok connect` first, or the scheduled run has '
              'nowhere to report to.', file=sys.stderr)
        return 1

    prompt = (
        'Work the current Ettok monitoring case. Load the ettok:working-a-case '
        'skill first and follow it. Sync knowledge, find posts that concern the '
        "case's communities, collect comments together with the posts they reply "
        'to, and scan them. Report what you found, what stopped the run, and '
        'anything an operator should act on.'
    )

    job = cron_jobs.create_job(
        prompt=prompt,
        schedule=getattr(args, 'every', '6h'),
        name=name,
        skills=['ettok:working-a-case'],
        enabled_toolsets=['ettok', 'browser'],
    )
    job_id = job.get('id') if isinstance(job, dict) else job
    print(f'Scheduled "{name}" every {getattr(args, "every", "6h")} (job {job_id}).')
    print('It survives restarts. `hermes cron list` to inspect, '
          f'`ettok schedule --remove --name {name}` to stop.')
    return 0


def _setup(args) -> int:
    from .setup_wizard import run
    return run(args)
