"""Inventory versioning service.

Customer mental model (Matt's Apr 29 confirmation):
  v0 = the original extraction snapshot. Inline edits made BEFORE any
       download go against the live data — the v0 snapshot stays as-extracted.
  v1, v2, … = a snapshot taken at every successful corrected-Excel re-import.

The frontend dropdown reads `list_versions(upload_id)`; default view is
the live data (no version filter); choosing v0/v1/etc. switches the page
to render the frozen snapshot from `get_snapshot(upload_id, version)`.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import async_session
from backend.models.orm import InventoryVersion, Upload

logger = logging.getLogger(__name__)


async def _resolve_upload_uuid(short_id: str, db: AsyncSession) -> uuid.UUID | None:
    row = (await db.execute(select(Upload).where(Upload.short_id == short_id))).scalar_one_or_none()
    return row.id if row else None


async def write_snapshot(
    short_id: str,
    rows: list[dict],
    *,
    source: str,
    note: str | None = None,
    file_blob: bytes | None = None,
    created_by: str | None = None,
    db: AsyncSession | None = None,
) -> dict | None:
    """Persist a new snapshot of the inventory.

    `source` is "extraction" for the v0 snapshot (taken when extraction
    completes) or "import" for v1+ snapshots (taken when a corrections-excel
    is re-imported).

    Behavior:
      - First call ever for an upload → creates v0
      - Each subsequent call increments the version number by 1
      - When `file_blob` is provided, computes its SHA-256 and stores it.
        If a prior version of the same `upload_id` already has the same
        `file_hash`, returns that existing version unchanged (de-dup —
        re-uploading the identical file twice without edits doesn't
        spawn a noise version).

    Returns the version dict {id, upload_id, version_number, source,
    rows_count, file_hash, note, created_at} or None on failure.
    """
    sess, owned = (db, False) if db is not None else (async_session(), True)
    try:
        upload_uuid = await _resolve_upload_uuid(short_id, sess)
        if upload_uuid is None:
            logger.warning("write_snapshot: upload %s not found", short_id)
            return None

        file_hash = None
        if file_blob is not None:
            file_hash = hashlib.sha256(file_blob).hexdigest()
            # De-dup: skip when the same blob already exists for this upload
            dup = (await sess.execute(
                select(InventoryVersion).where(
                    InventoryVersion.upload_id == upload_uuid,
                    InventoryVersion.file_hash == file_hash,
                )
            )).scalar_one_or_none()
            if dup:
                logger.info(
                    "write_snapshot: identical file already at v%d for upload %s — dedup",
                    dup.version_number, short_id,
                )
                return _to_dict(dup)

        # Next version number
        max_v = (await sess.execute(
            select(func.coalesce(func.max(InventoryVersion.version_number), -1))
            .where(InventoryVersion.upload_id == upload_uuid)
        )).scalar_one()
        next_v = (max_v if max_v is not None else -1) + 1

        snap = InventoryVersion(
            upload_id=upload_uuid,
            version_number=next_v,
            source=source,
            rows_snapshot=rows or [],
            file_blob=file_blob,
            file_hash=file_hash,
            rows_count=len(rows or []),
            note=note,
            created_by=created_by,
        )
        sess.add(snap)
        if owned:
            await sess.commit()
            await sess.refresh(snap)
        else:
            await sess.flush()
        logger.info(
            "write_snapshot: upload %s now at v%d (%s, %d rows)",
            short_id, next_v, source, len(rows or []),
        )
        return _to_dict(snap)
    except Exception as e:
        logger.exception("write_snapshot failed for %s: %s", short_id, e)
        if owned:
            await sess.rollback()
        return None
    finally:
        if owned:
            await sess.close()


async def list_versions(short_id: str, *, db: AsyncSession | None = None) -> list[dict]:
    """Return every version for an upload, newest first."""
    sess, owned = (db, False) if db is not None else (async_session(), True)
    try:
        upload_uuid = await _resolve_upload_uuid(short_id, sess)
        if upload_uuid is None:
            return []
        rows = (await sess.execute(
            select(InventoryVersion)
            .where(InventoryVersion.upload_id == upload_uuid)
            .order_by(InventoryVersion.version_number.desc())
        )).scalars().all()
        return [_to_dict(r) for r in rows]
    finally:
        if owned:
            await sess.close()


async def get_snapshot(
    short_id: str,
    version_number: int,
    *,
    db: AsyncSession | None = None,
) -> dict | None:
    """Return a single snapshot's full payload (rows_snapshot included)."""
    sess, owned = (db, False) if db is not None else (async_session(), True)
    try:
        upload_uuid = await _resolve_upload_uuid(short_id, sess)
        if upload_uuid is None:
            return None
        row = (await sess.execute(
            select(InventoryVersion).where(
                InventoryVersion.upload_id == upload_uuid,
                InventoryVersion.version_number == version_number,
            )
        )).scalar_one_or_none()
        if not row:
            return None
        d = _to_dict(row)
        d["rows_snapshot"] = row.rows_snapshot or []
        return d
    finally:
        if owned:
            await sess.close()


def _to_dict(row: InventoryVersion) -> dict[str, Any]:
    """Lightweight summary — no rows_snapshot or file_blob (heavy)."""
    return {
        "id": str(row.id),
        "upload_id": str(row.upload_id),
        "version_number": row.version_number,
        "source": row.source,
        "rows_count": row.rows_count,
        "file_hash": row.file_hash,
        "has_file": row.file_blob is not None,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
    }
