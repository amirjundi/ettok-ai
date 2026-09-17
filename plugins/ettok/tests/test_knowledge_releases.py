"""Which knowledge produced a verdict.

Every classification records the version of the lexicon, tropes and cases it was
made under, so a reviewer asking why the agent changed its mind can be answered.
The version was a count -- and a curator who edits a term, or replaces one with
another, leaves the count unchanged. Two runs with different detection knowledge
carried the same string, which is exactly the case the stamp exists for.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from plugins.ettok.platform.knowledge import Knowledge


def _terms(*pairs):
    return [
        {'id': i, 'term': term, 'variants': [], 'never_flag_when': [],
         'severity_weight': 8, 'category': 'dehumanization', 'is_regex': False,
         'target_group_slug': 'yazidi'}
        for i, term in pairs
    ]


class TestItIdentifiesTheContent:
    def test_replacing_a_term_changes_the_release(self):
        """The count stays at two. The knowledge is not the same knowledge."""
        before = Knowledge(terms=_terms((1, 'alpha'), (2, 'beta')))
        after = Knowledge(terms=_terms((1, 'alpha'), (2, 'gamma')))

        assert before.versions['lexicon'] != after.versions['lexicon']

    def test_editing_a_term_in_place_changes_the_release(self):
        before = Knowledge(terms=_terms((1, 'alpha')))
        edited = Knowledge(terms=_terms((1, 'alpha ')))

        assert before.versions['lexicon'] != edited.versions['lexicon']

    def test_changing_a_severity_changes_the_release(self):
        """It changes what a verdict says, so it is part of the release."""
        before = Knowledge(terms=_terms((1, 'alpha')))
        after = Knowledge(terms=_terms((1, 'alpha')))
        after.terms[0]['severity_weight'] = 3

        assert before.versions['lexicon'] != after.versions['lexicon']

    def test_adding_a_term_changes_the_release(self):
        one = Knowledge(terms=_terms((1, 'alpha')))
        two = Knowledge(terms=_terms((1, 'alpha'), (2, 'beta')))

        assert one.versions['lexicon'] != two.versions['lexicon']


class TestItIsStable:
    def test_the_same_knowledge_twice_is_the_same_release(self):
        """Otherwise every run looks like a new release and the stamp says
        nothing."""
        assert (Knowledge(terms=_terms((1, 'alpha'))).versions
                == Knowledge(terms=_terms((1, 'alpha'))).versions)

    def test_a_reordered_response_is_not_a_new_release(self):
        """The platform makes no promise about row order, and a re-ordered
        response is the same knowledge."""
        forward = Knowledge(terms=_terms((1, 'alpha'), (2, 'beta')))
        backward = Knowledge(terms=_terms((2, 'beta'), (1, 'alpha')))

        assert forward.versions['lexicon'] == backward.versions['lexicon']

    def test_the_size_is_still_readable_at_a_glance(self):
        """It says how much knowledge was loaded without opening anything."""
        assert Knowledge(terms=_terms((1, 'a'), (2, 'b'))).versions['lexicon'].startswith('2@')

    def test_an_empty_body_of_knowledge_still_has_an_id(self):
        assert Knowledge().versions['lexicon'].startswith('0@')


class TestEveryBodyOfKnowledgeIsStamped:
    def test_tropes_and_cases_too(self):
        plain = Knowledge()
        with_tropes = Knowledge(tropes=[{'id': 1, 'name': 'Devil worship libel',
                                         'surface_forms': ['x'], 'severity_weight': 8}])
        assert plain.versions['tropes'] != with_tropes.versions['tropes']

    def test_the_three_are_independent(self):
        """A lexicon edit must not look like a case change."""
        base = Knowledge(terms=_terms((1, 'alpha')), cases=[{'id': 1, 'title': 'c'}])
        edited = Knowledge(terms=_terms((1, 'beta')), cases=[{'id': 1, 'title': 'c'}])

        assert base.versions['cases'] == edited.versions['cases']
        assert base.versions['lexicon'] != edited.versions['lexicon']
