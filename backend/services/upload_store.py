"""Durable upload-state store (Phase B, Apr-2026).

Replaces the previous Redis-backed `_save_upload`/`_get_upload`/`_list_uploads`
helpers in `backend/api/uploads.py`. Upload metadata (project name, client,
classifications, results) now lives in Postgres so it survives Redis flushes,
container restarts, and TTLs — three failure modes that previously made
projects appear as anonymous "(unnamed)" entries on local + prod.

Design choices:

* **short_id stays the user-facing id.** The DB primary key is the existing
  UUID column on `uploads`; `short_id` (8 hex chars, like the legacy Redis
  key) is the lookup key used in URLs and the frontend. Existing API URLs
  (e.g. `/api/uploads/{upload_id}/results`) keep their format.
* **Returns a dict, not an ORM object.** The dict shape matches the legacy
  Redis JSON exactly (with `upload_id` aliased to `short_id`) so the API
  layer can switch over with minimal changes. ORM access is a follow-on
  refactor for the API layer.
* **Files_processed stays in Redis.** It's a high-frequency counter updated
  during extraction; Postgres writes are wasteful for that. Everything else
  lives in Postgres.
* **Optional injected session.** All functions accept an optional
  `AsyncSession` so FastAPI endpoints can share a request-scoped session
  via `Depends(get_db)`. Background tasks (extraction, merge) pass `None`
  and the helper opens its own session/transaction.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import redis
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import async_session
from backend.models.orm import Upload
from backend.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis (live counter only) — files_processed is the lone field that stays in
# Redis because it ticks once per file during extraction. Postgres for that
# would be 200+ tiny writes per project. Everything else is durable.
# ---------------------------------------------------------------------------

_REDIS: redis.Redis | None = None


def _r() -> redis.Redis:
    global _REDIS
    if _REDIS is None:
        _REDIS = redis.from_url(settings.redis_url, decode_responses=True)
    return _REDIS


def _files_processed_key(short_id: str) -> str:
    return f"dd:upload:{short_id}:files_processed"


def get_files_processed(short_id: str) -> int:
    val = _r().get(_files_processed_key(short_id))
    return int(val) if val else 0


def set_files_processed(short_id: str, n: int) -> None:
    _r().set(_files_processed_key(short_id), n)


def incr_files_processed(short_id: str) -> int:
    """Atomic increment, returns new value."""
    return int(_r().incr(_files_processed_key(short_id)))


# ---------------------------------------------------------------------------
# Short-id generation
# ---------------------------------------------------------------------------


def new_short_id() -> str:
    """8 hex chars — same shape as the legacy uuid4()[:8] format."""
    return secrets.token_hex(4)


# ---------------------------------------------------------------------------
# Dict ↔ ORM marshaling
# ---------------------------------------------------------------------------


def _to_dict(u: Upload) -> dict[str, Any]:
    """Match the legacy Redis dict shape exactly so the API layer is unchanged.

    `upload_id` is aliased to `short_id` (the user-facing id). `project_name`
    is aliased to the DB `name` column. Datetimes are ISO strings (the API
    serialised them this way before).
    """
    return {
        "upload_id": u.short_id,
        "project_name": u.name or "",
        "client_name": u.client_name or "",
        "client_id": str(u.client_id) if u.client_id else None,
        "description": u.description or "",
        "status": u.status or "pending",
        "files": u.files or {},
        "classified": u.classified or [],
        "file_assignments": u.file_assignments or [],
        "results": u.results or [],
        "raw_results": u.raw_results,
        "has_raw_results": bool(u.has_raw_results),
        "files_processed": get_files_processed(u.short_id) if u.short_id else 0,
        "computed_carriers": u.computed_carriers or [],
        "rows_with_issues": u.rows_with_issues or 0,
        "rows_error_level": u.rows_error_level or 0,
        "unique_accounts": u.unique_accounts or 0,
        "rows_needing_carrier_validation": u.rows_needing_carrier_validation or 0,
        "created_at": u.created_at.replace(tzinfo=timezone.utc).isoformat() if u.created_at else "",
        "deleted_at": u.deleted_at.replace(tzinfo=timezone.utc).isoformat() if u.deleted_at else None,
    }


# Map the legacy Redis dict keys to ORM column names. `project_name` →
# `name`, the rest are 1:1.
_FIELD_MAP = {
    "project_name": "name",
    "client_name": "client_name",
    "client_id": "client_id",
    "description": "description",
    "status": "status",
    "files": "files",
    "classified": "classified",
    "file_assignments": "file_assignments",
    "results": "results",
    "raw_results": "raw_results",
    "has_raw_results": "has_raw_results",
    "computed_carriers": "computed_carriers",
    "rows_with_issues": "rows_with_issues",
    "rows_error_level": "rows_error_level",
    "unique_accounts": "unique_accounts",
    "rows_needing_carrier_validation": "rows_needing_carrier_validation",
    "deleted_at": "deleted_at",
}


def _coerce_for_orm(field: str, value: Any) -> Any:
    """Light coercion: ISO strings → datetime, str client_ids → UUID."""
    if value is None:
        return None
    if field in ("deleted_at",) and isinstance(value, str):
        # Accept ISO-8601, fall back to None on parse failure
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    if field == "client_id" and isinstance(value, str):
        import uuid as _uuid
        try:
            return _uuid.UUID(value)
        except ValueError:
            return None
    return value


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def _open_session(db: AsyncSession | None) -> tuple[AsyncSession, bool]:
    """Yield a session — either the caller's, or a freshly-opened one we own."""
    if db is not None:
        return db, False
    sess = async_session()
    return sess, True


