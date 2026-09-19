"""Detection: the minimal pair, the gate, and staying in step with the platform.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from plugins.ettok.detect import match as match_mod
from plugins.ettok.detect.normalize import normalize

# The canonical utterance from the field data. Ordinary piety on most posts, the
# devil-worship libel under Yazidi content. Everything here turns on that.
REFUGE = 'اعوذ بالله من الشيطان الرجيم'


class FakeKnowledge:
    """Stands in for a run's fetched knowledge without a platform."""

    # The real Knowledge carries this; a run refuses to use one that has
    # gone stale. A stub without it does not model the thing it stands in
    # for, which is how it went unnoticed that nothing enforced freshness.
    age_seconds = 0.0

    def __init__(self, terms=None, tropes=None, markers=None):
        self.terms = terms or []
        self.tropes = tropes or []
        self._markers = markers or {}

    def group_markers(self):
        return self._markers

    def tropes_for(self, slug):
        return [
            t for t in self.tropes
            if not (t.get('target_group_slug') or '') or t.get('target_group_slug') == slug
        ]


def _trope(**over):
    base = {
        'id': 4, 'name': 'Devil-worship framing', 'target_group_slug': 'yazidi',
        'requires_target_group': True, 'activation_topics': ['yazidi', 'sinjar', 'lalish'],
        'surface_forms': [REFUGE], 'severity_weight': 8, 'negative_examples': [],
    }
    base.update(over)
    return base


def _term(**over):
    base = {
        'id': 12, 'term': 'عبدة الشيطان', 'is_explicit': True, 'is_regex': False,
        'target_group_slug': 'yazidi', 'severity_weight': 8, 'category': 'dehumanization',
        'variants': [], 'never_flag_when': [],
    }
    base.update(over)
    return base


MARKERS = {'yazidi': ['ايزيدي', 'الايزيدية', 'سنجار', 'لالش']}


# ---------------------------------------------------------------------------
# The minimal pair -- the whole point of the system
# ---------------------------------------------------------------------------

def test_the_refuge_formula_is_not_flagged_on_an_unrelated_post():
    """A road accident is not a hate speech context. This must stay quiet."""
    know = FakeKnowledge(tropes=[_trope()], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': REFUGE, 'parent_post_text': 'حادث سير على طريق دهوك زاخو'}, know,
    )
    assert not result.matched, result.explain()


def test_the_same_words_fire_under_yazidi_content():
    know = FakeKnowledge(tropes=[_trope()], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': REFUGE, 'parent_post_text': 'الايزيدية تحيي ذكرى سنجار'}, know,
    )
    assert result.matched
    assert result.fired_tropes[0]['name'] == 'Devil-worship framing'
    assert 'yazidi' in result.fired_tropes[0]['activation_reason']


def test_the_gate_fires_through_topic_markers_not_literal_topics():
    """Which is why topic_markers are load-bearing, not a convenience.

    activation_topics are latin slugs ("yazidi", "sinjar"), and the posts are in
    Arabic. A literal search for "sinjar" inside "سنجار" finds nothing, so the
    gate almost never fires on the topic strings themselves -- it fires because
    the Arabic topic_markers identified the community first.

    A group with no topic_markers therefore has no working gate, no matter how
    carefully its tropes were curated. Today that is eight of the nine groups.
    """
    trope = _trope()
    with_markers = match_mod.evaluate(
        {'text': REFUGE, 'parent_post_text': 'الايزيدية تحيي ذكرى سنجار'},
        FakeKnowledge(tropes=[trope], markers=MARKERS),
    )
    without_markers = match_mod.evaluate(
        {'text': REFUGE, 'parent_post_text': 'الايزيدية تحيي ذكرى سنجار'},
        FakeKnowledge(tropes=[trope], markers={}),
    )

    assert with_markers.matched
    assert not without_markers.matched, (
        'the gate fired without topic markers; it should not be able to'
    )


def test_an_ungated_trope_never_fires_on_its_own():
    """All 19 seeded tropes are in this state today.

    Empty activation_topics means no gate has been curated yet, never "always
    active". Reading it the other way would flag every devout phrase in Iraq.
    """
    know = FakeKnowledge(tropes=[_trope(activation_topics=[])], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': REFUGE, 'parent_post_text': 'الايزيدية تحيي ذكرى سنجار'}, know,
    )
    assert not result.matched, 'an uncurated trope must not flag anything by itself'


# ---------------------------------------------------------------------------
# Explicit vs context-dependent terms
# ---------------------------------------------------------------------------

def test_an_explicit_term_fires_anywhere():
    know = FakeKnowledge(terms=[_term()], markers=MARKERS)
    result = match_mod.evaluate({'text': 'عبدة الشيطان', 'parent_post_text': 'أي منشور'}, know)
    assert result.matched


def test_a_context_dependent_term_needs_its_community_present():
    """Treating every term as explicit escalates ordinary speech containing one."""
    know = FakeKnowledge(terms=[_term(is_explicit=False)], markers=MARKERS)

    off_topic = match_mod.evaluate(
        {'text': 'عبدة الشيطان', 'parent_post_text': 'مباراة كرة قدم'}, know)
    assert not off_topic.matched

    on_topic = match_mod.evaluate(
        {'text': 'عبدة الشيطان', 'parent_post_text': 'الايزيدية في سنجار'}, know)
    assert on_topic.matched


