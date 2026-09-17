"""Move the agent's state directory to its own name, without breaking anything.

The directory is called `hermes` after the upstream project this was forked
from. An operator handed software called Ettok finds another product's name in
their home folder, and on Windows it sits in `%LOCALAPPDATA%\\hermes` where it
is the first thing they see.

Renaming it in the code is not an option: about a hundred places build the
legacy path themselves rather than asking the resolver, including sandbox rules
in `agent/file_safety.py`. So the move happens on disk, once, deliberately, and
leaves a link behind at the old location so every one of those places keeps
resolving.

The rules this follows, in order of how much they matter:

1. Nothing is destroyed. The directory is moved, never copied-then-deleted, so
   there is no window where two divergent copies exist.
2. No half-states. If the compatibility link cannot be created, the move is
   undone and the command fails. A machine left with the directory renamed and
   nothing at the old path is a machine where a hardcoded path silently reads an
   empty directory -- which is worse than never having run this.
3. It refuses while anything is running. Moving a directory out from under a
   live gateway or an open SQLite database corrupts both.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


def paths() -> Tuple[Path, Path]:
    """(current, target) for this platform, whether or not either exists."""
    from hermes_constants import HOME_DIR_NAME, LEGACY_HOME_DIR_NAME

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / LEGACY_HOME_DIR_NAME, base / HOME_DIR_NAME

    home = Path.home()
    return home / f'.{LEGACY_HOME_DIR_NAME}', home / f'.{HOME_DIR_NAME}'


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows directory junction.

    `is_symlink()` returns False for a junction, so a second run would read the
    junction this command created as a real directory and refuse to proceed for
    the wrong reason.
    """
    if path.is_symlink():
        return True
    try:
        return bool(os.readlink(str(path)))
    except (OSError, ValueError):
        return False


def _link(target: Path, at: Path) -> Optional[str]:
    """Point `at` to `target`. Returns an error string, or None on success.

    A junction on Windows rather than a symlink: a symlink needs either
    administrator rights or developer mode, and requiring an elevated prompt for
    a cosmetic rename is how a migration gets run half way and abandoned. A
    junction needs neither and resolves identically for every path in this
    codebase.
    """
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(at), str(target)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return (result.stderr or result.stdout or 'mklink failed').strip()
            return None
        at.symlink_to(target, target_is_directory=True)
        return None
    except Exception as exc:                          # noqa: BLE001
        return str(exc)


def _running() -> list:
    """Processes that would be holding the directory open."""
    try:
        import psutil
    except Exception:                                 # noqa: BLE001
        # Without psutil this cannot be checked, and a migration that guesses
        # "probably nothing is running" is how a database gets corrupted.
        return ['unknown']

    live = []
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = ' '.join(process.info.get('cmdline') or [])
        except Exception:                             # noqa: BLE001
            continue
        if 'ettok' in cmdline.lower() and process.pid != os.getpid():
            live.append(f"{process.info.get('name') or 'process'} ({process.pid})")
    return live


def status() -> dict:
    """What a migration would do, without doing it."""
    current, target = paths()
    return {
        'current': str(current),
        'target': str(target),
        'current_exists': current.exists(),
        'target_exists': target.exists(),
        'already_migrated': target.is_dir() and not _is_link(target),
        'compat_link_present': current.exists() and _is_link(current),
    }


def migrate(*, force: bool = False) -> Tuple[bool, str]:
    """Do it. Returns (ok, message)."""
    current, target = paths()

    target_is_real = target.is_dir() and not _is_link(target)
    legacy_is_real = current.is_dir() and not _is_link(current)

    # Both real directories is the dangerous case and has to be caught before
    # anything else. Reporting "already done" here would leave the machine's
    # actual state where it is while the resolver, which prefers the new name,
    # starts reading a different and probably empty directory -- a silent switch
    # to the wrong state is worse than any failure this command can report.
    if target_is_real and legacy_is_real:
        return False, (f'{target} already exists and is not a migration of '
                       f'{current}. Both hold state. Move or remove one by hand; '
                       f'this command will not merge two state directories.')

    if target_is_real:
        return True, f'Already done. State lives in {target}.'

    if not current.exists():
        return True, (f'Nothing to move: {current} does not exist, so this machine '
                      f'already starts fresh in {target}.')

    if _is_link(current):
        return False, (f'{current} is already a link. Something has migrated this '
                       f'machine before, or set it up by hand. Nothing was changed.')

    if target.exists():
        return False, (f'{target} already exists. Move or remove it first; this '
                       f'command will not merge two state directories.')

    live = _running()
    if live and not force:
        names = 'an unknown process (psutil is not installed)' if live == ['unknown'] \
            else ', '.join(live)
        return False, (f'Refusing while {names} is running. Moving the directory out '
                       f'from under a live gateway or an open database corrupts both. '
                       f'Stop it and run this again.')

    try:
        shutil.move(str(current), str(target))
    except Exception as exc:                          # noqa: BLE001
        return False, f'Could not move {current}: {exc}. Nothing was changed.'

    error = _link(target, current)
    if error:
        # Rule 2: no half-states. About a hundred paths in this codebase still
        # name the old directory, and leaving them pointing at nothing is worse
        # than not having run this at all.
        try:
            shutil.move(str(target), str(current))
        except Exception as move_back:                # noqa: BLE001
            return False, (
                f'Moved the directory to {target} but could not create the '
                f'compatibility link ({error}), and could not move it back '
                f'({move_back}). Move {target} back to {current} by hand before '
                f'starting the agent.'
            )
        return False, (f'Could not create the compatibility link at {current} '
                       f'({error}), so the move was undone. Nothing changed.')

    return True, (f'State now lives in {target}. {current} is a link to it, so any '
                  f'path that still names the old directory keeps working.')
