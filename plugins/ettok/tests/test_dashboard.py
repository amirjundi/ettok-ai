"""The dashboard bundle is JavaScript, so this is mostly a delegation.

Its two parsers -- the markdown renderer and the SSE frame reader -- have real
logic and a real edge each: one builds HTML from model output, the other decides
whether a tool call is visible. Both are checked under Node rather than trusted.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
BUNDLE = HERE.parent / 'dashboard' / 'dist' / 'index.js'


def test_bundle_exists_and_registers_the_tab():
    source = BUNDLE.read_text(encoding='utf-8')
    assert '__HERMES_PLUGINS__.register("ettok"' in source
    # The chat view lives in this bundle because a dashboard plugin gets exactly
    # one tab, and the host's own Chat tab is a PTY terminal we do not control.
    assert 'function Chat(' in source


@pytest.mark.skipif(shutil.which('node') is None, reason='Node not installed')
def test_bundle_parsers():
    result = subprocess.run(
        [shutil.which('node'), str(HERE / 'bundle_check.js')],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
