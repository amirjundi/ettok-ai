"""The dashboard reaper must recognise an Ettok dashboard on Windows.

This is here rather than upstream because both halves of the bug are ours: the
console script is named `ettok`, and the agent is specified to run on an operator
PC, which on this project means Windows.

The failure mode is silent by construction. `_DASHBOARD_PATTERNS` is substring
matched against process cmdlines, and a pattern that matches nothing makes the
reaper report no processes -- so `ettok update` finishes cleanly while leaving the
old process running, and the next thing the operator sees is a stale-code 503
from a page that worked yesterday.
"""

from __future__ import annotations

from hermes_cli.dashboard_procs import _DASHBOARD_PATTERNS


def matches(cmdline: str) -> bool:
    return any(p in cmdline for p in _DASHBOARD_PATTERNS)


def test_windows_console_script():
    # What Windows actually reports: a quoted .exe shim, because the default
    # install path has a space in it.
    assert matches(
        r'"C:\Users\a\AppData\Roaming\uv\python\python.exe" '
        r'"C:\xampp\htdocs\HSMA v1\ettok-ai\.venv\Scripts\ettok.exe" '
        r'dashboard --skip-build --port 8123'
    )
    # And unquoted, for a path without spaces.
    assert matches(r'C:\venv\Scripts\ettok.exe dashboard --port 8123')


def test_posix_console_script():
    assert matches('/home/a/.venv/bin/ettok dashboard --port 8123')
    assert matches('/home/a/.venv/bin/ettok serve --host 127.0.0.1')


def test_upstream_names_still_match():
    # A partially-migrated install still has to be reaped.
    assert matches('ettok dashboard --port 9119')
    assert matches('python -m hermes_cli.main dashboard --port 9119')


def test_unrelated_processes_are_left_alone():
    assert not matches('python -m pytest plugins/ettok/tests')
    assert not matches('node /usr/bin/ettok-something-else')
