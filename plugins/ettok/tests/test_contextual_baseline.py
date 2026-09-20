"""Every collected comment gets read, not only the ones with a word on a list.

From the contextual implementation guide of 20 September 2026 (section 3).

The lexicon was the gate: `if not result.matched: continue` sent an unmatched
comment straight to storage with `not_assessed` on it and never showed it to a
model. That is exactly backwards for the two failures this system has to catch.

  "They should all go back where they came from, this is not their land."

matches nothing in a lexicon of slurs, and is the attack. Meanwhile a journalist
quoting a slur matches, and is not. A dictionary hit is a reason to read
something first, not a licence to read nothing else.

What replaces it is not "assess everything at any price": the budget still
stops, and what the budget stops stays visibly unassessed rather than becoming a
clearance. Matched items are assessed first, so a run that runs out of money
runs out of it on the least likely items rather than on the most likely.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.detect import classify as classify_mod


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


class FakeKnowledge:
    """One explicit term, one case about Yazidis. Nothing else on the list."""

    versions = {'lexicon': '1@aaa', 'release': 'rel-1'}
    age_seconds = 0.0

    def __init__(self, limits=None):
        self.terms = [{
            'id': 12, 'term': 'devil worshippers', 'is_explicit': True,
            'is_regex': False, 'target_group_slug': 'yazidi', 'severity_weight': 8,
            'category': 'dehumanization', 'variants': [], 'never_flag_when': [],
        }]
        self.tropes = []
        self.cases = [{
            'id': 1, 'title': 'Sinjar anniversary', 'state': 'active',
            'target_groups': [{'slug': 'yazidi', 'topic_markers': ['sinjar'],
                               'background': 'b'}],
            'seed_sources': [{'kind': 'page', 'value': 'X'}],
            'limits': limits or {'items_remaining': None, 'cost_remaining_usd': None},
            'suggests_closing': False,
        }]

    def group_markers(self):
        return {'yazidi': ['sinjar']}

    def tropes_for(self, slug):
        return self.tropes


class Ctx:
    """A model that answers, and a record of what it was asked."""

    def __init__(self, reply=None, knowledge=None):
        self._ettok_knowledge = knowledge or FakeKnowledge()
        self.llm = mock.MagicMock()
        self.llm.complete_structured.return_value = mock.MagicMock(
            parsed=reply or {'is_hate_speech': False, 'reason': 'ordinary speech'},
            usage=mock.MagicMock(cost_usd=0.001, total_tokens=100),
        )

    def get_config(self, key, default=None):
        return default

    @property
    def prompts(self):
        return [c.kwargs['input'][0]['text']
                for c in self.llm.complete_structured.call_args_list]


UNDER_A_POST = 'A post about sinjar and the anniversary'

# Matches nothing on the list, and is the thing the list exists to find.
EXCLUSION = {'text': 'They should all go back where they came from, this is '
                     'not their land and never was.',
             'parent_post_text': UNDER_A_POST,
             'url': 'https://facebook.com/a', 'platform': 'facebook'}

HARMLESS = {'text': 'Beautiful photos, thank you for posting these.',
            'parent_post_text': UNDER_A_POST,
            'url': 'https://facebook.com/b', 'platform': 'facebook'}

MATCHED = {'text': 'they are devil worshippers',
           'parent_post_text': UNDER_A_POST,
           'url': 'https://facebook.com/c', 'platform': 'facebook'}


def judgements():
    """What the agent wrote down about each comment, in the order it read them.

    Read from the local store rather than the run summary: the summary is a
    tool return that an orchestration model has to carry, and the guide is
    explicit that the evidence payload must not travel that way.
    """
    from plugins.ettok.store import schema
    conn = schema.connect()
    return [dict(r) for r in conn.execute(
        'SELECT excerpt, state, why_flagged, fired_terms FROM classification '
        'ORDER BY id')]


def states():
    return [r['state'] for r in judgements()]


class TestTheGateIsGone:
    def test_an_exclusionary_sentence_with_no_listed_term_is_assessed(self, home):
        ctx = Ctx({'is_hate_speech': True, 'reason': 'calls for expulsion',
                   'category': 'exclusion', 'severity': 7})
        result = scan_mod.run(ctx, items=[EXCLUSION], submit=False)

        assert result['classified'] == 1, 'no model read it'
        assert states() == [classify_mod.STATE_MODEL_POSITIVE]

    def test_a_harmless_comment_is_cleared_rather_than_ignored(self, home):
        """A model negative and "nobody looked" are different answers, and the
        difference is the whole denominator."""
        scan_mod.run(Ctx(), items=[HARMLESS], submit=False)

        assert states() == [classify_mod.STATE_MODEL_NEGATIVE]

    def test_no_match_is_invented_to_justify_reading_it(self, home):
        """The item is assessed on the policy, not by pretending a term fired."""
        result = scan_mod.run(Ctx(), items=[HARMLESS], submit=False)

        assert result['flagged'] == 0
        assert judgements()[0]['why_flagged'] == ''
        assert judgements()[0]['fired_terms'] == '[]'

    def test_the_prompt_says_nothing_matched(self, home):
        """Silence about the lexicon reads as "the lexicon was not consulted".
        The model is told it was, and found nothing -- which is information."""
        ctx = Ctx()
        scan_mod.run(ctx, items=[HARMLESS], submit=False)

        assert 'NO CURATED TERM OR PATTERN MATCHED' in ctx.prompts[0]


class TestTheBudgetStillStops:
    def test_what_the_budget_did_not_reach_stays_unassessed(self, home):
        """Never a negative. An item nobody paid to read is not a cleared one."""
        know = FakeKnowledge(limits={'items_remaining': None,
                                     'cost_remaining_usd': 0.001})
        result = scan_mod.run(Ctx(knowledge=know),
                              items=[HARMLESS, EXCLUSION], submit=False)

        assert result['classified'] == 1
        assert sorted(states()) == [classify_mod.STATE_MODEL_NEGATIVE,
                                    classify_mod.STATE_NOT_ASSESSED]

    def test_the_evidence_is_kept_anyway(self, home):
        """Collection is the half that cannot be redone later."""
        know = FakeKnowledge(limits={'items_remaining': None,
                                     'cost_remaining_usd': 0.0})
        result = scan_mod.run(Ctx(knowledge=know),
                              items=[HARMLESS, EXCLUSION], submit=False)

        assert states() == [classify_mod.STATE_NOT_ASSESSED] * 2
        assert result['classified'] == 0
        assert result['scanned'] == 2

    def test_matched_items_are_assessed_before_the_rest(self, home):
        """Lexical matching stopped being the gate and became the queue order.

        With money for one call and the match arriving last, the old order
        would spend it on the first comment in the batch. The list is a
        priority, not a permission.
        """
        know = FakeKnowledge(limits={'items_remaining': None,
                                     'cost_remaining_usd': 0.001})
        ctx = Ctx(knowledge=know)
        scan_mod.run(ctx, items=[HARMLESS, EXCLUSION, MATCHED], submit=False)

        assert len(ctx.prompts) == 1
        assert 'devil worshippers' in ctx.prompts[0]


class TestNothingElseMoved:
    def test_a_run_with_classification_off_assesses_nothing(self, home):
        """`classify=False` is the offline path: selectors, a comment by hand."""
        ctx = Ctx()
        result = scan_mod.run(ctx, items=[MATCHED, HARMLESS],
                              classify=False, submit=False)

        assert ctx.prompts == []
        assert result['flagged'] == 1
        assert len(judgements()) == 2

    def test_a_matched_item_still_reports_why(self, home):
        ctx = Ctx({'is_hate_speech': True, 'reason': 'dehumanising'})
        result = scan_mod.run(ctx, items=[MATCHED], submit=False)

        assert result['flagged'] == 1
        assert 'devil worshippers' in judgements()[0]['why_flagged']
