"""A version string that does not move when detection moves identifies nothing.

From the architecture review of 19 September 2026 (ARCH-02). The release ID
hashed an allowlist of fields, and the allowlist left out the ones that gate a
match: `is_explicit` and `case_id` on a term, and the activation topics, plural
target groups, negation rule, examples and visual status on a trope. The
review's two probes flipped a match from true to false while the recorded
version stayed byte-identical.

That is the one thing a version string must never do. A reviewer asking why the
agent changed its mind about a comment gets told nothing changed, and the audit
chain a referral rests on has a hole in it exactly where somebody would look.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from plugins.ettok.platform.knowledge import Knowledge

TERM = {
    'id': 1, 'term': 'عبدة الأصنام', 'language': 'ar', 'category': 'slur',
    'severity_weight': 9, 'is_regex': False, 'is_explicit': True,
    'variants': [], 'never_flag_when': [], 'target_group_slug': 'yazidi',
    'case_id': None,
}

TROPE = {
    'id': 1, 'name': 'Devil-worship libel', 'description': 'd', 'example': 'e',
    'surface_forms': ['devil worshippers'], 'activation_topics': ['sinjar'],
    'negation_cancels': True, 'negative_examples': [], 'counter_speech_examples': [],
    'severity_weight': 8, 'requires_target_group': True, 'is_visual': False,
    'target_group_slug': 'yazidi', 'target_groups': ['yazidi'], 'case_id': None,
}

CASE = {
    'id': 1, 'title': 'Sinjar watch', 'state': 'active',
    'target_groups': [{'slug': 'yazidi', 'topic_markers': ['sinjar'], 'background': ''}],
    'next_scan_at': '2026-09-19T03:00:00',
    'limits': {'items_remaining': 40, 'cost_remaining_usd': 1.0},
    'due': True,
}


def versions(terms=None, tropes=None, cases=None):
    return Knowledge(terms=list(terms or []), tropes=list(tropes or []),
                     cases=list(cases or [])).versions


def changed(field, value, *, kind='lexicon'):
    """Does editing this field move the release ID?"""
    if kind == 'lexicon':
        before, after = versions([TERM]), versions([dict(TERM, **{field: value})])
    else:
        before, after = versions(tropes=[TROPE]), versions(tropes=[dict(TROPE, **{field: value})])
    return before[kind] != after[kind]


class TestTheFieldsThatGateAMatch:
    """Each of these changed a match result in the review's probes while the
    version stayed the same."""

    def test_term_explicitness(self):
        assert changed('is_explicit', False)

    def test_term_case_scope(self):
        assert changed('case_id', 7)

    def test_trope_activation_topics(self):
        assert changed('activation_topics', ['halabja'], kind='tropes')

    def test_trope_plural_target_groups(self):
        assert changed('target_groups', ['yazidi', 'assyrian'], kind='tropes')

    def test_trope_case_scope(self):
        assert changed('case_id', 7, kind='tropes')

    def test_trope_visual_status(self):
        assert changed('is_visual', True, kind='tropes')

    def test_trope_requires_target_group(self):
        assert changed('requires_target_group', False, kind='tropes')

    def test_trope_negation_rule(self):
        assert changed('negation_cancels', False, kind='tropes')


class TestTheFieldsThatReachTheModel:
    """Not matchers, but they are in the prompt, so they change verdicts."""

    def test_trope_description(self):
        assert changed('description', 'something else', kind='tropes')

    def test_trope_negative_examples(self):
        assert changed('negative_examples', ['a benign use'], kind='tropes')

    def test_trope_counter_speech_examples(self):
        assert changed('counter_speech_examples', ['do not say this'], kind='tropes')

    def test_term_exemptions(self):
        assert changed('never_flag_when', ['news_quotation'])


class TestWhatMustNotMoveIt:
    def test_reordering_is_not_a_new_release(self):
        """The platform promises no order, and a re-ordered response is the
        same knowledge."""
        second = dict(TERM, id=2, term='كفرة')
        assert versions([TERM, second])['lexicon'] == versions([second, TERM])['lexicon']

    def test_the_schedule_and_the_budget_do_not(self):
        """A case's remaining budget moves on every run now that collection is
        counted. Hashing it would give an ID that never repeats, which
        identifies nothing just as surely as one that never changes."""
        spent = dict(CASE, next_scan_at='2026-09-20T03:00:00', due=False,
                     limits={'items_remaining': 3, 'cost_remaining_usd': 0.1})
        assert versions(cases=[CASE])['cases'] == versions(cases=[spent])['cases']

    def test_but_a_topic_marker_does(self):
        """Markers decide whether a post is recognised as concerning a
        community at all, so they are detection knowledge."""
        edited = dict(CASE, target_groups=[
            {'slug': 'yazidi', 'topic_markers': ['sinjar', 'شنكال'], 'background': ''}])
        assert versions(cases=[CASE])['cases'] != versions(cases=[edited])['cases']


def test_the_matcher_version_is_part_of_the_id():
    """Knowledge is only half of what decides a verdict; the code reading it is
    the other half."""
    from plugins.ettok.platform import knowledge as mod

    before = versions([TERM])['lexicon']
    original = mod.SCHEMA_VERSION
    try:
        mod.SCHEMA_VERSION = 'something-else'
        assert versions([TERM])['lexicon'] != before
    finally:
        mod.SCHEMA_VERSION = original


def test_the_count_is_still_readable_up_front():
    """"42@a1b2c3d4" says at a glance how much knowledge was loaded."""
    assert versions([TERM, dict(TERM, id=2)])['lexicon'].startswith('2@')
