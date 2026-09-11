"""The plugin loads, registers, and refuses to work unpaired.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q

These deliberately live outside the runtime's own `tests/` tree. Nothing this fork
adds should land in a directory upstream also edits, so that `git merge
upstream/main` stays a merge rather than a negotiation.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def hermes_home(monkeypatch):
    """An isolated HERMES_HOME, so discovery is real but state is disposable."""
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(home))
    for var in ('ETTOK_AGENT_KEY', 'ETTOK_AGENT_ID', 'ETTOK_PLATFORM_URL'):
        monkeypatch.delenv(var, raising=False)
    return home


def _enable(home: Path, *names: str) -> None:
    (home / 'config.yaml').write_text(
        yaml.safe_dump({'plugins': {'enabled': list(names)}}), encoding='utf-8',
    )


def _load(home: Path):
    from hermes_cli import plugins as pmod
    manager = pmod.PluginManager()
    manager.discover_and_load()
    return manager


# ---------------------------------------------------------------------------
# Discovery and registration
# ---------------------------------------------------------------------------

def test_discovered_but_not_enabled_by_default(hermes_home):
    """Opt-in on purpose.

    Auto-loading put this plugin's system-prompt section into the runtime's own
    test suite and broke an upstream test asserting an exact prompt. Requiring an
    enable costs one line in the installer; the alternative was carrying a
    conflict on every upstream merge.
    """
    manager = _load(hermes_home)
    assert 'ettok' in manager._plugins
    loaded = manager._plugins['ettok']
    assert loaded.manifest.source == 'bundled'
    assert not loaded.enabled
    assert loaded.error and 'not enabled' in loaded.error


def test_loads_and_registers_its_tools_when_enabled(hermes_home):
    _enable(hermes_home, 'ettok')
    manager = _load(hermes_home)

    loaded = manager._plugins['ettok']
    assert loaded.enabled, f'plugin failed to load: {loaded.error}'

    registered = set(manager._plugin_tool_names)
    for tool in ('ettok_sync_knowledge', 'ettok_submit', 'ettok_case_status',
                 'ettok_match', 'ettok_classify', 'ettok_explain'):
        assert tool in registered, f'{tool} was not registered (have: {sorted(registered)})'


def test_registers_the_operator_cli(hermes_home):
    _enable(hermes_home, 'ettok')
    manager = _load(hermes_home)
    assert 'ettok' in getattr(manager, '_cli_commands', {}), 'the `ettok` command did not register'


# ---------------------------------------------------------------------------
# Refusing to work unpaired
# ---------------------------------------------------------------------------

def test_an_unpaired_agent_says_so_instead_of_crashing(hermes_home):
    """Ettok AI is open source, so an unpaired machine is the normal first state.

    It has to be a readable message rather than a traceback, because it is the
    first thing a new operator will hit.
    """
    from plugins.ettok import config as config_mod
    from plugins.ettok.platform.client import NotPairedError, PlatformClient

    cfg = config_mod.load(None)
    assert not cfg.is_paired

    with pytest.raises(NotPairedError) as exc:
        PlatformClient(cfg)._headers()
    assert 'ettok connect' in str(exc.value)


def test_tools_return_json_errors_rather_than_raising(hermes_home):
    """A handler that raises takes the whole turn down with it."""
    _enable(hermes_home, 'ettok')
    manager = _load(hermes_home)
    assert manager._plugins['ettok'].enabled

    from plugins.ettok import _guard

    @_guard
    def explodes(args, **kwargs):
        raise RuntimeError('boom')

    payload = json.loads(explodes({}))
    assert 'error' in payload
    assert 'boom' in payload['error']


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_storage_lives_outside_the_plugin_directory(hermes_home):
    """A plugin update deletes the install directory.

    State kept there would be destroyed silently, after collection, which is the
    worst possible time to discover the convention.
    """
    from plugins.ettok.store import schema

    conn = schema.connect()
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {'outbox', 'collected_item', 'evidence_artifact', 'seen_item'} <= tables

    data_dir = schema.data_dir()
    assert 'plugin-data' in str(data_dir)
    assert 'plugins' + os.sep + 'ettok' not in str(data_dir)


def test_no_table_holds_platform_knowledge(hermes_home):
    """Structural, not a convention.

    The lexicon, tropes, exemptions and rubric belong to the platform and are
    fetched per run. Hardcoding any of them would require adding storage that
    deliberately does not exist, so the omission is enforced rather than trusted.
    """
    from plugins.ettok.store import schema

    conn = schema.connect()
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for forbidden in ('lexicon', 'term', 'trope', 'exemption', 'rubric'):
        assert not any(forbidden in name for name in tables), \
            f'a table matching "{forbidden}" exists; knowledge must not be persisted'
