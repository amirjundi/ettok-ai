"""Four different answers had been arriving as two booleans.

From the architecture review of 19 September 2026 (ARCH-09, the half beyond the
strict boolean).

  * A curated rule fired and no model ran      -> reported is_hate_speech True
  * Nothing examined the comment at all        -> reported is_hate_speech False
  * A model read it and found hate speech      -> True
  * A model read it and cleared it             -> False

So "a rule matched" was indistinguishable from "a model concluded this person
wrote hate speech", and "nobody looked" was indistinguishable from "a
classifier read this and found nothing wrong". The platform then recorded
agreement and disagreement between its own model and an agent that, in half
those cases, had expressed no opinion at all -- and that disagreement rate is
the number meant to show when one side's rubric has drifted.

The prompt also told the model to answer false when it could not tell, which
turned every undecidable case into a clearance with a doubt written in the
reason where nothing reads it.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from unittest import mock

import pytest

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.classify import (
    STATE_MODEL_NEGATIVE, STATE_MODEL_POSITIVE, STATE_NEEDS_CONTEXT,
    STATE_NEEDS_VISUAL, STATE_NOT_ASSESSED, STATE_RULE_CANDIDATE,
    from_match_only, strict_bool,
)
from plugins.ettok.detect.match import Match


def matched():
    return Match(fired_terms=[{'id': 1, 'term': 'x', 'category': 'slur',
                               'severity_weight': 9}],
                 fired_tropes=[], ungated_tropes=[], topic_groups=['yazidi'])


def unmatched():
    return Match(fired_terms=[], fired_tropes=[], ungated_tropes=[], topic_groups=[])


def classify(reply):
    ctx = mock.MagicMock()
    ctx.llm.complete_structured.return_value = mock.MagicMock(parsed=reply)
    return classify_mod.classify(ctx, {'text': 't', 'parent_post_text': 'p'},
                                 matched(), versions={})


class TestARuleIsNotAVerdict:
    def test_a_match_with_no_model_claims_nothing(self):
        """It said True. A curated word firing is a reason to look, not a
        finding that a person wrote hate speech."""
        verdict = from_match_only(matched(), {})
        assert verdict.state == STATE_RULE_CANDIDATE
        assert verdict.is_hate_speech is None

    def test_nothing_examined_is_not_a_clearance(self):
        verdict = from_match_only(unmatched(), {})
        assert verdict.state == STATE_NOT_ASSESSED
        assert verdict.is_hate_speech is None

    def test_the_reason_still_explains_what_matched(self):
        """Losing the boolean must not lose the evidence for looking."""
        assert from_match_only(matched(), {}).reason


class TestAModelVerdictStillSaysYesOrNo:
    def test_positive(self):
        verdict = classify({'is_hate_speech': True, 'reason': 'r'})
        assert (verdict.state, verdict.is_hate_speech) == (STATE_MODEL_POSITIVE, True)

    def test_negative(self):
        verdict = classify({'is_hate_speech': False, 'reason': 'r'})
        assert (verdict.state, verdict.is_hate_speech) == (STATE_MODEL_NEGATIVE, False)


class TestUncertaintyHasSomewhereToGo:
    def test_needs_context_is_not_a_negative(self):
        """The prompt used to instruct exactly this case to answer false."""
        verdict = classify({'is_hate_speech': False, 'needs_context': True, 'reason': 'r'})
        assert verdict.state == STATE_NEEDS_CONTEXT
        assert verdict.is_hate_speech is None

    def test_waiting_on_an_image_is_not_a_clearance(self):
        verdict = classify({'is_hate_speech': False, 'requires_visual': True, 'reason': 'r'})
        assert verdict.state == STATE_NEEDS_VISUAL
        assert verdict.is_hate_speech is None

    def test_an_unparseable_verdict_is_undecided_not_negative(self):
        verdict = classify({'is_hate_speech': 'maybe', 'reason': 'r'})
        assert verdict.state == STATE_NEEDS_CONTEXT

    def test_the_prompt_no_longer_tells_it_to_answer_false(self):
        assert 'needs_context true' in classify_mod.WHAT_COUNTS
        assert 'say it is not hate speech and give your doubt' not in classify_mod.WHAT_COUNTS


class TestTheStrictReader:
    @pytest.mark.parametrize('value,want', [
        (True, True), (False, False), ('true', True), ('false', False),
        ('Yes', True), ('NO', False), (1, True), (0, False),
    ])
    def test_the_answers_it_accepts(self, value, want):
        assert strict_bool(value) is want

    @pytest.mark.parametrize('value', ['maybe', '', None, {'a': 1}, [], 7])
    def test_everything_else_is_no_answer(self, value):
        assert strict_bool(value) is None


def test_the_state_travels_to_the_platform():
    payload = from_match_only(matched(), {}).as_payload(matched())
    assert payload['state'] == STATE_RULE_CANDIDATE
    assert payload['is_hate_speech'] is None
