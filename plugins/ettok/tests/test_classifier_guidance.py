"""What the classifier is actually told.

The curators' work lived in the platform database, was fetched by this agent on
every run, and was then dropped before the model saw it: the prompt carried a
trope's *name* and nothing else. So the model was told that a pattern called
"Identity stripping" had fired and never what that means, while the platform
classifier judging the same comment had the full description, the attested
example and the benign uses. Two classifiers working from different rules
disagree, and the agent was the one guessing.

There was also no written standard anywhere -- the model was told it classified
hate speech for a monitoring system in Iraq and left to supply its own
definition of the term.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect import match as match_mod

TROPE = {
    'id': 1,
    'name': 'Devil-worship libel',
    'description': 'Asserting that Yazidis worship the devil.',
    'example': 'they are devil worshippers',
    'surface_forms': ['devil worshippers'],
    'activation_topics': ['sinjar'],
    'negative_examples': ['I seek refuge in God from the accursed devil'],
    'counter_speech_examples': ['Do not call them devil worshippers, it is a lie'],
    'severity_weight': 8,
    'requires_target_group': True,
    'target_group_slug': 'yazidi',
}

COMMENT = {'text': 'they are devil worshippers', 'parent_post_text': 'sinjar anniversary'}


def _match(trope=None, **over):
    fired = match_mod.match_tropes(
        COMMENT['text'], [trope or TROPE],
        groups_in_context=['yazidi'], parent_text=COMMENT['parent_post_text'],
    )
    return match_mod.Match(
        fired_tropes=[t for t in fired if not t.get('ungated')],
        ungated_tropes=[t for t in fired if t.get('ungated')],
        topic_groups=['yazidi'],
        **over,
    )


class TestTheCuratorsWorkReachesTheModel:
    def test_the_description_is_in_the_prompt(self):
        """Its help text on the platform says "what the classifier should look
        for". It reached the agent and stopped there."""
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'Asserting that Yazidis worship the devil' in prompt

    def test_the_attested_example_is_in_the_prompt(self):
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'attested example' in prompt

    def test_counter_speech_is_in_the_prompt(self):
        """Shipped by the platform and read by nothing, on either side. It is
        the commonest false positive: somebody arguing against the attack, in
        language that looks exactly like the attack."""
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'Do not call them devil worshippers' in prompt
        assert 'somebody opposing it' in prompt

    def test_benign_uses_are_still_there(self):
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'NEVER flag this use' in prompt


class TestAnUngatedPatternIsGuidanceNotEvidence:
    """`match_tropes` has always documented that these "reach the classifier as
    guidance". They were filtered out one function later and reached nothing."""

    def _ungated(self):
        trope = dict(TROPE, activation_topics=[])
        return _match(trope)

    def test_it_does_not_count_as_a_finding(self):
        assert self._ungated().fired_tropes == []

    def test_but_the_model_is_told_about_it(self):
        prompt = classify_mod.build_prompt(COMMENT, self._ungated())
        assert 'Asserting that Yazidis worship the devil' in prompt

    def test_and_told_it_is_not_grounds_on_its_own(self):
        prompt = classify_mod.build_prompt(COMMENT, self._ungated())
        assert 'never grounds on their own' in prompt


class TestTheStandardIsWrittenDown:
    def test_the_prompt_says_what_counts(self):
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'WHAT COUNTS AS HATE SPEECH HERE' in prompt

    def test_and_what_does_not(self):
        """Reporting, study, refutation and reclaiming are not attacks, and a
        package that turns out to contain a journalist quoting a slur costs more
        than every attack missed that week."""
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'WHAT DOES NOT COUNT' in prompt
        assert 'refuting hate speech' in prompt

    def test_criticism_of_a_politician_is_excluded(self):
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'Attacking a politician is not attacking their community' in prompt

    def test_uncertainty_resolves_to_not_hate_speech(self):
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'WHEN YOU CANNOT TELL' in prompt

    def test_severity_has_a_scale_rather_than_a_number_out_of_ten(self):
        prompt = classify_mod.build_prompt(COMMENT, _match())
        assert 'SEVERITY:' in prompt
        assert 'call for violence' in prompt


class TestAMatchIsNotAVerdict:
    def test_the_model_is_told_a_term_hit_is_only_why_it_is_reading(self):
        """A lexicon hit is why the comment was read, not the answer. The prompt
        listed the terms and left the model to infer their standing."""
        match = match_mod.Match(
            fired_terms=[{'term': 'x', 'category': 'slur', 'severity_weight': 7,
                          'is_explicit': True}],
            topic_groups=['yazidi'],
        )
        prompt = classify_mod.build_prompt(COMMENT, match)
        assert 'not by itself the answer' in prompt
