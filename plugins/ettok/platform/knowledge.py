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

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


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

        There is no version endpoint, so this is derived from what arrived. Counts
        change whenever a curator adds or edits anything, which is enough to tell
        two runs apart when a reviewer asks why they disagreed.
        """
        return {
            'lexicon': f'{len(self.terms)}-terms',
            'tropes': f'{len(self.tropes)}-tropes',
            'cases': f'{len(self.cases)}-cases',
        }

    def tropes_for(self, group_slug: str) -> list:
        """Tropes that can apply to this community.

        Includes tropes with no target group, which fire regardless -- the
        always-on tier. A trope scoped to another community is not relevant here.
        """
        out = []
        for trope in self.tropes:
            slug = (trope.get('target_group_slug') or '').strip()
            if not slug or slug == group_slug:
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
