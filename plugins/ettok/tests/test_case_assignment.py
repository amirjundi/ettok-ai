"""Work belongs to a case, or it is refused.

From the architecture review of 19 September 2026 (ARCH-04, the agent half).

Two ways a run lost its case without anyone being told:

`scan.run` guarded the implicit path -- no case due, nothing collected -- but
an explicitly requested id that named no available case fell through both
branches. `pick` returned None, `case_id` was not None, and the run collected a
whole page of observations attached to no case: absent from the case screen,
outside every budget, indistinguishable from general monitoring. The review
reproduced exactly this with `case_id=999`.

And the helper tools took no case at all, so they answered with general
vocabulary while a scan of the same comment applied the episode's own terms as
well. An operator asking "why did this not flag" was answered by a different
rulebook than the one that judged it.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.platform.knowledge import Knowledge

CASE = {
    'id': 5, 'title': 'Sinjar watch', 'state': 'active', 'due': True,
    'target_groups': [{'slug': 'yazidi', 'name_en': 'Yazidi',
                       'topic_markers': ['sinjar'], 'background': ''}],
    'limits': {'deadline_at': None, 'items_remaining': None,
               'cost_remaining_usd': None},
}

ITEM = {'text': 'they are devil worshippers', 'parent_post_text': 'sinjar anniversary',
        'platform': 'facebook', 'url': 'https://x.test/p/1#c1'}


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


@pytest.fixture
def ctx(home):
    holder = mock.MagicMock()
    holder._ettok_knowledge = Knowledge(terms=[], tropes=[], cases=[CASE])
    return holder


def run(ctx, **over):
    kwargs = {'items': [dict(ITEM)], 'classify': False, 'submit': False}
    kwargs.update(over)
    with mock.patch('plugins.ettok.config.load', return_value={}), \
         mock.patch('plugins.ettok.platform.client.PlatformClient'):
        return scan_mod.run(ctx, **kwargs)


class TestAnUnknownCaseStopsTheRun:
    def test_it_collects_nothing(self, ctx):
        """The review's own probe: case_id=999 alongside a real case 5."""
        summary = run(ctx, case_id=999)
        assert summary['stop_reason'] == 'unknown_case'
        assert summary['scanned'] == 0

    def test_it_says_which_id_and_why(self, ctx):
        """A typo and a case that finished are different problems and the
        operator has to be able to tell which one this is."""
        summary = run(ctx, case_id=999)
        assert '999' in summary['note']

    def test_a_real_case_still_runs(self, ctx):
        summary = run(ctx, case_id=5)
        assert summary['stop_reason'] != 'unknown_case'
        assert summary['scanned'] == 1

    def test_no_case_asked_for_is_not_an_error(self, ctx):
        """Working the rota is the normal path, and case 5 is due."""
        summary = run(ctx)
        assert summary['stop_reason'] != 'unknown_case'


class TestTheHelpersUseTheSameCaseContext:
    """`ettok_match`, `ettok_classify` and `ettok_explain` answer questions an
    operator asks about a comment a scan has already seen."""

    def tools(self, ctx):
        from plugins.ettok import _make_tools
        return {spec['name']: handler for _key, spec, handler, _icon in _make_tools(ctx)}

    def test_match_accepts_a_case(self, ctx):
        out = json.loads(self.tools(ctx)['ettok_match']({'text': 'anything', 'case_id': 5}))
        assert 'error' not in out

    def test_match_refuses_an_unknown_case(self, ctx):
        out = json.loads(self.tools(ctx)['ettok_match']({'text': 'anything', 'case_id': 999}))
        assert '999' in out['error']

    def test_explain_refuses_one_too(self, ctx):
        out = json.loads(self.tools(ctx)['ettok_explain']({'text': 'anything', 'case_id': 999}))
        assert 'error' in out

    def test_a_non_numeric_case_is_refused_not_ignored(self, ctx):
        """Ignoring it would answer with the wrong rulebook and look certain."""
        out = json.loads(self.tools(ctx)['ettok_match']({'text': 'x', 'case_id': 'five'}))
        assert 'error' in out

    def test_omitting_the_case_is_still_allowed(self, ctx):
        """General vocabulary only, which is the right answer to a question
        asked without a case."""
        out = json.loads(self.tools(ctx)['ettok_match']({'text': 'anything'}))
        assert 'error' not in out

    def test_every_helper_advertises_the_argument(self, ctx):
        """An argument no caller can discover is one no caller will pass."""
        specs = {spec['name']: spec for _k, spec, _h, _i in _tool_specs(ctx)}
        for name in ('ettok_match', 'ettok_classify', 'ettok_explain'):
            assert 'case_id' in specs[name]['parameters']['properties'], name


def _tool_specs(ctx):
    from plugins.ettok import _make_tools
    return _make_tools(ctx)
