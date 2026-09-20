"""The two rows of the guide's test table that had no end-to-end cover.

From the contextual implementation guide of 20 September 2026, section 13.

  | Post/comment tree with multiple posts and nested replies | Exact
    parent/ancestor association, no sibling leakage |
  | Budget exhausted mid-batch | Remaining items explicitly unassessed;
    evidence retained |

Both were covered in part and in the wrong place. Sibling leakage was tested
in jsdom, against the extractor, which proves the page is read correctly and
says nothing about what survives the run afterwards -- and the run is where
the comment is hashed, scoped, matched and judged, each step with its own
opportunity to pair a comment with the wrong post.

Budget exhaustion was tested for money only. The item budget is the other
half and it behaves differently: it stops collection rather than assessment,
so the failure it can produce is not an unassessed item but a lost one.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.platform.knowledge import Knowledge
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


def knowledge(limits=None):
    return Knowledge(
        terms=[{
            'id': 12, 'term': 'devil worshippers', 'is_explicit': True,
            'is_regex': False, 'target_group_slug': 'yazidi',
            'severity_weight': 8, 'category': 'dehumanization',
            'variants': [], 'never_flag_when': [],
        }],
        tropes=[],
        cases=[{
            'id': 1, 'title': 'Sinjar', 'state': 'active',
            'target_groups': [{'slug': 'yazidi', 'name_en': 'Yazidis',
                               'topic_markers': ['sinjar'], 'background': 'b'}],
            'seed_sources': [],
            'limits': limits or {'items_remaining': None,
                                 'cost_remaining_usd': None},
            'suggests_closing': False,
        }],
    )


class Ctx:
    def __init__(self, know=None, reply=None):
        self._ettok_knowledge = know or knowledge()
        self.llm = mock.MagicMock()
        self.llm.complete_structured.return_value = mock.MagicMock(
            parsed=reply or {'is_hate_speech': False, 'reason': 'ordinary'},
            usage=mock.MagicMock(cost_usd=0.001, total_tokens=50))

    def prompts_for(self, task):
        return [c.kwargs['input'][0]['text']
                for c in self.llm.complete_structured.call_args_list
                if c.kwargs.get('task') == task]

    @property
    def prompts(self):
        """Classification prompts only.

        The scope check reads the post text too, and legitimately -- it is
        deciding whether that conversation is monitored at all. Counting both
        would make a correct scope call look like context leaking between
        posts.
        """
        return self.prompts_for('ettok_classify')


def stored():
    return [dict(r) for r in schema.connect().execute(
        'SELECT excerpt, parent_excerpt, state, scope_status, why_flagged '
        'FROM classification ORDER BY id')]


# ---------------------------------------------------------------------------
# A page with several posts and nested replies
# ---------------------------------------------------------------------------

SINJAR = 'Remembering the anniversary at Sinjar'
FOOTBALL = 'What a match last night'

TREE = [
    # Under the Sinjar post: a top-level comment and a reply to it.
    {'text': 'they should all be driven out',
     'parent_post_text': SINJAR, 'parent_post_url': 'https://facebook.com/p/1',
     'url': 'https://facebook.com/p/1#c1', 'platform': 'facebook',
     'author_id': 'fb:1'},
    {'text': 'Exactly.',
     'parent_post_text': SINJAR, 'parent_post_url': 'https://facebook.com/p/1',
     'url': 'https://facebook.com/p/1#c2', 'platform': 'facebook',
     'author_id': 'fb:2', 'is_reply': True,
     'reply_to_text': 'they should all be driven out',
     'reply_to_author': 'Ali'},
    # Under the football post, on the same feed: the same word, a different
    # conversation entirely.
    {'text': 'Exactly.',
     'parent_post_text': FOOTBALL, 'parent_post_url': 'https://facebook.com/p/2',
     'url': 'https://facebook.com/p/2#c1', 'platform': 'facebook',
     'author_id': 'fb:3', 'is_reply': True,
     'reply_to_text': 'the keeper was asleep', 'reply_to_author': 'Zana'},
]


class TestMultiplePostsOnOnePage:
    def test_each_comment_keeps_its_own_post(self, home):
        scan_mod.run(Ctx(), items=TREE, submit=False)

        rows = stored()
        assert len(rows) == 3
        under = {row['excerpt']: row['parent_excerpt'] for row in rows}
        assert under['they should all be driven out'] == SINJAR
        # Two comments read "Exactly." and they sit under different posts,
        # which is the whole hazard: one of them is agreeing with a call for
        # expulsion and the other with a remark about a goalkeeper.
        assert SINJAR in [r['parent_excerpt'] for r in rows]
        assert FOOTBALL in [r['parent_excerpt'] for r in rows]

    def test_identical_comments_under_different_posts_are_two_findings(self, home):
        """Deduplication hashes the parent as well as the words, precisely so
        these do not collapse into one."""
        result = scan_mod.run(Ctx(), items=TREE, submit=False)

        assert result['duplicates'] == 0
        assert len(stored()) == 3

    def test_no_reply_borrows_a_sibling_context(self, home):
        ctx = Ctx()
        scan_mod.run(ctx, items=TREE, submit=False)

        football = [p for p in ctx.prompts if FOOTBALL in p]
        assert len(football) == 1
        assert 'driven out' not in football[0], 'the other post leaked in'

    def test_the_right_parent_reaches_the_right_reply(self, home):
        ctx = Ctx()
        scan_mod.run(ctx, items=TREE, submit=False)

        sinjar_reply = [p for p in ctx.prompts
                        if 'THE COMMENT TO JUDGE: Exactly.' in p and SINJAR in p]
        assert len(sinjar_reply) == 1
        assert 'they should all be driven out' in sinjar_reply[0]
        assert 'the keeper was asleep' not in sinjar_reply[0]

    def test_a_marker_on_one_post_does_not_scope_the_other(self, home):
        """The Sinjar post carries a curated topic marker and the football
        post does not. Scope is decided per post, so one cannot vouch for
        the other."""
        scan_mod.run(Ctx(), items=TREE, submit=False)

        by_post = {}
        for row in stored():
            by_post.setdefault(row['parent_excerpt'], set()).add(row['scope_status'])
        assert by_post[SINJAR] == {'in_scope'}
        assert 'in_scope' not in by_post[FOOTBALL]

    def test_posts_are_counted_separately(self, home):
        result = scan_mod.run(Ctx(), items=TREE, submit=False)

        assert result['posts'] == 2


# ---------------------------------------------------------------------------
# The item budget, which stops collection rather than assessment
# ---------------------------------------------------------------------------

def many(n):
    return [{
        'text': f'comment number {i}',
        'parent_post_text': SINJAR,
        'parent_post_url': 'https://facebook.com/p/1',
        'url': f'https://facebook.com/p/1#c{i}',
        'platform': 'facebook', 'author_id': f'fb:{i}',
    } for i in range(n)]


class TestTheItemBudget:
    def test_it_stops_the_run_and_says_why(self, home):
        result = scan_mod.run(
            Ctx(knowledge({'items_remaining': 3, 'cost_remaining_usd': None})),
            items=many(10), submit=False)

        assert result['stop_reason'] == 'item_budget'

    def test_what_it_reached_is_kept(self, home):
        """Collection is the half that cannot be redone: these posts may be
        deleted before the next allowance arrives."""
        scan_mod.run(
            Ctx(knowledge({'items_remaining': 3, 'cost_remaining_usd': None})),
            items=many(10), submit=False)

        assert len(stored()) == 3

    def test_what_it_did_not_reach_is_not_recorded_as_anything(self, home):
        """The item budget stops collection, so the rest were never
        collected. They are absent, which is honest -- what must never happen
        is a stored row claiming a model read them."""
        scan_mod.run(
            Ctx(knowledge({'items_remaining': 3, 'cost_remaining_usd': None})),
            items=many(10), submit=False)

        assert all(row['state'] != classify_mod.STATE_MODEL_NEGATIVE
                   for row in stored()[3:])

    def test_a_later_run_picks_up_where_it_stopped(self, home):
        """Nothing beyond the cut was marked seen, so the allowance arriving
        tomorrow does not find them already deduplicated away."""
        scan_mod.run(
            Ctx(knowledge({'items_remaining': 3, 'cost_remaining_usd': None})),
            items=many(10), submit=False)

        second = scan_mod.run(Ctx(), items=many(10), submit=False)

        assert second['scanned'] == 10
        assert second['duplicates'] == 3, 'only the three already collected'

    def test_the_run_is_still_delivered(self, home):
        result = scan_mod.run(
            Ctx(knowledge({'items_remaining': 3, 'cost_remaining_usd': None})),
            items=many(10), submit=False)

        assert result['queue']['pending'] >= 1
