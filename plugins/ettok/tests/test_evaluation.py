"""Measuring against examples the classifier was never shown.

From the contextual implementation guide of 20 September 2026, section 13.

This produces a number, and almost every test here is about stopping that
number being read as more than it is.

Abstention is the one that matters most. A classifier answering "I cannot
tell" to every hard case and getting the easy ones right scores beautifully
and is useless -- so abstentions are never folded into accuracy, never counted
as wrong, and never counted as right. The guide says it plainly: "abstaining
on every hard example is not a successful detector."

And the held-out examples must never have been in a prompt. If they have, the
figure measures memory, and nothing about it looks wrong.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from unittest import mock

from plugins.ettok import evaluate as evaluate_mod
from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.platform.knowledge import Knowledge


def held_out(eid, outcome='hate_speech', **over):
    row = {
        'id': eid, 'revision': 1,
        'comment_text': f'comment {eid}',
        'parent_post_text': 'a post about Sinjar',
        'parent_post_unavailable': False,
        'reply_to_text': '', 'reply_to_author': '', 'ancestors': [],
        'media_description': '', 'media_is_derived': True,
        'expected_outcome': outcome, 'explanation': 'because',
        'language': 'ar', 'language_uncertain': False,
        'term_ids': [], 'trope_ids': [], 'target_groups': ['yazidi'],
        'case_id': None, 'pair_group': '', 'provenance': 'observed',
        'context_is_complete': True,
    }
    row.update(over)
    return row


def knowledge():
    return Knowledge(terms=[], tropes=[], cases=[])


def ctx_answering(*replies):
    ctx = mock.MagicMock()
    ctx.llm.complete_structured.side_effect = [
        mock.MagicMock(parsed=r, usage=mock.MagicMock(cost_usd=0.002,
                                                      total_tokens=50))
        for r in replies
    ]
    return ctx


HATE = {'is_hate_speech': True, 'reason': 'dehumanising'}
NOT_HATE = {'is_hate_speech': False, 'reason': 'ordinary'}
CANNOT_TELL = {'is_hate_speech': None, 'reason': 'who is "they"?',
               'needs_context': True}


class TestScoring:
    def test_a_correct_answer_counts(self):
        out = evaluate_mod.run(ctx_answering(HATE), [held_out(1)], knowledge())

        assert out['decided'] == 1 and out['correct'] == 1

    def test_a_wrong_answer_counts_against(self):
        out = evaluate_mod.run(ctx_answering(NOT_HATE), [held_out(1)], knowledge())

        assert out['decided'] == 1 and out['correct'] == 0

    def test_accuracy_is_over_decided_items_only(self):
        out = evaluate_mod.run(
            ctx_answering(HATE, NOT_HATE, CANNOT_TELL),
            [held_out(1), held_out(2), held_out(3)], knowledge())

        # Two decided, one right. The abstention is not a half-mark.
        assert out['decided'] == 2
        assert out['accuracy_on_decided'] == 0.5

    def test_the_two_kinds_of_error_are_kept_apart(self):
        """A missed attack costs one observation. A false positive costs the
        credibility of every finding beside it in a referral."""
        out = evaluate_mod.run(
            ctx_answering(NOT_HATE, HATE),
            [held_out(1, 'hate_speech'), held_out(2, 'not_hate_speech')],
            knowledge())

        assert out['missed_attacks'] == 1
        assert out['false_alarms'] == 1

    def test_they_are_not_averaged_in_the_sentence(self):
        out = evaluate_mod.run(
            ctx_answering(NOT_HATE), [held_out(1)], knowledge())

        assert 'should not be averaged' in out['summary']


class TestAbstentionIsNeverHiddenInTheScore:
    def test_it_is_counted_separately(self):
        out = evaluate_mod.run(ctx_answering(CANNOT_TELL), [held_out(1)],
                               knowledge())

        assert out['abstained'] == 1
        assert out['decided'] == 0

    def test_it_is_not_counted_as_wrong(self):
        out = evaluate_mod.run(ctx_answering(CANNOT_TELL), [held_out(1)],
                               knowledge())

        assert out['missed_attacks'] == 0 and out['false_alarms'] == 0

    def test_it_is_not_counted_as_right(self):
        out = evaluate_mod.run(ctx_answering(CANNOT_TELL), [held_out(1)],
                               knowledge())

        assert out['correct'] == 0

    def test_the_rate_is_reported_beside_the_accuracy(self):
        out = evaluate_mod.run(
            ctx_answering(HATE, CANNOT_TELL),
            [held_out(1), held_out(2)], knowledge())

        assert out['accuracy_on_decided'] == 1.0
        assert out['abstention_rate'] == 0.5

    def test_abstaining_on_everything_is_not_a_score(self):
        """It would otherwise read as no data rather than as a finding about
        the detector."""
        out = evaluate_mod.run(
            ctx_answering(CANNOT_TELL, CANNOT_TELL),
            [held_out(1), held_out(2)], knowledge())

        assert out['accuracy_on_decided'] is None
        assert 'is not a detector' in out['summary']


class TestContamination:
    def test_a_held_out_example_is_kept_out_of_the_guidance(self):
        """If it has been in a prompt, the figure measures memory."""
        example = held_out(7)
        out = evaluate_mod.run(ctx_answering(HATE), [example], knowledge(),
                               guidance=[example, held_out(8)])

        assert out['held_out_items_removed_from_guidance'] == 1

    def test_it_does_not_reach_the_prompt(self):
        example = held_out(7, comment_text='the held out words')
        ctx = ctx_answering(HATE)

        evaluate_mod.run(ctx, [example], knowledge(), guidance=[example])

        prompt = ctx.llm.complete_structured.call_args.kwargs['input'][0]['text']
        # It is the thing being judged, so it appears once -- as the comment,
        # never as a worked example telling the model the answer.
        assert 'WORKED EXAMPLES' not in prompt

    def test_ordinary_guidance_still_reaches_it(self):
        """The classifier should be doing the job it does in a real run."""
        ctx = ctx_answering(HATE)

        evaluate_mod.run(ctx, [held_out(1)], knowledge(),
                         guidance=[held_out(99, 'not_hate_speech')])

        prompt = ctx.llm.complete_structured.call_args.kwargs['input'][0]['text']
        assert 'WORKED EXAMPLES' in prompt


class TestWhatTheNumberIsNot:
    def test_the_sample_size_travels_with_it(self):
        """Nine examples and nine hundred look identical once they are a
        percentage on a slide."""
        out = evaluate_mod.run(ctx_answering(HATE), [held_out(1)], knowledge())

        assert out['scored'] == 1
        assert 'of 1 decided example(s)' in out['summary']

    def test_no_threshold_is_implied(self):
        out = evaluate_mod.run(ctx_answering(HATE), [held_out(1)], knowledge())

        assert 'No threshold is implied' in out['caveat']
        assert 'for the organisation to decide' in out['caveat']

    def test_it_says_it_is_not_accuracy_on_live_material(self):
        out = evaluate_mod.run(ctx_answering(HATE), [held_out(1)], knowledge())

        assert 'not accuracy on live material' in out['caveat']

    def test_the_release_it_measured_is_named(self):
        out = evaluate_mod.run(ctx_answering(HATE), [held_out(1)], knowledge())

        assert 'knowledge_versions' in out


class TestBreakdowns:
    def test_results_are_split_by_language(self):
        """Coverage of Arabic, Kurdish and Syriac is not interchangeable, and
        an average over all three hides whichever is worst."""
        out = evaluate_mod.run(
            ctx_answering(HATE, NOT_HATE),
            [held_out(1, language='ar'), held_out(2, language='ku')],
            knowledge())

        assert out['by_language']['ar']['correct'] == 1
        assert out['by_language']['ku']['correct'] == 0

    def test_an_unstated_language_is_named_rather_than_dropped(self):
        out = evaluate_mod.run(ctx_answering(HATE), [held_out(1, language='')],
                               knowledge())

        assert 'unknown' in out['by_language']


class TestFailuresAreNotWrongAnswers:
    def test_a_provider_error_is_counted_as_an_error(self):
        """Counting it as a wrong answer makes an outage look like a quality
        problem and sends somebody tuning prompts to fix a network."""
        ctx = mock.MagicMock()
        ctx.llm.complete_structured.side_effect = RuntimeError('502')

        out = evaluate_mod.run(ctx, [held_out(1)], knowledge())

        assert out['errors'] == 1
        assert out['decided'] == 0 and out['correct'] == 0

    def test_the_budget_stops_it_and_says_so(self):
        out = evaluate_mod.run(
            ctx_answering(HATE, HATE, HATE),
            [held_out(1), held_out(2), held_out(3)], knowledge(),
            max_spend_usd=0.003)

        assert out['stopped'] == 'budget'
        assert out['attempted'] < out['offered']

    def test_nothing_offered_is_handled(self):
        out = evaluate_mod.run(ctx_answering(), [], knowledge())

        assert out['summary'] == 'Nothing was scored.'
