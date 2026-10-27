# -*- coding: utf-8 -*-
"""Score the whole agent against the platform's human-labelled gold set.

`goldset.py` checks the free tier against built-in sentences. This answers the
pilot's question instead: on real content that people labelled, how often is
the agent right, how often does it raise a false alarm, and how much hate does
it miss? It runs each entry through the same steps a scan does -- match, scope,
model -- so the figures describe what monitoring would actually have done.

It costs one model call per entry, plus one per post no topic marker places.

Three outcomes per entry, because the agent has three:

  ``hate``    the model said hate speech
  ``benign``  the model cleared it, or nothing assessed it at all
  ``review``  the model would not decide (context, image, uncertain) -- it goes
              to a person, which is neither a hit nor a miss

and, separately, whether a finding reached a reviewer at all: a model-cleared
comment where a lexicon term fired still lands in the queue as "cleared".
"""

from __future__ import annotations

from collections import defaultdict

from . import classify as classify_mod
from . import guidance as guidance_mod
from . import match as match_mod
from . import scope as scope_mod
from .normalize import normalize

HATE, BENIGN, REVIEW = 'hate', 'benign', 'review'


def predicted(verdict) -> str:
    if verdict is None or verdict.state == classify_mod.STATE_MODEL_NEGATIVE:
        return BENIGN
    if verdict.state == classify_mod.STATE_MODEL_POSITIVE:
        return HATE
    return REVIEW


def run(ctx, know, entries: list, *, progress=None) -> list:
    """One row per entry: the label, what the agent did, and why."""
    from .. import cases as cases_mod

    cases = [cases_mod.CaseWork.from_payload(c) for c in (know.cases or [])]
    communities, backgrounds = {}, {}
    for case in cases:
        communities.update(case.communities())
        for slug in case.communities():
            backgrounds.setdefault(slug, case.background_for(slug))

    # An entry the classifier is also shown as guidance would score itself.
    shown = {normalize(e.get('comment_text') or '') for e in (know.examples or [])}

    rows = []
    for n, entry in enumerate(entries, start=1):
        item = {
            'text': entry.get('text_content') or '',
            'parent_post_text': entry.get('parent_post_text') or '',
            'parent_media_text': '',
        }
        row = {k: entry.get(k) for k in ('id', 'label', 'target_group', 'dialect', 'hard_case')}
        if normalize(item['text']) in shown:
            row.update(predicted='skipped', finding=False,
                       reason='also a guidance example, so it would grade itself')
            rows.append(row)
            continue

        result = match_mod.evaluate(item, know)
        if result.topic_groups:
            scope = scope_mod.from_markers(result.topic_groups)
        else:
            scope = scope_mod.assess(ctx, post_text=item['parent_post_text'],
                                     communities=communities)
            if set(scope.groups) - set(result.topic_groups):
                result = match_mod.evaluate(item, know, groups=scope.groups)

        verdict = None
        if scope.assessable or result.matched:
            groups = result.topic_groups or scope.groups
            verdict = classify_mod.classify(
                ctx, item, result, versions=know.versions,
                group_background='\n\n'.join(dict.fromkeys(
                    backgrounds[g] for g in groups if backgrounds.get(g))),
                policy=know.policy_text,
                severity_guidance=know.severity_guidance,
                allowed_categories=know.allowed_categories,
                guidance=guidance_mod.select(know.examples or [], match=result, groups=groups),
            )

        guess = predicted(verdict)
        row.update(
            predicted=guess,
            # What the platform turns into a finding (ingest: everything but a
            # model clearance with no rule behind it).
            finding=guess != BENIGN or (verdict is not None and result.matched),
            state=verdict.state if verdict else 'out_of_scope',
            reason=(verdict.reason if verdict else scope.reason) or '',
        )
        rows.append(row)
        if progress:
            progress(n, len(entries), row)
    return rows


def tally(rows: list) -> dict:
    """Precision, recall and the rest over hate/benign labels; ambiguous apart."""
    scored = [r for r in rows if r['predicted'] != 'skipped']
    hate = [r for r in scored if r['label'] == 'hate']
    benign = [r for r in scored if r['label'] == 'benign']

    tp = sum(r['predicted'] == HATE for r in hate)
    fp = sum(r['predicted'] == HATE for r in benign)
    fn = sum(r['predicted'] == BENIGN for r in hate)
    tn = sum(r['predicted'] == BENIGN for r in benign)

    def ratio(a, b):
        return round(a / b, 3) if b else None

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = (round(2 * precision * recall / (precision + recall), 3)
          if precision and recall else None)
    return {
        'entries': len(scored),
        'skipped': len(rows) - len(scored),
        'hate': len(hate), 'benign': len(benign),
        'true_positives': tp, 'false_positives': fp,
        'false_negatives': fn, 'true_negatives': tn,
        'hate_sent_to_review': sum(r['predicted'] == REVIEW for r in hate),
        'benign_sent_to_review': sum(r['predicted'] == REVIEW for r in benign),
        'precision': precision, 'recall': recall, 'f1': f1,
        'accuracy': ratio(tp + tn, tp + tn + fp + fn),
        # The miss that matters in practice: hate no reviewer ever saw.
        'hate_reaching_a_reviewer': ratio(sum(r['finding'] for r in hate), len(hate)),
        'ambiguous': dict(_count(r['predicted'] for r in scored if r['label'] == 'ambiguous')),
    }


def breakdown(rows: list, key: str) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[str(r.get(key) or '-')].append(r)
    return {name: tally(members) for name, members in sorted(groups.items())}


def _count(values):
    out = defaultdict(int)
    for v in values:
        out[v] += 1
    return out


def describe(rows: list) -> str:
    t = tally(rows)
    pct = lambda v: '-' if v is None else f'{v:.0%}'     # noqa: E731
    lines = [
        f"{t['entries']} entries scored ({t['hate']} hate, {t['benign']} benign, "
        f"{sum(t['ambiguous'].values())} ambiguous), {t['skipped']} skipped",
        f"  precision {pct(t['precision'])}   recall {pct(t['recall'])}   "
        f"F1 {pct(t['f1'])}   accuracy {pct(t['accuracy'])}",
        f"  false positives {t['false_positives']}   false negatives {t['false_negatives']}   "
        f"sent to a person: {t['hate_sent_to_review']} hate, {t['benign_sent_to_review']} benign",
        f"  hate that reached a reviewer: {pct(t['hate_reaching_a_reviewer'])}",
    ]
    for key, title in (('target_group', 'community'), ('dialect', 'dialect'),
                       ('hard_case', 'hard case')):
        lines.append(f'\nBy {title}:')
        for name, s in breakdown(rows, key).items():
            lines.append(f"  {name:<20} n={s['entries']:<4} precision {pct(s['precision']):>4}  "
                         f"recall {pct(s['recall']):>4}  FP {s['false_positives']}  "
                         f"FN {s['false_negatives']}")
    return '\n'.join(lines)
