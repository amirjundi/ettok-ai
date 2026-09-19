"""A verdict interrupted is a verdict that can be finished later.

From the architecture review of 19 September 2026 (ARCH-10). The run stored its
observations first and judged them second, which is the right order: storage is
cheap and local, judgement needs a provider that can be down. But the judging
half left no trace. A model outage, or a laptop closed mid-run, and those items
had no verdict and no record that one had been attempted -- on every screen
indistinguishable from items the agent simply had not reached yet.

A retry loop would not have helped. A retry inside the run dies with the run.
What was missing is that the intention to classify something is written down
*before* the attempt, so that after a crash there is a list of what was in
flight.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import jobs as jobs_mod
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


@pytest.fixture
def conn(home):
    return schema.connect()


ITEM = {'content_hash': 'h1', 'text': 'they are devil worshippers',
        'parent_post_text': 'sinjar anniversary', 'platform': 'facebook'}


def enqueue(conn, **over):
    item = dict(ITEM, **over)
    return jobs_mod.enqueue(conn, item=item, case_key='5', knowledge_id='42@abc')


class TestAJobOutlivesTheRunThatMadeIt:
    def test_an_interrupted_job_is_still_there(self, conn):
        """The whole point. `running` after the process is gone means something
        died holding it -- nothing is running once its owner is not."""
        job_id = enqueue(conn)
        jobs_mod.start(conn, job_id)

        assert [r['id'] for r in jobs_mod.replayable(conn)] == [job_id]

    def test_a_completed_job_is_not_replayed(self, conn):
        job_id = enqueue(conn)
        jobs_mod.complete(conn, job_id, {'state': 'model_positive'})
        assert jobs_mod.replayable(conn) == []

    def test_a_failed_job_keeps_its_reason(self, conn):
        """"It failed" and "it failed because no provider is configured" need
        different responses, and the second is a five-second fix somebody has
        to be told about."""
        job_id = enqueue(conn)
        jobs_mod.fail(conn, job_id, 'No enabled LLM provider for task')

        row = jobs_mod.replayable(conn)[0]
        assert 'No enabled LLM provider' in jobs_mod.last_error_of(row)

    def test_it_stops_retrying_but_does_not_disappear(self, conn):
        """A job that vanishes after three tries is the silent loss this exists
        to stop. It stops costing provider calls; it stays visible."""
        job_id = enqueue(conn)
        for _ in range(jobs_mod.MAX_ATTEMPTS):
            jobs_mod.start(conn, job_id)
            jobs_mod.fail(conn, job_id, 'still down')

        assert jobs_mod.replayable(conn) == []
        assert jobs_mod.status(conn)['needs_attention'] == 1


class TestTheJobCarriesEnoughToReplay:
    def test_the_whole_observation_is_stored(self, conn):
        """Replay happens hours or days later and the post is usually deleted
        by then. A job holding only a URL cannot be replayed at all."""
        enqueue(conn)
        row = jobs_mod.replayable(conn)[0]
        assert jobs_mod.item_of(row)['text'] == ITEM['text']

    def test_a_corrupt_payload_does_not_raise(self, conn):
        job_id = enqueue(conn)
        conn.execute('UPDATE classification_job SET item_json = ? WHERE id = ?',
                     ('not json', job_id))
        conn.commit()
        assert jobs_mod.item_of(jobs_mod.replayable(conn)[0]) == {}


class TestIdentity:
    def test_the_same_comment_twice_is_one_job(self, conn):
        """A re-run must not pay a second time for the same verdict."""
        assert enqueue(conn) == enqueue(conn)

    def test_new_knowledge_makes_a_new_job(self, conn):
        """An earlier verdict is a version, not something to overwrite: the
        comment judged under an edited lexicon is a different question."""
        first = enqueue(conn)
        second = jobs_mod.enqueue(conn, item=dict(ITEM), case_key='5',
                                  knowledge_id='43@def')
        assert first != second

    def test_the_same_comment_under_two_cases_is_two_jobs(self, conn):
        first = enqueue(conn)
        second = jobs_mod.enqueue(conn, item=dict(ITEM), case_key='6',
                                  knowledge_id='42@abc')
        assert first != second


class TestWhatAnOperatorSees:
    def test_the_counts_separate_stuck_from_waiting(self, conn):
        waiting = enqueue(conn)
        stuck = jobs_mod.enqueue(conn, item=dict(ITEM, content_hash='h2'),
                                 case_key='5', knowledge_id='42@abc')
        for _ in range(jobs_mod.MAX_ATTEMPTS):
            jobs_mod.start(conn, stuck)
            jobs_mod.fail(conn, stuck, 'down')

        status = jobs_mod.status(conn)
        assert status['queued'] == 1
        assert status['needs_attention'] == 1
        del waiting

    def test_nothing_queued_reads_as_healthy(self, conn):
        assert jobs_mod.status(conn) == {
            'queued': 0, 'running': 0, 'completed': 0, 'failed': 0,
            'needs_attention': 0,
        }


class TestTrimming:
    def test_completed_jobs_are_trimmed_but_a_tail_is_kept(self, conn):
        """A completed job is the local record of what was sent and under which
        release -- the answer an operator wants without asking the platform."""
        for i in range(8):
            job_id = jobs_mod.enqueue(conn, item=dict(ITEM, content_hash=f'h{i}'),
                                      case_key='5', knowledge_id='42@abc')
            jobs_mod.complete(conn, job_id, {'state': 'model_negative'})

        jobs_mod.forget_completed(conn, keep_last=3)
        remaining = conn.execute(
            'SELECT COUNT(*) AS n FROM classification_job').fetchone()['n']
        assert remaining == 3

    def test_unfinished_work_is_never_trimmed(self, conn):
        enqueue(conn)
        jobs_mod.forget_completed(conn, keep_last=0)
        assert len(jobs_mod.replayable(conn)) == 1


class TestTheReplayTool:
    @pytest.fixture
    def tools(self, conn, monkeypatch):
        import plugins.ettok as plugin
        from plugins.ettok.platform.knowledge import Knowledge

        ctx = mock.MagicMock()
        ctx.llm.complete_structured.return_value = mock.MagicMock(
            parsed={'is_hate_speech': True, 'reason': 'r', 'severity': 9},
            usage=mock.MagicMock(cost_usd=0.01, total_tokens=100),
        )
        ctx._ettok_knowledge = Knowledge(terms=[], tropes=[], cases=[])
        monkeypatch.setattr(plugin, '_services',
                            lambda _c: (mock.MagicMock(), conn, mock.MagicMock()))
        return {spec['name']: handler
                for _k, spec, handler, _i in plugin._make_tools(ctx)}

    def test_it_finishes_an_interrupted_job(self, tools, conn):
        job_id = enqueue(conn)
        jobs_mod.start(conn, job_id)

        out = json.loads(tools['ettok_replay']({}))
        assert out['replayed'] == 1
        assert jobs_mod.replayable(conn) == []

    def test_nothing_to_replay_is_the_healthy_answer(self, tools, conn):
        out = json.loads(tools['ettok_replay']({}))
        assert out['replayed'] == 0
        assert 'no classification was interrupted' in out['note']

    def test_a_job_with_no_text_is_closed_rather_than_retried_forever(
            self, tools, conn):
        jobs_mod.enqueue(conn, item={'content_hash': 'h9'}, case_key='5',
                         knowledge_id='42@abc')
        out = json.loads(tools['ettok_replay']({}))
        assert out['replayed'] == 0
        assert out['failed'][0]['error'] == 'empty item'

    def test_it_refuses_without_knowledge_rather_than_guessing(self, conn, monkeypatch):
        import plugins.ettok as plugin

        ctx = mock.MagicMock()
        ctx._ettok_knowledge = None
        monkeypatch.setattr(plugin, '_services',
                            lambda _c: (mock.MagicMock(), conn, mock.MagicMock()))
        tools = {spec['name']: handler
                 for _k, spec, handler, _i in plugin._make_tools(ctx)}

        out = json.loads(tools['ettok_replay']({}))
        assert 'error' in out
        assert 'unknown age' in out['error']


OLD_CLASSIFICATION = """
CREATE TABLE classification (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER,
    content_hash      TEXT NOT NULL DEFAULT '',
    case_id           TEXT NOT NULL DEFAULT '',
    case_title        TEXT NOT NULL DEFAULT '',
    platform          TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    excerpt           TEXT NOT NULL DEFAULT '',
    parent_excerpt    TEXT NOT NULL DEFAULT '',
    is_hate_speech    INTEGER NOT NULL DEFAULT 0,
    why_flagged       TEXT NOT NULL DEFAULT '',
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
INSERT INTO classification(content_hash, excerpt, is_hate_speech, reason, created_at)
VALUES ('keep-me', 'an earlier verdict', 1, 'because', '2026-09-01T00:00:00Z');
"""


class TestUpgradingAnOlderStore:
    """`is_hate_speech` was NOT NULL, so "no answer" had nowhere to go.

    SQLite cannot drop a NOT NULL in place, so this one table is rebuilt. It is
    copied rather than dropped: these rows are the only record of what the
    agent decided while unpaired, and losing them to a schema change would be
    the same silent loss the column itself was causing.
    """

    @pytest.fixture
    def old_store(self, tmp_path):
        import sqlite3

        conn = sqlite3.connect(tmp_path / 'old.db')
        conn.row_factory = sqlite3.Row
        conn.executescript(OLD_CLASSIFICATION)
        conn.commit()
        return conn

    def upgrade(self, conn):
        schema._drop_stale_tables(conn)
        conn.executescript(schema._TABLES)
        return conn

    def test_an_earlier_verdict_survives(self, old_store):
        rows = self.upgrade(old_store).execute(
            'SELECT * FROM classification').fetchall()
        assert len(rows) == 1
        assert rows[0]['content_hash'] == 'keep-me'
        assert rows[0]['is_hate_speech'] == 1
        assert rows[0]['reason'] == 'because'

    def test_the_constraint_is_gone(self, old_store):
        conn = self.upgrade(old_store)
        info = {r['name']: r['notnull']
                for r in conn.execute('PRAGMA table_info(classification)')}
        assert not info['is_hate_speech']
        assert 'state' in info

    def test_no_answer_can_now_be_recorded(self, old_store):
        conn = self.upgrade(old_store)
        conn.execute(
            "INSERT INTO classification(content_hash, is_hate_speech, state, "
            "created_at) VALUES ('new', NULL, 'not_assessed', 'now')")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM classification WHERE content_hash = 'new'").fetchone()
        assert row['is_hate_speech'] is None

    def test_running_it_twice_is_harmless(self, old_store):
        """It runs on every connection, so it has to be a no-op once done."""
        conn = self.upgrade(old_store)
        self.upgrade(conn)
        assert conn.execute(
            'SELECT COUNT(*) AS n FROM classification').fetchone()['n'] == 1

    def test_the_old_table_is_not_left_behind(self, old_store):
        conn = self.upgrade(old_store)
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert 'classification_old' not in tables
