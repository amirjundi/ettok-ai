"""The model is told what was said, never who said it.

Ettok.net publishes a privacy policy, and it makes this promise in as many
words: "Only the text and its surrounding context are sent; the name, account
and profile link of the person who posted it are not."

That sentence used to be checked against the platform's own classifier prompt.
The platform no longer classifies -- the agent is the only thing that judges a
collected comment -- so the prompt the promise is about is the one built here,
and the check belongs here with it. The platform keeps the other half: that
nothing in that repository sends a collected comment to a model at all.

Why it is worth a test rather than care. The people in this material are
already targets, and an identity leaving the organisation is the harm the whole
system exists to avoid, not an inconvenience. A prompt is also exactly the kind
of string that grows a field during a debugging session and keeps it.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

from plugins.ettok.detect import classify as classify_mod
from plugins.ettok.detect.match import Match

# What the collector gathers about a person, alongside what they wrote. Every
# one of these is in the item dict that reaches `build_prompt`.
IDENTIFYING = {
    'author_name': 'Bilal Al-Rashid',
    'author_id': 'fa:100012345',
    'author_url': 'https://facebook.com/bilal.alrashid',
    'url': 'https://facebook.com/groups/1/posts/2?comment_id=3',
    'parent_post_url': 'https://facebook.com/groups/1/posts/2',
}

ITEM = {
    'text': 'they are devil worshippers',
    'parent_post_text': 'sinjar anniversary today',
    'parent_media_text': 'a photo of a crowd',
    **IDENTIFYING,
}


def matched():
    return Match(
        fired_terms=[{'id': 1, 'term': 'devil worshippers', 'category': 'slur',
                      'severity_weight': 9, 'is_explicit': True}],
        fired_tropes=[], ungated_tropes=[], topic_groups=['yazidi'],
    )


def test_no_identifying_value_reaches_the_prompt():
    """The promise, checked by value rather than by field name -- a prompt that
    interpolated the name under a different label would still break it."""
    prompt = classify_mod.build_prompt(ITEM, matched())
    for field, value in IDENTIFYING.items():
        assert value not in prompt, f'{field} reached the classifier'


def test_the_words_and_their_context_do_reach_it():
    """The other half. Withholding the identity must not withhold the thing
    being judged, or the classification is worthless and the item is lost."""
    prompt = classify_mod.build_prompt(ITEM, matched())
    assert ITEM['text'] in prompt
    assert ITEM['parent_post_text'] in prompt
    assert ITEM['parent_media_text'] in prompt


def test_an_account_handle_inside_the_comment_is_not_stripped():
    """A comment that names somebody is evidence about what was said to them.
    The promise is about the poster's identity as metadata, not about censoring
    the words -- and quietly editing the text would corrupt the evidence."""
    item = dict(ITEM, text='@someone they are devil worshippers')
    assert '@someone' in classify_mod.build_prompt(item, matched())


def test_the_prompt_has_no_placeholder_for_one():
    """Cheap, and it catches the reintroduction rather than the leak: a format
    field added here would be filled by whatever the caller passes."""
    prompt = classify_mod.build_prompt(ITEM, matched())
    for field in ('author_name', 'author_id', 'author_url', 'content_url'):
        assert '{' + field + '}' not in prompt
        assert field not in prompt
