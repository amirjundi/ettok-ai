"""Saying what is missing, by name, so somebody can go and get it.

From the contextual implementation guide of 20 September 2026, sections 9 and
4.2.

`needs_context` with the reason in prose is unactionable. Something has to read
it and decide what to fetch, and nothing does that reliably from free text -- so
in practice the state meant "a person will look at this eventually", which is
the same as not having the state.

Naming the gap is what makes retrieval possible, and naming it honestly is what
stops a retrieval pass spending money looking for something nothing can fetch:
a missing reply is on the page, an unresolved event is a curator's note.

The second half is `negation_cancels`. The platform has shipped that flag since
the beginning and nothing has ever read it. The reading its name invites --
contains a negation, therefore harmless -- is the one thing the guide forbids
outright, because a quotation endorses as often as it condemns and "no Yazidi
belongs in this town" is a negation carrying the whole attack.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from unittest import mock

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.match import Match


def nothing_matched():
    return Match(fired_terms=[], fired_tropes=[], ungated_tropes=[],
                 topic_groups=['yazidi'])


def verdict(reply, item=None, match=None):
    ctx = mock.MagicMock()
    ctx.llm.complete_structured.return_value = mock.MagicMock(
        parsed=reply, usage=mock.MagicMock(cost_usd=0.001, total_tokens=50))
    return classify_mod.classify(
        ctx, item or {'text': 'Exactly.', 'parent_post_text': 'a post'},
        match or nothing_matched(), versions={})


class TestNamingWhatIsMissing:
    def test_the_model_can_name_it(self):
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'needs_context': True, 'missing': ['reply']})

        assert out.missing == [classify_mod.MISSING_REPLY]

    def test_several_at_once(self):
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'needs_context': True,
                       'missing': ['reply', 'referent']})

        assert out.missing == sorted(
            [classify_mod.MISSING_REPLY, classify_mod.MISSING_REFERENT])

    def test_an_invented_kind_is_dropped(self):
        """It would send the retrieval pass looking for something nothing can
        fetch, and then report failure for a request nobody made."""
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'needs_context': True, 'missing': ['vibes', 'reply']})

        assert out.missing == [classify_mod.MISSING_REPLY]

    def test_a_bare_needs_context_is_still_given_a_name(self):
        """An empty list reads as "nothing is missing", which is the opposite
        of what the model just said."""
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'needs_context': True},
                      item={'text': 'Exactly.', 'parent_post_text': ''})

        assert classify_mod.MISSING_PARENT_POST in out.missing

    def test_a_reply_with_no_parent_comment_is_inferred(self):
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'needs_context': True},
                      item={'text': 'Exactly.', 'parent_post_text': 'a post',
                            'is_reply': True, 'reply_to_text': ''})

        assert classify_mod.MISSING_REPLY in out.missing

    def test_an_unread_image_names_itself(self):
        """The model cannot know that the description it was given is absent
        rather than of an image with nothing in it."""
        out = verdict({'is_hate_speech': False, 'reason': 'r'},
                      item={'text': 't', 'parent_post_text': 'p',
                            'parent_media_unread': True})

        assert out.state == classify_mod.STATE_NEEDS_VISUAL
        assert classify_mod.MISSING_MEDIA in out.missing


class TestNothingToFetchMeansNothingIsAsked:
    def test_a_decided_verdict_asks_for_nothing(self):
        out = verdict({'is_hate_speech': True, 'reason': 'r'})

        assert out.missing == []

    def test_a_cleared_one_asks_for_nothing(self):
        out = verdict({'is_hate_speech': False, 'reason': 'r'})

        assert out.missing == []

    def test_an_uncertain_verdict_asks_for_nothing(self):
        """`uncertain` means everything arrived and the words still carry two
        readings. A retrieval pass on it spends money learning what it knows."""
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'unresolved': True})

        assert out.state == classify_mod.STATE_UNCERTAIN
        assert out.missing == []


class TestSomeOfItCanBeFetchedAndSomeCannot:
    def test_the_page_can_be_looked_at_again(self):
        for kind in (classify_mod.MISSING_PARENT_POST,
                     classify_mod.MISSING_REPLY,
                     classify_mod.MISSING_MEDIA):
            assert kind in classify_mod.RECOVERABLE

    def test_curator_work_is_not_a_retrieval(self):
        """An unresolved event is a note somebody has to write. Sending a
        browser after it spends money and finds nothing."""
        for kind in (classify_mod.MISSING_REFERENT, classify_mod.MISSING_EVENT):
            assert kind not in classify_mod.RECOVERABLE


class TestTheRequestTravels:
    def test_it_is_in_the_payload(self):
        out = verdict({'is_hate_speech': None, 'reason': 'r',
                       'needs_context': True, 'missing': ['reply']})

        assert out.as_payload(nothing_matched())['missing'] == ['reply']

    def test_the_context_revision_travels_with_it(self):
        """A reassessment after fetching is a new reading of new evidence, not
        a retry of the old one, and the number is what says so."""
        out = verdict({'is_hate_speech': True, 'reason': 'r'},
                      item={'text': 't', 'parent_post_text': 'p',
                            'context_revision': 2})

        assert out.as_payload(nothing_matched())['context_revision'] == 2

    def test_the_prompt_asks_for_names_not_prose(self):
        built = classify_mod.build_prompt({'text': 't'}, nothing_matched())

        assert 'parent_post, reply, media, referent, event' in built


class TestNegationIsACueNotARule:
    def trope(self, **over):
        row = {'id': 1, 'name': 'Devil-worship libel',
               'activation_reason': 'the post concerns "yazidi"',
               'description': 'd', 'negation_cancels': False,
               'example': '', 'negative_examples': [],
               'counter_speech_examples': []}
        row.update(over)
        return row

    def lines(self, **over):
        return '\n'.join(classify_mod._trope_lines(self.trope(**over)))

    def test_a_flagged_trope_asks_about_stance(self):
        assert 'rejecting it' in self.lines(negation_cancels=True)

    def test_it_never_says_a_negation_is_harmless(self):
        """"No Yazidi belongs in this town" is a negation carrying the whole
        attack, and a quotation endorses as often as it condemns."""
        text = self.lines(negation_cancels=True)

        assert 'do not treat a negation or a quotation as harmless' in text

    def test_an_unflagged_trope_says_nothing_about_it(self):
        assert 'rejecting it' not in self.lines(negation_cancels=False)

    def test_the_matcher_carries_the_flag_through(self):
        """It was shipped by the platform, dropped in the matcher, and read by
        nothing -- so the curator's tick had no effect anywhere."""
        from plugins.ettok.detect.match import _trope_guidance

        assert _trope_guidance({'negation_cancels': True})['negation_cancels'] is True
        assert _trope_guidance({})['negation_cancels'] is False
