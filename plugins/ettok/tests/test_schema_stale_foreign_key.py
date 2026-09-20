"""A capture table left pointing at a table that no longer exists.

`collected_item` was declared, never written to, and dropped from the schema. But
`evidence_artifact` was created by earlier builds with
`collected_item_id INTEGER REFERENCES collected_item(id) ON DELETE CASCADE` on it,
and that foreign key cannot be dropped in place -- so an upgraded database kept
the reference to a table that was now gone.

Nothing raised at the time. `PRAGMA foreign_keys=ON` resolves constraints when a
statement is prepared, so the dangling key sat there unseen until the first
INSERT, where it raised "no such table: main.collected_item". Collection died at
the exact moment it captured evidence, which is the moment this store exists for,
and the failure looked like a database problem rather than a missing table.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from plugins.ettok.store import schema

# The shape an older build wrote: same columns, plus the foreign key.
_EVIDENCE_TABLE_AS_AN_OLDER_BUILD_WROTE_IT = """CREATE TABLE evidence_artifact (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER REFERENCES collected_item(id) ON DELETE CASCADE,
    screenshot_path  TEXT NOT NULL DEFAULT '',
    archive_path     TEXT NOT NULL DEFAULT '',
    source_url       TEXT NOT NULL DEFAULT '',
    captured_at      TEXT NOT NULL,
    content_hash     TEXT NOT NULL DEFAULT '',
    delivered_at     TEXT
)"""


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


def _database_written_by_an_older_build(home) -> None:
    """Upgrade in place: current tables, then the old capture table over them."""
    schema.connect().close()

    conn = schema.connect()
    conn.execute('DROP TABLE evidence_artifact')
    conn.execute('CREATE TABLE collected_item (id INTEGER PRIMARY KEY AUTOINCREMENT)')
    conn.executescript(_EVIDENCE_TABLE_AS_AN_OLDER_BUILD_WROTE_IT)
    conn.execute('INSERT INTO collected_item(id) VALUES (1)')
    conn.execute(
        "INSERT INTO evidence_artifact(collected_item_id, screenshot_path, archive_path, "
        "source_url, captured_at, content_hash) "
        "VALUES (1, 'shot.png', 'page.html', 'https://facebook.com/p/1', "
        "'2026-09-20T08:31:19+00:00', 'abc123')"
    )
    conn.commit()
    conn.close()


def _capture_columns(conn):
    return {row['name'] for row in conn.execute('PRAGMA table_info(evidence_artifact)')}


class TestTheStaleForeignKeyIsRemoved:
    def test_an_older_database_still_opens(self, home):
        _database_written_by_an_older_build(home)

        conn = schema.connect()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='evidence_artifact'"
        ).fetchone()[0]
        # The column stays -- an insert still names it -- but the constraint into
        # a table that no longer exists must not.
        assert 'collected_item_id' in sql
        assert 'REFERENCES' not in sql
        assert not conn.execute('PRAGMA foreign_key_list(evidence_artifact)').fetchall()
        # The dropped table stays dropped: leaving it behind keeps alive a claim
        # about the design that was reversed.
        assert not conn.execute('PRAGMA table_info(collected_item)').fetchall()

    def test_a_capture_can_be_recorded_afterwards(self, home):
        """What actually broke. The row is the index of a file on disk."""
        _database_written_by_an_older_build(home)

        conn = schema.connect()
        cursor = conn.execute(
            "INSERT INTO evidence_artifact(screenshot_path, archive_path, source_url, "
            "captured_at, content_hash, case_id) VALUES (?, ?, ?, ?, ?, ?)",
            ('shot2.png', 'page2.html', 'https://facebook.com/p/2',
             '2026-09-20T09:00:00+00:00', 'def456', 7),
        )
        conn.commit()
        assert cursor.lastrowid

    def test_captures_taken_before_the_upgrade_survive(self, home):
        """They are the only record of a post that may already be deleted."""
        _database_written_by_an_older_build(home)

        conn = schema.connect()
        rows = conn.execute('SELECT * FROM evidence_artifact').fetchall()
        assert len(rows) == 1
        assert rows[0]['archive_path'] == 'page.html'
        assert rows[0]['content_hash'] == 'abc123'
        # An attribute a later migration added is carried over, not defaulted.
        assert 'case_id' in _capture_columns(conn)

    def test_running_twice_changes_nothing_the_second_time(self, home):
        _database_written_by_an_older_build(home)

        schema.connect().close()
        conn = schema.connect()
        assert len(conn.execute('SELECT * FROM evidence_artifact').fetchall()) == 1
        assert not conn.execute('PRAGMA foreign_key_list(evidence_artifact)').fetchall()
