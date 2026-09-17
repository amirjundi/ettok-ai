"""The evidence archive: capture, upload, and only then delete.

`capture()` has always written the files. Nothing ever recorded them, nothing
sent them, and nothing removed them -- so the archive had no index, the platform
held only URLs that a deleted post makes worthless, and the captures piled up on
the operator's machine until the disk filled.

The order is the property worth pinning: upload, wait for the platform to say it
holds the copy, and only then remove the local one. This is the only copy.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from plugins.ettok.collect import evidence as evidence_mod
from plugins.ettok.platform import client as client_mod
from plugins.ettok.store import schema


@pytest.fixture
def home(monkeypatch):
    path = Path(tempfile.mkdtemp())
    monkeypatch.setenv('HERMES_HOME', str(path))
    return path


@pytest.fixture
def conn(home):
    return schema.connect()


def _captured(home, text='page contents'):
    return evidence_mod.capture(
        url='https://facebook.com/p/1', page_text=text,
        screenshot_b64='aGVsbG8=', directory=home / 'ev',
    )


class FakeClient:
    """Accepts uploads, or refuses them the way a dropped line does."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.uploads = []

    def upload_evidence(self, **kwargs):
        if self.fail:
            raise client_mod.TransientError('connection failed')
        self.uploads.append(kwargs)
        return client_mod.Response(status=200, data={'evidence_id': len(self.uploads)})


class TestItIsRecorded:
    def test_a_capture_becomes_a_row(self, conn, home):
        evidence_mod.store(conn, None, _captured(home))
        assert len(evidence_mod.pending(conn)) == 1

    def test_an_uploaded_capture_is_no_longer_pending(self, conn, home):
        evidence_mod.store(conn, None, _captured(home))
        evidence_mod.deliver(conn, FakeClient())
        assert evidence_mod.pending(conn) == []


class TestTheOrderOfOperations:
    """This is the only copy of the evidence."""

    def test_the_local_file_is_deleted_only_after_the_upload(self, conn, home):
        captured = _captured(home)
        evidence_mod.store(conn, None, captured)
        assert Path(captured.archive_path).exists()

        summary = evidence_mod.deliver(conn, FakeClient())

        assert summary['uploaded'] == 1
        assert not Path(captured.archive_path).exists()
        assert not Path(captured.screenshot_path).exists()

    def test_a_failed_upload_keeps_the_file(self, conn, home):
        """A machine that deletes on optimism loses the evidence permanently."""
        captured = _captured(home)
        evidence_mod.store(conn, None, captured)

        summary = evidence_mod.deliver(conn, FakeClient(fail=True))

        assert summary['uploaded'] == 0
        assert summary['failed'] == 1
        assert Path(captured.archive_path).exists()

    def test_a_failed_upload_is_retried_by_the_next_run(self, conn, home):
        """The row is the queue. No separate outbox entry, and nothing lost."""
        evidence_mod.store(conn, None, _captured(home))
        evidence_mod.deliver(conn, FakeClient(fail=True))

        assert len(evidence_mod.pending(conn)) == 1

        client = FakeClient()
        evidence_mod.deliver(conn, client)
        assert len(client.uploads) == 1
        assert evidence_mod.pending(conn) == []


class TestWhatIsSent:
    def test_both_artefacts_and_the_digest_travel(self, conn, home):
        captured = _captured(home)
        evidence_mod.store(conn, None, captured)

        client = FakeClient()
        evidence_mod.deliver(conn, client)
        sent = client.uploads[0]

        assert sent['page_hash'] == captured.content_hash
        assert sent['source_url'] == 'https://facebook.com/p/1'
        assert sent['screenshot'] and sent['archive']

    def test_the_comment_digest_is_sent_so_the_platform_can_link_it(self, conn, home):
        """Two different hashes: one identifies a comment, the other the captured
        files. Only the first is shared with the submission."""
        evidence_mod.store(conn, None, _captured(home))

        client = FakeClient()
        evidence_mod.deliver(
            conn, client,
            item_hash_for={'https://facebook.com/p/1': 'comment-digest-1'},
        )

        assert client.uploads[0]['item_content_hash'] == 'comment-digest-1'

    def test_a_capture_that_matches_no_item_is_still_sent(self, conn, home):
        """It is the proof for whatever was on that page."""
        evidence_mod.store(conn, None, _captured(home))

        client = FakeClient()
        evidence_mod.deliver(conn, client, item_hash_for={})

        assert len(client.uploads) == 1
        assert client.uploads[0]['item_content_hash'] == ''

    def test_a_row_whose_files_vanished_stops_being_retried_forever(self, conn, home):
        """Someone empties the directory. There is nothing left to send, and a
        row retried on every run until the end of time helps nobody."""
        captured = _captured(home)
        evidence_mod.store(conn, None, captured)
        Path(captured.archive_path).unlink()
        Path(captured.screenshot_path).unlink()

        client = FakeClient()
        summary = evidence_mod.deliver(conn, client)

        assert client.uploads == []
        assert summary['uploaded'] == 0
        assert evidence_mod.pending(conn) == []


class TestNothingIsSentTwice:
    def test_a_delivered_capture_is_not_uploaded_again(self, conn, home):
        evidence_mod.store(conn, None, _captured(home))
        client = FakeClient()

        evidence_mod.deliver(conn, client)
        evidence_mod.deliver(conn, client)

        assert len(client.uploads) == 1
