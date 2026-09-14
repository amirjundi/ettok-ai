# -*- coding: utf-8 -*-
"""An operator can lift a quarantine they have actually dealt with.

The agent quarantines an account for 24 hours when a page comes back as a
CAPTCHA or a checkpoint. That is right -- the challenge is the platform saying
it has noticed. What was missing is the way back: an operator who signed in
themselves, cleared it as the human being tested, and watched the account behave
had no way to say so, and waiting out a cooldown that no longer describes
reality is not safety.

This does not let the agent clear a challenge. It lets a person who has dealt
with one say the account is fine.
"""
import pytest

from plugins.ettok.collect import session as session_mod
from plugins.ettok.store import schema


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(schema, 'data_dir', lambda: tmp_path)
    return schema.connect()


def test_a_challenge_quarantines_the_account(conn):
    session_mod.quarantine(conn, 'fb-watch', 'captcha')
    assert session_mod.account_state(conn, 'fb-watch') == session_mod.QUARANTINED


def test_an_operator_can_release_it(conn):
    session_mod.quarantine(conn, 'fb-watch', 'captcha')

    assert session_mod.release(conn, 'fb-watch', note='cleared it myself') is True
    assert session_mod.account_state(conn, 'fb-watch') == session_mod.HEALTHY


def test_the_release_is_recorded_as_a_human_decision(conn):
    """Distinguishable from a run that simply succeeded -- they are different
    facts and an operator reviewing later needs to tell them apart."""
    session_mod.quarantine(conn, 'fb-watch', 'checkpoint')
    session_mod.release(conn, 'fb-watch', note='signed in, 2FA cleared')

    row = session_mod.account_health(conn)[0]
    assert 'operator' in row['block_reason']
    assert 'signed in, 2FA cleared' in row['block_reason']
    assert row['cooldown_until'] is None


def test_releasing_an_unknown_account_says_so(conn):
    assert session_mod.release(conn, 'never-seen') is False


def test_health_reports_the_effective_state(conn):
    """A cooldown that has expired reads as healthy even though the stored row
    still says quarantined; the report has to show what will actually happen."""
    session_mod.quarantine(conn, 'fb-watch', 'captcha')
    conn.execute(
        "UPDATE account_health SET cooldown_until = '2000-01-01T00:00:00+00:00' "
        "WHERE account_id = ?", ('fb-watch',))
    conn.commit()

    row = session_mod.account_health(conn)[0]
    assert row['state'] == session_mod.QUARANTINED
    assert row['effective_state'] == session_mod.HEALTHY


def test_release_does_not_touch_other_accounts(conn):
    session_mod.quarantine(conn, 'fb-one', 'captcha')
    session_mod.quarantine(conn, 'fb-two', 'captcha')

    session_mod.release(conn, 'fb-one')

    assert session_mod.account_state(conn, 'fb-one') == session_mod.HEALTHY
    assert session_mod.account_state(conn, 'fb-two') == session_mod.QUARANTINED
