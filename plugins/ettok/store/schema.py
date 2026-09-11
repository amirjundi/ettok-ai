"""Local storage: this agent's work in progress, and nothing else.

The rule that decides what belongs here:

    Anything the platform owns is not persisted. Anything about this agent's own
    work in progress is.

So there is no table for terms, tropes, exemptions or the rubric. Those are fetched
at the start of every run and held in memory for that run only, which means a
curator's edit reaches every agent on the next run and a stolen or reimaged laptop
leaks nothing that is not already on the server. Hardcoding them would require
adding storage that deliberately does not exist.

Storage lives under ``<hermes home>/plugin-data/ettok/`` via ``plugin_db``, never in
the plugin's own directory -- that one is deleted by a plugin update, which would
destroy collected evidence silently and after the fact.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PLUGIN_NAME = 'ettok'

SCHEMA_VERSION = 1

_TABLES = """
-- One attempt at working a case. Written before collection starts, so a crash
-- mid-run is visible rather than invisible.
CREATE TABLE IF NOT EXISTS case_run (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_case_id   INTEGER,
    target_group_slug  TEXT    NOT NULL DEFAULT '',
    started_at         TEXT    NOT NULL,
    ended_at           TEXT,
    stop_reason        TEXT,
    knowledge_versions TEXT    NOT NULL DEFAULT '{}',
    posts_scanned      INTEGER NOT NULL DEFAULT 0,
    items_flagged      INTEGER NOT NULL DEFAULT 0,
    spend              REAL    NOT NULL DEFAULT 0,
    errors             TEXT    NOT NULL DEFAULT '[]'
);

-- A comment with the post it replies to, captured as one unit. An item without
-- parent_post_text cannot be judged for context-dependent hate, and an item
-- without evidence cannot be reported once the original is deleted.
CREATE TABLE IF NOT EXISTS collected_item (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    case_run_id       INTEGER REFERENCES case_run(id) ON DELETE SET NULL,
    content_hash      TEXT    NOT NULL,
    platform          TEXT    NOT NULL DEFAULT '',
    url               TEXT    NOT NULL DEFAULT '',
    post_id           TEXT    NOT NULL DEFAULT '',
    comment_id        TEXT    NOT NULL DEFAULT '',
    text              TEXT    NOT NULL DEFAULT '',
    normalized_text   TEXT    NOT NULL DEFAULT '',
    parent_post_text  TEXT    NOT NULL DEFAULT '',
    parent_media_text TEXT    NOT NULL DEFAULT '',
    thread_context    TEXT    NOT NULL DEFAULT '',
    author_handle     TEXT    NOT NULL DEFAULT '',
    author_id         TEXT    NOT NULL DEFAULT '',
    lang_hint         TEXT    NOT NULL DEFAULT '',
    requires_visual   INTEGER NOT NULL DEFAULT 0,
    collected_at      TEXT    NOT NULL,
    evidence_id       INTEGER,
    submitted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_item_hash ON collected_item(content_hash);
CREATE INDEX IF NOT EXISTS idx_item_run  ON collected_item(case_run_id);

-- Captured at the moment of collection, before an item counts as collected.
-- Hate speech posts get deleted, often within hours of being reported, and
-- evidence that was not captured is evidence you do not have.
CREATE TABLE IF NOT EXISTS evidence_artifact (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER REFERENCES collected_item(id) ON DELETE CASCADE,
    screenshot_path  TEXT NOT NULL DEFAULT '',
    archive_path     TEXT NOT NULL DEFAULT '',
    source_url       TEXT NOT NULL DEFAULT '',
    captured_at      TEXT NOT NULL,
    content_hash     TEXT NOT NULL DEFAULT '',
    delivered_at     TEXT
);

-- Advisory. The platform re-evaluates every item and its verdict is the one that
-- stands; this is kept so a disagreement is visible rather than silent.
CREATE TABLE IF NOT EXISTS classification (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER REFERENCES collected_item(id) ON DELETE CASCADE,
    category          TEXT NOT NULL DEFAULT '',
    severity          INTEGER,
    reason            TEXT NOT NULL DEFAULT '',
    fired_terms       TEXT NOT NULL DEFAULT '[]',
    fired_tropes      TEXT NOT NULL DEFAULT '[]',
    exemption_applied TEXT NOT NULL DEFAULT '',
    tier              TEXT NOT NULL DEFAULT 'matched_only',
    versions          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);

-- The most important table. A residential connection drops mid-submit and must
-- lose nothing and duplicate nothing; the idempotency key is what lets a retry be
-- replayed by the platform rather than performed twice.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint        TEXT NOT NULL,
    payload         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state           TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_status     INTEGER,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    delivered_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, next_attempt_at);

-- Hashes, not content. The agent needs to know it has seen a comment before; it
-- does not need to keep the comment on the laptop in order to know that.
CREATE TABLE IF NOT EXISTS seen_item (
    content_hash  TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1
);

-- Unknown language recurring on target-group content. Counted across runs,
-- because seen once is noise and seen fourteen times across three days is a
-- pattern the review queue can sort by.
CREATE TABLE IF NOT EXISTS vocab_candidate (
    normalized_form   TEXT PRIMARY KEY,
    raw_forms         TEXT NOT NULL DEFAULT '[]',
    target_group_slug TEXT NOT NULL DEFAULT '',
    suggested_category TEXT NOT NULL DEFAULT '',
    occurrences       INTEGER NOT NULL DEFAULT 1,
    evidence_urls     TEXT NOT NULL DEFAULT '[]',
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    proposed_at       TEXT
);

-- A challenge or block moves an account here. It is a detection signal, never an
-- obstacle to clear: solving it removes the warning and leaves the detection.
CREATE TABLE IF NOT EXISTS account_health (
    account_id     TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'healthy',
    last_success_at TEXT,
    last_block_at  TEXT,
    block_reason   TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT
);

-- Budget is donation-funded and varies month to month, including months at zero.
CREATE TABLE IF NOT EXISTS budget_ledger (
    month     TEXT PRIMARY KEY,
    limit_usd REAL,
    spent_usd REAL NOT NULL DEFAULT 0,
    warned_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_NAME)


def evidence_dir() -> Path:
    path = data_dir() / 'evidence'
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    """Open the plugin database, creating the schema on first use.

    WAL because collection writes while the outbox drains, and the default
    journal mode would have them blocking each other on a single-disk laptop.
    """
    from plugins.plugin_storage import plugin_db

    conn = plugin_db(PLUGIN_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(_TABLES)
    conn.execute(
        'INSERT INTO schema_meta(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        ('schema_version', str(SCHEMA_VERSION)),
    )
    conn.commit()
    return conn
