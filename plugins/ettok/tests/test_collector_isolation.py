"""The extractor must not confuse a comment with the post it replies to.

From the architecture review of 19 September 2026 (ARCH-06). This is the only
defect in that review that corrupts the evidence itself rather than the
judgement made about it, which is why it goes first: a comment filed under the
wrong post, or a post whose subject was written by a commenter, cannot be
repaired by a better classifier downstream.

The extractor is JavaScript that runs inside the page, so Python can only mock
what it returns -- which is how both defects survived a suite that covers the
rest of this plugin closely. `extract_check.js` mounts the real script in jsdom
against fixtures shaped like the pages it runs on, and this file is the pytest
door to it.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plugins.ettok.collect.base import DEFAULT_EXTRACTORS, _EXTRACT_JS

HERE = Path(__file__).parent


@pytest.mark.skipif(shutil.which('node') is None, reason='Node not installed')
def test_the_real_extractor_against_a_real_dom(tmp_path):
    selectors = DEFAULT_EXTRACTORS['facebook']
    script = tmp_path / 'extract.js'
    script.write_text(_EXTRACT_JS % json.dumps(selectors), encoding='utf-8')

    result = subprocess.run(
        [shutil.which('node'), str(HERE / 'extract_check.js'),
         str(script), json.dumps(selectors)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 1 and 'Cannot find module' in result.stderr:
        pytest.skip('jsdom not installed')
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_script_still_templates_its_selectors():
    """`%s` is the only format placeholder in it. A stray `%` elsewhere would
    raise here rather than in a browser at three in the morning."""
    rendered = _EXTRACT_JS % json.dumps(DEFAULT_EXTRACTORS['facebook'])
    assert 'role=\\"article\\"' in rendered or 'role="article"' in rendered


def test_the_newline_escape_survives_being_a_python_string():
    r"""The script joins text blocks with '\n'.

    It lives in a Python string, so an unraw one turns that escape into a real
    newline and the JS becomes an unterminated string literal -- a syntax error
    inside the page, where nothing here would see it.
    """
    assert '\\n' in _EXTRACT_JS
