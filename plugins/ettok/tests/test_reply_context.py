"""What a reply is answering, kept, and kept separate.

From the contextual implementation guide of 20 September 2026, sections 6 and
13. Collection recorded `is_reply` as a boolean, which is the one fact about a
reply that does not help.

  "Exactly."

under "they should all be driven out of Sinjar", and

  "Exactly."

under "they have every right to live where their families always have", are the
same word. What separates them is the comment directly above, which was thrown
away at extraction and reconstructed by nobody. Judging the second as an attack
accuses a person of the opposite of what they said -- and that person is
usually the one defending the community this system exists to protect.

Three levels, labelled and kept apart: the comment being judged, the comment it
answers, the post underneath it all. Flattening them into one paragraph is how
a quotation becomes an endorsement.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.match import Match


@pytest.fixture
def home(monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(Path(tempfile.mkdtemp())))


def nothing_matched():
    return Match(fired_terms=[], fired_tropes=[], ungated_tropes=[],
                 topic_groups=['yazidi'])


POST = 'Marking the anniversary at Sinjar today'
EXCLUSION = 'they should all be driven out of Sinjar, this was never their place'
DEFENCE = 'they have every right to live where their families always have'


def prompt(**item):
    base = {'text': 'Exactly.', 'parent_post_text': POST}
    base.update(item)
    return classify_mod.build_prompt(base, nothing_matched())


class TestTheDirectParentSurvives:
    def test_it_reaches_the_prompt(self):
        assert EXCLUSION in prompt(reply_to_text=EXCLUSION)

    def test_the_same_reply_carries_different_context(self):
        """The whole point. Two prompts, one word of difference in the comment
        being judged -- none -- and opposite evidence beneath it."""
        attacking = prompt(reply_to_text=EXCLUSION)
        defending = prompt(reply_to_text=DEFENCE)

        assert EXCLUSION in attacking and EXCLUSION not in defending
        assert DEFENCE in defending and DEFENCE not in attacking

    def test_who_wrote_it_is_named(self):
        built = prompt(reply_to_text=EXCLUSION, reply_to_author='Someone Else')

        assert 'Someone Else' in built

    def test_the_author_is_told_it_is_not_their_words(self):
        """Otherwise the parent reads as part of what this person wrote, and a
        reply to an attack becomes the attack."""
        built = prompt(reply_to_text=EXCLUSION)

        assert 'written by other people' in built

    def test_the_three_levels_stay_apart(self):
        built = prompt(reply_to_text=EXCLUSION)

        assert 'THE COMMENT TO JUDGE:' in built
        assert 'IT IS A REPLY TO THIS COMMENT' in built
        assert 'ALL OF IT SITS UNDER A POST THAT SAYS:' in built
        assert built.index('THE COMMENT TO JUDGE:') < built.index('IT IS A REPLY')


class TestTheChainAboveIt:
    def test_ancestors_are_included(self):
        built = prompt(reply_to_text='And the year before that',
                       ancestors=[{'author_name': 'A', 'text': 'It happened in 2014'}])

        assert 'It happened in 2014' in built

    def test_only_a_few_of_them(self):
        """Context thins with distance and the budget does not. The direct
        parent is never cut; the far end is."""
        deep = [{'author_name': 'x', 'text': f'ancestor number {n}'} for n in range(9)]
        built = prompt(reply_to_text='near', ancestors=deep)

        assert 'ancestor number 0' in built
        assert 'ancestor number 8' not in built


class TestAMissingParentIsNotAnAbsentOne:
    def test_a_reply_whose_parent_was_not_read_says_so(self):
        """A short reply with no parent is undecidable, and the model has to be
        told that rather than left to read it as a top-level comment."""
        built = prompt(is_reply=True)

        assert 'COULD NOT BE READ' in built
        assert 'needs_context' in built

    def test_a_top_level_comment_claims_no_missing_parent(self):
        built = prompt()

        assert 'COULD NOT BE READ' not in built


class TestCollection:
    """The Python that reads what the extractor returns. The JS itself is
    covered by tests/extract_check.js under jsdom."""

    def test_the_chain_comes_off_the_page(self, home):
        from plugins.ettok.tests.test_collect import FakeCtx, NoWait
        from plugins.ettok.collect import base as collect_mod

        ctx = FakeCtx(page_text='ordinary page', extracted={
            'parent_post_text': POST,
            'comments': [{
                'text': 'Exactly.', 'author_name': 'B', 'is_reply': True,
                'reply_to_text': EXCLUSION, 'reply_to_author': 'A',
                'reply_depth': 2,
                'ancestors': [{'author_name': 'C', 'text': 'older still'}],
            }],
        })
        result = collect_mod.FacebookCollector(
            ctx, pacer=NoWait()).collect('https://facebook.com/p')

        item = result.items[0]
        assert item['reply_to_text'] == EXCLUSION
        assert item['reply_to_author'] == 'A'
        assert item['reply_depth'] == 2
        assert item['ancestors'] == [{'author_name': 'C', 'text': 'older still'}]

    def test_an_extractor_that_sends_none_of_it_still_works(self, home):
        """The two halves release separately, and a collector built before
        this returns comments without a chain."""
        from plugins.ettok.tests.test_collect import FakeCtx, NoWait
        from plugins.ettok.collect import base as collect_mod

        ctx = FakeCtx(page_text='ordinary page', extracted={
            'parent_post_text': POST,
            'comments': [{'text': 'Exactly.', 'author_name': 'B'}],
        })
        result = collect_mod.FacebookCollector(
            ctx, pacer=NoWait()).collect('https://facebook.com/p')

        assert result.items[0]['reply_to_text'] == ''
        assert result.items[0]['reply_depth'] == 0
