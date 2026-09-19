"""A trope aimed at several communities must fire for any of them.

From the architecture review of 19 September 2026 (ARCH-05). The survey records
tropes aimed at more than one community at once, and the platform sends the
whole list as `target_groups`; `target_group_slug` is only its first element,
kept for rows that predate the many-to-many table.

The gate read the singular field. So when a case had established the subject --
the route that exists precisely for comments whose own text names no community
-- the trope could only be activated by whichever group happened to sort first.
For a trope curated against both Yazidi and Assyrian communities, half the
cases it was written for could not fire it, and nothing reported a miss: the
comment simply came back clean.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from plugins.ettok.detect import match as match_mod

TEXT = 'they are devil worshippers'

TROPE = {
    'id': 1,
    'name': 'Devil-worship libel',
    'description': 'Asserting that a community worships the devil.',
    'surface_forms': ['devil worshippers'],
    'activation_topics': ['sinjar'],
    'severity_weight': 8,
    'requires_target_group': True,
    # Sorted by slug on the platform, so the Assyrian slug arrives first and is
    # what the singular field carries.
    'target_group_slug': 'assyrian',
    'target_groups': ['assyrian', 'yazidi'],
}


def fire(trope, groups, parent=''):
    return match_mod.match_tropes(
        TEXT, [trope], groups_in_context=groups, parent_text=parent)


class TestEveryGroupTheTropeIsAimedAt:
    def test_the_case_subject_activates_it_even_when_it_sorts_second(self):
        """The defect. A Yazidi case could not fire a trope whose first listed
        group was Assyrian."""
        assert fire(TROPE, ['yazidi'])

    def test_the_first_listed_group_still_activates_it(self):
        assert fire(TROPE, ['assyrian'])

    def test_a_community_it_was_not_curated_against_does_not(self):
        """Widening the gate must not remove it."""
        assert fire(TROPE, ['turkmen']) == []


class TestTheOlderShapeStillWorks:
    def test_a_row_with_only_the_singular_field(self):
        """Rows predating the many-to-many table carry no `target_groups`."""
        old = {k: v for k, v in TROPE.items() if k != 'target_groups'}
        old['target_group_slug'] = 'yazidi'
        assert fire(old, ['yazidi'])

    def test_an_empty_group_list_falls_back_to_the_singular_field(self):
        assert fire(dict(TROPE, target_groups=[], target_group_slug='yazidi'),
                   ['yazidi'])

    def test_a_trope_with_no_group_at_all_needs_its_topic(self):
        """No group to match on, so only the curated topic can gate it."""
        loose = dict(TROPE, target_groups=[], target_group_slug='')
        assert fire(loose, ['yazidi']) == []
        assert fire(loose, ['yazidi'], parent='sinjar anniversary')


def test_the_topic_in_the_parent_post_still_wins_on_its_own():
    """The group route is the fallback; a curated topic in the post it replies
    to activates the trope whatever the case is about."""
    assert fire(TROPE, [], parent='sinjar anniversary')


class TestTheFilterUpstreamOfTheGate:
    """`tropes_for` runs first and hands the gate what survives it.

    Fixing only the activation gate fixed nothing in practice: a trope aimed at
    two communities was dropped here, for the second of them, before the gate
    could ever see it.
    """

    def _know(self):
        from plugins.ettok.platform.knowledge import Knowledge
        know = Knowledge.__new__(Knowledge)
        know.tropes = [TROPE]
        return know

    def test_the_second_community_still_gets_the_trope(self):
        assert self._know().tropes_for('yazidi') == [TROPE]

    def test_the_first_community_still_gets_it(self):
        assert self._know().tropes_for('assyrian') == [TROPE]

    def test_an_unrelated_community_does_not(self):
        assert self._know().tropes_for('turkmen') == []

    def test_a_trope_aimed_at_nobody_reaches_everyone(self):
        from plugins.ettok.platform.knowledge import Knowledge
        know = Knowledge.__new__(Knowledge)
        know.tropes = [dict(TROPE, target_groups=[], target_group_slug='')]
        assert len(know.tropes_for('turkmen')) == 1
