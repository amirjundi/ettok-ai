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


def pending(conn, limit: int = 50) -> list:
    """Captures that still exist only on this machine."""
    return conn.execute(
        'SELECT * FROM evidence_artifact WHERE delivered_at IS NULL '
        "AND (screenshot_path != '' OR archive_path != '') "
        'ORDER BY captured_at LIMIT ?',
        (limit,),
    ).fetchall()


def deliver(conn, client, *, case_id=None, item_hash_for=None, limit: int = 50) -> dict:
    """Upload what has not reached the platform, then delete the local copy.

    The order is the whole point. Upload, wait for the platform to say it holds
    it, and only then remove the file -- because this is the only copy, and a
    machine that deletes on optimism loses the evidence permanently. A capture
    that fails to upload stays on disk and is retried by the next run, which is
    why the row is the queue and no separate outbox entry is needed.

    `item_hash_for` maps a captured page URL to the deduplication digest of a
    comment on it, so the platform can file the artefact against the item. It is
    optional: an artefact that matches nothing is still worth holding.
    """
    from ..platform import client as client_mod

    summary = {'uploaded': 0, 'failed': 0, 'removed': 0, 'errors': []}
    rows = pending(conn, limit)
    if not rows:
        return summary

    delivered = []
    for row in rows:
        try:
            screenshot = _read(row['screenshot_path'])
            archive = _read(row['archive_path'])
            if not screenshot and not archive:
                # The files are gone from disk but the row says undelivered.
                # Nothing to send and nothing to keep waiting for.
                delivered.append(row['id'])
                continue
            client.upload_evidence(
                page_hash=row['content_hash'],
                source_url=row['source_url'],
                captured_at=row['captured_at'],
                item_content_hash=(item_hash_for or {}).get(row['source_url'], ''),
                case_id=case_id,
                screenshot=screenshot,
                archive=archive,
            )
            delivered.append(row['id'])
            summary['uploaded'] += 1
        except client_mod.PlatformError as exc:
            # Kept on disk. A failed upload is the case this design exists for.
            summary['failed'] += 1
            summary['errors'].append(f'evidence {row["id"]}: {exc}')

    mark_delivered(conn, delivered)
    summary['removed'] = discard_delivered(conn)
    return summary


def _read(path: str) -> Optional[bytes]:
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        log.warning('ettok: evidence file missing on disk: %s', path)
        return None


def mark_delivered(conn, evidence_ids: list) -> None:
    if not evidence_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        'UPDATE evidence_artifact SET delivered_at = ? WHERE id = ?',
        [(now, eid) for eid in evidence_ids],
    )
    conn.commit()
