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

SCHEMA_VERSION = 4

# Defined on its own so the one migration that has to rebuild this table
# can create it from the same text the schema script uses -- two copies of
# a CREATE statement drift, and this one is a table holding evidence of
# what the agent decided.
_TABLE_CLASSIFICATION = """CREATE TABLE IF NOT EXISTS classification (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER,
    content_hash      TEXT NOT NULL DEFAULT '',
    case_id           TEXT NOT NULL DEFAULT '',
    case_title        TEXT NOT NULL DEFAULT '',
    platform          TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    excerpt           TEXT NOT NULL DEFAULT '',
    parent_excerpt    TEXT NOT NULL DEFAULT '',
    -- Nullable, because "no answer" is a real result and the commonest one.
    -- It was NOT NULL DEFAULT 0, so a rule firing with no model behind it, a
    -- comment nothing examined, and a model clearing somebody all stored the
    -- same value. `state` beside it carries which.
    is_hate_speech    INTEGER,
    why_flagged       TEXT NOT NULL DEFAULT '',
    category          TEXT NOT NULL DEFAULT '',
    severity          INTEGER,
    reason            TEXT NOT NULL DEFAULT '',
    fired_terms       TEXT NOT NULL DEFAULT '[]',
    fired_tropes      TEXT NOT NULL DEFAULT '[]',
    exemption_applied TEXT NOT NULL DEFAULT '',
    tier              TEXT NOT NULL DEFAULT 'matched_only',
    -- The result state, which `is_hate_speech` above cannot express: a rule
    -- firing, a comment nobody examined and a model clearing something all
    -- wrote 0 or 1 into that column and were indistinguishable afterwards.
    state             TEXT NOT NULL DEFAULT '',
    versions          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);"""


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

-- collected_item was here. Nothing ever wrote to it: a run hashes an item,
-- submits it and keeps only the hash, and the evidence archive it was meant to
-- be the local half of now lives on the platform, where a deleted original is
-- still recoverable. A table that has never held a row and now has nothing to
-- hold is a claim about the design that is no longer true, so it is gone rather
-- than documented.

-- Captured at the moment of collection, before an item counts as collected.
-- Hate speech posts get deleted, often within hours of being reported, and
-- evidence that was not captured is evidence you do not have.
CREATE TABLE IF NOT EXISTS evidence_artifact (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER,
    screenshot_path  TEXT NOT NULL DEFAULT '',
    archive_path     TEXT NOT NULL DEFAULT '',
    source_url       TEXT NOT NULL DEFAULT '',
    captured_at      TEXT NOT NULL,
    content_hash     TEXT NOT NULL DEFAULT '',
    delivered_at     TEXT,
    -- Who this capture belongs to, recorded when it is taken.
    --
    -- Delivery used to stamp every pending artefact with whichever case the
    -- run draining the queue happened to be working. A capture that failed to
    -- upload during case A -- which is the case this whole store exists for --
    -- was then uploaded during case B, as B's evidence. Ownership of evidence
    -- is not something a later unrelated run gets to decide.
    case_id          INTEGER
);

-- Advisory. The platform re-evaluates every item and its verdict is the one that
-- stands; this is kept so a disagreement is visible rather than silent.
-- Self-contained on purpose. It used to hold only a foreign key to
-- collected_item, which nothing has ever written, so a row here could not be
-- displayed without the platform -- and the operator's own machine could not
-- answer "what did my agent decide, and why" without a network round trip to
-- somebody else's database.
__CLASSIFICATION_TABLE__
CREATE INDEX IF NOT EXISTS idx_classification_hash ON classification(content_hash);
CREATE INDEX IF NOT EXISTS idx_classification_made ON classification(created_at);

-- Classification work that survives the process doing it.
--
-- A model provider goes down, or the laptop is closed mid-run, and the items
-- that were being judged were simply gone: the run had already stored them for
-- submission, the verdict attempt left no trace, and nothing anywhere said an
-- item had been given up on. On every screen that is indistinguishable from an
-- item nothing has reached yet.
--
-- Keyed by the observation and the knowledge that judged it, so re-classifying
-- the same comment under an edited lexicon is a new job rather than an
-- overwrite -- an earlier verdict is a version, never something to lose.
--
-- The item payload is kept here because replay has to happen without the page:
-- the post it came from is usually deleted by the time anybody retries.
CREATE TABLE IF NOT EXISTS classification_job (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash   TEXT NOT NULL,
    case_key       TEXT NOT NULL DEFAULT '',
    knowledge_id   TEXT NOT NULL DEFAULT '',
    item_json      TEXT NOT NULL,
    -- queued | running | completed | failed
    state          TEXT NOT NULL DEFAULT 'queued',
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    verdict_json   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(content_hash, case_key, knowledge_id)
);
CREATE INDEX IF NOT EXISTS idx_job_state ON classification_job(state);

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
-- Keyed on the case as well as the hash: one comment can legitimately belong to
-- two cases -- an anniversary watch and a standing watch read the same thread --
-- and each case needs its own copy of the evidence. Keyed on the hash alone,
-- whichever case scanned first starved the other, silently.
CREATE TABLE IF NOT EXISTS seen_item (
    content_hash  TEXT NOT NULL,
    case_id       TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (content_hash, case_id)
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

# One copy of the classification CREATE, shared by the schema script and by
# the migration that has to rebuild that table. Two copies drift, and this is
# the table holding what the agent decided.
_TABLES = _TABLES.replace('__CLASSIFICATION_TABLE__', _TABLE_CLASSIFICATION)


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_NAME)


