"""The standard comes from the platform, and says so when it does not.

From the contextual implementation guide of 20 September 2026, section 2:
"Full inference policy lives in `detect/classify.py` as `WHAT_COUNTS` ... Move
the curated policy into the versioned contract."

Everything around the definition was curated already -- the terms, the tropes,
the exemptions, the case background. The definition was a constant in this
repository, changeable only by a developer and a redeployment, and it is the
thing that decides what all the rest means.

Two properties here, and the second is the one that gets forgotten. The
published wording is used when there is one. And when there is not, the prompt
says so out loud, so a verdict formed against the built-in wording is not
later mistaken for one formed against the organisation's own.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from unittest import mock

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.match import Match
from plugins.ettok.platform.knowledge import Knowledge, assessment_id

PUBLISHED = 'WHAT COUNTS HERE: only what the Duhok survey recorded.'


def nothing_matched():
    return Match(fired_terms=[], fired_tropes=[], ungated_tropes=[],
                 topic_groups=['yazidi'])


class TestThePromptUsesWhatWasPublished:
    def test_the_published_wording_replaces_the_built_in_one(self):
        built = classify_mod.build_prompt(
            {'text': 't'}, nothing_matched(), policy=PUBLISHED)

        assert PUBLISHED in built
        assert 'WHAT DOES NOT COUNT, however offensive' not in built

    def test_without_one_the_built_in_wording_is_used(self):
        built = classify_mod.build_prompt({'text': 't'}, nothing_matched())

        assert 'WHAT COUNTS AS HATE SPEECH HERE' in built

    def test_and_the_prompt_admits_it(self):
        """A verdict formed against the tool's own wording must not be
        mistaken for one formed against the organisation's. The difference is
        what an auditor asks about."""
        built = classify_mod.build_prompt({'text': 't'}, nothing_matched())

        assert "built-in wording" in built

    def test_a_published_one_carries_no_such_note(self):
        built = classify_mod.build_prompt(
            {'text': 't'}, nothing_matched(), policy=PUBLISHED)

        assert 'built-in wording' not in built

    def test_the_severity_scale_is_publishable_too(self):
        """Two runs on different scales produce severities that cannot be
        compared, and severity is what sorts the review queue."""
        built = classify_mod.build_prompt(
            {'text': 't'}, nothing_matched(),
            severity_guidance='SEVERITY: 1 is worst, 10 is nothing.')

        assert '1 is worst' in built
        assert '9-10 a direct threat' not in built


class TestCategoriesAreCheckedAgainstTheList:
    def verdict(self, category, allowed):
        ctx = mock.MagicMock()
        ctx.llm.complete_structured.return_value = mock.MagicMock(
            parsed={'is_hate_speech': True, 'reason': 'r', 'category': category},
            usage=mock.MagicMock(cost_usd=0.001, total_tokens=10))
        return classify_mod.classify(ctx, {'text': 't'}, nothing_matched(),
                                     versions={}, allowed_categories=allowed)

    def test_a_curated_category_is_kept(self):
        assert self.verdict('dehumanization', ['dehumanization']).category \
            == 'dehumanization'

    def test_an_invented_one_is_discarded(self):
        """It would store a label no screen filters on and no export
        understands, which reads downstream as a category with one member
        rather than as a mistake."""
        assert self.verdict('vibes', ['dehumanization']).category == ''

    def test_the_verdict_itself_survives_the_discard(self):
        """The category is a label on the finding, not the finding."""
        assert self.verdict('vibes', ['dehumanization']).is_hate_speech is True

    def test_an_empty_allowlist_checks_nothing(self):
        """The server did not say. Checking against an empty list would reject
        every category there is."""
        assert self.verdict('dehumanization', []).category == 'dehumanization'


class TestItIsPartOfTheRelease:
    def knowledge(self, **over):
        fields = {'terms': [], 'tropes': [], 'cases': []}
        fields.update(over)
        return Knowledge(**fields)

    def test_a_changed_policy_changes_the_versions(self):
        a = self.knowledge(policy={'body': 'one'})
        b = self.knowledge(policy={'body': 'two'})

        assert a.versions['policy'] != b.versions['policy']

    def test_and_therefore_the_assessment_identity(self):
        """A job keyed without it looks already done after a curator rewrites
        the definition, and the corrected reading is never produced."""
        a = self.knowledge(policy={'body': 'one'})
        b = self.knowledge(policy={'body': 'two'})

        assert assessment_id(a.versions) != assessment_id(b.versions)

    def test_an_unchanged_policy_does_not(self):
        a = self.knowledge(policy={'body': 'one'})
        b = self.knowledge(policy={'body': 'one'})

        assert assessment_id(a.versions) == assessment_id(b.versions)

    def test_a_prompt_change_is_a_new_assessment_too(self, monkeypatch):
        """The same comment under the same lexicon gets a different answer
        when the wording around it moves, and a reviewer comparing two runs
        has to be able to see that something did."""
        know = self.knowledge(policy={'body': 'one'})
        before = assessment_id(know.versions)

        monkeypatch.setattr(classify_mod, 'PROMPT_VERSION', 'a-later-date')

        assert assessment_id(know.versions) != before


class TestReadingTheBundle:
    def test_an_older_server_leaves_it_empty_rather_than_absent(self):
        """The two halves release separately. A server without the field means
        the built-in wording is used, and the prompt says so -- it does not
        mean the organisation published nothing on purpose."""
        know = Knowledge(terms=[], tropes=[], cases=[])

        assert know.policy_text == ''
        assert know.allowed_categories == []

    def test_the_slugs_come_out_sorted_and_deduplicated(self):
        know = Knowledge(categories=[{'slug': 'slur'}, {'slug': 'threat'},
                                     {'slug': 'slur'}, {'slug': '  '}])

        assert know.allowed_categories == ['slur', 'threat']
