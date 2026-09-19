"""The detection knowledge, fetched per run and never written down.

The platform owns the lexicon, the tropes, the exemptions and the rubric. This
agent holds none of it between runs, which buys three things at once: a curator's
edit reaches every agent on its next run with no redeployment, the knowledge is
backed up with the rest of the platform database, and a stolen or reimaged laptop
leaks nothing that is not already on the server.

It is also why a failed fetch aborts the run rather than falling back to something
stale. Findings produced against unknown knowledge cannot be attributed to a
version, and an audit chain with a hole in it is not an audit chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)



# Bumped when the matcher, the normalizer or the rubric changes shape.
#
# A release ID has to move when the *meaning* of the knowledge moves, not only
# when its content does. Two runs that classified the same comment differently
# because this code changed in between otherwise carry the same version string,
# and a reviewer asking why the agent changed its mind is told nothing did.
SCHEMA_VERSION = '2026-09-19'


def _release_id(rows: list, fields: tuple = None) -> str:
    """A stable identifier for one body of knowledge.

    Order-independent, because the platform does not promise one and a
    re-ordered response is not a new release.

    Every field, unless a caller names a subset. The allowlist this replaces
    was meant to keep cosmetic edits from invalidating past classifications,
    and it omitted `is_explicit` and `case_id` on terms, and the activation
    topics, plural target groups, negation rule, examples and visual status on
    tropes. Every one of those decides whether something matches -- two probes
    in the architecture review flipped a match from true to false while the
    recorded version stayed identical. There is also no longer such a thing as
    a field no matcher reads: descriptions, examples and counter-speech
    examples are all in the prompt now.

    An unnecessary version bump costs a reviewer a moment. A missed one is a
    hole in the audit chain, so the default errs the other way.
    """
    digests = sorted(
        hashlib.sha256(
            json.dumps(row if fields is None else [row.get(field) for field in fields],
                       ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()
        for row in (rows or [])
    )
    combined = hashlib.sha256(
        (SCHEMA_VERSION + ''.join(digests)).encode('utf-8')).hexdigest()[:8]
    return f'{len(rows or [])}@{combined}'


class KnowledgeError(RuntimeError):
    """The run cannot proceed. Never downgrade this into a warning."""


@dataclass
class Knowledge:
    """One run's worth of detection knowledge. In memory, discarded at exit."""

    terms: list = field(default_factory=list)
    tropes: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    config: dict = field(default_factory=dict)
    skipped_terms: list = field(default_factory=list)

    @property
    def versions(self) -> dict:
        """Recorded on every classification, so a verdict can be reproduced.

        A digest of the content, not a count of it. The count was wrong in the
        one case it most needed to be right: a curator who edits a term, or
        replaces one with another, leaves the count unchanged, so two runs with
        different detection knowledge carried the same version string and a
        reviewer asking why the agent changed its mind was told nothing had.

        Counted length stays in the string as a readable prefix -- "42@a1b2c3d4"
        says at a glance how much knowledge was loaded -- but the digest after it
        is what actually identifies the release.
        """
        return {
            'lexicon': _release_id(self.terms),
            'tropes': _release_id(self.tropes),
            # Cases keep a subset, and are the reason `fields` still exists: the
            # payload carries the schedule and the remaining budget, which move
            # on every run without changing what can be detected. Hashing those
            # would give a version string that never repeats and so identifies
            # nothing. `target_groups` is nested here, so the topic markers and
            # background that do decide detection are already covered.
            'cases': _release_id(self.cases, ('id', 'title', 'state', 'target_groups')),
        }

    def tropes_for(self, group_slug: str) -> list:
        """Tropes that can apply to this community.

        Includes tropes with no target group, which fire regardless -- the
        always-on tier. A trope scoped to another community is not relevant here.
        """
        out = []
        for trope in self.tropes:
            # Every group, not the first one. `target_group_slug` is the first
            # element of `target_groups`, kept for rows predating the
            # many-to-many table -- so filtering on it here dropped a trope
            # curated against two communities from the second one's case
            # before the activation gate could ever see it.
            slugs = [str(g).strip() for g in (trope.get('target_groups') or [])
                     if str(g).strip()]
            if not slugs:
                single = (trope.get('target_group_slug') or '').strip()
                slugs = [single] if single else []
            if not slugs or group_slug in slugs:
                out.append(trope)
        return out

    def group_markers(self) -> dict:
        """Topic markers by group slug, from the open cases.

        These answer "does this post concern this community", which decides whether
        the comments under it are worth reading at all. They are not hate terms and
        must never be used to flag anything.
        """
        markers: dict = {}
        for case in self.cases:
            for group in case.get('target_groups', []):
                slug = group.get('slug')
                if slug:
                    markers.setdefault(slug, []).extend(group.get('topic_markers') or [])
        return {slug: sorted(set(values)) for slug, values in markers.items()}


def fetch(client, *, languages: Optional[list] = None) -> Knowledge:
    """Pull everything this run needs, in one place, before anything is collected."""
    try:
        heartbeat = client.heartbeat({'status': 'syncing'})
        tasks = client.tasks()
        lexicon = client.lexicon(languages=languages)
        tropes = client.tropes()
        accounts = client.accounts()
    except Exception as exc:
        raise KnowledgeError(
            f'could not fetch detection knowledge: {exc}. The run is aborted rather '
            f'than collecting against knowledge of unknown age.'
        ) from exc

    terms = lexicon.get('terms', []) or []
    # `lexicon/` carries the tropes array too, so a run that wants both can make one
    # call. Prefer the dedicated endpoint and fall back to the embedded copy.
    trope_rows = tropes.get('tropes') or lexicon.get('tropes') or []

    knowledge = Knowledge(
        terms=terms,
        tropes=trope_rows,
        cases=tasks.get('cases', []) or [],
        accounts=accounts.get('accounts', accounts.get('monitoring_accounts', [])) or [],
        config=heartbeat.get('config', {}) or {},
    )

    ungated = sum(
        1 for t in knowledge.tropes
        if t.get('requires_target_group') and not (t.get('activation_topics') or [])
    )
    if ungated:
        # Not a failure. The contract is explicit that an empty activation_topics
        # means "no deterministic gate yet", never "always active", and an operator
        # should know how much of the trope set is still guidance only.
        log.info('ettok: %d of %d tropes have no activation gate yet',
                 ungated, len(knowledge.tropes))

    return knowledge
