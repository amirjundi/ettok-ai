"""The agent's own record of what it decided.

An operator could see what the platform made of a submission and never what
their own agent decided or why: the local table existed and nothing wrote to it.
So "the agent is judging badly" was a claim that could only be checked by
logging into somebody else's database, and a run made while unpaired left no
trace of its reasoning at all.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.store import schema
from plugins.ettok.tests.test_scan import Ctx, FakeKnowledge, _items  # noqa: F401


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


def _rows(**where):
    conn = schema.connect()
    clause = ''
    params = []
    if where:
        clause = ' WHERE ' + ' AND '.join(f'{k} = ?' for k in where)
        params = list(where.values())
    return conn.execute(
        'SELECT * FROM classification' + clause + ' ORDER BY id', params
    ).fetchall()


class TestEveryDecisionIsKept:
    def test_a_finding_is_recorded(self, home):
        scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)

        row = _rows()[0]
        assert row['why_flagged']
        assert row['excerpt']
        assert row['case_title'] == 'Sinjar anniversary backlash'

    def test_a_comment_that_matched_nothing_is_recorded_too(self, home):
        """It is the denominator. Findings without the number of comments they
        came out of cannot be turned into a rate, and the local view has the
        same problem the platform had."""
        quiet = [{'text': 'ordinary comment', 'parent_post_text': 'ordinary post',
                  'platform': 'facebook'}]
        scan_mod.run(Ctx(), items=quiet, classify=False, submit=False)

        row = _rows()[0]
        assert row['why_flagged'] == ''
        assert row['is_hate_speech'] == 0

    def test_it_records_which_knowledge_decided(self, home):
        """So a disagreement between two runs can be traced to a curator's edit
        rather than argued about."""
        scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)

        versions = json.loads(_rows()[0]['versions'])
        assert versions.get('lexicon')

    def test_it_records_what_fired(self, home):
        scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)

        row = _rows()[0]
        assert json.loads(row['fired_terms'])

    def test_a_run_with_no_model_says_so(self, home):
        """`matched_only` is the tier that means there was no budget for a model
        call, which is a different thing from a model saying yes."""
        scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)

        assert _rows()[0]['tier'] == 'matched_only'

    def test_only_an_excerpt_is_kept(self, home):
        """This is a laptop on a residential connection. The full text of an
        attack on a named person does not need a second permanent home here."""
        long_comment = [{'text': 'x' * 900, 'parent_post_text': 'y' * 900,
                         'platform': 'facebook'}]
        scan_mod.run(Ctx(), items=long_comment, classify=False, submit=False)

        row = _rows()[0]
        assert len(row['excerpt']) <= 400
        assert len(row['parent_excerpt']) <= 200


class TestItSurvivesWithoutThePlatform:
    def test_a_judgement_is_readable_with_no_network(self, home):
        """The point of keeping it locally. Nothing in this test can reach a
        platform, and the record is complete anyway."""
        scan_mod.run(Ctx(), items=_items(2), classify=False, submit=False)

        rows = _rows()
        assert len(rows) == 2
        assert all(row['excerpt'] and row['created_at'] for row in rows)

    def test_the_table_does_not_depend_on_collected_item(self, home):
        """It used to hold only a foreign key into a table nothing writes, so a
        row here could not be displayed at all."""
        scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)

        row = _rows()[0]
        assert row['collected_item_id'] is None
        assert row['excerpt'], 'the row is displayable on its own'


# ---------------------------------------------------------------------------
# Opening a database an older build left behind.
#
# This shipped broken. `_TABLES` creates an index on classification(content_hash);
# on a database from an older build that column does not exist, so the whole
# script raised on the index, the connection never opened, and every page of the
# dashboard answered 500. The migration that would have fixed it ran after the
# statement that failed.
#
# Nobody had an old-shaped database in a test, so nothing caught it.
# ---------------------------------------------------------------------------

_OLD_CLASSIFICATION = """CREATE TABLE classification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_item_id INTEGER REFERENCES collected_item(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT '', severity INTEGER,
    reason TEXT NOT NULL DEFAULT '', fired_terms TEXT NOT NULL DEFAULT '[]',
    fired_tropes TEXT NOT NULL DEFAULT '[]', exemption_applied TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'matched_only', versions TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL)"""

_OLD_SEEN_ITEM = """CREATE TABLE seen_item (
    content_hash TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL, times_seen INTEGER NOT NULL DEFAULT 1)"""


@pytest.fixture
def installed_machine(home):
    """A database shaped the way an already-installed agent has it."""
    conn = schema.connect()
    conn.execute('DROP TABLE classification')
    conn.execute('DROP TABLE seen_item')
    conn.execute(_OLD_CLASSIFICATION)
    conn.execute(_OLD_SEEN_ITEM)
    conn.execute("INSERT INTO seen_item VALUES ('an-old-hash', 'then', 'then', 1)")
    conn.commit()
    conn.close()
    return home


class TestUpgradingAnExistingInstall:
    def test_the_database_opens_at_all(self, installed_machine):
        """The whole failure: it did not, and every dashboard page answered 500."""
        conn = schema.connect()
        assert conn.execute('SELECT 1').fetchone() is not None

    def test_the_tables_arrive_in_the_new_shape(self, installed_machine):
        conn = schema.connect()

        def columns(table):
            return {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}

        assert 'content_hash' in columns('classification')
        assert 'case_id' in columns('seen_item')

    def test_the_dashboard_can_read_it(self, installed_machine):
        from plugins.ettok.dashboard import plugin_api

        assert plugin_api.status() is not None
        assert plugin_api.judgements() is not None
        assert plugin_api.threads() is not None

    def test_a_run_still_works_afterwards(self, installed_machine):
        scan_mod.run(Ctx(), items=_items(1), classify=False, submit=False)

        assert len(_rows()) == 1

    def test_a_fresh_machine_is_unaffected(self, home):
        """The upgrade path must not be something a first install walks into."""
        conn = schema.connect()
        assert 'content_hash' in {
            row['name'] for row in conn.execute('PRAGMA table_info(classification)')
        }
