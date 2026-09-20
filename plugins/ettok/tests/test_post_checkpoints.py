"""Knowing which threads to open a second time.

From the contextual implementation guide of 20 September 2026, section 12:
"Keep per-source checkpoints and revisit recent posts for newly added
replies."

A pile-on arrives over hours. The post is collected at nine with four comments
and has ninety by noon, and nothing brought the agent back -- so the run that
mattered was the one nobody made, and the day's report says four.

The cost was never the obstacle: deduplication already means a second visit
pays only for what is new. What was missing was the list of doors worth
knocking on twice.

Two filters keep that list from becoming a treadmill. Recent, because an old
thread is finished and its comments are not coming back. And still moving: a
post whose last read found nothing new has gone quiet, and dropping it is not
a prediction, it is declining to pay a page load to confirm what the last page
load already said.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.platform.knowledge import Knowledge
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


@pytest.fixture
def conn(home):
    return schema.connect()


def note(conn, identity, *, hours_ago=0, times=1, new=5, case='1'):
    when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        'INSERT INTO seen_post(post_identity, url, platform, case_id, '
        'first_seen_at, last_seen_at, times_seen, last_comment_count, '
        'last_new_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (identity, f'https://facebook.com/{identity}', 'facebook', case,
         when, when, times, new + 2, new),
    )
    conn.commit()


def identities(rows):
    return {row['post_identity'] for row in rows}


class TestWhichThreadsComeBack:
    def test_a_recent_thread_still_gaining_comments_does(self, conn):
        note(conn, 'p1', hours_ago=2, times=2, new=40)

        assert identities(scan_mod.posts_to_revisit(conn)) == {'p1'}

    def test_an_old_one_does_not(self, conn):
        """Its comments are not coming back, and revisiting forever would
        spend every run re-reading history."""
        note(conn, 'p1', hours_ago=200, times=2, new=40)

        assert scan_mod.posts_to_revisit(conn) == []

    def test_one_that_went_quiet_does_not(self, conn):
        """Not a prediction about the future -- a refusal to pay a page load
        to confirm what the last page load already said."""
        note(conn, 'p1', hours_ago=1, times=3, new=0)

        assert scan_mod.posts_to_revisit(conn) == []

    def test_one_read_only_once_is_always_offered(self, conn):
        """There is no evidence either way yet, and the cheap wrong answer
        here is skipping a thread that was about to erupt."""
        note(conn, 'p1', hours_ago=1, times=1, new=0)

        assert identities(scan_mod.posts_to_revisit(conn)) == {'p1'}

    def test_the_busiest_come_first(self, conn):
        note(conn, 'quiet', hours_ago=1, times=2, new=1)
        note(conn, 'busy', hours_ago=1, times=2, new=90)

        assert scan_mod.posts_to_revisit(conn)[0]['post_identity'] == 'busy'

    def test_one_case_can_be_asked_for_alone(self, conn):
        note(conn, 'p1', case='1')
        note(conn, 'p2', case='2')

        assert identities(scan_mod.posts_to_revisit(conn, case_key='1')) == {'p1'}

    def test_the_window_is_configurable(self, conn):
        note(conn, 'p1', hours_ago=100)

        assert scan_mod.posts_to_revisit(conn, within_hours=8) == []
        assert identities(scan_mod.posts_to_revisit(conn, within_hours=200)) == {'p1'}


# ---------------------------------------------------------------------------
# What a run writes down
# ---------------------------------------------------------------------------

def knowledge():
    return Knowledge(
        terms=[], tropes=[],
        cases=[{'id': 1, 'title': 'Sinjar', 'state': 'active',
                'target_groups': [{'slug': 'yazidi', 'name_en': 'Yazidis',
                                   'topic_markers': ['sinjar'],
                                   'background': 'b'}],
                'seed_sources': [], 'limits': {}, 'suggests_closing': False}],
    )


class Ctx:
    def __init__(self):
        self._ettok_knowledge = knowledge()
        self.llm = mock.MagicMock()
        self.llm.complete_structured.return_value = mock.MagicMock(
            parsed={'is_hate_speech': False, 'reason': 'ordinary'},
            usage=mock.MagicMock(cost_usd=0.001, total_tokens=50))

    def get_config(self, key, default=None):
        return default


def comments(n, page='https://facebook.com/p/1', offset=0):
    return [{
        'text': f'comment number {i + offset}',
        'parent_post_text': 'remembering sinjar',
        'parent_post_url': page,
        'url': f'{page}#c{i + offset}',
        'platform': 'facebook',
    } for i in range(n)]


class TestARunRecordsWhatItRead:
    def test_the_post_is_remembered(self, home):
        scan_mod.run(Ctx(), items=comments(3), submit=False)

        rows = schema.connect().execute('SELECT * FROM seen_post').fetchall()
        assert len(rows) == 1
        assert rows[0]['last_comment_count'] == 3
        assert rows[0]['last_new_count'] == 3

    def test_a_second_visit_counts_only_what_was_new(self, home):
        """Which is what makes the revisit decision possible, and what makes
        the revisit cheap."""
        ctx = Ctx()
        scan_mod.run(ctx, items=comments(3), submit=False)
        scan_mod.run(ctx, items=comments(5), submit=False)

        row = schema.connect().execute('SELECT * FROM seen_post').fetchone()
        assert row['times_seen'] == 2
        assert row['last_new_count'] == 2, 'three were already seen'

    def test_a_thread_that_gained_nothing_records_zero(self, home):
        ctx = Ctx()
        scan_mod.run(ctx, items=comments(3), submit=False)
        scan_mod.run(ctx, items=comments(3), submit=False)

        row = schema.connect().execute('SELECT * FROM seen_post').fetchone()
        assert row['last_new_count'] == 0
        assert scan_mod.posts_to_revisit(schema.connect()) == []

    def test_two_posts_are_two_rows(self, home):
        items = comments(2) + comments(2, page='https://facebook.com/p/2',
                                       offset=90)
        scan_mod.run(Ctx(), items=items, submit=False)

        rows = schema.connect().execute('SELECT * FROM seen_post').fetchall()
        assert len(rows) == 2

    def test_an_item_with_no_post_url_is_not_a_post(self, home):
        scan_mod.run(Ctx(), items=[{
            'text': 'a comment submitted by hand',
            'parent_post_text': 'remembering sinjar',
            'platform': 'facebook'}], submit=False)

        assert schema.connect().execute(
            'SELECT COUNT(*) AS n FROM seen_post').fetchone()['n'] == 0