def test_variants_catch_deliberate_misspellings():
    """The transcript's own example: same word, different alef, no match without this."""
    know = FakeKnowledge(terms=[_term(term='الشيطان', variants=['الشبطان'])])
    assert match_mod.evaluate({'text': 'يا الشبطان', 'parent_post_text': ''}, know).matched


def test_exemptions_are_carried_forward_not_dropped():
    """The classifier has to be told what must never be flagged."""
    know = FakeKnowledge(terms=[_term(never_flag_when=['news_quotation', 'counter_speech'])])
    result = match_mod.evaluate({'text': 'عبدة الشيطان', 'parent_post_text': ''}, know)
    assert result.exemption_hints == ['counter_speech', 'news_quotation']


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_an_uncompilable_pattern_is_skipped_and_reported():
    """One curator typo must not stop a scan, and must not vanish either."""
    know = FakeKnowledge(terms=[_term(term='[unclosed', is_regex=True)])
    result = match_mod.evaluate({'text': 'anything', 'parent_post_text': ''}, know)
    assert not result.matched
    assert result.skipped_terms and 'compile' in result.skipped_terms[0]['reason']


def test_topic_markers_never_flag_by_themselves():
    """They decide where to look, never what to flag."""
    know = FakeKnowledge(terms=[], tropes=[], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': 'تعليق عادي تماما', 'parent_post_text': 'الايزيدية في سنجار'}, know)
    assert result.topic_groups == ['yazidi']
    assert not result.matched


def test_explain_says_why_nothing_matched():
    """User Story 6: distinguishing "no term matched" from "gate unmet"."""
    know = FakeKnowledge(tropes=[_trope()], markers=MARKERS)
    quiet = match_mod.evaluate({'text': REFUGE, 'parent_post_text': 'حادث سير'}, know)
    assert 'nothing matched' in quiet.explain()

    loud = match_mod.evaluate(
        {'text': REFUGE, 'parent_post_text': 'سنجار'}, know)
    assert 'fired' in loud.explain()


# ---------------------------------------------------------------------------
# Parity with the platform
# ---------------------------------------------------------------------------

PLATFORM_NORMALIZE = Path(
    'C:/xampp/htdocs/Ettok.net/news_platform/apps/hate_speech/normalize.py'
)

CORPUS = [
    REFUGE,
    'عبدة الشيطان',
    'الشيطآن',            # different alef -- the transcript's own example
    'الإيزيديين',
    'ئێزیدی',             # Kurdish: must NOT be folded into Arabic
    'سَنجار',             # harakat
    'الـــعراق',          # tatweel
    '٢٠١٤',               # Arabic-Indic digits
    'MiXeD case ASCII',
    '  spaced   out  ',
    '',
]


@pytest.mark.skipif(not PLATFORM_NORMALIZE.exists(),
                    reason='platform repository not checked out next door')
def test_normalizer_agrees_with_the_platform_character_for_character():
    """The agent prefilters against a lexicon that lives on the platform.

    That only works if both sides reduce text to the same canonical form, and a
    divergence produces no error -- just terms that quietly stop matching. This
    catches the drift the moment either side changes.
    """
    spec = importlib.util.spec_from_file_location('ettok_platform_normalize', PLATFORM_NORMALIZE)
    platform = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(platform)

    for text in CORPUS:
        assert normalize(text) == platform.normalize(text), f'diverged on {text!r}'


def test_kurdish_orthography_survives_normalization():
    """ئێزیدی is not a misspelling of anything.

    Standard Arabic search normalization collapses hamza carriers into alef,
    which would destroy Kurdish spelling and silently un-match every Kurdish term.
    """
    assert 'ئ' in normalize('ئێزیدی')


# ---------------------------------------------------------------------------
# Context-dependent terms that belong to no single community
# ---------------------------------------------------------------------------
#
# The field data reports "stinking", "dirty people" and "you are infidels"
# against several communities at once, so a curator cannot pick one target
# group for them -- and they are ordinary vocabulary everywhere else.

STINKING = 'گەنی'


def _groupless(**over):
    base = _term(
        id=24, term=STINKING, target_group_slug='', is_explicit=False,
        category='slur', severity_weight=5,
    )
    base.update(over)
    return base


def test_a_groupless_context_term_stays_quiet_off_topic():
    """An ordinary insult under an unrelated post is not hate speech.

    This is the regression: an empty target group used to skip the gate
    entirely, so marking the term context-dependent changed nothing and the
    word flagged every post that contained it.
    """
    know = FakeKnowledge(terms=[_groupless()], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': f'الاكل هنا {STINKING}', 'parent_post_text': 'مطعم جديد في دهوك'},
        know,
    )
    assert not result.matched, result.explain()


def test_a_groupless_context_term_fires_when_a_community_is_the_subject():
    know = FakeKnowledge(terms=[_groupless()], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': f'هذول {STINKING}', 'parent_post_text': 'اخبار من سنجار عن الايزيدية'},
        know,
    )
    assert result.matched
    assert result.fired_terms[0]['term'] == STINKING


def test_a_groupless_explicit_term_still_fires_anywhere():
    """Closing the gate must not mute terms that are attacks on their own."""
    know = FakeKnowledge(terms=[_groupless(is_explicit=True)], markers=MARKERS)
    result = match_mod.evaluate(
        {'text': f'هذول {STINKING}', 'parent_post_text': 'مطعم جديد في دهوك'}, know,
    )
    assert result.matched