async def save_upload(short_id: str, data: dict[str, Any], *, db: AsyncSession | None = None) -> dict[str, Any]:
    """Insert or update an upload row keyed by short_id. Returns the dict view.

    Used by the classify endpoint after files are saved to disk; also used by
    the disk-bootstrap path during migration.
    """
    sess, owned = await _open_session(db)
    try:
        # Build the column payload from the legacy dict.
        payload: dict[str, Any] = {"short_id": short_id}
        for k, col in _FIELD_MAP.items():
            if k in data:
                payload[col] = _coerce_for_orm(k, data[k])
        # Created-at: respect caller-provided string (used during migration to
        # preserve original timestamps), else default to now().
        if "created_at" in data and isinstance(data["created_at"], str):
            try:
                payload["created_at"] = datetime.fromisoformat(
                    data["created_at"].replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError:
                pass

        stmt = pg_insert(Upload).values(**payload)
        # Upsert on short_id — keeps the helper idempotent for the bootstrap
        # path. Excluded values come from the proposed insert.
        update_cols = {col: getattr(stmt.excluded, col) for col in payload if col not in ("short_id",)}
        stmt = stmt.on_conflict_do_update(index_elements=["short_id"], set_=update_cols)
        await sess.execute(stmt)
        if owned:
            await sess.commit()

        # Reset the in-flight files_processed counter on first save so old
        # values don't leak across re-uploads with the same short_id.
        if "files_processed" in data:
            set_files_processed(short_id, int(data["files_processed"]))
        elif "status" in data and data["status"] == "classified":
            # New classify run — start counter from 0
            set_files_processed(short_id, 0)

        # Re-read so we return canonical state (with defaults applied).
        return await _get_by_short_id(sess, short_id)
    finally:
        if owned:
            await sess.close()


async def _get_by_short_id(sess: AsyncSession, short_id: str) -> dict[str, Any] | None:
    row = (await sess.execute(select(Upload).where(Upload.short_id == short_id))).scalar_one_or_none()
    return _to_dict(row) if row else None


async def get_upload(short_id: str, *, db: AsyncSession | None = None) -> dict[str, Any] | None:
    sess, owned = await _open_session(db)
    try:
        return await _get_by_short_id(sess, short_id)
    finally:
        if owned:
            await sess.close()


async def update_upload_field(short_id: str, *, db: AsyncSession | None = None, **fields: Any) -> dict[str, Any] | None:
    """Patch one or more fields. Unknown keys are ignored (matches the prior
    Redis behavior of accepting arbitrary kwargs).

    Special case: `files_processed` is routed to the Redis live counter
    instead of Postgres — it ticks once per file during extraction, and a
    DB write per increment is wasteful. Callers can pass it transparently.
    """
    # Pull files_processed off before the DB update.
    fp = fields.pop("files_processed", None)
    if fp is not None:
        try:
            set_files_processed(short_id, int(fp))
        except (TypeError, ValueError):
            logger.warning("update_upload_field: bad files_processed value %r", fp)

    sess, owned = await _open_session(db)
    try:
        row = (await sess.execute(select(Upload).where(Upload.short_id == short_id))).scalar_one_or_none()
        if row is None:
            # Even if the DB row is missing, the Redis counter set above is
            # harmless. Return None so callers can detect the missing row.
            return None
        # If only files_processed was passed, no DB columns to update — just
        # return the current row state.
        if fields:
            for k, v in fields.items():
                col = _FIELD_MAP.get(k, k if hasattr(row, k) else None)
                if not col:
                    logger.debug("update_upload_field: ignoring unknown field %r", k)
                    continue
                setattr(row, col, _coerce_for_orm(k, v))
            if owned:
                await sess.commit()
        return _to_dict(row)
    finally:
        if owned:
            await sess.close()


async def update_upload_results(short_id: str, results: list, *, db: AsyncSession | None = None) -> None:
    await update_upload_field(short_id, results=results, db=db)


async def set_raw_results(short_id: str, raw_results: list, *, db: AsyncSession | None = None) -> None:
    await update_upload_field(short_id, raw_results=raw_results, has_raw_results=True, db=db)


async def get_raw_results(short_id: str, *, db: AsyncSession | None = None) -> list | None:
    sess, owned = await _open_session(db)
    try:
        row = (await sess.execute(select(Upload).where(Upload.short_id == short_id))).scalar_one_or_none()
        return row.raw_results if row else None
    finally:
        if owned:
            await sess.close()


async def list_uploads(
    *, include_deleted: bool = False, only_deleted: bool = False, db: AsyncSession | None = None
) -> list[dict[str, Any]]:
    """List uploads, ordered by created_at desc.

    Soft-deleted ones (deleted_at IS NOT NULL) are hidden by default.
    Pass `only_deleted=True` to render the bin, or `include_deleted=True`
    to get both.

    NOTE: unlike the previous Redis implementation, this does NOT silently
    bootstrap fresh entries for orphaned disk folders. That was the second
    half of the "(unnamed) uploads" bug. Orphans are surfaced via a separate
    `find_orphan_disk_folders()` helper so the UI / admin can decide.
    """
    sess, owned = await _open_session(db)
    try:
        stmt = select(Upload)
        if only_deleted:
            stmt = stmt.where(Upload.deleted_at.isnot(None))
        elif not include_deleted:
            stmt = stmt.where(Upload.deleted_at.is_(None))
        stmt = stmt.order_by(Upload.created_at.desc())
        rows = (await sess.execute(stmt)).scalars().all()
        return [_to_dict(r) for r in rows]
    finally:
        if owned:
            await sess.close()


async def soft_delete_upload(short_id: str, *, db: AsyncSession | None = None) -> bool:
    """Move to bin (set deleted_at). Files on disk are preserved."""
    return bool(
        await update_upload_field(short_id, deleted_at=datetime.now(timezone.utc).isoformat(), db=db)
    )


async def restore_upload(short_id: str, *, db: AsyncSession | None = None) -> bool:
    return bool(await update_upload_field(short_id, deleted_at=None, db=db))


async def hard_delete_upload(short_id: str, *, db: AsyncSession | None = None) -> bool:
    """Purge: remove DB row, Redis counter, and stored files (local or GCS).

    Storage cleanup goes through `storage.delete_prefix(temp/{short_id})` so
    GCS objects are deleted when STORAGE_BACKEND=gcs and local files are
    removed when STORAGE_BACKEND=local.
    """
    sess, owned = await _open_session(db)
    try:
        row = (await sess.execute(select(Upload).where(Upload.short_id == short_id))).scalar_one_or_none()
        if row is None:
            return False
        await sess.delete(row)
        if owned:
            await sess.commit()
    finally:
        if owned:
            await sess.close()

    # Wipe Redis counter
    _r().delete(_files_processed_key(short_id))

    # Wipe stored files via the storage abstraction. Imported lazily to avoid
    # a circular import (storage → settings; upload_store imported at app boot).
    try:
        from backend.services.storage import get_storage
        get_storage().delete_prefix(f"temp/{short_id}")
    except Exception as e:
        logger.warning("hard_delete_upload: storage cleanup failed for %s: %s", short_id, e)
        # Don't fail the whole purge — DB row is already gone.

    return True


# ---------------------------------------------------------------------------
# Orphan-folder detection (replaces the silent disk-bootstrap path)
# ---------------------------------------------------------------------------


async def find_orphan_disk_folders(*, db: AsyncSession | None = None) -> list[dict[str, Any]]:
    """Return temp/* folders that have no matching DB row.

    Previously `_list_uploads` would silently bootstrap a Redis entry for
    each orphan with `project_name=""` — the root of the "(unnamed) uploads"
    UX bug. Now orphans are surfaced explicitly so the admin can decide:
    re-link to an existing project, recover with a fresh name, or purge.

    Local storage: walks `<storage_base_dir>/temp/`.
    GCS storage: lists `gs://<bucket>/temp/` keys grouped by the {short_id}
    path component.
    """
    sess, owned = await _open_session(db)
    try:
        known = {
            r[0]
            for r in (await sess.execute(select(Upload.short_id))).all()
            if r[0]
        }
    finally:
        if owned:
            await sess.close()

    # Local: walk the filesystem directly so we can read mtime + size cheaply.
    if (settings.storage_backend or "local").lower() == "local":
        temp_dir = Path(settings.storage_base_dir) / "temp"
        if not temp_dir.exists():
            return []
        orphans = []
        for folder in sorted(temp_dir.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name in known:
                continue
            files = sorted(
                f.name for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")
            )
            if not files:
                continue
            mtime = datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc).isoformat()
            orphans.append({
                "short_id": folder.name,
                "path": str(folder),
                "file_count": len(files),
                "mtime": mtime,
                "sample_files": files[:5],
            })
        return orphans

    # GCS: list every blob under temp/, group by the short_id path segment.
    from backend.services.storage import get_storage
    paths = get_storage().list_prefix("temp")
    by_short_id: dict[str, list[str]] = {}
    for p in paths:
        # gs://bucket/temp/{short_id}/{filename}
        try:
            after_temp = p.split("/temp/", 1)[1]
            short_id, _, filename = after_temp.partition("/")
            if short_id and filename and short_id not in known:
                by_short_id.setdefault(short_id, []).append(filename)
        except IndexError:
            continue
    orphans = []
    for sid, files in by_short_id.items():
        files.sort()
        orphans.append({
            "short_id": sid,
            "path": f"gs://temp/{sid}/",
            "file_count": len(files),
            "mtime": "",  # GCS list doesn't surface dir mtime cheaply
            "sample_files": files[:5],
        })
    return orphans