def evidence_dir() -> Path:
    path = data_dir() / 'evidence'
    path.mkdir(parents=True, exist_ok=True)
    return path



def _drop_stale_tables(conn) -> None:
    """Remove tables an older build shaped differently, before the schema runs.

    Both of these are rebuilt rather than migrated, and both can afford it:

    `seen_item` is a memory of hashes and nothing else. The digest formula now
    covers more fields, so every hash an older build wrote is stale and cannot
    match anything this build computes. The cost is one re-collection pass, and
    the platform deduplicates on its own side.

    `classification` held only a foreign key into a table nothing has ever
    written, and no build ever inserted a row into it, so there is nothing to
    carry over.

    A table that does not exist yet reports no columns, so a fresh database
    falls straight through.
    """
    for table, required in (('seen_item', 'case_id'), ('classification', 'content_hash')):
        columns = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
        if columns and required not in columns:
            conn.execute(f'DROP TABLE {table}')

    # evidence_artifact is added to, never rebuilt. The rows are the queue of
    # captures that exist nowhere else yet, so dropping the table to reshape it
    # would orphan files on disk that are the only copy of a deleted post.
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(evidence_artifact)')}
    if columns and 'case_id' not in columns:
        conn.execute('ALTER TABLE evidence_artifact ADD COLUMN case_id INTEGER')

    # Same reasoning for the local judgement log: it is the only record of what
    # this agent decided while unpaired, so it is added to rather than rebuilt.
    info = list(conn.execute('PRAGMA table_info(classification)'))
    columns = {row['name'] for row in info}
    if columns and 'state' not in columns:
        conn.execute("ALTER TABLE classification ADD COLUMN state TEXT NOT NULL DEFAULT ''")

    # `is_hate_speech` has to be able to say "no answer", and SQLite cannot
    # drop a NOT NULL in place. So this one table is rebuilt -- copied, not
    # dropped, because these rows are the only record of what this agent
    # decided while unpaired and losing them to a schema change would be the
    # same silent loss the column itself was causing.
    #
    # Guarded on the flag rather than a version number, so it runs once and is
    # a no-op on every connection afterwards, including a fresh install where
    # the column is already nullable.
    not_null = {row['name'] for row in info if row['notnull']}
    if 'is_hate_speech' in not_null:
        kept = [name for name in (
            'id', 'collected_item_id', 'content_hash', 'case_id', 'case_title',
            'platform', 'url', 'excerpt', 'parent_excerpt', 'is_hate_speech',
            'why_flagged', 'category', 'severity', 'reason', 'fired_terms',
            'fired_tropes', 'exemption_applied', 'tier', 'state', 'versions',
            'created_at',
        ) if name in columns or name == 'state']
        names = ', '.join(kept)
        conn.executescript(f"""
            ALTER TABLE classification RENAME TO classification_old;
            {_TABLE_CLASSIFICATION}
            INSERT INTO classification({names}) SELECT {names} FROM classification_old;
            DROP TABLE classification_old;
        """)

    # collected_item is gone from the schema. An agent installed before this
    # still has the table on disk, empty -- nothing ever wrote to it -- and
    # leaving it there would keep the claim it makes about the design alive in
    # every future reader's head.
    if conn.execute('PRAGMA table_info(collected_item)').fetchall():
        conn.execute('DROP TABLE IF EXISTS collected_item')


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

    # Stale tables go BEFORE the schema script, never after. `_TABLES` creates
    # an index on `classification(content_hash)`, and on a database from an
    # older build that column does not exist yet -- so the whole script raises
    # on the index, the connection never opens, and every page of the dashboard
    # answers 500. A migration that runs after the thing it is fixing is not a
    # migration.
    _drop_stale_tables(conn)
    conn.executescript(_TABLES)

    conn.execute(
        'INSERT INTO schema_meta(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        ('schema_version', str(SCHEMA_VERSION)),
    )
    conn.commit()
    return conn
