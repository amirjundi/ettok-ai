# -*- coding: utf-8 -*-
"""The dashboard's credential vault endpoints.

Monitoring needs accounts, and an operator with no other route types the
password into the chat -- which is the one place it must never go, because a
transcript keeps it for good. The vault existed as a CLI command and nothing in
the dashboard mentioned it, so the CLI was the only way to discover it.
"""
import asyncio
import json

import pytest

from agent.vault_store import VaultStore
from plugins.ettok.dashboard import plugin_api as api


class FakeRequest:
    def __init__(self, host, body=None):
        self.client = type('C', (), {'host': host})
        self._body = body or {}

    async def json(self):
        return self._body


LOGIN = {
    'kind': 'login',
    'label': 'Facebook monitoring',
    'origin': 'https://www.facebook.com',
    'identifier': 'watcher@example.org',
    'identifier_type': 'email',
    'password': 'not-a-real-password',
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    vault = VaultStore(base_dir=tmp_path)
    monkeypatch.setattr(api, '_vault', lambda: vault)
    return vault


def test_an_empty_vault_lists_nothing(store):
    assert api.vault_list()['items'] == []


def test_a_login_can_be_stored_and_listed(store):
    assert asyncio.run(api.vault_add(FakeRequest('127.0.0.1', LOGIN)))['ok'] is True

    items = api.vault_list()['items']
    assert len(items) == 1
    assert items[0]['label'] == 'Facebook monitoring'
    assert items[0]['identifier'] == 'watcher@example.org'


def test_the_password_is_never_returned(store):
    """The identifier is metadata by design -- the agent types it itself. The
    password is the only secret, and nothing reads it back out."""
    asyncio.run(api.vault_add(FakeRequest('127.0.0.1', LOGIN)))

    blob = json.dumps(api.vault_list())
    assert 'not-a-real-password' not in blob
    assert 'password' not in blob.lower()


def test_a_write_from_off_the_machine_is_refused(store):
    """A password on the wire, to save it from a transcript, is not a trade
    worth making."""
    result = asyncio.run(api.vault_add(FakeRequest('192.168.1.50', LOGIN)))

    assert result['ok'] is False
    assert 'this machine' in result['error']
    assert api.vault_list()['items'] == []


def test_ipv6_loopback_counts_as_local(store):
    assert asyncio.run(api.vault_add(FakeRequest('::1', LOGIN)))['ok'] is True


@pytest.mark.parametrize('missing', ['label', 'origin', 'identifier', 'password'])
def test_every_field_is_required(store, missing):
    body = dict(LOGIN)
    body[missing] = ''

    result = asyncio.run(api.vault_add(FakeRequest('127.0.0.1', body)))

    assert result['ok'] is False
    assert api.vault_list()['items'] == []


def test_an_item_can_be_removed(store):
    asyncio.run(api.vault_add(FakeRequest('127.0.0.1', LOGIN)))
    item_id = api.vault_list()['items'][0]['id']

    assert api.vault_remove(item_id)['ok'] is True
    assert api.vault_list()['items'] == []


def test_a_2fa_seed_is_recorded_without_being_exposed(store):
    """A monitoring account should have 2FA on, and the agent needs to mint
    codes without asking a human at three in the morning."""
    body = dict(LOGIN, otp_secret='JBSWY3DPEHPK3PXP')
    asyncio.run(api.vault_add(FakeRequest('127.0.0.1', body)))

    item = api.vault_list()['items'][0]
    assert item.get('has_otp') is True
    assert 'JBSWY3DPEHPK3PXP' not in json.dumps(item)
