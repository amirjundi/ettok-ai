"""Fold Arabic and Kurdish spelling variants so matching works.

**This is a port of the platform's `apps/hate_speech/normalize.py` and must stay
character-for-character identical to it.** The lexicon lives on the platform and
this agent prefilters against it locally, which only works if both sides reduce
text to the same canonical form. A divergence produces no error anywhere -- just
terms that quietly stop matching, which is the failure mode this whole project
keeps running into.

`tests/test_normalizer_parity.py` loads the platform's copy directly when the
repository is checked out next door, so drift fails a test the moment either side
changes. It skips rather than fails when the platform is absent, since the agent
ships independently.

The field data proves the need on its own: one transcript row records the term as
"عبدة الشيطان" and the attested utterance beside it as "الشيطآن" -- same word,
different alef, no match. Social media varies far more than that.
"""

from __future__ import annotations

import re

# Harakat, superscript alef and tatweel are decoration, never lexical.
_STRIP = re.compile('[ً-ْٰـ]')

_FOLD = str.maketrans({
    # Alef variants. This is the failure the transcript demonstrates.
    'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا',
    # Alef maqsura, and the Farsi/Kurdish yeh Kurdish keyboards produce.
    'ى': 'ي', 'ی': 'ي',
    # Ta marbuta is written as ha throughout Iraqi dialect.
    'ة': 'ه',
    # Farsi/Kurdish kaf.
    'ک': 'ك',
    # Arabic-Indic digits -- the transcript writes the genocide year as ٢٠١٤.
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
    '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
})

# Deliberately NOT folded: ئ ؤ ە ێ ۆ ڕ ڵ. Standard Arabic search normalization
# collapses hamza carriers into alef, which would destroy Kurdish orthography
# -- ئێزیدی is not a misspelling of anything.


def normalize(text):
    """Return text in the canonical form used for lexicon matching."""
    if not text:
        return ''
    return ' '.join(_STRIP.sub('', text).translate(_FOLD).lower().split())
