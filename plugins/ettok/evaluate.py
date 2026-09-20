"""Measuring the classifier against examples it was never shown.

From the contextual implementation guide of 20 September 2026, section 13.

This produces a number. Almost everything here is about stopping that number
being read as more than it is.

**It is a result over this set.** Nine examples and nine hundred look identical
once they are a percentage on a slide, so the sample size travels with every
figure and the summary says so in words.

**Abstention is reported beside accuracy, not folded into it.** A classifier
that answers "I cannot tell" to every hard case and is right about the easy
ones scores beautifully and is useless. The guide is explicit: "abstaining on
every hard example is not a successful detector."

**Nothing here sets a threshold.** No 95%, no 99%, no pass or fail. The
organisation decides what is good enough after seeing what the numbers
actually are, which is the opposite order from inventing a target and then
measuring towards it.

And the examples used are the held-out ones, fetched from a separate endpoint
that the normal sync cannot reach. An evaluation example that has been in a
prompt measures memory.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .detect import classify as classify_mod
from .detect import match as match_mod

log = logging.getLogger(__name__)

EVALUATION_VERSION = '2026-09-20'

OUTCOME_HATE = 'hate_speech'
OUTCOME_NOT_HATE = 'not_hate_speech'
OUTCOME_UNCLEAR = 'insufficient_context'

# How the six result states line up against what a curator expected. The
# undecided states are not wrong answers and not right ones -- they are
# abstentions, and they are counted separately for exactly that reason.
_DECIDED = {
    classify_mod.STATE_MODEL_POSITIVE: OUTCOME_HATE,
    classify_mod.STATE_MODEL_NEGATIVE: OUTCOME_NOT_HATE,
}
_ABSTAINED = {
    classify_mod.STATE_NEEDS_CONTEXT,
    classify_mod.STATE_NEEDS_VISUAL,
    classify_mod.STATE_UNCERTAIN,
}


def _item_from(example: dict) -> dict:
    """An example, shaped like something the collector produced.

    The same fields the real path carries, so the classifier is doing the
    same work it does in a run. An evaluation that exercises a different code
    path measures that path.
    """
    return {
        'text': example.get('comment_text') or '',
        'parent_post_text': example.get('parent_post_text') or '',
        'parent_media_text': example.get('media_description') or '',
        'reply_to_text': example.get('reply_to_text') or '',
        'reply_to_author': example.get('reply_to_author') or '',
        'ancestors': example.get('ancestors') or [],
        'is_reply': bool(example.get('reply_to_text')),
        'url': '', 'parent_post_url': '', 'platform': 'evaluation',
        'author_name': '', 'author_id': '', 'author_url': '',
    }


def run(ctx, examples: list, know, *, max_spend_usd: float = 1.0,
        guidance: list = None) -> dict:
    """Classify each held-out example and compare with what was expected.

    `guidance` is the published example set, passed in so the evaluation can
    show the classifier the same guidance a real run would -- but never the
    held-out examples themselves, which is the whole point of them.
    """
    from .detect import guidance as guidance_mod

    held_out_ids = {e.get('id') for e in examples}
    library = [e for e in (guidance or []) if e.get('id') not in held_out_ids]
    leaked = len(guidance or []) - len(library)

    results, spent = [], 0.0
    stopped = None

    for example in examples:
        if spent >= max_spend_usd:
            stopped = 'budget'
            break

        item = _item_from(example)
        try:
            result = match_mod.evaluate(item, know, case_id=example.get('case_id'))
            picked = guidance_mod.select(
                library, case_id=example.get('case_id'), match=result,
                groups=example.get('target_groups') or [])
            verdict = classify_mod.classify(
                ctx, item, result, versions=know.versions,
                policy=know.policy_text,
                severity_guidance=know.severity_guidance,
                allowed_categories=know.allowed_categories,
                guidance=picked)
        except Exception as exc:                              # noqa: BLE001
            # A provider failure is not a wrong answer. Counting it as one
            # would make an outage look like a quality problem and send
            # somebody tuning prompts to fix a network.
            log.warning('ettok: evaluation item %s failed: %s',
                        example.get('id'), exc)
            results.append({'id': example.get('id'), 'error': str(exc)[:200]})
            continue

        spent += verdict.cost_usd if verdict.cost_usd is not None else 0.002
        expected = example.get('expected_outcome')
        got = _DECIDED.get(verdict.state)

        results.append({
            'id': example.get('id'),
            'revision': example.get('revision'),
            'expected': expected,
            'state': verdict.state,
            'decided_as': got,
            'abstained': verdict.state in _ABSTAINED,
            # Right, wrong, or neither. `None` where the classifier declined
            # to answer, which is a third thing and not a half-mark.
            'correct': (None if verdict.state in _ABSTAINED
                        else (got == expected if got else None)),
            'language': example.get('language') or 'unknown',
            'category': verdict.category or '',
            'reason': verdict.reason,
            'cost_usd': verdict.cost_usd,
        })

    return summarise(results, spent=spent, stopped=stopped,
                     offered=len(examples), leaked=leaked,
                     versions=know.versions)


def summarise(results: list, *, spent: float = 0.0, stopped=None,
              offered: int = 0, leaked: int = 0, versions=None) -> dict:
    """Turn the per-item results into figures that cannot be over-read."""
    scored = [r for r in results if 'error' not in r]
    errors = [r for r in results if 'error' in r]

    decided = [r for r in scored if not r['abstained'] and r['correct'] is not None]
    abstained = [r for r in scored if r['abstained']]
    correct = [r for r in decided if r['correct']]

    # The two errors, kept apart. They cost entirely different things here: a
    # missed attack costs one observation, a false positive costs the
    # credibility of every finding beside it in a referral.
    missed = [r for r in decided
              if r['expected'] == OUTCOME_HATE and not r['correct']]
    false_alarms = [r for r in decided
                    if r['expected'] == OUTCOME_NOT_HATE and not r['correct']]

    def _by(field):
        out = {}
        for row in scored:
            key = row.get(field) or 'unknown'
            bucket = out.setdefault(key, {'decided': 0, 'correct': 0,
                                          'abstained': 0})
            if row['abstained']:
                bucket['abstained'] += 1
            elif row['correct'] is not None:
                bucket['decided'] += 1
                bucket['correct'] += 1 if row['correct'] else 0
        return out

    accuracy = (len(correct) / len(decided)) if decided else None

    return {
        'evaluation_version': EVALUATION_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'knowledge_versions': versions or {},

        'offered': offered,
        'attempted': len(results),
        'scored': len(scored),
        'errors': len(errors),

        'decided': len(decided),
        'correct': len(correct),
        'abstained': len(abstained),
        'missed_attacks': len(missed),
        'false_alarms': len(false_alarms),

        # A ratio over decided items only, which is why the abstention count
        # sits beside it and the sentence below repeats both.
        'accuracy_on_decided': round(accuracy, 3) if accuracy is not None else None,
        'abstention_rate': (round(len(abstained) / len(scored), 3)
                            if scored else None),

        'by_language': _by('language'),
        'by_category': _by('category'),

        'spend_usd': round(spent, 4),
        'stopped': stopped,
        # If this is ever non-zero something has gone wrong upstream: an
        # example cannot be both held out and in the guidance library.
        'held_out_items_removed_from_guidance': leaked,

        'summary': _sentence(len(correct), len(decided), len(abstained),
                             len(scored), len(missed), len(false_alarms)),
        'caveat': (
            'A result over this held-out set, not accuracy on live material. '
            'The examples were written or chosen by the monitoring team and '
            'are not a random sample of anything. No threshold is implied: '
            'what counts as good enough is for the organisation to decide '
            'after seeing these numbers, not before.'
        ),
    }


def _sentence(correct, decided, abstained, scored, missed, false_alarms):
    """The figures in words, because a bare percentage loses its denominator
    the moment it is copied anywhere."""
    if not scored:
        return 'Nothing was scored.'
    if not decided:
        return (f'The classifier declined to decide all {scored} example(s). '
                f'That is not a score; a detector that abstains on everything '
                f'is not a detector.')
    return (
        f'{correct} of {decided} decided example(s) matched what a curator '
        f'expected, out of {scored} shown. It declined to decide {abstained}. '
        f'{missed} attack(s) were read as harmless and {false_alarms} harmless '
        f'comment(s) were read as attacks -- these cost different things and '
        f'should not be averaged.'
    )
