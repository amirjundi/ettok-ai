"""Onboarding: asking when someone is there, deciding when nobody is.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import io
import sys

import pytest

from plugins.ettok import setup_wizard as wizard


class FakeStdin:
    def __init__(self, tty: bool, lines=()):
        self._tty = tty
        self._lines = list(lines)

    def isatty(self):
        return self._tty

    def readline(self):
        return self._lines.pop(0) if self._lines else ''


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr(sys, 'stdin', FakeStdin(tty=False))


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(sys, 'stdin', FakeStdin(tty=True))


# ---------------------------------------------------------------------------
# Not hanging when nobody is watching
# ---------------------------------------------------------------------------

def test_a_blocking_step_never_starts_unattended(headless):
    """Pairing waits on an administrator somewhere else.

    Started from a provisioning script it would hang for ten minutes while the
    person who could approve it has no idea they were asked. That reads as a
    broken install, which is the opposite of what onboarding is for.
    """
    assert wizard._confirm('Request access now?', blocking=True) is False


def test_a_harmless_step_still_takes_its_default_unattended(headless):
    """Only the steps that wait on a human are suppressed."""
    assert wizard._confirm('Continue?', default=True) is True
    assert wizard._confirm('Do the risky thing?', default=False) is False


def test_prompts_take_their_default_rather_than_blocking(headless):
    assert wizard._ask('Platform URL', 'http://localhost:8000') == 'http://localhost:8000'


def test_a_terminal_is_recognised(terminal):
    assert wizard._interactive() is True


def test_a_pipe_is_not(headless):
    assert wizard._interactive() is False


def test_an_interrupted_prompt_falls_back_to_the_default(terminal, monkeypatch):
    """Ctrl-C during setup should not leave a traceback on someone's first run."""
    def interrupted(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr('builtins.input', interrupted)
    assert wizard._ask('Platform URL', 'http://localhost:8000') == 'http://localhost:8000'


# ---------------------------------------------------------------------------
# Telling an operator what the agent cannot yet detect
# ---------------------------------------------------------------------------

class FakeKnowledge:
    def __init__(self, terms=None, tropes=None, cases=None, markers=None):
        self.terms = terms if terms is not None else []
        self.tropes = tropes if tropes is not None else []
        self.cases = cases if cases is not None else []
        self._markers = markers or {}

    def group_markers(self):
        return self._markers


def test_a_trope_with_no_surface_forms_is_reported_as_inert():
    """It cannot fire at all, however complete it looks in the admin."""
    know = FakeKnowledge(tropes=[{'surface_forms': [], 'requires_target_group': True,
                                  'activation_topics': ['yazidi']}])
    gaps = ' '.join(wizard._knowledge_gaps(know))
    assert 'no surface forms' in gaps and 'cannot fire' in gaps


def test_an_ungated_trope_is_reported():
    know = FakeKnowledge(tropes=[{'surface_forms': ['x'], 'requires_target_group': True,
                                  'activation_topics': []}])
    assert 'no activation gate' in ' '.join(wizard._knowledge_gaps(know))


def test_terms_without_exemptions_are_reported_with_who_gets_hurt():
    """The consequence is the point: an operator reading "no exemptions" learns
    nothing, and one reading "journalists would be flagged" acts."""
    know = FakeKnowledge(terms=[{'never_flag_when': []}])
    gaps = ' '.join(wizard._knowledge_gaps(know))
    assert 'journalists and survivors' in gaps


def test_a_group_with_no_topic_markers_is_named():
    """Naming it matters -- "some groups" is not something a curator can act on."""
    know = FakeKnowledge(
        cases=[{'target_groups': [{'slug': 'shabak'}, {'slug': 'yazidi'}]}],
        markers={'yazidi': ['سنجار']},
    )
    gaps = ' '.join(wizard._knowledge_gaps(know))
    assert 'shabak' in gaps and 'yazidi' not in gaps.split('markers')[-1]


def test_no_open_case_is_reported_as_a_gap_not_an_error():
    assert any('no case is open' in g for g in wizard._knowledge_gaps(FakeKnowledge()))


def test_complete_knowledge_reports_nothing():
    know = FakeKnowledge(
        terms=[{'never_flag_when': ['news_quotation']}],
        tropes=[{'surface_forms': ['x'], 'requires_target_group': True,
                 'activation_topics': ['yazidi']}],
        cases=[{'target_groups': [{'slug': 'yazidi'}]}],
        markers={'yazidi': ['سنجار']},
    )
    assert wizard._knowledge_gaps(know) == []


# ---------------------------------------------------------------------------
# The tool list the agent carries
# ---------------------------------------------------------------------------

def test_the_monitoring_set_keeps_what_the_agent_actually_uses():
    """Trimming must not remove a tool the agent needs mid-run.

    Cheaper is not the goal; cheaper while still able to do the job is.
    """
    from plugins.ettok.setup_wizard import MONITORING_TOOLSETS

    for needed in ('ettok', 'browser', 'skills', 'web'):
        assert needed in MONITORING_TOOLSETS, f'{needed} would be dropped'


def test_the_monitoring_set_drops_what_it_never_uses():
    """Each toolset's schemas are re-read by the model on every single turn, so
    an agent that will never open Spotify still pays for knowing how."""
    from plugins.ettok.setup_wizard import MONITORING_TOOLSETS

    for irrelevant in ('spotify', 'kanban', 'desktop_ui', 'video_gen',
                       'homeassistant', 'discord', 'image_gen'):
        assert irrelevant not in MONITORING_TOOLSETS


def _tool_names(defs) -> set:
    """Definitions come flat or wrapped in a function envelope, depending on shape."""
    names = set()
    for d in defs:
        name = d.get('name') or (d.get('function') or {}).get('name')
        if name:
            names.add(name)
    return names


@pytest.fixture
def plugin_loaded(tmp_path, monkeypatch):
    """Load the plugin the way a session does, before toolsets are resolved.

    This ordering is the point of the fixture, not incidental setup. The `ettok`
    toolset does not exist until register(ctx) runs, so resolving toolsets first
    logs "Unknown toolset: ettok" and silently drops every one of the agent's own
    tools -- an agent that starts up fine and can do nothing.
    """
    import yaml

    from plugins.ettok.setup_wizard import MONITORING_TOOLSETS

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'plugins': {'enabled': ['ettok']},
        'toolsets': list(MONITORING_TOOLSETS),
        # Without this the runtime defers plugin tools behind tool_search, and
        # the agent's own ten become invisible to it. That is what these tests
        # exist to catch.
        'tools': {'tool_search': {'enabled': 'off'}},
    }), encoding='utf-8')

    import importlib
    import tools.tool_search as ts
    import model_tools
    importlib.reload(ts)
    importlib.reload(model_tools)

    from hermes_cli import plugins as pmod
    manager = pmod.PluginManager()
    manager.discover_and_load()
    assert manager._plugins['ettok'].enabled
    return manager


