"""The agent's own reading of an item. Advisory, and the first thing to cut.

The platform re-evaluates everything submitted and its verdict is the one that
stands, which makes this the *least* essential thing the agent spends money on.
That is why the budget ladder sacrifices it before discovery: the platform
reproduces classification authoritatively, and nothing anywhere reproduces
discovery.

Two properties are not negotiable.

The call is isolated. It is built fresh each time and never inherits the agent's
conversation, because the runtime compresses long conversations and the previous
attempt at this system lost its lexicon exactly that way -- recall degrading
through a run with no error and no signal.

The prompt is bounded. Only the terms and tropes that actually matched this item
go into it, so the prompt stays the same size whether the dictionary holds a
hundred entries or ten thousand. Pasting the whole lexicon is the other half of
how the previous attempt failed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

TIER_MATCHED_ONLY = 'matched_only'
TIER_CLASSIFIED = 'classified'

_SCHEMA = {
    'type': 'object',
    'properties': {
        'is_hate_speech': {'type': 'boolean'},
        'category': {'type': 'string'},
        'severity': {'type': 'integer'},
        'reason': {'type': 'string'},
        'exemption_applied': {'type': 'string'},
        'requires_visual': {'type': 'boolean'},
    },
    'required': ['is_hate_speech', 'reason'],
}


@dataclass
class Verdict:
    is_hate_speech: bool = False
    category: str = ''
    severity: Optional[int] = None
    reason: str = ''
    exemption_applied: str = ''
    requires_visual: bool = False
    tier: str = TIER_MATCHED_ONLY
    versions: dict = field(default_factory=dict)

    def as_payload(self, match) -> dict:
        """The `agent_verdict` block the platform contract accepts."""
        return {
            'is_hate_speech': self.is_hate_speech,
            'category': self.category,
            'severity': self.severity,
            'reason': self.reason,
            'exemption_applied': self.exemption_applied or None,
            'requires_visual': self.requires_visual,
            'tier': self.tier,
            'fired_terms': [t.get('id') for t in match.fired_terms if t.get('id')],
            'fired_tropes': [
                {'id': t.get('id'), 'activation_reason': t.get('activation_reason')}
                for t in match.fired_tropes
            ],
            'versions': self.versions,
        }


def from_match_only(match, versions: dict) -> Verdict:
    """The no-budget verdict: what matched, and nothing inferred beyond it.

    This is a complete, submittable finding. A month with no funding still
    produces evidence-backed items for review -- it produces them without an
    opinion attached, which the platform supplies anyway.
    """
    severity = max(
        [t.get('severity_weight', 5) for t in match.fired_terms]
        + [t.get('severity_weight', 5) for t in match.fired_tropes]
        + [0]
    ) or None
    return Verdict(
        is_hate_speech=bool(match.matched),
        category=(match.fired_terms[0].get('category', '') if match.fired_terms else ''),
        severity=severity,
        reason=match.explain(),
        tier=TIER_MATCHED_ONLY,
        versions=versions,
    )


# The standard, written down. It used to exist nowhere: the model was told it
# "classifies hate speech for a human rights monitoring system in Iraq" and left
# to supply its own definition, which is why two runs, and the agent and the
# platform, could reach different answers on the same comment.
#
# The exclusions are not softness. This system exists to produce evidence that
# an outside body will act on, and a package that turns out to contain a
# journalist quoting a slur, or a survivor reclaiming one, costs more than every
# attack it missed that week.
WHAT_COUNTS = """WHAT COUNTS AS HATE SPEECH HERE:
- Dehumanising a community or its members: calling them animals, filth, a
  disease, devil-worshippers, not real people.
- Denying their identity, religion or history, or denying a massacre against
  them, or saying they deserved it.
- Calling for or endorsing violence, expulsion, exclusion or discrimination
  against them.
- Slurs and epithets aimed at the community, including coded or misspelt ones.
- Asserting the community is collectively guilty, disloyal, or a danger.

WHAT DOES NOT COUNT, however offensive the words look:
- Quoting, reporting, studying or refuting hate speech. A journalist, a
  researcher or a survivor repeating a slur to condemn it is not attacking
  anybody.
- A member of the community using a term about themselves.
- Ordinary religious language with no community as its subject. "I seek refuge
  in God from the accursed devil" is common piety and becomes the devil-worship
  libel only under content about Yazidis.
- Criticism of a government, a party, an armed group, a policy or a named
  individual. Attacking a politician is not attacking their community.
- Disagreement, insult or rudeness aimed at one person for something they said,
  with no reference to their community.
- Discussion of the community that is merely negative, inaccurate or clumsy
  without hostility.

