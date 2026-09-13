# -*- coding: utf-8 -*-
"""The offline selector check, against markup shaped like the real thing.

This cannot prove the selectors match Facebook -- only a page saved from
Facebook can do that, which is exactly what the check exists to make possible.
What it proves is that the checker itself works: that it runs the collector's
own extraction JavaScript, that it reports the three failures separately, and
that it does not quietly return an empty result when something went wrong.
"""
import json
import shutil

import pytest

from plugins.ettok.collect import base as base_mod
from plugins.ettok.collect import selector_check

pytestmark = pytest.mark.skipif(
    shutil.which('node') is None, reason='needs Node to run the extractor',
)


def _page(comments, *, author_href=True, permalink=True):
    """Markup in the shape the default Facebook selectors expect."""
    blocks = []
    for index, text in enumerate(comments):
        link = (
            f'<a role="link" href="https://www.facebook.com/profile.php?id=10{index}'
            f'&amp;__cft__[0]=trackingjunk"><span>User {index}</span></a>'
            if author_href else f'<span>User {index}</span>'
        )
        stamp = (
            f'<a href="https://www.facebook.com/permalink.php?story_fbid=9&amp;comment_id=5{index}">2h</a>'
            if permalink else ''
        )
        blocks.append(
            f'<div role="article">{link}{stamp}<div dir="auto">{text}</div></div>'
        )
    inner = ''.join(blocks)
    body = f'<div role="article"><div dir="auto">اخبار من سنجار</div>{inner}</div>'
    return f'<!doctype html><html><body>{body}</body></html>'


def _write(tmp_path, html):
    path = tmp_path / 'page.html'
    path.write_text(html, encoding='utf-8')
    return path


def test_it_finds_comments_and_their_identities(tmp_path):
    path = _write(tmp_path, _page(['اليزيديون يعبدون الشيطان', 'كلام عادي']))
    report = selector_check.check(path)

    assert report['comments'] == 2
    assert report['parent_post_found']
    assert report['with_author_link'] == 2
    assert report['with_permalink'] == 2
    # The tracking junk Facebook staples on every link has to come off, or the
    # same person yields a different id on every page load -- and a different
    # account history every time they are seen.
    assert report['sample_ids'][0]['id'] == 'fa:100'
    assert report['sample_ids'][1]['id'] == 'fa:101'
    assert '__cft__' not in report['sample_ids'][0]['href']


def test_no_comments_is_reported_as_such_rather_than_as_success(tmp_path):
    """A page saved before the comments rendered, or a selector that no longer
    matches. Returning an empty list quietly is how a broken collector passes
    for a quiet feed."""
    path = _write(tmp_path, _page([]))
    report = selector_check.check(path)

    assert report['comments'] == 0
    assert 'No comments were extracted' in selector_check.describe(report)


def test_missing_profile_links_are_called_out_separately(tmp_path):
    """Comments found but no author links: detection works, identity is dead."""
    path = _write(tmp_path, _page(['كلام'], author_href=False))
    report = selector_check.check(path)

    assert report['comments'] == 1
    assert report['with_author_link'] == 0
    assert 'Repeat offenders cannot be tracked' in selector_check.describe(report)


def test_missing_permalinks_are_called_out_separately(tmp_path):
    path = _write(tmp_path, _page(['كلام'], permalink=False))
    report = selector_check.check(path)

    assert report['with_permalink'] == 0
    assert 'cite the page' in selector_check.describe(report)


def test_a_missing_parent_post_is_called_out(tmp_path):
    """Without the post above them, the gated half of the lexicon never fires.

    Reached with a custom selector set, because the Facebook defaults cannot
    produce it: the comment selector requires a comment to sit inside an
    article, so anything that finds comments has already found a post. That is a
    property of those selectors and not a guarantee -- a tuned set can lose the
    post while keeping the comments, and that is the case worth warning about.
    """
    html = ('<!doctype html><html><body>'
            '<p class="c">كلام</p><p class="c">كلام اخر</p>'
            '</body></html>')
    path = _write(tmp_path, html)
    report = selector_check.check(path, selectors={
        'post': '#missing', 'comment': '.c', 'author': 'b', 'text': 'p',
    })

    assert report['comments'] == 2
    assert not report['parent_post_found']
    assert 'Context-dependent detection' in selector_check.describe(report)


def test_a_candidate_selector_set_can_be_tried_without_saving_it(tmp_path):
    """How a broken selector gets fixed: try, look, then save as learned."""
    html = '<!doctype html><html><body><section id="p"><p class="c">كلام</p></section></body></html>'
    path = _write(tmp_path, html)

    assert selector_check.check(path)['comments'] == 0

    report = selector_check.check(path, selectors={
        'post': '#p', 'comment': '.c', 'author': 'b', 'text': 'p',
    })
    assert report['comments'] == 1


def test_it_runs_the_collectors_own_javascript(tmp_path):
    """The whole value depends on this. A second copy of the extractor would
    drift from the one collection runs, and the check would then be measuring
    something nobody uses."""
    path = _write(tmp_path, _page(['كلام']))
    report = selector_check.check(path)

    assert report['selectors'] == base_mod.DEFAULT_EXTRACTORS['facebook']
    # Built from the module's own expression, not a copy pasted here.
    assert '%s' not in base_mod._EXTRACT_JS % json.dumps({'post': 'x'})


def test_a_missing_file_is_an_error_not_an_empty_result(tmp_path):
    with pytest.raises(selector_check.CheckError):
        selector_check.check(tmp_path / 'nope.html')


def test_an_unknown_platform_is_an_error(tmp_path):
    path = _write(tmp_path, _page(['كلام']))
    with pytest.raises(selector_check.CheckError):
        selector_check.check(path, platform='myspace')


def test_arabic_survives_the_round_trip(tmp_path):
    """The evidence is Arabic, Kurdish and Syriac, and it crosses a file, a Node
    process and JSON on the way back. Any one of those defaulting to a system
    codepage turns a finding into mojibake that no reviewer can read and no
    matcher can match."""
    comment = 'اليزيديون يعبدون الشيطان'
    path = _write(tmp_path, _page([comment]))

    report = selector_check.check(path)

    assert report['comments'] == 1
    assert comment in report['samples'][0]
    assert 'اخبار من سنجار' in report['parent_post_excerpt']


def test_the_extracted_text_carries_the_author_name_and_timestamp(tmp_path):
    """Worth knowing before reading any output: innerText on a comment node
    includes its children, so the name and the "2h" stamp sit in front of the
    words. Substring matching is unaffected, but a person reading the evidence
    sees them, and so does anything measuring comment length."""
    path = _write(tmp_path, _page(['اليزيديون يعبدون الشيطان']))
    extracted = selector_check.check(path)['samples'][0]

    assert extracted.startswith('User 0')
    assert extracted.endswith('اليزيديون يعبدون الشيطان')
