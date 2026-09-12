"""The dashboard is two plugins, and the split is the thing worth asserting.

A dashboard plugin gets exactly one tab. `ettok` uses its tab for the monitoring
panels; `ettok-chat` uses its own to override `/chat`, which is how the host's
xterm terminal page is replaced rather than merely sat next to.

The chat bundle's two parsers -- the markdown renderer and the SSE frame reader
-- have real logic and a real edge each: one builds HTML from model output, the
other decides whether a tool call is visible at all. Both are checked under Node
rather than trusted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
PLUGINS = HERE.parent.parent
PANEL = PLUGINS / 'ettok' / 'dashboard'
CHAT = PLUGINS / 'ettok-chat' / 'dashboard'


def test_panel_owns_its_own_tab():
    manifest = json.loads((PANEL / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['tab']['path'] == '/ettok'
    assert 'override' not in manifest['tab']
    source = (PANEL / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert '__HERMES_PLUGINS__.register("ettok"' in source


def test_chat_replaces_the_built_in_terminal():
    manifest = json.loads((CHAT / 'manifest.json').read_text(encoding='utf-8'))
    # Without this the host mounts its PTY terminal page and ours sits beside it.
    assert manifest['tab']['override'] == '/chat'
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert '__HERMES_PLUGINS__.register("ettok-chat"' in source
    # It has no backend of its own: the proxy lives with the agent's other local
    # state, in the ettok plugin.
    assert 'api' not in manifest
    assert '"/api/plugins/ettok"' in source


@pytest.mark.skipif(shutil.which('node') is None, reason='Node not installed')
def test_chat_bundle_parsers():
    result = subprocess.run(
        [shutil.which('node'), str(HERE / 'bundle_check.js')],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
