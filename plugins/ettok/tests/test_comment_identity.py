"""Which post a comment sits under is part of its identity.

From the architecture review of 19 September 2026 (ARCH-06/07). The digest held
the parent post's *text* but not which post it was, so two posts carrying the
same words -- a caption reshared, or a photo with no caption at all -- merged
every reply under one of them with the identical reply under the other. The
second was dropped as a duplicate, which is the pile-on the counts exist to
measure, undercounted in proportion to how coordinated it was.

The opposite error matters just as much and is the reason the URL is filtered
rather than used raw: an identity that shifts between scans re-files the same
comment as a new finding on every run, and since collection now counts against
the case budget, that spends money as well as distorting the numbers.
"""

from plugins.ettok.scan import content_hash, post_identity


def item(**over):
    base = {
        'text': 'نفس الكلام',
        'parent_post_text': '',
        'parent_post_url': 'https://www.facebook.com/page/posts/111',
        'url': '',
        'platform': 'facebook',
        'author_id': 'fa:1',
    }
    base.update(over)
    return base


class TestTheParentPostIsPartOfTheIdentity:
    def test_the_same_words_under_two_posts_are_two_findings(self):
        """The defect. Both posts have no caption, so text alone cannot tell
        them apart."""
        a = content_hash(item(parent_post_url='https://www.facebook.com/page/posts/111'))
        b = content_hash(item(parent_post_url='https://www.facebook.com/page/posts/222'))
        assert a != b

    def test_the_same_comment_under_the_same_post_is_one_finding(self):
        assert content_hash(item()) == content_hash(item())

    def test_two_people_saying_it_under_one_post_stay_separate(self):
        """The pile-on this is meant to measure."""
        assert content_hash(item(author_id='fa:1')) != content_hash(item(author_id='fa:2'))


class TestTheIdentityHoldsStillBetweenScans:
    def test_click_tracking_does_not_change_it(self):
        """The same link copied twice carries a different fbclid each time."""
        clean = item()
        tracked = item(
            parent_post_url='https://www.facebook.com/page/posts/111'
                            '?fbclid=IwAR_abc123&mibextid=zz')
        assert content_hash(clean) == content_hash(tracked)

    def test_the_phone_and_the_desktop_page_are_one_post(self):
        desktop = item(parent_post_url='https://www.facebook.com/page/posts/111')
        phone = item(parent_post_url='https://m.facebook.com/page/posts/111')
        assert content_hash(desktop) == content_hash(phone)

    def test_a_trailing_slash_is_not_a_different_post(self):
        assert content_hash(item(parent_post_url='https://x.test/p/1')) == \
            content_hash(item(parent_post_url='https://x.test/p/1/'))


class TestTheQueryIsFilteredNotDiscarded:
    def test_a_post_id_in_the_query_still_distinguishes_posts(self):
        """Facebook puts the post id in the query. Dropping it outright would
        merge every post reached through permalink.php into one."""
        one = post_identity('https://www.facebook.com/permalink.php?story_fbid=1&id=9')
        two = post_identity('https://www.facebook.com/permalink.php?story_fbid=2&id=9')
        assert one != two

    def test_parameter_order_does_not_matter(self):
        assert post_identity('https://x.test/p?a=1&b=2') == \
            post_identity('https://x.test/p?b=2&a=1')

    def test_no_url_is_not_an_error(self):
        assert post_identity('') == ''
        assert post_identity(None) == ''


def test_a_comment_permalink_equal_to_the_post_url_adds_nothing():
    """A page with no per-comment link hands back the post URL; that is the
    post's identity, not the comment's. Still true once both are filtered."""
    with_permalink = item(url='https://m.facebook.com/page/posts/111?fbclid=z')
    without = item(url='')
    assert content_hash(with_permalink) == content_hash(without)
