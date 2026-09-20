"""Driving the browser: navigate, check, extract, prove.

This calls the runtime's own browser tools rather than opening a browser of its
own. That is deliberate -- the runtime already ships a supervised browser with a
Camoufox backend for fingerprint resistance, and a second driver in here would be
a second thing to keep working against a moving target.

The order of operations is the part that matters, and it is not the obvious one:

    navigate -> check for a block -> capture evidence -> extract

A challenge page contains no comments, so extracting first and finding nothing
would report a quiet run and hide the fact that the account has been noticed.
And evidence is captured before extraction because the page can change or vanish
between the two, and an item whose proof arrived late is an item with no proof.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import session as session_mod
from .evidence import capture

log = logging.getLogger(__name__)

# Extraction runs as JavaScript in the page rather than by parsing an
# accessibility snapshot. Social platforms render comments through generated
# class names that change without notice, so the selectors below WILL need tuning
# against the real site -- they are configuration, not logic, and live here so
# that tuning them is a one-line change rather than a rewrite.
DEFAULT_EXTRACTORS = {
    'facebook': {
        'post': '[role="article"]',
        'comment': '[role="article"] [role="article"]',
        'author': 'a[role="link"] span',
        'text': '[dir="auto"]',
        # The author's profile link, which is where a stable account id lives.
        # A display name is not an identity: names change and repeat.
        'author_link': 'a[role="link"][href*="/"]',
        # A comment's own permalink, usually its timestamp. Without it every
        # comment on a page cites the same URL and none can be reached again
        # after the page moves on.
        'permalink': 'a[href*="comment_id"], a[href*="/posts/"], a[href*="permalink"]',
    },
}

_EXTRACT_JS = r"""
(() => {
  const sel = %s;
  // Bumped whenever this changes what "the comment" or "the post" means, so a
  // stored observation can be read against the rules that produced it.
  const EXTRACTION_VERSION = 4;
  // Top-level posts only. A comment is an article too, so the post selector
  // matches every comment on the page as well -- a plain count said four on a
  // post with three comments, which would have read as a feed.
  const posts = Array.from(document.querySelectorAll(sel.post)).filter(
    (el) => !(el.parentElement && el.parentElement.closest(sel.post)));
  // When the page has opened a post in a modal -- which is what a share link
  // does -- the articles behind that modal belong to whatever page was
  // underneath: a feed post, an unrelated profile. Taking the first article on
  // the page then collected the feed post *behind* the dialog and left the post
  // that was actually on screen, with every one of its comments, unread. The
  // result read as "nothing was extracted", which is the honest report of a
  // collection that looked in the wrong place.
  // The first dialog in the DOM is not necessarily the post's: a share link
  // leaves an empty wrapper behind, and picking that one put the collection back
  // on the feed post underneath. Take the dialog that actually holds a post.
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
  const modal = dialogs.find((d) => posts.some((el) => d.contains(el))) || null;
  const onScreen = modal ? posts.filter((el) => modal.contains(el)) : [];
  const post = onScreen[0] || posts[0] || null;
  // A page where nothing matched the post selector is refused for collection:
  // comments without the post they answer cannot be judged for
  // context-dependent hate, and handing them back anyway is exactly the quiet
  // findings report this is meant to stop. The error says so.
  //
  // The comments are still gathered and returned beside it, because the
  // selector tuning tool reads this same output and needs to see which half of
  // a candidate set matched in order to say which half is wrong.
  //
  // Where they live is the other half of the same problem. The comment selector
  // assumes a comment is an article nested inside the post, which is how the
  // first render looks. Open the thread -- which is the whole point of collecting
  // it -- and the comments are re-rendered as siblings of the post, so the nested
  // selector finds the ten that stayed nested and reports a 499-comment thread as
  // empty. Inside a modal that is unambiguous: one post, its thread, nothing
  // else, so the post selector minus the post is exactly the comment set.
  const modalScope = (modal && post && modal.contains(post)) ? modal : null;
  const scope = modalScope || post || document;
  const commentSelector = modalScope ? sel.post : sel.comment;

  // Anything nested inside a node that is itself an article is a comment or a
  // reply, not part of that node's own body.
  const nested = sel.nested || '[role="article"]';

  // The body of one node, with its nested comments taken out.
  //
  // This is the whole of ARCH-06. The post selector matches an article and the
  // comment selector matches articles inside it, so `post.innerText` carried
  // every comment on the page. A topic marker written by one commenter then
  // read as the subject of the post, which is the gate that decides whether
  // context-dependent terms count -- so one person could switch on, or off,
  // the detection applied to everybody else in the thread.
  const bodyOf = (node) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll(nested).forEach((c) => c.remove());
    // The author's name, the timestamp and the Like/Reply controls sit inside
    // the same article as the comment and are not what the person wrote.
    if (sel.author_link) copy.querySelectorAll(sel.author_link).forEach((a) => a.remove());
    if (sel.permalink) copy.querySelectorAll(sel.permalink).forEach((a) => a.remove());
    if (sel.text) {
      const parts = [];
      copy.querySelectorAll(sel.text).forEach((t) => {
        const s = (t.innerText || '').trim();
        if (s && parts.indexOf(s) === -1) parts.push(s);
      });
      if (parts.length) return parts.join('\n');
    }
    return (copy.innerText || '').trim();
  };

  const parentText = post ? bodyOf(post) : '';

  // Scoped to this post. A document-wide search paired every comment on a feed
  // with the first post's context, so a comment under post three was judged
  // against what post one was about.
  const found = Array.from(scope.querySelectorAll(commentSelector))
    .filter((el) => el !== post);
  const MAX_COMMENTS = 200;
  const out = [];

  // Which comment a node hangs under, and the whole way up to the post.
  //
  // `is_reply` was a boolean, which is the one fact about a reply that does
  // not help. "Exactly" under a call for expulsion and "Exactly" under a
  // defence of somebody's rights are the same six letters, and the thing that
  // separates them is the comment directly above -- which was thrown away
  // here and reconstructed by nobody. Judging the second as hate speech
  // accuses a person of the opposite of what they said.
  const ancestorsOf = (node) => {
    const chain = [];
    let at = node.parentElement ? node.parentElement.closest(sel.comment) : null;
    // Bounded: a thread nests, but not forty deep, and a selector that matches
    // its own ancestor would otherwise loop until the tab dies.
    while (at && chain.length < 8) {
      const a = at.querySelector(sel.author);
      chain.push({
        text: bodyOf(at).slice(0, 600),
        author_name: a ? (a.innerText || '').trim().slice(0, 200) : '',
        index: found.indexOf(at),
      });
      at = at.parentElement ? at.parentElement.closest(sel.comment) : null;
    }
    // Nearest first: the direct parent is the one that decides the meaning,
    // and a budget that truncates the chain must cut the far end.
    return chain;
  };

  found.slice(0, MAX_COMMENTS).forEach((node, i) => {
    const body = bodyOf(node);
    if (!body) return;
    const authorEl = node.querySelector(sel.author);
    const authorLink = sel.author_link ? node.querySelector(sel.author_link) : null;
    const permalink = sel.permalink ? node.querySelector(sel.permalink) : null;
    const chain = ancestorsOf(node);
    out.push({
      text: body.slice(0, 2000),
      text_truncated: body.length > 2000,
      author_name: authorEl ? (authorEl.innerText || '').trim().slice(0, 200) : '',
      author_href: authorLink ? (authorLink.href || '') : '',
      permalink: permalink ? (permalink.href || '') : '',
      is_reply: chain.length > 0,
      // The comment this one answers, in full. Everything above it, in order,
      // as excerpts -- context thins out with distance, the direct parent
      // never does.
      reply_to_text: chain.length ? chain[0].text : '',
      reply_to_author: chain.length ? chain[0].author_name : '',
      reply_to_index: chain.length ? chain[0].index : -1,
      ancestors: chain.slice(1),
      // How deep, even where the chain was cut. A reply at depth 12 with 8
      // ancestors recorded says so rather than looking like a top-level one.
      reply_depth: chain.length,
      index: i,
    });
  });

  // Does the post carry an image worth describing? Avatars, reaction icons and
  // tracking pixels are everywhere on a social page, so size is the filter: a
  // post image is displayed large, a profile picture is not.
  let mediaCount = 0;
  const media = post || document;
  media.querySelectorAll('img').forEach((im) => {
    // Rendered size where the image has painted, intrinsic size where it has
    // not: a feed lazy-loads, so an image below the fold measures 0x0 by
    // rect while naturalWidth is already correct. Either one counts.
    const r = im.getBoundingClientRect();
    const w = Math.max(r.width, im.naturalWidth || 0);
    const h = Math.max(r.height, im.naturalHeight || 0);
    if (w >= 120 && h >= 120) mediaCount += 1;
  });
  mediaCount += media.querySelectorAll('video').length;

  return JSON.stringify({
    error: post ? undefined : 'no element matched the post selector',
    parent_post_text: parentText.slice(0, 4000),
    parent_post_text_truncated: parentText.length > 4000,
    parent_post_url: location.href,
    parent_media_count: mediaCount,
    // What was on the page versus what is being returned. Silent truncation
    // turns "no hate found here" and "we stopped reading at two hundred" into
    // the same answer.
    posts_on_page: posts.length,
    comments_loaded: found.length,
    comments_returned: out.length,
    comments_truncated: found.length > MAX_COMMENTS,
    extraction_version: EXTRACTION_VERSION,
    comments: out,
  });
})()
"""


# Opening the collapsed half of a thread, before extraction.
#
# Not the same job as the wait: a post can render ten comments and hold six
# hundred behind "view all N replies" and an unscrolled panel. The wait is
# satisfied by the ten, so without this the run reports a quiet thread on
# exactly the posts that are not quiet.
_EXPAND_THREAD_JS = r"""
(() => {
  const SEL = %s;
  // "View all N replies" / "View more comments", in the page's own language.
  // Matched on the words and not on the exact string: the site pads the label
  // with a count and a chevron, and neither is part of the label.
  const wanted = /(view|show|عرض).{0,24}(comment|repl|تعليق|رد)/i;
  // The controls that carry one of those words but do not open anything.
  const exact = /^(comment|reply|like|share|تعليق|مشاركة|إعجاب|رد)$/i;
  let clicked = 0;
  document.querySelectorAll('[role="button"], [role="link"], span').forEach((el) => {
    const label = (el.innerText || '').trim();
    if (!label || label.length > 60 || exact.test(label)) return;
    if (!wanted.test(label)) return;
    // Innermost match only: every wrapper carries its child's label, so from
    // the outside every one of these looks like a button and one click lands
    // several times on the same control.
    if (el.querySelector('[role="button"]')) return;
    try { el.click(); clicked += 1; } catch (e) {}
  });

  // The thread scrolls inside its own panel, not the document. Scrolling the
  // window moves nothing and loads nothing.
  let panel = null;
  document.querySelectorAll('div').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 300) panel = el;
  });
  if (panel) panel.scrollTop = panel.scrollHeight;
  else window.scrollTo(0, document.body.scrollHeight);

  return JSON.stringify({
    clicked,
    // Named apart from the extractor's `comments`, which is a list of them:
    // sharing the name made a count out of an array and raised.
    comment_nodes: document.querySelectorAll(SEL).length,
    scrolled: !!panel,
  });
})()
"""


@dataclass
class CollectionResult:
    items: list = field(default_factory=list)
    evidence: Optional[object] = None
    blocked: Optional[str] = None
    auth_lost: bool = False
    url: str = ''
    # How much of the page this actually saw: posts on it, comments loaded,
    # comments returned, what was cut. Empty when extraction did not run.
    #
    # Without it, "no hate speech under this post" and "we stopped reading at
    # two hundred comments" are the same answer, and the first is the one
    # anybody would assume.
    coverage: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.blocked is None


class BrowserCollector:
    """Collects comments from one page, through the runtime's browser tools."""

    platform = 'unknown'

    def __init__(self, ctx, *, pacer=None, extractors=None, selectors=None):
        self._ctx = ctx
        self._pacer = pacer or session_mod.Pacer()
        self._extractors = extractors or DEFAULT_EXTRACTORS
        # An override the agent supplies after reading the page itself. Selectors
        # against a social platform are a moving target, and an agent that can look
        # at the DOM beats a constant that was right last month.
        self._selectors = selectors

    # -- tool plumbing ----------------------------------------------------

    def _call(self, tool: str, args: dict) -> dict:
        raw = self._ctx.dispatch_tool(tool, args)
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {'raw': raw}

    def _page_text(self) -> str:
        snapshot = self._call('browser_snapshot', {'full': False})
        for key in ('snapshot', 'text', 'content', 'raw'):
            value = snapshot.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(snapshot, ensure_ascii=False)

    # -- the run ----------------------------------------------------------

    def page_outline(self, *, limit: int = 6000) -> str:
        """What the page looks like, for an agent that has to work out the layout.

        Returned when the configured selectors find nothing, so the agent can read
        the structure itself rather than an operator guessing at class names that
        changed last week.
        """
        return self._page_text()[:limit]

    def probe(self, selectors: dict) -> dict:
        """Try a set of selectors and report what they would yield.

        Lets the agent test a guess cheaply before committing to it, and before
        anything is attributed to a case.
        """
        expression = _EXTRACT_JS % json.dumps(selectors)
        try:
            payload = self._call('browser_console', {'expression': expression})
        except Exception as exc:                      # noqa: BLE001
            return {'ok': False, 'error': str(exc)}

        blob = payload.get('result') or payload.get('value') or payload.get('raw') or ''
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except ValueError:
                return {'ok': False, 'error': 'selectors returned unusable output'}
        if not isinstance(blob, dict):
            return {'ok': False, 'error': 'selectors returned no object'}

        comments = blob.get('comments', [])
        return {
            'ok': bool(comments),
            'comments_found': len(comments),
            'parent_post_found': bool((blob.get('parent_post_text') or '').strip()),
            'sample': [c.get('text', '')[:120] for c in comments[:3]],
        }

    def collect(self, url: str, *, capture_evidence: bool = True) -> CollectionResult:
        """Open one page and take what is on it.

        Never raises for a block. A blocked collection is a result to report and
        act on, not an exception to bubble up through a scan.
        """
        result = CollectionResult(url=url)

        self._pacer.wait()
        self._call('browser_navigate', {'url': url})
        self._pacer.wait()

        page_text = self._page_text()

        # Before anything is extracted. A challenge page has no comments on it,
        # and reading it as an empty result would hide the detection.
        blocked = session_mod.detect_block(page_text)
        if blocked is not None:
            result.blocked = blocked.reason
            result.auth_lost = blocked.auth_lost
            return result

        # Then wait for the thread, still before anything is extracted.
        #
        # A social page paints its post immediately and its comments seconds
        # later: they live in a panel the site fills in after the page settles,
        # and sometimes only once it is scrolled. Extracting when navigation
        # returns therefore reads an empty thread, and an empty thread and a
        # post nobody replied to are the same result -- the silent-empty-page
        # failure this collector can least afford, arriving before the selectors
        # are even consulted.
        #
        # Bounded, because a post genuinely without comments must not hold a run
        # open, and the count is kept either way so coverage can say whether the
        # wait expired or finished.
        selectors = self._selectors or self._extractors.get(self.platform) or {}
        comments_seen = self._wait_for_comments(selectors)

        # Then open the rest of the thread, which is not the same problem. A
        # page can have ten comments rendered and six hundred more behind "view
        # more comments" and an unscrolled panel: the wait above finds the ten
        # and stops, because ten is not zero. Every reply thread on a Facebook
        # post collapses to "view all N replies", so the majority of the hate on
        # a pile-on sits behind a click that never happens.
        #
        # Run even when the wait found nothing. A panel that has not rendered at
        # all is the case where a scroll is worth the most: the site fills the
        # panel when it comes into view, and a tab that is not the front one is
        # exactly where that does not happen on its own.
        comments_expanded = self._expand_thread(selectors)

        if comments_seen or comments_expanded:
            # Re-read: the first read was the post on its own, which is what the
            # block check needed and not what the evidence should show.
            page_text = self._page_text()

        if capture_evidence:
            screenshot = ''
            try:
                shot = self._call('browser_vision', {'question': 'capture', 'annotate': False})
                screenshot = shot.get('image_b64') or shot.get('screenshot') or ''
            except Exception:
                log.debug('ettok: screenshot unavailable', exc_info=True)
            result.evidence = capture(url=url, page_text=page_text, screenshot_b64=screenshot)

        result.items, result.coverage = self._extract(url, page_text)
        result.coverage['comments_when_collection_started'] = comments_seen
        result.coverage['comments_after_expanding'] = comments_expanded
        return result

    # Long enough for a thread to paint on a slow connection, short enough that
    # a page with no comments does not hold a run open.
    DEFAULT_WAIT_FOR_COMMENTS_SECONDS = 30.0
    WAIT_POLL_SECONDS = 2.0
    # A seam, the same one the pacer has: tests drive the poll without waiting.
    _wait_sleep = staticmethod(time.sleep)

    def _wait_seconds(self) -> float:
        """``wait_for_comments_seconds`` from plugin config, else the default."""
        try:
            raw = self._ctx.get_config(
                'wait_for_comments_seconds', self.DEFAULT_WAIT_FOR_COMMENTS_SECONDS,
            )
        except Exception:                                     # noqa: BLE001
            return self.DEFAULT_WAIT_FOR_COMMENTS_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return self.DEFAULT_WAIT_FOR_COMMENTS_SECONDS
        return value if value > 0 else self.DEFAULT_WAIT_FOR_COMMENTS_SECONDS

    def _wait_for_comments(self, selectors: dict, *, sleep=None) -> int:
        """Poll until a comment node exists, or the wait expires.

        Cheap on purpose: a count, not an extraction, so a page that never
        renders costs polls rather than parses. Returns the last count seen,
        which is 0 when the wait ran out.
        """
        sleep = sleep or self._wait_sleep
        selector = (selectors or {}).get('comment') or ''
        if not selector:
            return 0
        expression = 'document.querySelectorAll(%s).length' % json.dumps(selector)
        deadline = time.monotonic() + self._wait_seconds()
        count = 0
        while True:
            try:
                payload = self._call('browser_console', {'expression': expression})
            except Exception:                                 # noqa: BLE001
                log.debug('ettok: comment wait probe failed', exc_info=True)
                return count
            value = payload.get('result')
            if value is None:
                value = payload.get('value')
            try:
                count = int(value)
            except (TypeError, ValueError):
                # The probe came back unusable -- an error payload rather than a
                # count. In a real page this expression always yields a number,
                # even on a blank one, so waiting cannot fix a non-answer and is
                # not worth the timeout.
                return count
            if count or time.monotonic() >= deadline:
                return count
            sleep(self.WAIT_POLL_SECONDS)

    # How many click-and-scroll rounds to give a collapsed thread. Zero turns
    # the step off; the loop also stops on its own once the count stops moving.
    DEFAULT_EXPAND_ROUNDS = 6

    def _expand_rounds(self) -> int:
        """``expand_thread_rounds`` from plugin config, else the default."""
        try:
            raw = self._ctx.get_config('expand_thread_rounds', self.DEFAULT_EXPAND_ROUNDS)
        except Exception:                                     # noqa: BLE001
            return self.DEFAULT_EXPAND_ROUNDS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return self.DEFAULT_EXPAND_ROUNDS
        return value if value > 0 else 0

    def _expand_thread(self, selectors: dict, *, sleep=None) -> int:
        """Open the collapsed reply threads and scroll the panel, then count.

        Every reply thread on a Facebook post is collapsed to "view all N
        replies", and the thread scrolls inside its own panel rather than the
        document, so a post with six hundred comments can render ten and stop.
        Ten is not zero, so the wait above is satisfied and the run reports a
        quiet thread -- on exactly the posts that are not quiet.

        Bounded twice over: a fixed number of rounds, and an early stop once the
        count stops growing, because a thread that will not open must not be
        clicked at for the rest of the run. Returns the count last seen; 0 means
        the step found nothing to do, which is not an error.
        """
        sleep = sleep or self._wait_sleep
        selector = (selectors or {}).get('comment') or ''
        rounds = self._expand_rounds()
        if not selector or rounds <= 0:
            return 0

        expression = _EXPAND_THREAD_JS % json.dumps(selector)
        # `previous` is the answer: the count that was standing when the thread
        # stopped growing. `count` is only ever the latest reading, and a reading
        # of zero on the round that decided to stop is not the result.
        previous = still = 0
        for _round in range(rounds):
            try:
                payload = self._call('browser_console', {'expression': expression})
            except Exception:                                 # noqa: BLE001
                log.debug('ettok: thread expansion failed', exc_info=True)
                return previous
            value = payload.get('result')
            if value is None:
                value = payload.get('value')
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    return previous
            if not isinstance(value, dict) or 'comment_nodes' not in value:
                # Not the expander's answer -- an extraction payload, or an
                # error object. Stop rather than raise inside a collection, and
                # rather than wait out a round on a page that cannot answer.
                return previous
            try:
                count = int(value.get('comment_nodes') or 0)
            except (TypeError, ValueError):
                return previous
            if count > previous:
                previous, still = count, 0
            else:
                still += 1
                if still >= 2:
                    return previous
            sleep(self.WAIT_POLL_SECONDS)
        return previous

    # A factual description, capped. Long enough for a flag, a gesture and a
    # line of meme text; short enough that it cannot crowd the comment out of
    # the classifier's prompt.
    _MEDIA_DESCRIPTION_LIMIT = 900

    _MEDIA_QUESTION = (
        'Describe only what is visibly present in the main image or video of this '
        'post: people, objects, gestures, flags, religious symbols, animals, and '
        'any text that appears inside the image. Transcribe image text verbatim in '
        'its original language and script; do not translate it. Report what is '
        'shown, not what it might mean -- do not interpret intent, do not say '
        'whether it is offensive, and do not draw a conclusion about the people '
        'depicted.'
    )

    def _image_description_enabled(self) -> bool:
        """`describe_post_images` in plugin config; on unless turned off.

        Separate from whether a vision model exists, because the two questions
        are different. A model may be configured for reading a page or checking
        a challenge without the operator wanting a paid vision call on every
        collected page that happens to carry a photograph -- and collection cost
        scales with pages, which is the axis that grows.

        On by default: the visual tropes are a quarter of the catalogue, and an
        operator who has gone to the trouble of configuring a vision model has
        said what they want.
        """
        try:
            raw = self._ctx.get_config('describe_post_images', True)
        except Exception:
            return True
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in {'off', 'false', 'no', '0', ''}

    def _describe_media(self, media_count: int) -> str:
        """What the post's image shows, as text the rest of the pipeline can read.

        A quarter of the active trope catalogue is visual -- donkey memes carrying
        the Assyrian flag, desecration video, doctored images of clergy. The
        matcher reads text, so every one of those tropes matches nothing at all,
        and the run reports a clean page. This is the only place the image can
        enter the pipeline: `parent_media_text` is already carried on every item
        and already read by the classifier prompt, and was only ever filled with
        an empty string.

        Deliberately a description and not a judgement. The classifier decides,
        with the comment and the post in front of it and the exemption list
        applied; a describer that pre-judged would smuggle a verdict past all of
        that. It is also why the description is of the POST, not the comment:
        the image is the context a reply is made against.

        Returns '' whenever anything is missing -- no media, no vision model, a
        failed call -- which is exactly the behaviour before this existed.
        """
        if media_count <= 0 or not self._image_description_enabled():
            return ''
        try:
            answer = self._call('browser_vision', {
                'question': self._MEDIA_QUESTION, 'annotate': False,
            })
        except Exception:
            log.debug('ettok: media description unavailable', exc_info=True)
            return ''

        if not isinstance(answer, dict):
            return ''
        # The aux-vision path returns its text under `analysis`. A native-vision
        # model attaches the screenshot to a conversation instead and returns no
        # text, which is useless here: the collector is not a model turn, and
        # there is no next turn to inspect it. Treat that as no description
        # rather than inventing one.
        text = (answer.get('analysis') or '').strip()
        if not text:
            return ''
        return text[:self._MEDIA_DESCRIPTION_LIMIT]

    def _extract(self, url: str, page_text: str) -> tuple:
        """Pull comments and their parent post out of the loaded page.

        Returns (items, coverage). Coverage is how much of the page was seen,
        and it travels with the items because an incomplete read that looks
        complete is the failure this collector can least afford.
        """
        selectors = self._selectors or self._extractors.get(self.platform)
        if not selectors:
            return [], {}

        expression = _EXTRACT_JS % json.dumps(selectors)
        try:
            payload = self._call('browser_console', {'expression': expression})
        except Exception:
            log.warning('ettok: extraction failed on %s', url, exc_info=True)
            return [], {}

        blob = payload.get('result') or payload.get('value') or payload.get('raw') or ''
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except ValueError:
                log.warning('ettok: extraction returned unusable output for %s', url)
                return [], {}
        if not isinstance(blob, dict):
            return [], {}

        # A layout the selectors do not fit is an extraction failure. It used
        # to return nothing, which reads identically to a post nobody replied
        # to -- so a selector broken by a site redesign would have looked like
        # weeks of quiet threads.
        if blob.get('error'):
            log.warning('ettok: %s on %s', blob['error'], url)
            return [], {'error': blob['error'],
                        'extraction_version': blob.get('extraction_version')}

        coverage = {
            'extraction_version': blob.get('extraction_version'),
            'posts_on_page': blob.get('posts_on_page'),
            'comments_loaded': blob.get('comments_loaded'),
            'comments_returned': blob.get('comments_returned'),
            'comments_truncated': bool(blob.get('comments_truncated')),
            'parent_post_text_truncated': bool(blob.get('parent_post_text_truncated')),
        }
        if coverage['comments_truncated']:
            log.warning('ettok: %s has more than %s comments; the rest were not read',
                        url, coverage['comments_returned'])
        if (coverage['posts_on_page'] or 0) > 1:
            # Only the first post's comments were taken, which is correct --
            # the alternative is filing them under the wrong parent -- but the
            # rest of the page went unread and that should be visible.
            log.info('ettok: %s holds %s posts; only the first was collected',
                     url, coverage['posts_on_page'])

        parent = (blob.get('parent_post_text') or '').strip()
        media_count = int(blob.get('parent_media_count') or 0)
        media = self._describe_media(media_count)
        # The post carries a picture that nothing has read: no vision model
        # configured, or the call failed. Recorded rather than left as an empty
        # description, because an unread image and a post with no image are the
        # same empty string downstream -- and a quarter of the trope catalogue
        # is visual, so treating them alike turns "we could not see it" into
        # "there was nothing to see".
        media_unread = bool(media_count) and not media
        # The page the comments hang under. This is the grouping key for
        # every per-post question -- how many comments were scanned here,
        # how many were findings -- so it has to be the same string for
        # every comment on the page.
        parent_url = _clean_url(blob.get('parent_post_url') or '') or url
        items = []
        for comment in blob.get('comments', []):
            text = (comment.get('text') or '').strip()
            if not text or text == parent:
                continue
            items.append({
                'text': text,
                # Carried on every item, because a comment without the post it
                # replies to cannot be judged for context-dependent hate.
                'parent_post_text': parent,
                'parent_media_text': media,
                'parent_media_unread': media_unread,
                # The comment's own permalink where the page offered one, and
                # the page URL otherwise. Evidence has to be reachable again
                # after the feed has moved on, and a page URL shared by two
                # hundred comments cannot single one out.
                'url': _clean_url(comment.get('permalink') or '') or url,
                'parent_post_url': parent_url or url,
                'platform': self.platform,
                'author_name': (comment.get('author_name') or '').strip(),
                'author_id': author_id_from_href(
                    comment.get('author_href') or '', self.platform,
                ),
                'author_url': _clean_url(comment.get('author_href') or ''),
                # A reply to another comment, rather than to the post. It
                # changes what the comment is answering, and so what it means.
                'is_reply': bool(comment.get('is_reply')),
                # And what it is answering, which is the part that decides.
                # "Exactly" under a call for expulsion and "Exactly" under a
                # defence of somebody's rights are the same word; only this
                # tells them apart, and a boolean cannot.
                'reply_to_text': (comment.get('reply_to_text') or '').strip(),
                'reply_to_author': (comment.get('reply_to_author') or '').strip(),
                # Everything above the direct parent, nearest first. Context
                # thins with distance; the direct parent never does.
                'ancestors': [
                    {'author_name': (a.get('author_name') or '').strip(),
                     'text': (a.get('text') or '').strip()}
                    for a in (comment.get('ancestors') or [])
                    if (a.get('text') or '').strip()
                ],
                # How deep it actually sits, even where the chain was cut, so
                # a truncated chain does not read as a top-level comment.
                'reply_depth': int(comment.get('reply_depth') or 0),
            })
        return items, coverage


