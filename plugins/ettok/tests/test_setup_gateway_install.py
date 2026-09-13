# -*- coding: utf-8 -*-
"""The wizard must not hang on a prompt the operator cannot see.

`gateway install` asks up to three questions. Running it with captured output
and no stdin swallowed them and blocked until the timeout, so the wizard simply
stopped after "Install the gateway so it starts automatically? y" -- with no
output, no error, and nothing to answer.
"""
import subprocess
from unittest import mock

from plugins.ettok import setup_wizard


def test_the_installer_answers_the_questions_it_can():
    """Both of the yes/no prompts have flags. Passing them is what makes this
    non-interactive rather than hoping nobody is asked."""
    with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)) as run:
        assert setup_wizard._install_gateway_service() is True

    argv = run.call_args[0][0]
    assert argv[-4:] == ['gateway', 'install', '--start-now', '--start-on-login']


def test_the_installer_never_captures_output():
    """Captured output hides the prompt and detaches stdin, which is the exact
    shape of the hang. Inheriting stdio is what lets the operator answer the
    elevation question."""
    with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)) as run:
        setup_wizard._install_gateway_service()

    kwargs = run.call_args[1]
    assert not kwargs.get('capture_output')
    assert 'stdout' not in kwargs
    assert 'stdin' not in kwargs


def test_a_failed_install_is_reported_not_raised():
    with mock.patch('subprocess.run', return_value=mock.Mock(returncode=1)):
        assert setup_wizard._install_gateway_service() is False


def test_a_hang_is_bounded_and_explained():
    """If it does block on something unforeseen, the wizard says so rather than
    appearing to have died."""
    said = []
    with mock.patch('subprocess.run',
                    side_effect=subprocess.TimeoutExpired('gateway', 600)), \
         mock.patch.object(setup_wizard, '_say', side_effect=lambda *a: said.append(a)):
        assert setup_wizard._install_gateway_service() is False

    assert any('did not finish' in str(entry) for entry in said)


def test_an_unexpected_failure_does_not_abort_the_wizard():
    """Setup has five steps after this one; a failed service install must not
    take them with it."""
    with mock.patch('subprocess.run', side_effect=OSError('no such file')), \
         mock.patch.object(setup_wizard, '_say'):
        assert setup_wizard._install_gateway_service() is False
