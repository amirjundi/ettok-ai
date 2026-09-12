"""Header names are a contract, not branding. They must not be rebranded.

This file exists because a rebrand sweep renamed them once and broke the
dashboard outright: the server began expecting `X-Ettok-Session-Token` while
the SPA -- TypeScript, untouched by a Python-only sweep -- kept sending
`X-Hermes-Session-Token`. Every authenticated request 401'd, and because an
unauthenticated endpoint returns 200 on the same page load, the "reload once"
guard was cleared every pass and the page reload-looped forever.

The same shape applies to webhook receivers that filter on the delivery headers
and signature prefix, and to this agent's own chat proxy, which resumes a
conversation by sending `X-Hermes-Session-Id` to the gateway.

Renaming these is a breaking protocol change. It may be done deliberately, on
both ends at once, with a migration -- but never as a side effect of changing
what a user reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# name -> the file that must still spell it this way
WIRE_NAMES = {
    'X-Hermes-Session-Token': 'hermes_cli/web_server.py',
    'X-Hermes-Session-Id': 'plugins/ettok/dashboard/plugin_api.py',
}


@pytest.mark.parametrize('header,relative', sorted(WIRE_NAMES.items()))
def test_header_name_is_unchanged(header, relative):
    source = (ROOT / relative).read_text(encoding='utf-8')
    assert header in source, (
        f'{relative} no longer spells {header}. Header names are a contract with '
        f'the SPA, with webhook receivers and with the gateway; renaming one end '
        f'breaks it silently.'
    )


def test_no_rebranded_header_names_anywhere():
    """Catch the whole family, not just the two we happen to name above."""
    offenders = []
    pattern = re.compile(r'X-Ettok-[A-Za-z-]+|Ettok-Signature-|Ettok-Agent-Outbound')
    for path in ROOT.rglob('*.py'):
        if {'node_modules', '.venv', '__pycache__', '.git'} & set(path.parts):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        # This file names them on purpose.
        if path.name == 'test_wire_contract.py':
            continue
        for match in pattern.findall(text):
            offenders.append(f'{path.relative_to(ROOT)}: {match}')
    assert not offenders, 'rebranded wire identifiers found:\n' + '\n'.join(offenders[:20])
