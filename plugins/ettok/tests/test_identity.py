"""A stable account id, derived from the profile link rather than the name.

Identity is the anchor for everything about repeat offenders. A display name is
not one: names change, and two people share one. A wrong id is worse than a
missing one -- it merges two people into a single account history, and that
history is what an advocacy report or a platform referral gets built on.
"""

from plugins.ettok.collect.base import _clean_url, author_id_from_href


def test_a_numeric_profile_link():
    assert author_id_from_href(
        'https://www.facebook.com/profile.php?id=100012345', 'facebook',
    ) == 'fa:100012345'


def test_a_vanity_profile_link():
    assert author_id_from_href(
        'https://www.facebook.com/some.person', 'facebook',
    ) == 'fa:some.person'


def test_a_group_member_link_yields_the_numeric_id():
    """The same person reached through a group link must be the same account."""
    assert author_id_from_href(
        'https://www.facebook.com/groups/123/user/98765/', 'facebook',
    ) == 'fa:98765'


def test_tracking_parameters_do_not_change_the_id():
    """Social platforms staple a fresh tracking blob onto every link. Left in,
    one person yields a different id on every page load and no two findings
    ever look like the same account."""
    plain = author_id_from_href('https://www.facebook.com/some.person', 'facebook')
    tracked = author_id_from_href(
        'https://www.facebook.com/some.person?__cft__[0]=AZX9&__tn__=R&fref=nf',
        'facebook',
    )
    assert plain == tracked == 'fa:some.person'


def test_a_route_is_not_an_identity():
    """/photo, /permalink.php and friends are pages, not people. Returning one
    as an id would file every commenter on a photo under the same account."""
    for href in (
        'https://www.facebook.com/photo.php?fbid=99',
        'https://www.facebook.com/permalink.php?story_fbid=1&id=2',
        'https://www.facebook.com/watch/?v=123',
    ):
        assert author_id_from_href(href, 'facebook') != 'fa:photo.php'
        assert 'permalink' not in author_id_from_href(href, 'facebook')


def test_no_link_means_no_id():
    """Empty, never a guess. An unattributed finding is still a finding."""
    assert author_id_from_href('', 'facebook') == ''
    assert author_id_from_href('not a url at all', 'facebook') == ''


def test_clean_url_keeps_meaningful_parameters():
    """Only the tracking junk goes: a story id is part of the address."""
    cleaned = _clean_url('https://x.invalid/permalink.php?story_fbid=7&__tn__=R')
    assert 'story_fbid=7' in cleaned
    assert '__tn__' not in cleaned


def test_indexed_tracking_parameters_are_stripped():
    """Facebook's tracking parameters are indexed: `__cft__[0]`, `__cft__[1]`.

    Matching the exact name let every one of them through, so cleaning did
    nothing on the parameter it was written for and the same profile produced a
    different URL on every page load. A per-load URL means a person seen twice
    looks like two people, and the repeat-offender history never accumulates.
    """
    from plugins.ettok.collect.base import _clean_url

    dirty = (
        'https://www.facebook.com/profile.php?id=100'
        '&__cft__[0]=AZW1&__cft__[1]=AZW2&__tn__=R'
    )
    cleaned = _clean_url(dirty)

    assert '__cft__' not in cleaned
    assert '__tn__' not in cleaned
    assert 'id=100' in cleaned


def test_two_sightings_of_one_profile_clean_to_the_same_url():
    """The property that actually matters, stated directly."""
    from plugins.ettok.collect.base import _clean_url

    first = 'https://www.facebook.com/nadia?__cft__[0]=AAA&__tn__=R'
    second = 'https://www.facebook.com/nadia?__cft__[0]=BBB&__tn__=-UC'

    assert _clean_url(first) == _clean_url(second)
