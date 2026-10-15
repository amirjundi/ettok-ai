# -*- coding: utf-8 -*-
"""What the run does with the knowledge it was sent, past the first match.

Four things the knowledge carried and the run dropped: the communities the
model's scope check found, the case's own account of the event, every
community's background on a shared post, and the words of an attack no
curated entry caught. Plus the matcher firing a three-letter term inside
unrelated words.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from plugins.ettok import scan as scan_mod
from plugins.ettok.detect import match as match_mod
from plugins.ettok.platform.knowledge import Knowledge


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    monkeypatch.setenv('ETTOK_AGENT_ID', 'test-agent')
    monkeypatch.setenv('ETTOK_AGENT_KEY', 'test-key')
    monkeypatch.setenv('ETTOK_PLATFORM_URL', 'http://platform.invalid')
    return path


def knowledge():
    return Knowledge(
        terms=[{
            # Context-only: an insult anywhere, an attack only about Yazidis.
            'id': 7, 'term': 'dirty people', 'is_explicit': False,
            'is_regex': False, 'target_group_slug': 'yazidi', 'severity_weight': 5,
            'category': 'slur', 'variants': [], 'never_flag_when': [],
        }],
        tropes=[],
        cases=[{
            'id': 1, 'title': 'Sinjar anniversary', 'state': 'active',
            'trigger': 'Comments under coverage of the 3 August remembrance.',
            'target_groups': [
                {'slug': 'yazidi', 'name_en': 'Yazidis',
                 'topic_markers': ['sinjar'], 'background': 'Yazidi background.'},
                {'slug': 'assyrian', 'name_en': 'Assyrians',
                 'topic_markers': ['nineveh plains'], 'background': 'Assyrian background.'},
            ],
            'seed_sources': [{'kind': 'page', 'value': 'X'}],
            'limits': {'items_remaining': None, 'cost_remaining_usd': None},
            'suggests_closing': False,
        }],
        categories=[{'slug': 'exclusion_call'}],
        platform_release_id='rel-1',
    )


class Ctx:
    def __init__(self, scope_reply, classify_reply):
        self._ettok_knowledge = knowledge()
        self.llm = mock.MagicMock()

        def answer(**kwargs):
            reply = scope_reply if kwargs.get('task') == 'ettok_scope' else classify_reply
            return mock.MagicMock(parsed=reply, usage=mock.MagicMock(
                cost_usd=0.001, total_tokens=100))

        self.llm.complete_structured.side_effect = answer

    def get_config(self, key, default=None):
        return default

    def prompts(self):
        return [c.kwargs['input'][0]['text'] for c in self.llm.complete_structured.call_args_list
                if c.kwargs.get('task') == 'ettok_classify']


def comment(text, post):
    return {'text': text, 'parent_post_text': post, 'url': 'https://facebook.com/c/' + text[:5],
            'parent_post_url': 'https://facebook.com/p/1', 'platform': 'facebook'}


def queued(endpoint):
    from plugins.ettok.store import schema
    return [json.loads(r['payload']) for r in schema.connect().execute(
        'SELECT payload FROM outbox WHERE endpoint = ?', (endpoint,))]


NO_MARKER_POST = 'The families who returned to the mountain last spring'
ABOUT_YAZIDIS = {'in_scope': True, 'reason': 'about Yazidi returnees', 'communities': ['Yazidis']}
NOT_HATE = {'is_hate_speech': False, 'reason': 'ordinary'}


def test_a_community_the_model_found_switches_on_context_terms(home):
    ctx = Ctx(ABOUT_YAZIDIS, NOT_HATE)
    result = scan_mod.run(ctx, items=[comment('they are dirty people', NO_MARKER_POST)],
                          submit=False)

    prompt = ctx.prompts()[0]
    assert 'TERMS THAT MATCHED' in prompt and 'dirty people' in prompt
    assert result['flagged'] == 1


def test_the_case_trigger_and_every_background_reach_the_model(home):
    ctx = Ctx(ABOUT_YAZIDIS, NOT_HATE)
    scan_mod.run(ctx, items=[comment('hello', 'sinjar and the nineveh plains')], submit=False)

    prompt = ctx.prompts()[0]
    assert '3 August remembrance' in prompt
    assert 'Yazidi background.' in prompt and 'Assyrian background.' in prompt


def test_hate_no_entry_caught_is_proposed_to_curators(home):
    hate = {'is_hate_speech': True, 'reason': 'calls for expulsion',
            'category': 'exclusion_call', 'key_phrase': 'go back to the mountain'}
    ctx = Ctx(ABOUT_YAZIDIS, hate)
    scan_mod.run(ctx, items=[
        comment('they should go back to the mountain', 'sinjar today'),
        comment('all of them should go back to the mountain', 'sinjar today'),
    ], submit=False)

    [payload] = queued('lexicon-gaps/')
    [gap] = payload['gaps']
    assert gap['suggested_term'] == 'go back to the mountain'
    assert gap['occurrences'] == 2
    assert gap['suggested_target_group'] == 'yazidi'
    assert gap['suggested_category'] == 'exclusion_call'


def test_a_phrase_the_model_invented_is_not_proposed(home):
    hate = {'is_hate_speech': True, 'reason': 'r', 'key_phrase': 'words nobody wrote'}
    scan_mod.run(Ctx(ABOUT_YAZIDIS, hate),
                 items=[comment('they should leave', 'sinjar today')], submit=False)

    assert queued('lexicon-gaps/') == []


@pytest.mark.parametrize('text, hit', [
    ('هذول قرج', True),
    ('والقرج كلهم', True),       # a prefixed form still counts
    ('مقرجين', False),           # the letters inside another word do not
])
def test_a_three_letter_term_has_to_be_a_word(text, hit):
    term = {'id': 1, 'term': 'قرج', 'is_explicit': True, 'is_regex': False,
            'target_group_slug': 'yazidi', 'variants': []}
    fired, _ = match_mod.match_terms(text, [term], groups_in_context=[])
    assert bool(fired) is hit
