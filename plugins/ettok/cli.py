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


# Each command's arguments, declared once. The same table builds the `ettok
# ettok <command>` group and the top-level `ettok <command>` shortcuts, so the
# two can never drift.
def _connect_args(p) -> None:
    p.add_argument('--platform', help='Platform base URL, e.g. https://ettok.example')
    p.add_argument('--name', help='Name the administrator will see when approving')
    p.add_argument('--timeout', type=float, default=600.0, help='Seconds to wait for approval')


def _setup_args(p) -> None:
    p.add_argument('--platform', help='Platform base URL')
    p.add_argument('--name', help='Name the administrator will see when approving')


def _selectors_args(p) -> None:
    p.add_argument('page', help='A post page saved from the browser (.html)')
    p.add_argument('--platform', default='facebook')
    p.add_argument('--url', default='',
                   help='The URL the page came from, if the file does not carry it')
    p.add_argument('--try', dest='candidate', default='',
                   help='A JSON object of selectors to try instead of the defaults')


def _accounts_args(p) -> None:
    p.add_argument('--release', metavar='ACCOUNT_ID',
                   help='Return a quarantined account to rotation')
    p.add_argument('--note', default='',
                   help='Why it is safe to resume, recorded with the release')


def _outbox_args(p) -> None:
    p.add_argument('action', nargs='?', default='status', choices=['status', 'drain', 'failed'])


def _schedule_args(p) -> None:
    p.add_argument('--every', default='6h',
                   help='Interval, e.g. 30m, 6h, or a 5-field cron expression')
    p.add_argument('--name', default='ettok-scan')
    p.add_argument('--remove', action='store_true', help='Remove the scheduled run')


def _no_args(p) -> None:
    pass


# name -> (help, argument builder, whether the runtime already owns that name).
#
# `setup`, `doctor` and `status` are upstream's own top-level commands and mean
# something different there -- the agent runtime rather than the monitoring it
# does -- so those three stay inside the group and the rest also get a shortcut.
COMMANDS = {
    'connect':   ('Pair this machine with an Ettok platform', _connect_args, False),
    'setup':     ('Set this machine up: platform, pairing, model, schedule', _setup_args, True),
    'doctor':    ('Check everything this agent needs in order to work', _no_args, True),
    'eval':      ('Run the gold set against the live lexicon', _no_args, False),
    'selectors': ('Check the extraction selectors against a saved page', _selectors_args, False),
    'status':    ('Show pairing, open cases and the delivery queue', _no_args, True),
    'accounts':  ('Account health, and lifting a quarantine', _accounts_args, False),
    'outbox':    ('Inspect or drain the delivery queue', _outbox_args, False),
    'schedule':  ('Run unattended on a recurring schedule', _schedule_args, False),
}

TOP_LEVEL = [name for name, (_h, _a, taken) in COMMANDS.items() if not taken]


def register_cli(subparser) -> None:
    """Build the `ettok ettok <command>` tree."""
    commands = subparser.add_subparsers(dest='ettok_command')
    for name, (help_text, add_args, _taken) in COMMANDS.items():
        add_args(commands.add_parser(name, help=help_text))
    subparser.set_defaults(func=handle_cli)


def make_top_level(name: str):
    """argparse setup for `ettok <name>`, registered as its own subcommand.

    ``ettok ettok scan`` is an artefact of how plugins register commands: the
    runtime gives a plugin one top-level name, ours is `ettok`, and the product
    is also called Ettok. Registering each command in its own right removes the
    stutter without breaking the group, which still works.
    """
    help_text, add_args, _taken = COMMANDS[name]

    def setup(parser) -> None:
        add_args(parser)
        parser.set_defaults(func=handle_cli, ettok_command=name)

    return help_text, setup


