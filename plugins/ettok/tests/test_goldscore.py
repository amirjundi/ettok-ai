# -*- coding: utf-8 -*-
"""The pilot's accuracy figures: counted right, and never graded on the answers."""

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect import goldscore
from plugins.ettok.detect import scope as scope_mod
from plugins.ettok.platform.knowledge import Knowledge

POS, NEG = classify_mod.STATE_MODEL_POSITIVE, classify_mod.STATE_MODEL_NEGATIVE


def entry(n, text, label, **extra):
    return {'id': n, 'text_content': text, 'parent_post_text': 'post',
            'label': label, 'target_group': 'yazidi', 'dialect': '', 'hard_case': False,
            **extra}


def test_run_follows_the_scan_and_never_grades_a_guidance_example(monkeypatch):
    model = {'hit': POS, 'missed': NEG, 'alarm': POS, 'quiet': NEG,
             'unsure': classify_mod.STATE_NEEDS_CONTEXT}
    monkeypatch.setattr(goldscore.scope_mod, 'assess', lambda ctx, **kw: scope_mod.Scope(
        status=scope_mod.STATUS_IN_SCOPE, groups=['yazidi']))
    monkeypatch.setattr(goldscore.classify_mod, 'classify', lambda ctx, item, match, **kw:
                        classify_mod.Verdict(state=model[item['text']]))
    know = Knowledge(examples=[{'comment_text': 'shown'}])

    rows = goldscore.run(None, know, [
        entry(1, 'hit', 'hate'), entry(2, 'missed', 'hate'), entry(3, 'unsure', 'hate'),
        entry(4, 'alarm', 'benign'), entry(5, 'quiet', 'benign'),
        entry(6, 'shown', 'hate'),
    ])
    t = goldscore.tally(rows)

    assert [r['predicted'] for r in rows] == ['hate', 'benign', 'review', 'hate', 'benign', 'skipped']
    assert (t['true_positives'], t['false_negatives'], t['false_positives'], t['true_negatives']) == (1, 1, 1, 1)
    assert t['hate_sent_to_review'] == 1 and t['skipped'] == 1
    assert t['precision'] == 0.5 and t['recall'] == 0.5
    # The cleared hate never reached anybody; the unsure one did.
    assert t['hate_reaching_a_reviewer'] == round(2 / 3, 3)
    assert 'precision 50%' in goldscore.describe(rows)


def test_out_of_scope_and_unmatched_is_benign_without_a_model_call(monkeypatch):
    monkeypatch.setattr(goldscore.scope_mod, 'assess', lambda ctx, **kw: scope_mod.Scope(
        status=scope_mod.STATUS_OUT_OF_SCOPE))

    def never(*a, **kw):
        raise AssertionError('an out-of-scope post must not reach the model')
    monkeypatch.setattr(goldscore.classify_mod, 'classify', never)

    rows = goldscore.run(None, Knowledge(), [entry(1, 'football', 'hate')])

    assert rows[0]['predicted'] == 'benign' and rows[0]['finding'] is False


def test_ambiguous_is_reported_apart_and_empty_sets_do_not_divide_by_zero():
    rows = [{'label': 'ambiguous', 'predicted': 'hate', 'finding': True}]
    t = goldscore.tally(rows)
    assert t['ambiguous'] == {'hate': 1}
    assert t['precision'] is None and t['f1'] is None
