"""Proof, captured before an item counts as collected.

Hate speech posts get deleted, often within hours, and especially once they are
reported. Evidence that was not captured at the moment of collection is evidence
you do not have, and a report whose only support is a dead URL is an assertion.

So capture happens first and an item without it is not a finding. The hash is what
makes the archive worth anything later: it lets someone confirm the copy they are
reading is the copy that was taken, rather than trusting that nobody edited it.

Files live under the plugin's data directory and are deleted once the platform
confirms delivery. There is no second copy on this machine afterwards, which is
deliberate -- screenshots of hate speech naming real people, sitting on a laptop
on a residential connection, are the most sensitive thing in the system.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Evidence:
    screenshot_path: str = ''
    archive_path: str = ''
    source_url: str = ''
    captured_at: str = ''
    content_hash: str = ''

    @property
    def is_complete(self) -> bool:
        """Whether this proves anything.

        A URL and a timestamp alone do not: the page they point at can change or
        vanish. At least one durable artefact has to exist locally.
        """
        return bool(self.content_hash and (self.screenshot_path or self.archive_path))


def _slug(url: str) -> str:
    return hashlib.sha256((url or '').encode('utf-8')).hexdigest()[:16]


def capture(*, url: str, page_text: str, screenshot_b64: str = '',
            directory: Optional[Path] = None) -> Evidence:
    """Write the artefacts and return what was captured.

    Takes already-fetched content rather than fetching it, so this is testable
    without a browser and cannot itself trigger a page load the pacer did not
    account for.
    """
    from ..store import schema

    base = Path(directory) if directory is not None else schema.evidence_dir()
    base.mkdir(parents=True, exist_ok=True)

    captured_at = datetime.now(timezone.utc)
    stem = f'{captured_at.strftime("%Y%m%dT%H%M%SZ")}-{_slug(url)}'

    archive_path = ''
    if page_text:
        path = base / f'{stem}.html'
        path.write_text(page_text, encoding='utf-8')
        archive_path = str(path)

    screenshot_path = ''
    if screenshot_b64:
        try:
            path = base / f'{stem}.png'
            path.write_bytes(base64.b64decode(screenshot_b64))
            screenshot_path = str(path)
        except Exception:
            # A missing screenshot degrades the evidence; it must not lose the item.
            log.warning('ettok: could not decode screenshot for %s', url, exc_info=True)

    digest = hashlib.sha256(
        (page_text or '').encode('utf-8') + (screenshot_b64 or '').encode('utf-8')
    ).hexdigest()

    return Evidence(
        screenshot_path=screenshot_path,
        archive_path=archive_path,
        source_url=url,
        captured_at=captured_at.isoformat(),
        content_hash=digest,
    )


def store(conn, collected_item_id: Optional[int], evidence: Evidence) -> int:
    cursor = conn.execute(
        'INSERT INTO evidence_artifact(collected_item_id, screenshot_path, archive_path, '
        'source_url, captured_at, content_hash) VALUES (?, ?, ?, ?, ?, ?)',
        (
            collected_item_id, evidence.screenshot_path, evidence.archive_path,
            evidence.source_url, evidence.captured_at, evidence.content_hash,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def discard_delivered(conn, *, before_iso: Optional[str] = None) -> int:
    """Delete local artefacts whose delivery the platform has confirmed.

    Only confirmed delivery counts. An unconfirmed one is undelivered, because
    there is no other copy of this and deleting on optimism loses the evidence
    permanently.
    """
    rows = conn.execute(
        'SELECT id, screenshot_path, archive_path FROM evidence_artifact '
        'WHERE delivered_at IS NOT NULL' + (' AND delivered_at <= ?' if before_iso else ''),
        (before_iso,) if before_iso else (),
    ).fetchall()

    removed = 0
    for row in rows:
        for field in ('screenshot_path', 'archive_path'):
            path = row[field]
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    log.warning('ettok: could not remove %s', path)
        conn.execute(
            'UPDATE evidence_artifact SET screenshot_path = \'\', archive_path = \'\' WHERE id = ?',
            (row['id'],),
        )
    conn.commit()
    return removed


def mark_delivered(conn, evidence_ids: list) -> None:
    if not evidence_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        'UPDATE evidence_artifact SET delivered_at = ? WHERE id = ?',
        [(now, eid) for eid in evidence_ids],
    )
    conn.commit()
