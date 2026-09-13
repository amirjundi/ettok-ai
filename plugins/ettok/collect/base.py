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

_EXTRACT_JS = """
(() => {
  const sel = %s;
  const out = [];
  const post = document.querySelector(sel.post);
  const postText = post ? (post.innerText || '').slice(0, 4000) : '';
  document.querySelectorAll(sel.comment).forEach((node, i) => {
    const text = (node.innerText || '').trim();
    if (!text) return;
    const authorEl = node.querySelector(sel.author);
    const authorLink = sel.author_link ? node.querySelector(sel.author_link) : null;
    const permalink = sel.permalink ? node.querySelector(sel.permalink) : null;
    out.push({
      text: text.slice(0, 2000),
      author_name: authorEl ? (authorEl.innerText || '').trim().slice(0, 200) : '',
      author_href: authorLink ? (authorLink.href || '') : '',
      permalink: permalink ? (permalink.href || '') : '',
      index: i,
    });
  });
  return JSON.stringify({
    parent_post_text: postText,
    parent_post_url: location.href,
    comments: out.slice(0, 200),
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

        if capture_evidence:
            screenshot = ''
            try:
                shot = self._call('browser_vision', {'question': 'capture', 'annotate': False})
                screenshot = shot.get('image_b64') or shot.get('screenshot') or ''
            except Exception:
                log.debug('ettok: screenshot unavailable', exc_info=True)
            result.evidence = capture(url=url, page_text=page_text, screenshot_b64=screenshot)

        result.items = self._extract(url, page_text)
        return result

    def _extract(self, url: str, page_text: str) -> list:
        """Pull comments and their parent post out of the loaded page."""
        selectors = self._selectors or self._extractors.get(self.platform)
        if not selectors:
            return []

        expression = _EXTRACT_JS % json.dumps(selectors)
        try:
            payload = self._call('browser_console', {'expression': expression})
        except Exception:
            log.warning('ettok: extraction failed on %s', url, exc_info=True)
            return []

        blob = payload.get('result') or payload.get('value') or payload.get('raw') or ''
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except ValueError:
                log.warning('ettok: extraction returned unusable output for %s', url)
                return []
        if not isinstance(blob, dict):
            return []

        parent = (blob.get('parent_post_text') or '').strip()
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
                'parent_media_text': '',
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
            })
        return items


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
    kept = [
        (key, value) for key, value in parse_qsl(parts.query)
        if key not in _TRACKING_PARAMS
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
