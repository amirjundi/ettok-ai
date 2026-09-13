"""Vocabulary scoped to one case must not leak into another.

A flare-up coins words that mean nothing outside it. Left general, a hashtag
from one episode keeps flagging every later case for as long as it stays in the
lexicon -- quietly, and in the direction that finds more than it should.
"""

import pytest

from plugins.ettok.detect import match as match_mod


def term(text, case_id=None):
    return {
        'id': abs(hash(text)) % 10000, 'term': text, 'language': 'ar',
        'category': 'slur', 'target_group': '', 'target_group_slug': '',
        'severity_weight': 5, 'is_regex': False, 'is_explicit': True,
        'never_flag_when': [], 'variants': [], 'case_id': case_id,
    }


def test_general_vocabulary_applies_everywhere():
    assert match_mod.in_scope(term('x'), case_id=None) is True
    assert match_mod.in_scope(term('x'), case_id=7) is True


def test_case_language_applies_only_in_its_case():
    scoped = term('x', case_id=7)
    assert match_mod.in_scope(scoped, case_id=7) is True
    assert match_mod.in_scope(scoped, case_id=8) is False


def test_case_language_is_ignored_when_no_case_is_being_worked():
    """A manual check or a probe runs without a case. Case language has, by
    definition, no case to be scoped to, so it must not fire."""
    assert match_mod.in_scope(term('x', case_id=7), case_id=None) is False


def test_a_scoped_term_does_not_fire_in_another_case():
    """The whole point, through the real matcher rather than the predicate."""
    class Knowledge:
        terms = [term('هاشتاغ_الحملة', case_id=7)]
        tropes = []

        def group_markers(self):
            return {}

        def tropes_for(self, slug):
            return []

    item = {'text': 'هاشتاغ_الحملة', 'parent_post_text': ''}

    assert match_mod.evaluate(item, Knowledge(), case_id=7).matched is True
    assert match_mod.evaluate(item, Knowledge(), case_id=8).matched is False
    assert match_mod.evaluate(item, Knowledge()).matched is False


def test_a_row_without_the_field_is_treated_as_general():
    """An older platform does not send case_id at all. Absent must mean general,
    or every term would vanish the moment the agent talks to one."""
    legacy = term('x')
    del legacy['case_id']
    assert match_mod.in_scope(legacy, case_id=7) is True
    assert match_mod.in_scope(legacy, case_id=None) is True