def handle_cli(args) -> int:
    command = getattr(args, 'ettok_command', None)
    handlers = {
        'setup': _setup,
        'connect': _connect,
        'doctor': _doctor,
        'eval': _eval,
        'selectors': _selectors,
        'status': _status,
        'accounts': _accounts,
        'outbox': _outbox,
        'schedule': _schedule,
    }
    handler = handlers.get(command)
    if handler is None:
        # No subcommand is how a new operator arrives here. Point at setup
        # rather than printing a list they have no basis for choosing from.
        print('Usage: ettok ettok {' + '|'.join(COMMANDS) + '}')
        print()
        print('Most of these also work on their own: ' + ', '.join(
            'ettok ' + name for name in TOP_LEVEL))
        print()
        print('New here? Run:  ettok ettok setup')
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

    `.env` under the Ettok home, alongside every other credential this runtime
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

    pairing.write_credentials(result, _env_path(), cfg.platform_url)
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

        # A browser that exists but whose tools the agent cannot call is the
        # worst of both worlds, and it is the runtime's DEFAULT. With
        # `browser.backend` unset and the Browser Use CLI runnable, the whole
        # browser_* surface is replaced by a single browser_exec -- so the
        # collector's navigate/snapshot/vision/console calls all resolve to
        # nothing, the run completes, and it reports no error at all.
        try:
            from tools.browser_use_cli import is_browser_use_cli_mode
            if is_browser_use_cli_mode():
                check('collector can reach the browser', False,
                      'Browser Use CLI mode has replaced the browser_* tools')
                print('        Set `browser.backend: off` in config.yaml (or re-run '
                      '`ettok setup`). Collection drives browser_navigate, '
                      'browser_snapshot, browser_vision and browser_console, and '
                      'Camoufox lives in that same built-in stack.')
            else:
                check('collector can reach the browser', True)
        except ImportError:
            # No Browser Use module at all: the built-in stack is the only one.
            check('collector can reach the browser', True)
        except Exception as exc:
            check('collector can reach the browser', False, str(exc))

    # Whether a sign-in will survive a restart.
    #
    # Camofox gives each task a random userId unless managed persistence is on,
    # so every task gets its own empty browser profile. An operator signs in
    # inside one profile and the collector reads the page in another, sees the
    # logged-out view, and reports the public comment count -- ten where there
    # were twenty-five. It reports a number, so it reads as a result rather than
    # a failure, which is the worst shape a bug can take in this product.
    try:
        import os

        from hermes_cli import config as hermes_config
        browser_cfg = (hermes_config.load_config() or {}).get('browser', {}) or {}
        camofox_cfg = browser_cfg.get('camofox') or {}
        pinned = bool(camofox_cfg.get('managed_persistence')
                      or camofox_cfg.get('user_id')
                      or os.environ.get('CAMOFOX_USER_ID'))
        if pinned:
            check('browser keeps its sign-in', True)
        else:
            check('browser keeps its sign-in', False,
                  'every task gets a new random browser profile')
            print("        A sign-in is saved into one task's profile and the next "
                  "task looks in a different one, so collection runs logged out and "
                  "reports the public comment count as if it were the whole thread.")
            print('        Fix: set browser.camofox.managed_persistence: true in '
                  'config.yaml, or re-run `ettok ettok setup`.')
    except Exception as exc:                          # noqa: BLE001
        check('browser keeps its sign-in', False, str(exc))

    # The dashboard's chat tab. Three states that look identical from the
    # browser -- not configured, configured but not running, running but
    # refusing the key -- and the tab reports all three as "the gateway is not
    # running", which sent an operator to run the right command twice on a
    # machine where it could not have worked.
    try:
        from hermes_cli import config as hermes_config
        platforms = ((hermes_config.load_config() or {})
                     .get('gateway', {}) or {}).get('platforms', {}) or {}
        api = platforms.get('api_server') or {}
        if not api.get('enabled'):
            check('dashboard chat configured', False,
                  'no api_server platform in gateway.platforms')
            print('        The chat tab has nothing to talk to. `ettok setup` '
                  'configures it; everything else works without it.')
        else:
            port = api.get('port', 8642)
            check('dashboard chat configured', True, f'api_server on port {port}')
            import httpx
            try:
                from agent.secret_scope import get_secret
                key = (get_secret('API_SERVER_KEY', '') or '').strip()
            except Exception:                         # noqa: BLE001
                key = ''

            # Enabled with no key is not a degraded chat -- it is a dead
            # gateway. The api_server platform treats a missing key as a
            # non-retryable startup conflict, so the whole gateway exits and
            # takes the cron scheduler and every messaging platform with it.
            # Worth its own check because the symptom (nothing runs) looks
            # nothing like the cause (one missing line in .env).
            if len(key) < 16:
                check('dashboard chat key', False,
                      'api_server is enabled but API_SERVER_KEY is missing or too short')
                print('        The gateway will refuse to start at all in this state, '
                      'not just the chat. Run `ettok ettok setup` to write one, or '
                      'disable api_server in config.yaml to get the gateway back.')
            else:
                check('dashboard chat key', True)
            headers = {'Authorization': f'Bearer {key}'} if key else {}
            host = api.get('host', '127.0.0.1')
            try:
                with httpx.Client(timeout=3.0) as http:
                    resp = http.get(f'http://{host}:{port}/v1/models', headers=headers)
                if resp.status_code == 401:
                    check('dashboard chat reachable', False,
                          'the gateway refused API_SERVER_KEY')
                    print('        The key in .env does not match the one the '
                          'gateway is using. Restart the gateway after changing it.')
                else:
                    check('dashboard chat reachable', resp.status_code < 400,
                          f'HTTP {resp.status_code}')
            except Exception:                         # noqa: BLE001
                check('dashboard chat reachable', False, 'nothing listening')
                print('        Start it with `ettok gateway run`, or '
                      '`ettok gateway install` so it comes back after a reboot.')
    except Exception as exc:                          # noqa: BLE001
        check('dashboard chat configured', False, str(exc))

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