WHEN YOU CANNOT TELL, say it is not hate speech and give your doubt as the
reason. A person reviews everything you mark; an unflagged comment they never
see costs one observation, and a wrongly flagged one costs the credibility of
every finding beside it."""


def build_prompt(item: dict, match, group_background: str = '') -> str:
    """Assemble the classification prompt for one item.

    Everything here is either the item, or something that matched the item. The
    dictionary's size never enters into it.

    What a curated trope actually says -- its description, its attested example,
    the benign uses and the counter-speech -- is included. It used to send the
    trope's *name* and nothing else, so the model was told that a pattern called
    "Identity stripping" had fired and never what that means, while the platform
    classifier judging the same comment had the full text. Two classifiers
    working from different rules disagree, and the agent was the one guessing.
    """
    lines = [
        'Decide whether this social media comment attacks a minority community in Iraq.',
        '',
        WHAT_COUNTS,
        '',
        f'COMMENT: {item.get("text", "")}',
    ]

    parent = (item.get('parent_post_text') or '').strip()
    if parent:
        lines += ['', f'IT REPLIES TO A POST THAT SAYS: {parent}']
    else:
        lines += [
            '',
            'NO PARENT POST WAS SUPPLIED. Judge only unambiguous hostility. Do not infer '
            'a context-dependent attack without the context that would establish it.',
        ]

    media = (item.get('parent_media_text') or '').strip()
    if media:
        lines += [f"TEXT IN THAT POST'S IMAGE OR VIDEO: {media}"]

    if group_background:
        lines += ['', f'ABOUT THE COMMUNITY CONCERNED: {group_background}']

    if match.fired_terms:
        lines += ['', 'TERMS THAT MATCHED:']
        lines += [
            f'- "{t["term"]}" ({t.get("category") or "uncategorised"},'
            f' severity {t.get("severity_weight")})'
            + ('' if t.get('is_explicit', True) else ' [counts only with this community as the subject]')
            for t in match.fired_terms
        ]
        lines += [
            'A term matching is not by itself the answer. It is why this comment was '
            'read; you decide whether it is an attack.',
        ]

    if match.fired_tropes:
        lines += ['', 'PATTERNS THAT FIRED:']
        for trope in match.fired_tropes:
            lines += _trope_lines(trope)

    if match.ungated_tropes:
        lines += [
            '',
            'PATTERNS WITH NO CURATED CONDITION YET. These matched the words but nobody '
            'has recorded what subject makes them an attack, so they are guidance and '
            'never grounds on their own:',
        ]
        for trope in match.ungated_tropes:
            lines += _trope_lines(trope)

    if match.exemption_hints:
        lines += [
            '',
            'THESE USES MUST NOT BE FLAGGED, whatever else matched: '
            + ', '.join(match.exemption_hints)
            + '.',
        ]

    lines += [
        '',
        'SEVERITY: 9-10 a direct threat or a call for violence; 6-8 dehumanisation, '
        'an extreme slur, or denial of a massacre; 3-5 a derogatory stereotype or an '
        'insult aimed at the community; 1-2 subtle bias or a divisive framing.',
        '',
        'Answer as JSON: is_hate_speech (boolean), category, severity 1-10, reason '
        '(one sentence, naming what makes it an attack or why it is not), '
        'exemption_applied (which exemption applied, or empty), '
        'requires_visual (true if the judgement depends on an image you cannot see).',
    ]
    return '\n'.join(lines)


def _trope_lines(trope: dict) -> list:
    """One curated pattern, with everything the curator wrote about it.

    Counter-speech is listed separately from the benign uses because it is the
    harder case and the commonest false positive: somebody quoting or arguing
    against the attack, in language that looks exactly like the attack.
    """
    out = [f'- {trope["name"]} -- {trope.get("activation_reason", "")}']
    if trope.get('description'):
        out.append(f'    what it is: {trope["description"]}')
    if trope.get('example'):
        out.append(f'    attested example: "{trope["example"]}"')
    for benign in (trope.get('negative_examples') or [])[:3]:
        out.append(f'    NEVER flag this use: "{benign}"')
    for counter in (trope.get('counter_speech_examples') or [])[:3]:
        out.append(f'    NOT an attack -- this is somebody opposing it: "{counter}"')
    return out


def classify(ctx, item: dict, match, *, versions: dict, group_background: str = '') -> Verdict:
    """Ask the model, once, in isolation.

    Falls back to the match-only verdict on any failure. A classifier that cannot
    be reached is a reason to submit with less opinion attached, never a reason to
    lose the finding.
    """
    prompt = build_prompt(item, match, group_background)

    try:
        result = ctx.llm.complete_structured(
            instructions=(
                'You classify hate speech for a human rights monitoring system in Iraq, '
                'against the written standard in the prompt and not against your own. '
                'Your judgement is advisory: a platform re-evaluates it and a human '
                'reviews it before anything is reported. Never claim more confidence '
                'than the evidence supports, and judge the comment together with the '
                'post it replies to rather than alone.'
            ),
            input=[{'type': 'text', 'text': prompt}],
            json_schema=_SCHEMA,
            task='ettok_classify',
            temperature=0,
            purpose='ettok-classify',
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else json.loads(result.parsed)
    except Exception as exc:
        log.warning('ettok: classification unavailable, submitting match-only: %s', exc)
        return from_match_only(match, versions)

    return Verdict(
        is_hate_speech=bool(parsed.get('is_hate_speech')),
        category=str(parsed.get('category') or ''),
        severity=parsed.get('severity'),
        reason=str(parsed.get('reason') or ''),
        exemption_applied=str(parsed.get('exemption_applied') or ''),
        requires_visual=bool(parsed.get('requires_visual')),
        tier=TIER_CLASSIFIED,
        versions=versions,
    )
