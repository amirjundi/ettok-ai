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
