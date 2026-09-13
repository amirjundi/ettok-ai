# -*- coding: utf-8 -*-
"""Check the extraction selectors against a saved page, with no live session.

Collection has never run against a live social platform. The Facebook selectors
in `base.py` are configuration written against markup that nobody has confirmed,
and everything built on top of them -- the account histories, the per-post rates,
the accounts page -- rests on them returning something. Until they are checked,
all of that is built and unproven.

The check needs no account, no login and no session: save one post page from a
browser and run it through the collector's own extraction JavaScript. The
expression and the selectors are read out of `base.py` here rather than copied,
so what this tests is exactly what a scan would run.

What it cannot tell you: whether the page you saved is representative, and
whether the site changes its markup next week. It answers one question -- would
today's selectors have found the comments on this page -- which is the question
currently unanswered.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import base as base_mod
from .base import _clean_url, author_id_from_href

CHECK_JS = Path(__file__).with_name('selector_check.js')


class CheckError(RuntimeError):
    """The check could not run. Distinct from the selectors finding nothing."""


def check(html_path, *, platform: str = 'facebook', selectors=None, url: str = ''):
    """Run the extractor against a saved page. Returns a report dict.

    `selectors` overrides the defaults, so a candidate set can be tried against
    the same page before anything is saved as learned.
    """
    node = shutil.which('node')
    if not node:
        raise CheckError(
            'Node is not on PATH. It is needed to run the same JavaScript the '
            'collector runs in the page.'
        )
    if not CHECK_JS.exists():                                     # pragma: no cover
        raise CheckError(f'missing {CHECK_JS}')

    html_path = Path(html_path)
    if not html_path.exists():
        raise CheckError(f'no such file: {html_path}')

    selectors = selectors or base_mod.DEFAULT_EXTRACTORS.get(platform)
    if not selectors:
        raise CheckError(f'no selectors configured for platform "{platform}"')

    # The same string the collector sends to the browser, built the same way.
    expression = base_mod._EXTRACT_JS % json.dumps(selectors)

    with tempfile.NamedTemporaryFile(
        'w', suffix='.json', delete=False, encoding='utf-8',
    ) as handle:
        json.dump({'expression': expression, 'selectors': selectors, 'url': url}, handle)
        payload_path = handle.name

    try:
        completed = subprocess.run(
            [node, str(CHECK_JS), str(html_path), payload_path],
            capture_output=True, text=True, timeout=120,
            # Node writes UTF-8. Without saying so, `text=True` decodes with the
            # locale encoding -- cp1252 or cp1256 on a Windows machine -- and
            # every Arabic comment comes back as one character per byte. The
            # counts would still look right, which is what makes it dangerous:
            # the check would report success while handing back text no matcher
            # could match and no reviewer could read.
            encoding='utf-8', errors='replace',
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError('the page took more than two minutes to parse') from exc
    finally:
        Path(payload_path).unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = (completed.stderr or '').strip().splitlines()
        if any('jsdom' in line for line in detail):
            raise CheckError(
                'jsdom is not installed. Run `npm install` in the agent repo.'
            )
        raise CheckError(
            'the checker failed: ' + (detail[-1] if detail else 'no output')
        )

    try:
        raw = json.loads(completed.stdout or '{}')
    except ValueError as exc:
        raise CheckError('the checker returned unreadable output') from exc

    if not raw.get('ok'):
        raise CheckError(raw.get('error') or 'extraction threw in the page')

    return _report(raw, platform, selectors)


def _report(raw: dict, platform: str, selectors: dict) -> dict:
    extracted = raw.get('extracted') or {}
    comments = extracted.get('comments') or []

    # Counted separately because they are separate failures. Comments found with
    # no author links means identity capture is dead while detection still
    # works; no permalinks means every finding on the page cites the same URL
    # and none can be reached again once the feed moves on.
    with_author = sum(1 for c in comments if (c.get('author_name') or '').strip())
    with_link = sum(1 for c in comments if (c.get('author_href') or '').strip())
    with_permalink = sum(1 for c in comments if (c.get('permalink') or '').strip())

    ids = []
    for comment in comments[:5]:
        href = comment.get('author_href') or ''
        ids.append({
            'name': (comment.get('author_name') or '')[:40],
            # Cleaned, because that is the form stored as `author_url`. Showing
            # the raw href would show tracking parameters that change on every
            # page load and are stripped before anything is written down.
            'href': _clean_url(href)[:80],
            'id': author_id_from_href(href, platform),
        })

    parent = (extracted.get('parent_post_text') or '').strip()
    return {
        'platform': platform,
        'selectors': selectors,
        'selector_hits': raw.get('selector_hits') or {},
        'parent_post_found': bool(parent),
        'parent_post_chars': len(parent),
        'parent_post_excerpt': parent[:160],
        'comments': len(comments),
        'with_author_name': with_author,
        'with_author_link': with_link,
        'with_permalink': with_permalink,
        'sample_ids': ids,
        'samples': [(c.get('text') or '')[:100] for c in comments[:3]],
    }


def describe(report: dict) -> str:
    """The report as something worth reading in a terminal."""
    total = report['comments']
    lines = [
        f"platform: {report['platform']}",
        '',
        'Raw selector hits on the page:',
    ]
    for name, hits in report['selector_hits'].items():
        lines.append(f"  {name:14} {hits}")

    lines += [
        '',
        f"parent post      {'found, ' + str(report['parent_post_chars']) + ' chars' if report['parent_post_found'] else 'NOT FOUND'}",
        f'comments         {total}',
    ]
    if total:
        lines += [
            f"  with a name    {report['with_author_name']}/{total}",
            f"  with a profile link {report['with_author_link']}/{total}",
            f"  with a permalink    {report['with_permalink']}/{total}",
        ]

    if not total:
        lines += [
            '',
            'No comments were extracted. Either the page was saved before the',
            'comments rendered, or the comment selector no longer matches. The raw',
            'hits above say which: a zero against "comment" is the selector.',
        ]
        return '\n'.join(lines)

    if report['sample_ids']:
        lines += ['', 'Account ids derived from the first few comments:']
        for sample in report['sample_ids']:
            shown = sample['id'] or '(none -- no usable profile link)'
            lines.append(f"  {sample['name'] or '(no name)':<24} -> {shown}")

    if report['samples']:
        lines += ['', 'First comments, as extracted:']
        lines += [f'  {text}' for text in report['samples']]

    warnings = []
    if not report['parent_post_found']:
        warnings.append(
            'No parent post text. Context-dependent detection is the whole design: '
            'without the post, a trope cannot tell piety from a taunt and the '
            'gated half of the lexicon never fires.'
        )
    if not report['with_author_link']:
        warnings.append(
            'No profile links. Repeat offenders cannot be tracked -- a display '
            'name is not an identity, and the accounts page would stay empty.'
        )
    if not report['with_permalink']:
        warnings.append(
            'No comment permalinks. Every finding on this page would cite the page '
            'URL, so none could be reached again after the feed moves on.'
        )
    if warnings:
        lines += ['', 'Problems:']
        lines += [f'  - {text}' for text in warnings]

    return '\n'.join(lines)
