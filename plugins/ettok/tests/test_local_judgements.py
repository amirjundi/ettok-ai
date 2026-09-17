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
