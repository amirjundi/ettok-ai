# -*- coding: utf-8 -*-
"""Run the gold set against the curated lexicon, not against invented terms.

The other detection tests build two or three terms by hand and prove the matcher
does what it says. Useful, and blind to the thing most likely to be wrong: the
data. `knowledge_snapshot.json` is the real lexicon as the platform serves it,
written by `manage.py export_knowledge_snapshot`, so a curator's edit that breaks
detection shows up here as a failing test with the reason attached.
"""
import json
from pathlib import Path

import pytest

from plugins.ettok.detect import goldset
from plugins.ettok.platform.knowledge import Knowledge

SNAPSHOT = Path(__file__).with_name('knowledge_snapshot.json')


@pytest.fixture(scope='module')
def knowledge():
    if not SNAPSHOT.exists():
        pytest.skip(
            'No knowledge snapshot. Refresh it with: '
            'manage.py export_knowledge_snapshot <path>'
        )
    data = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    return Knowledge(
        terms=data['terms'], tropes=data['tropes'], cases=data['cases'],
    )


@pytest.fixture(scope='module')
def report(knowledge):
    return goldset.run(knowledge)


def test_every_community_has_a_topic_gate(knowledge):
    """Without markers a community's context-dependent vocabulary cannot fire.

    This was true of eight of the nine communities, including the Assyrians --
    the most targeted group in the survey.
    """
    markers = knowledge.group_markers()
    ungated = [
        group['slug']
        for case in knowledge.cases
        for group in case.get('target_groups', [])
        if not markers.get(group['slug'])
    ]
    assert not ungated, f'communities with no topic markers: {ungated}'


def test_the_gold_set_passes(report):
    assert not report['failures'], '\n' + goldset.describe(report)


@pytest.mark.parametrize('case_id', [c['id'] for c in goldset.CASES])
def test_each_case_individually(report, case_id):
    """Named separately so a failure says which sentence broke, in one line."""
    item = next(r for r in report['results'] if r['id'] == case_id)
    assert item['ok'], f"{item['why']} -- matcher said: {item['explain']}"


def test_no_term_in_the_lexicon_is_left_without_exemptions(knowledge):
    """Every slur appears in reporting, research and refutation.

    A term with no exemptions tells the classifier nothing about what would make
    the match legitimate, so the journalist and the survivor get flagged
    alongside the abuser.
    """
    bare = [t['term'] for t in knowledge.terms if not t.get('never_flag_when')]
    assert not bare, f'{len(bare)} terms carry no exemptions: {bare[:10]}'


def test_active_tropes_can_actually_fire(knowledge):
    """An active trope with no surface forms is skipped and nobody is told.

    Tropes that only ever appeared as images, and the two that turned out to be
    notes rather than detectors, are retired or marked visual instead -- so
    anything still active and still empty is real curation debt.
    """
    inert = [
        t['name'] for t in knowledge.tropes
        if not (t.get('surface_forms') or []) and not t.get('is_visual')
    ]
    assert not inert, f'active tropes that cannot fire: {inert}'
