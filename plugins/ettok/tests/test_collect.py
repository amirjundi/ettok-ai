"""Collection: stopping when told, and proving what was taken.

The safety behaviour is the part that must be right. Everything else here can be
retuned against a live site; a collector that works around a block cannot be, and
costs an account each time it is wrong.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from plugins.ettok.collect import base as collect_mod
from plugins.ettok.collect import evidence as evidence_mod
from plugins.ettok.collect import session as session_mod


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


class FakeCtx:
    """A browser that returns whatever the test says the page contains."""

    def __init__(self, page_text='', extracted=None):
        self.page_text = page_text
        self.extracted = extracted
        self.calls = []

    def dispatch_tool(self, tool, args, **kwargs):
        self.calls.append(tool)
        if tool == 'browser_snapshot':
            return json.dumps({'snapshot': self.page_text})
        if tool == 'browser_console':
            return json.dumps({'result': json.dumps(self.extracted or {})})
        if tool == 'browser_vision':
            return json.dumps({'image_b64': ''})
        return json.dumps({'ok': True})


class NoWait(session_mod.Pacer):
    _sleep = staticmethod(lambda _seconds: None)


# ---------------------------------------------------------------------------
# Stopping when the platform says it has noticed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('page,expected', [
    ('Please complete this CAPTCHA to continue', 'CAPTCHA'),
    ('Security Check', 'security check'),
    ('We detected unusual activity on your account', 'unusual activity'),
    ('Your account has been temporarily blocked', 'temporarily blocked'),
    ('Checkpoint required', 'checkpoint'),
])
def test_a_challenge_is_detected(page, expected):
    blocked = session_mod.detect_block(page)
    assert blocked is not None, f'missed a block signal in: {page}'
    assert expected.lower() in blocked.reason.lower()


def test_a_lost_session_is_told_apart_from_a_block():
    """They need opposite responses: one is a rest, the other needs a sign-in."""
    assert session_mod.detect_block('Please log in to continue').auth_lost is True
    assert session_mod.detect_block('Please complete this CAPTCHA').auth_lost is False


def test_an_ordinary_page_is_not_treated_as_blocked():
    assert session_mod.detect_block('الايزيدية تحيي ذكرى سنجار') is None


def test_a_blocked_page_is_never_extracted_from(home):
    """A challenge page has no comments. Extracting first and finding none would
    report a quiet run and hide the fact that the account has been noticed."""
    ctx = FakeCtx(page_text='Security Check', extracted={'comments': [{'text': 'x'}]})
    collector = collect_mod.FacebookCollector(ctx, pacer=NoWait())

    result = collector.collect('https://facebook.com/x')

    assert not result.ok
    assert result.items == []
    assert 'browser_console' not in ctx.calls, 'it tried to extract from a challenge page'


def test_blocking_does_not_raise(home):
    """A block is a result to act on, not an exception to bubble through a scan."""
    collector = collect_mod.FacebookCollector(FakeCtx(page_text='CAPTCHA'), pacer=NoWait())
    assert collector.collect('https://facebook.com/x').blocked


# ---------------------------------------------------------------------------
# Account health
# ---------------------------------------------------------------------------

def test_a_quarantined_account_is_out_of_rotation(home):
    from plugins.ettok.store import schema

    conn = schema.connect()
    session_mod.quarantine(conn, 'monitor-01', 'a CAPTCHA was presented')

    assert session_mod.account_state(conn, 'monitor-01') == session_mod.QUARANTINED
    assert session_mod.healthy_accounts(conn, [{'account_name': 'monitor-01'}]) == []


def test_capacity_loss_is_reported_before_it_becomes_silence(home):
    """Collection degrading to nothing should not be something you infer later."""
    from plugins.ettok.store import schema

    conn = schema.connect()
    accounts = [{'account_name': 'a'}, {'account_name': 'b'}]
    assert session_mod.capacity_warning(conn, accounts) is None

    session_mod.quarantine(conn, 'a', 'blocked')
    assert '1 of 2' in session_mod.capacity_warning(conn, accounts)

    session_mod.quarantine(conn, 'b', 'blocked')
    assert 'No healthy monitoring accounts' in session_mod.capacity_warning(conn, accounts)


def test_a_recovered_account_returns_to_rotation(home):
    from datetime import datetime, timedelta, timezone

    from plugins.ettok.store import schema

    conn = schema.connect()
    session_mod.quarantine(conn, 'monitor-01', 'blocked')
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute('UPDATE account_health SET cooldown_until = ? WHERE account_id = ?',
                 (past, 'monitor-01'))
    conn.commit()

    assert session_mod.account_state(conn, 'monitor-01') == session_mod.HEALTHY


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def test_evidence_is_captured_before_extraction(home):
    """The page can change or vanish between the two, and proof that arrived late
    is an item with no proof."""
    ctx = FakeCtx(page_text='الايزيدية في سنجار',
                  extracted={'parent_post_text': 'الايزيدية في سنجار', 'comments': []})
    collector = collect_mod.FacebookCollector(ctx, pacer=NoWait())
    collector.collect('https://facebook.com/post/1')

    assert ctx.calls.index('browser_vision') < ctx.calls.index('browser_console')


def test_evidence_without_an_artefact_proves_nothing(home):
    """A URL and a timestamp are not evidence: the page they point at can vanish."""
    thin = evidence_mod.Evidence(source_url='https://x', captured_at='now', content_hash='abc')
    assert not thin.is_complete

    real = evidence_mod.capture(url='https://x', page_text='page contents',
                                directory=home / 'ev')
    assert real.is_complete
    assert Path(real.archive_path).exists()


def test_the_hash_changes_when_the_content_does(home):
    """Otherwise the archive proves only that a file exists, not which one."""
    a = evidence_mod.capture(url='https://x', page_text='one', directory=home / 'ev')
    b = evidence_mod.capture(url='https://x', page_text='two', directory=home / 'ev')
    assert a.content_hash != b.content_hash


def test_delivered_evidence_is_removed_from_this_machine(home):
    """Screenshots naming real people must not accumulate on a laptop."""
    from plugins.ettok.store import schema

    conn = schema.connect()
    captured = evidence_mod.capture(url='https://x', page_text='contents',
                                    directory=schema.evidence_dir())
    evidence_id = evidence_mod.store(conn, None, captured)
    assert Path(captured.archive_path).exists()

    evidence_mod.mark_delivered(conn, [evidence_id])
    evidence_mod.discard_delivered(conn)

    assert not Path(captured.archive_path).exists()


def test_undelivered_evidence_is_never_removed(home):
    """An unconfirmed delivery is undelivered. There is no other copy."""
    from plugins.ettok.store import schema

    conn = schema.connect()
    captured = evidence_mod.capture(url='https://x', page_text='contents',
                                    directory=schema.evidence_dir())
    evidence_mod.store(conn, None, captured)
    evidence_mod.discard_delivered(conn)

    assert Path(captured.archive_path).exists()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_every_item_carries_the_post_it_replies_to(home):
    """Without it, context-dependent hate cannot be judged -- which is most of it."""
    ctx = FakeCtx(
        page_text='ordinary page',
        extracted={
            'parent_post_text': 'الايزيدية تحيي ذكرى سنجار',
            'comments': [{'text': 'اعوذ بالله من الشيطان الرجيم', 'author_name': 'X'}],
        },
    )
    result = collect_mod.FacebookCollector(ctx, pacer=NoWait()).collect('https://facebook.com/p')

    assert len(result.items) == 1
    assert result.items[0]['parent_post_text'] == 'الايزيدية تحيي ذكرى سنجار'
    assert result.items[0]['platform'] == 'facebook'


def test_the_post_itself_is_not_collected_as_its_own_comment(home):
    ctx = FakeCtx(page_text='ordinary', extracted={
        'parent_post_text': 'منشور', 'comments': [{'text': 'منشور'}, {'text': 'تعليق'}],
    })
    result = collect_mod.FacebookCollector(ctx, pacer=NoWait()).collect('https://facebook.com/p')
    assert [i['text'] for i in result.items] == ['تعليق']


def test_unusable_extractor_output_yields_nothing_rather_than_crashing(home):
    """Selectors are configuration against a moving target. They will break."""
    ctx = FakeCtx(page_text='ordinary')
    ctx.dispatch_tool = lambda tool, args, **kw: (
        json.dumps({'snapshot': 'ordinary'}) if tool == 'browser_snapshot'
        else json.dumps({'result': 'not json at all'})
    )
    result = collect_mod.FacebookCollector(ctx, pacer=NoWait()).collect(
        'https://facebook.com/p', capture_evidence=False)
    assert result.ok and result.items == []


def test_pacing_is_randomised(home):
    """A constant delay is as distinctive a signature as none, only slower."""
    delays = {round(NoWait(min_seconds=1, max_seconds=5).wait(), 6) for _ in range(20)}
    assert len(delays) > 1
