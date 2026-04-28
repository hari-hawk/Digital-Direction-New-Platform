"""One-time migration: copy upload state from Redis → Postgres.

Background
----------
Phase B (Apr-2026) moved durable upload metadata out of Redis (ephemeral RAM,
lost on container restart, evicted by 24h TTL) and into Postgres. This script
copies whatever state is still in Redis into the new `uploads` table so users
don't lose their existing projects when the new backend ships.

Usage
-----
    # Dry run — prints what would be migrated, writes nothing
    python scripts/migrate_redis_uploads_to_pg.py --dry-run

    # Real run — copies into Postgres. Idempotent (uses upsert on short_id).
    python scripts/migrate_redis_uploads_to_pg.py

    # After verifying the UI, optionally clear the old Redis keys to free RAM
    python scripts/migrate_redis_uploads_to_pg.py --flush-after

Behaviour
---------
* Reads the `dd:uploads` set + every `dd:upload:<short_id>` key.
* Joins the `:results` and `:results:raw` siblings.
* Calls `upload_store.save_upload(short_id, data)` per row — upsert keyed on
  short_id, so re-running is safe.
* Preserves `created_at`, `deleted_at`, `files_processed` from Redis.
* `files_processed` becomes the new Redis-backed live counter (handled by
  upload_store.save_upload's special case).
* Leaves Redis untouched unless `--flush-after` is passed.

Non-goals
---------
* Does NOT move files on disk — those already live at storage/temp/<id>/.
* Does NOT migrate disk-bootstrapped orphan folders (they have no Redis
  state). Use `GET /api/uploads/orphans` after migration to triage them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

import redis

from backend.services import upload_store as us
from backend.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# Quiet SQLAlchemy's verbose echo so the migration's per-row INFO lines stay readable.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
log = logging.getLogger("migrate")


def _connect_redis() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True)


def _legacy_key(short_id: str) -> str:
    return f"dd:upload:{short_id}"


def _read_legacy_entry(r: redis.Redis, short_id: str) -> dict[str, Any] | None:
    """Pull the three legacy Redis keys (state + results + raw_results) and
    rebuild the dict shape the pipeline used to see."""
    raw = r.get(_legacy_key(short_id))
    if not raw:
        return None
    data = json.loads(raw)

    results_raw = r.get(f"{_legacy_key(short_id)}:results")
    data["results"] = json.loads(results_raw) if results_raw else []

    raw_results_raw = r.get(f"{_legacy_key(short_id)}:results:raw")
    if raw_results_raw:
        data["raw_results"] = json.loads(raw_results_raw)
        data["has_raw_results"] = True

    return data


async def _migrate_one(short_id: str, data: dict[str, Any], dry_run: bool) -> str:
    """Returns 'created', 'updated', or 'skipped'."""
    existing = await us.get_upload(short_id)
    action = "updated" if existing else "created"

    if dry_run:
        return f"would-{action}"

    # save_upload upserts. raw_results needs the dedicated setter so
    # has_raw_results gets toggled.
    raw = data.pop("raw_results", None)
    await us.save_upload(short_id, data)
    if raw is not None:
        await us.set_raw_results(short_id, raw)
    return action


def _summarise(data: dict[str, Any]) -> str:
    name = data.get("project_name") or "(unnamed)"
    files = len(data.get("classified") or [])
    rows = len(data.get("results") or [])
    status = data.get("status") or "?"
    deleted = " [BIN]" if data.get("deleted_at") else ""
    return f"'{name}' files={files} rows={rows} status={status}{deleted}"


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be migrated, don't write")
    parser.add_argument(
        "--flush-after",
        action="store_true",
        help="After successful migration, delete the old dd:upload:* keys + dd:uploads set",
    )
    args = parser.parse_args(argv)

    r = _connect_redis()
    try:
        legacy_ids = sorted(r.smembers("dd:uploads") or [])
    except redis.RedisError as e:
        log.error("Failed to read dd:uploads from Redis: %s", e)
        return 1

    log.info("Found %d legacy upload(s) in Redis", len(legacy_ids))
    if not legacy_ids:
        log.info("Nothing to migrate.")
        return 0

    counts: dict[str, int] = {}
    failed: list[str] = []
    for short_id in legacy_ids:
        try:
            data = _read_legacy_entry(r, short_id)
        except Exception as e:
            log.error("Failed to read legacy entry %s: %s", short_id, e)
            failed.append(short_id)
            continue

        if data is None:
            log.warning("Stale dd:uploads index entry %s — no key found, skipping", short_id)
            counts["stale"] = counts.get("stale", 0) + 1
            continue

        try:
            result = await _migrate_one(short_id, data, args.dry_run)
            counts[result] = counts.get(result, 0) + 1
            log.info("[%s] %s — %s", result, short_id, _summarise(data))
        except Exception as e:
            log.exception("Failed to migrate %s: %s", short_id, e)
            failed.append(short_id)

    log.info("Summary: %s%s", counts, f"  failed={failed}" if failed else "")

    if args.flush_after and not args.dry_run and not failed:
        log.info("Flushing legacy Redis keys...")
        for short_id in legacy_ids:
            r.delete(_legacy_key(short_id))
            r.delete(f"{_legacy_key(short_id)}:results")
            r.delete(f"{_legacy_key(short_id)}:results:raw")
        r.delete("dd:uploads")
        log.info("Flushed %d legacy upload(s)", len(legacy_ids))
    elif args.flush_after and failed:
        log.warning("Skipping flush because %d upload(s) failed to migrate", len(failed))

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
