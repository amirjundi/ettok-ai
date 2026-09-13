# -*- coding: utf-8 -*-
"""Opening the chat tab brings its backend up.

"The dashboard is running, so the chat should work" is a reasonable thing to
expect, and it was not true: the tab needed a second process, configured in a
third place, and said only "not running". Two machines and several rounds of the
same error later, the setup step was the bug.
"""
from unittest import mock

import pytest

from plugins.ettok.dashboard import plugin_api as api


@pytest.fixture(autouse=True)
def _reset_once_guard():
    api._autostart_attempted = False
    yield
    api._autostart_attempted = False


def test_a_running_gateway_is_left_alone():
    """Starting a second one would not fix anything and would fight for the port."""
    with mock.patch.object(api, '_gateway_is_up', return_value=True), \
         mock.patch.object(api, '_spawn_gateway') as spawn:
        result = api._ensure_chat_backend()

    spawn.assert_not_called()
    assert result['attempted'] is False


def test_a_stopped_gateway_is_configured_and_started():
    with mock.patch.object(api, '_gateway_is_up', return_value=False), \
         mock.patch.object(api, '_configure_api_server', return_value=(True, 'ok')), \
         mock.patch.object(api, '_spawn_gateway', return_value=True) as spawn:
        result = api._ensure_chat_backend()

    spawn.assert_called_once()
    assert result == {'attempted': True, 'configured': 'ok', 'started': True}


def test_it_is_attempted_once_per_process():
    """The health endpoint is polled. Without the guard, a chat tab left open
    would spawn a gateway every few seconds."""
    with mock.patch.object(api, '_gateway_is_up', return_value=False), \
         mock.patch.object(api, '_configure_api_server', return_value=(True, 'ok')), \
         mock.patch.object(api, '_spawn_gateway', return_value=True) as spawn:
        api._ensure_chat_backend()
        api._ensure_chat_backend()
        api._ensure_chat_backend()

    assert spawn.call_count == 1


def test_a_configuration_failure_does_not_spawn_anything():
    """Starting a gateway with nothing configured produces the same error the
    operator already has, from a process they now also have to find and kill."""
    with mock.patch.object(api, '_gateway_is_up', return_value=False), \
         mock.patch.object(api, '_configure_api_server', return_value=(False, 'nope')), \
         mock.patch.object(api, '_spawn_gateway') as spawn:
        result = api._ensure_chat_backend()

    spawn.assert_not_called()
    assert result['attempted'] is False
    assert 'nope' in result['reason']


def test_a_gateway_that_answers_401_counts_as_up():
    """401 means something is listening and serving the API. The key is a
    separate problem, and a second gateway would not fix it."""
    response = mock.Mock(status_code=401)
    client = mock.MagicMock()
    client.__enter__.return_value.get.return_value = response

    with mock.patch('httpx.Client', return_value=client):
        assert api._gateway_is_up() is True


def test_a_refused_connection_counts_as_down():
    client = mock.MagicMock()
    client.__enter__.return_value.get.side_effect = OSError('refused')

    with mock.patch('httpx.Client', return_value=client):
        assert api._gateway_is_up() is False


def test_configuring_never_overwrites_a_chosen_port():
    """An operator who moved it off 8642 keeps their port."""
    stored = {'gateway': {'platforms': {'api_server': {'port': 9999}}}}

    with mock.patch('hermes_cli.config.load_config', return_value=stored), \
         mock.patch('hermes_cli.config.save_config') as save, \
         mock.patch('plugins.ettok.setup_wizard._ensure_api_server_key', return_value=False):
        ok, detail = api._configure_api_server()

    assert ok
    assert '9999' in detail
    written = save.call_args[0][0]
    assert written['gateway']['platforms']['api_server']['port'] == 9999
    assert written['gateway']['platforms']['api_server']['enabled'] is True