class FacebookCollector(BrowserCollector):
    platform = 'facebook'


COLLECTORS = {'facebook': FacebookCollector}


def for_platform(ctx, platform: str, **kwargs) -> Optional[BrowserCollector]:
    collector = COLLECTORS.get((platform or '').lower())
    return collector(ctx, **kwargs) if collector else None


# Identity is the anchor for everything about repeat offenders, and a display
# name is not one: names change, and two people share one readily. The stable
# handle is in the profile link, so that is what is turned into an id.
#
# The normalisation below is deliberately conservative. It returns '' rather
# than a guess, because a wrong id is worse than a missing one -- it merges two
# people into one account history, and that history is what an advocacy report
# or a platform referral would be built on.

_TRACKING_PARAMS = ('__cft__', '__tn__', 'fref', 'refid', 'eav', 'hc_ref', 'rdid')


def _clean_url(href: str) -> str:
    """Drop the tracking parameters social platforms staple onto every link.

    Left in, the same profile yields a different string on every page load and
    no two findings ever look like the same person.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if not href:
        return ''
    try:
        parts = urlsplit(href.strip())
    except ValueError:
        return ''
    # Matched on the name up to any bracket. Facebook's are indexed --
    # `__cft__[0]`, `__cft__[1]` -- so an exact-name comparison let every one of
    # them through, and the cleaning silently did nothing on the parameter it
    # was written for. A profile link then differed on every page load, which
    # means the same person builds a new account history each time they are
    # seen: the opposite of what identity capture is for.
    kept = [
        (key, value) for key, value in parse_qsl(parts.query)
        if key.split('[')[0] not in _TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ''))


def author_id_from_href(href: str, platform: str = '') -> str:
    """A stable account id from a profile link, or '' when there isn't one.

    Facebook exposes two shapes -- a numeric id behind `profile.php?id=` or a
    vanity path -- and inside a group the member link carries the numeric id
    after `/user/`. All three reduce to one token so the same person recognised
    through different link shapes is one account.
    """
    from urllib.parse import parse_qs, urlsplit

    cleaned = _clean_url(href)
    if not cleaned:
        return ''

    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return ''

    # A real link, or nothing. Without this a stray string becomes a path and
    # its first word becomes an account id -- which is the wrong-id failure this
    # function exists to avoid, arriving through the front door.
    if not parts.netloc:
        return ''

    prefix = (platform or '').strip().lower()[:2] or 'xx'
    segments = [s for s in parts.path.split('/') if s]

    numeric = parse_qs(parts.query).get('id', [''])[0].strip()
    if numeric.isdigit():
        return f'{prefix}:{numeric}'

    # .../groups/<group>/user/<id>/ -- the group member link.
    if 'user' in segments:
        index = segments.index('user')
        if index + 1 < len(segments) and segments[index + 1].isdigit():
            return f'{prefix}:{segments[index + 1]}'

    # A vanity path: the first segment, unless it is a route rather than a name.
    routes = {
        'groups', 'photo', 'photo.php', 'permalink.php', 'story.php', 'watch',
        'reel', 'posts', 'p', 'share', 'video.php', 'events', 'pages',
    }
    if segments and segments[0].lower() not in routes and '.php' not in segments[0]:
        return f'{prefix}:{segments[0].lower()}'

    return ''