def test_trimming_measurably_shrinks_what_is_sent(plugin_loaded):
    """The claim is a material reduction. Assert it rather than trust it.

    Measured through the call the agent actually makes -- with an explicit
    toolset list, not the no-argument form, which means "everything" and would
    show no difference at all. Getting that wrong once made a working change look
    like it did nothing.
    """
    import json

    import model_tools
    from plugins.ettok.setup_wizard import MONITORING_TOOLSETS

    everything = json.dumps(model_tools.get_tool_definitions())
    monitoring = json.dumps(
        model_tools.get_tool_definitions(enabled_toolsets=MONITORING_TOOLSETS))

    assert len(monitoring) < len(everything), 'trimming did not reduce the payload'
    reduction = 1 - (len(monitoring) / len(everything))
    assert reduction > 0.2, f'only {reduction:.0%} smaller; expected a material cut'


def test_the_ettok_tools_survive_trimming(plugin_loaded):
    """The failure this guards against is silent and total.

    If the `ettok` toolset is not recognised, the filter drops all ten of the
    agent's tools and it starts up looking healthy with nothing to do.
    """
    import model_tools
    from plugins.ettok.setup_wizard import MONITORING_TOOLSETS

    names = _tool_names(
        model_tools.get_tool_definitions(enabled_toolsets=MONITORING_TOOLSETS))
    for tool in ('ettok_scan', 'ettok_collect', 'ettok_sync_knowledge', 'ettok_submit'):
        assert tool in names, f'{tool} was trimmed away'


def test_the_browser_survives_trimming(plugin_loaded):
    """Collection is the whole job; trimming must not remove the browser."""
    import model_tools
    from plugins.ettok.setup_wizard import MONITORING_TOOLSETS

    names = _tool_names(
        model_tools.get_tool_definitions(enabled_toolsets=MONITORING_TOOLSETS))
    assert any(n.startswith('browser_') for n in names)


def test_wizard_pins_the_browser_backend(monkeypatch):
    """Browser Use CLI mode is the runtime default and breaks collection silently.

    With `browser.backend` unset and the CLI runnable, the whole browser_* surface
    is replaced by one browser_exec. The collector drives browser_navigate,
    browser_snapshot, browser_vision and browser_console and would find none of
    them -- the run completes, reports no error, and collects nothing. Camoufox,
    which this agent needs for fingerprint resistance, is in the built-in stack too.
    """
    from plugins.ettok import setup_wizard

    saved = {}
    monkeypatch.setattr(setup_wizard, '_interactive', lambda: False, raising=False)

    class FakeConfig:
        @staticmethod
        def load_config():
            return {}

        @staticmethod
        def save_config(cfg):
            saved.update(cfg)

    # setattr on the package, not setitem on sys.modules: `_apply_toolsets` does
    # `from hermes_cli import config`, which reads the package attribute. Once any
    # earlier test has imported the real module that attribute is already bound,
    # and a sys.modules swap silently does nothing -- the test then passes alone
    # and fails in the suite.
    import hermes_cli
    monkeypatch.setattr(hermes_cli, 'config', FakeConfig)
    assert setup_wizard._apply_toolsets() is True
    assert saved['browser']['backend'] == 'off'


def test_wizard_leaves_an_explicit_backend_alone(monkeypatch):
    """An operator who chose a backend has made a decision; the wizard defers."""
    from plugins.ettok import setup_wizard

    saved = {}

    class FakeConfig:
        @staticmethod
        def load_config():
            return {'browser': {'backend': 'browser_use'}}

        @staticmethod
        def save_config(cfg):
            saved.update(cfg)

    # setattr on the package, not setitem on sys.modules: `_apply_toolsets` does
    # `from hermes_cli import config`, which reads the package attribute. Once any
    # earlier test has imported the real module that attribute is already bound,
    # and a sys.modules swap silently does nothing -- the test then passes alone
    # and fails in the suite.
    import hermes_cli
    monkeypatch.setattr(hermes_cli, 'config', FakeConfig)
    assert setup_wizard._apply_toolsets() is True
    assert saved['browser']['backend'] == 'browser_use'
