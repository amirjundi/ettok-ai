"""Evidence belongs to the case it was captured for, not the one draining the queue.

From the architecture review of 19 September 2026 (ARCH-07, the evidence half).

Captures were stored with no owner, and `deliver` stamped every pending row
with whichever case the *current* run was working. So a capture taken during
case A that failed to upload -- which is the exact situation this local store
exists to survive -- was uploaded later, during case B's scan, as B's evidence.

That is worse than losing it. A screenshot proving what was said under a Sinjar
post, filed against an unrelated case, is a record that misstates where it came
from, in the one part of this system whose whole job is to be trustworthy about
where things came from.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from plugins.ettok.collect import evidence as evidence_mod
from plugins.ettok.collect.evidence import Evidence
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


@pytest.fixture
def conn(home):
    return schema.connect()


class FakeClient:
    """Records what reached the platform."""

    def __init__(self):
        self.uploads = []

    def upload_evidence(self, **kwargs):
        self.uploads.append(kwargs)
        return {'ok': True}


def artifact(tmp_path, name, *, url):
    shot = tmp_path / f'{name}.png'
    shot.write_bytes(b'not really a png')
    return Evidence(
        screenshot_path=str(shot), archive_path='', source_url=url,
        captured_at='2026-09-19T10:00:00+00:00', content_hash=f'hash-{name}',
    )


def test_a_capture_keeps_the_case_it_was_taken_for(conn, tmp_path):
    """The defect, end to end.

    Case 11 captures a page and the upload fails. Case 22 runs later and drains
    the queue. The artefact must still say 11.
    """
    evidence_mod.store(conn, None, artifact(tmp_path, 'a', url='https://x.test/p/1'),
                       case_id=11)

    client = FakeClient()
    evidence_mod.deliver(conn, client)

    assert [u['case_id'] for u in client.uploads] == [11]


def test_two_cases_captures_do_not_merge(conn, tmp_path):
    evidence_mod.store(conn, None, artifact(tmp_path, 'a', url='https://x.test/p/1'),
                       case_id=11)
    evidence_mod.store(conn, None, artifact(tmp_path, 'b', url='https://x.test/p/2'),
                       case_id=22)

    client = FakeClient()
    evidence_mod.deliver(conn, client)

    assert sorted(u['case_id'] for u in client.uploads) == [11, 22]


def test_no_case_stays_no_case(conn, tmp_path):
    """Unattributed is honest. Attributed to whatever ran last is not, and the
    two are indistinguishable once the upload has happened."""
    evidence_mod.store(conn, None, artifact(tmp_path, 'a', url='https://x.test/p/1'))

    client = FakeClient()
    evidence_mod.deliver(conn, client)

    assert client.uploads[0]['case_id'] is None


def test_delivery_takes_no_case_argument():
    """Kept as a test because the argument is what caused this, and re-adding
    it would look like a convenience."""
    import inspect

    assert 'case_id' not in inspect.signature(evidence_mod.deliver).parameters


def test_the_column_is_added_to_an_existing_store(conn):
    """The rows are captures that exist nowhere else, so this table is migrated
    rather than rebuilt -- the pattern used for the disposable tables would
    orphan the only copy of a deleted post."""
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(evidence_artifact)')}
    assert 'case_id' in columns