def _eval(args) -> int:
    """Score the live lexicon against the gold set.

    `doctor` answers "can this agent run". This answers "would it be any good if
    it did", which is a different question and the one that decides whether a
    scan is worth starting. It fetches the real knowledge, so it measures what a
    curator actually left in the database rather than a snapshot taken some time
    ago.

    Every case is a sentence from the focus group transcript or the survey, or a
    false positive the vocabulary would produce. The failures are the output that
    matters: each one names the sentence, what detection did, and why the case is
    in the set at all.
    """
    from .detect import goldset

    cfg = _load_config(args)
    if not cfg.is_paired:
        print('Not paired with a platform yet. Run:  ettok setup')
        return 1

    client = PlatformClient(cfg)
    try:
        know = knowledge_mod.fetch(client)
    except Exception as exc:                                      # noqa: BLE001
        print(f'Could not fetch the lexicon: {exc}')
        return 1

    markers = know.group_markers()
    gated = sum(1 for values in markers.values() if values)
    inert = [
        t.get('name', '?') for t in know.tropes
        if not (t.get('surface_forms') or []) and not t.get('is_visual')
    ]
    bare = [t['term'] for t in know.terms if not t.get('never_flag_when')]

    report = goldset.run(know)
    print(f'{len(know.terms)} terms, {len(know.tropes)} tropes, '
          f'{gated} communities with a topic gate')
    print(goldset.describe(report))

    # Curation debt, reported whether or not the gold set passed: the gold set
    # only covers the communities it has sentences for, and silence about the
    # others would read as a clean bill of health.
    if inert:
        print(f'\n{len(inert)} active trope(s) cannot fire -- no surface forms:')
        for name in inert:
            print(f'  - {name}')
    if bare:
        print(f'\n{len(bare)} term(s) carry no exemptions, so nothing '
              f'downstream is told what would make a match legitimate:')
        for term in bare[:10]:
            print(f'  - {term}')

    return 0 if not report['failures'] else 1


def _selectors(args) -> int:
    """Check the extraction selectors against a page saved from a browser.

    The one unknown no amount of testing here can close is whether the selectors
    match the real site, because collection has never run against it. This is the
    cheapest way to find out: it needs no account and no login, only a saved
    page, and it runs the collector's own extraction JavaScript so what it
    reports is what a scan would get.

        ettok selectors ~/Downloads/post.html

    Save the page after the comments have loaded -- scroll to them first and
    expand "view more comments", because a page saved before they render has
    nothing in it to find and the result would blame the selectors.
    """
    import json as _json

    from .collect import selector_check

    candidate = None
    if getattr(args, 'candidate', ''):
        try:
            candidate = _json.loads(args.candidate)
        except ValueError as exc:
            print(f'--try needs a JSON object of selectors: {exc}')
            return 1

    try:
        report = selector_check.check(
            args.page, platform=args.platform, selectors=candidate, url=args.url,
        )
    except selector_check.CheckError as exc:
        print(f'Could not run the check: {exc}')
        return 1

    print(selector_check.describe(report))

    # Non-zero when the page yielded nothing, so this can gate a deploy.
    return 0 if report['comments'] else 1


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


def _accounts(args) -> int:
    """Account health, and the operator's override on a quarantine.

    The agent quarantines an account for 24 hours when a page comes back as a
    CAPTCHA, a checkpoint or an "unusual activity" notice. That is right: the
    challenge is the platform saying it has noticed, and carrying on regardless
    is how a recoverable restriction becomes a ban.

    What was missing is the way back. An operator who has signed in themselves,
    cleared the challenge as the human being tested, and watched the account
    behave knows something this agent cannot see from a page of HTML -- and had
    no way to say so. Waiting out a cooldown that no longer describes reality is
    not safety, it is the tool refusing to be told.

    This does not let the agent clear a challenge. It lets a person who has
    dealt with one say the account is fine again.
    """
    from .collect import session as session_mod
    from .store import schema

    conn = schema.connect()

    if getattr(args, 'release', None):
        account_id = args.release
        if session_mod.release(conn, account_id, note=getattr(args, 'note', '')):
            print(f'Released "{account_id}" -- it will be used again on the next run.')
            print('Recorded as an operator release, so it stays distinguishable '
                  'from a run that simply succeeded.')
            return 0
        print(f'No account called "{account_id}" is on record.')
        print('Run `ettok ettok accounts` to see the ones that are.')
        return 1

    rows = session_mod.account_health(conn)
    if not rows:
        print('No account health recorded yet. Nothing has been collected with.')
        return 0

    print(f'{"account":24} {"state":12} {"until":22} reason')
    for row in rows:
        state = row['effective_state']
        stored = row['state']
        shown = state if state == stored else f'{state} (was {stored})'
        until = (row['cooldown_until'] or '')[:19]
        print(f'{row["account_id"][:24]:24} {shown:12} {until:22} {row["block_reason"] or ""}')

    quarantined = [r for r in rows if r['effective_state'] != session_mod.HEALTHY]
    if quarantined:
        print()
        print('An account is quarantined after a challenge. If you have signed in')
        print('yourself and cleared it, release it rather than waiting out the')
        print('cooldown:')
        print(f'    ettok ettok accounts --release {quarantined[0]["account_id"]}')
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
    print('It survives restarts. `ettok cron list` to inspect, '
          f'`ettok schedule --remove --name {name}` to stop.')
    return 0


def _setup(args) -> int:
    from .setup_wizard import run
    return run(args)
