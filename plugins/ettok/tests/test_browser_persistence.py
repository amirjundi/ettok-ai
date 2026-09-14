# -*- coding: utf-8 -*-
"""A sign-in has to survive a restart.

Camofox resolves a userId per task, and with managed persistence off it falls
through to `hermes_<random>` -- a new identity, and a new empty browser profile,
for every task and every restart. For a general assistant that is a sensible
default: a throwaway profile leaks nothing between unrelated jobs.

For this agent it quietly breaks the product. Monitoring means signing into an
account once and using it for weeks. With a random identity the operator signs
in inside one task's profile and the collector reads the page in another, sees
the logged-out view, and reports ten public comments where the thread had
twenty-five. It reports a number, so it reads as a result rather than a failure.
"""
from unittest import mock

from plugins.ettok import setup_wizard


def _applied(stored):
    """Run the wizard's config pass over `stored` and return what it saved."""
    with mock.patch('hermes_cli.config.load_config', return_value=stored), \
         mock.patch('hermes_cli.config.save_config') as save:
        assert setup_wizard._apply_toolsets() is True
    return save.call_args[0][0]


def test_a_fresh_install_pins_the_browser_identity():
    written = _applied({})

    camofox = written['browser']['camofox']
    assert camofox['managed_persistence'] is True


def test_an_operator_who_turned_it_off_keeps_it_off():
    """An explicit false is a decision. The wizard fills blanks; it does not
    overrule somebody who has already chosen."""
    written = _applied({'browser': {'camofox': {'managed_persistence': False}}})

    assert written['browser']['camofox']['managed_persistence'] is False


def test_a_chosen_user_id_is_left_alone():
    written = _applied({'browser': {'camofox': {'user_id': 'ettok-field-01'}}})

    assert written['browser']['camofox']['user_id'] == 'ettok-field-01'
    # Still switched on, because the two work together rather than against.
    assert written['browser']['camofox']['managed_persistence'] is True


def test_the_backend_setting_is_untouched_by_this():
    """Both live under `browser` and are set in the same pass; neither may eat
    the other."""
    written = _applied({'browser': {'backend': 'browser-use'}})

    assert written['browser']['backend'] == 'browser-use'
    assert written['browser']['camofox']['managed_persistence'] is True


def test_the_runtime_reads_the_flag_the_wizard_writes():
    """The wizard and the runtime must name the same key. A rename on either
    side would leave this silently doing nothing, which is how it started."""
    from tools import browser_camofox

    assert browser_camofox._managed_persistence_enabled({'managed_persistence': True}) is True
    assert browser_camofox._managed_persistence_enabled({}) is False
