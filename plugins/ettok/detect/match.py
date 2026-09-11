"""Deterministic matching: the free tier, and the one that always runs.

This costs no model call, so it works in a month with no budget at all, and it is
what decides whether anything more expensive is worth spending on an item.

Two distinctions do all the work here, and getting either backwards breaks the
system in opposite directions:

`is_explicit` separates a word that is an attack wherever it appears from one that
only counts when the community is the subject. Treating every term as explicit
escalates ordinary speech that happens to contain one.

A trope's activation condition separates "اعوذ بالله من الشيطان الرجيم" said as
ordinary piety from the same words under Yazidi content. An empty
`activation_topics` means *no gate has been curated yet*, never *always active* --
the contract is explicit about this, and reading it the other way would flag every
devout phrase in Iraq.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .normalize import normalize

log = logging.getLogger(__name__)


@dataclass
class Match:
    """Why an item was touched, in enough detail for a human to check."""

    fired_terms: list = field(default_factory=list)
    fired_tropes: list = field(default_factory=list)
    topic_groups: list = field(default_factory=list)
    skipped_terms: list = field(default_factory=list)
    exemption_hints: list = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.fired_terms or self.fired_tropes)

    def explain(self) -> str:
        """The answer to "why was this flagged", or "why wasn't it"."""
        if self.fired_terms or self.fired_tropes:
            parts = []
            if self.fired_terms:
                parts.append(', '.join(t['term'] for t in self.fired_terms) + ' matched')
            for trope in self.fired_tropes:
                parts.append(f"trope \"{trope['name']}\" fired ({trope['activation_reason']})")
            return '; '.join(parts)
        if self.topic_groups:
            return (f'nothing matched, though the post concerns '
                    f'{", ".join(self.topic_groups)}')
        return 'nothing matched and the post concerns no monitored community'


def _compile(pattern: str) -> Optional[re.Pattern]:
    """Compile a curator's regex, or decline it.

    Patterns are authored and validated on the platform, in JavaScript, where
    `\\w`, `\\b` and `\\d` are ASCII-only. In Python they are Unicode-aware. A
    pattern that looks correct to the curator can therefore match differently
    here, and the platform's own importer warns about exactly this.

    One typo must not stop a scan, so a pattern that will not compile is skipped
    and reported -- a silently dropped term is a silently missed category.
    """
    try:
        return re.compile(pattern, re.IGNORECASE | re.UNICODE)
    except re.error as exc:
        log.warning('ettok: skipping uncompilable pattern %r: %s', pattern, exc)
        return None


def post_concerns(parent_text: str, markers_by_group: dict) -> list:
    """Which communities this post is about.

    The cheap filter that makes the whole thing affordable: most hostility sits
    under posts that are themselves benign, so the agent has to recognise the
    subject before reading a comment section, and a model call per post in a feed
    is unaffordable. Markers are neutral words and never flag anything by
    themselves.
    """
    haystack = normalize(parent_text)
    if not haystack:
        return []
    return sorted(
        slug for slug, markers in (markers_by_group or {}).items()
        if any(normalize(marker) and normalize(marker) in haystack for marker in markers)
    )


def match_terms(text: str, terms: list, *, groups_in_context: list) -> tuple:
    """Match the lexicon against one comment.

    Returns (fired, skipped). A non-explicit term only counts when its community
    is the subject of the post.
    """
    haystack = normalize(text)
    fired, skipped = [], []
    if not haystack:
        return fired, skipped

    for term in terms:
        raw = (term.get('term') or '').strip()
        if not raw:
            continue

        if term.get('is_regex'):
            compiled = _compile(raw)
            if compiled is None:
                skipped.append({'term': raw, 'reason': 'pattern did not compile'})
                continue
            hit = bool(compiled.search(haystack))
        else:
            candidates = [raw, *(term.get('variants') or [])]
            hit = any(normalize(c) and normalize(c) in haystack for c in candidates)

        if not hit:
            continue

        slug = (term.get('target_group_slug') or '').strip()
        if not term.get('is_explicit', True):
            # Context-dependent: only counts with its community in the post.
            if slug and slug not in groups_in_context:
                continue

        fired.append({
            'id': term.get('id'),
            'term': raw,
            'category': term.get('category', ''),
            'severity_weight': term.get('severity_weight', 5),
            'target_group_slug': slug,
            'is_explicit': bool(term.get('is_explicit', True)),
            'never_flag_when': term.get('never_flag_when') or [],
        })

    return fired, skipped


def match_tropes(text: str, tropes: list, *, groups_in_context: list, parent_text: str = '') -> list:
    """Match tropes, honouring their activation conditions.

    A trope with no curated gate is reported as `ungated` rather than fired. It
    still reaches the classifier as guidance, but it must not flag anything on its
    own, because without the condition it cannot tell piety from a libel.
    """
    haystack = normalize(text)
    parent = normalize(parent_text)
    fired = []
    if not haystack:
        return fired

    for trope in tropes:
        surfaces = trope.get('surface_forms') or []
        if not surfaces:
            continue
        if not any(normalize(s) and normalize(s) in haystack for s in surfaces):
            continue

        slug = (trope.get('target_group_slug') or '').strip()
        requires_group = bool(trope.get('requires_target_group', True))
        topics = trope.get('activation_topics') or []

        if not requires_group:
            reason = 'applies regardless of subject'
        elif not topics:
            # The contract's own instruction: treat as "no deterministic gate
            # yet", not as "always active".
            fired.append({
                'id': trope.get('id'), 'name': trope.get('name', ''),
                'ungated': True, 'activation_reason': 'no activation topics curated yet',
                'severity_weight': trope.get('severity_weight', 5),
                'negative_examples': trope.get('negative_examples') or [],
            })
            continue
        else:
            hit_topic = next(
                (t for t in topics if normalize(t) and normalize(t) in parent), None,
            )
            if hit_topic is None and slug and slug in groups_in_context:
                hit_topic = slug        # the case already established the subject
            if hit_topic is None:
                continue
            reason = f'the post concerns "{hit_topic}"'

        fired.append({
            'id': trope.get('id'),
            'name': trope.get('name', ''),
            'ungated': False,
            'activation_reason': reason,
            'severity_weight': trope.get('severity_weight', 5),
            'negative_examples': trope.get('negative_examples') or [],
        })

    return fired


def evaluate(item: dict, knowledge) -> Match:
    """Match one item against everything the platform sent."""
    parent = item.get('parent_post_text', '') or ''
    markers = knowledge.group_markers()
    groups = post_concerns(parent, markers)

    relevant_tropes = []
    for slug in groups or ['']:
        relevant_tropes.extend(knowledge.tropes_for(slug))
    seen, deduped = set(), []
    for trope in relevant_tropes:
        if trope.get('id') not in seen:
            seen.add(trope.get('id'))
            deduped.append(trope)

    terms, skipped = match_terms(
        item.get('text', ''), knowledge.terms, groups_in_context=groups,
    )
    tropes = match_tropes(
        item.get('text', ''), deduped, groups_in_context=groups, parent_text=parent,
    )

    # Every exemption the fired terms carry, so the classifier is told what must
    # not be flagged rather than being left to infer it.
    hints = sorted({
        exemption for term in terms for exemption in (term.get('never_flag_when') or [])
    })

    return Match(
        fired_terms=terms,
        fired_tropes=[t for t in tropes if not t.get('ungated')],
        topic_groups=groups,
        skipped_terms=skipped,
        exemption_hints=hints,
    )
