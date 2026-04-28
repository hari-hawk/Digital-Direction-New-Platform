"""Recover orphan disk folders into Postgres uploads.

Use case
--------
Phase B (Apr-2026) moved upload metadata from Redis to Postgres. If the
backend was deployed without first running scripts/migrate_redis_uploads_to_pg.py,
any uploads that existed before the deploy lose their Redis-side state when
the new code starts (or when Redis is flushed/restarted), but the files
themselves remain on disk under storage/temp/<short_id>/.

The new pipeline surfaces those folders via GET /api/uploads/orphans rather
than silently bootstrapping them with empty names (the old "(unnamed)"
bug). This script takes the next step: walk every orphan, classify the
files, and insert a real Postgres uploads row so the user sees them in the
Previous Uploads list — without re-uploading.

What it does
------------
For each orphan folder under storage/temp/:
  1. Read the on-disk files
  2. Run filename-based classification (no LLM call) to fill in carrier +
     doc_type for each file when possible
  3. Insert an uploads row keyed on the folder name (short_id) with
     project_name="Recovered <date> (<n> files)" so the user can rename it
  4. Optionally classify with first-page text (slower; opt in via --deep)

Usage
-----
    python scripts/recover_orphan_uploads.py --dry-run        # preview
    python scripts/recover_orphan_uploads.py                  # commit, fast classify
    python scripts/recover_orphan_uploads.py --deep           # commit, content-based classify
    python scripts/recover_orphan_uploads.py --short-ids 28cf5284,1a919772
                                                              # only these orphans

Idempotent — uses upsert on short_id. Safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend.pipeline.classifier import classify_by_filename, classify_by_content
from backend.services import upload_store as us
from backend.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
log = logging.getLogger("recover")


def _classify_files(folder: Path, deep: bool) -> list[dict]:
    """Build the `classified` list for an orphan folder.

    Stage A (filename) is always done — it's free.
    Stage B (first-page content) is opt-in via --deep — it costs disk IO
    per file but no LLM calls.
    """
    items: list[dict] = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        size = f.stat().st_size
        a = classify_by_filename(f.name)
        carrier = a.carrier
        doc_type = a.document_type
        format_variant = None
        if deep:
            try:
                b = classify_by_content(str(f))
                if b.carrier and not carrier:
                    carrier = b.carrier
                if b.document_type and not doc_type:
                    doc_type = b.document_type
                if b.format_variant:
                    format_variant = b.format_variant
            except Exception as e:
                log.debug("deep classify skipped for %s: %s", f.name, e)

        # Map carrier slug → display name when we have a config for it
        from backend.config_loader import get_config_store
        store = get_config_store()
        cfg = store.get_carrier(carrier) if carrier else None
        carrier_display = cfg.name if cfg else (carrier.title() if carrier else None)

        items.append({
            "filename": f.name,
            "carrier": carrier_display,
            "doc_type": doc_type,
            "format_variant": format_variant,
            "file_size": size,
        })
    return items


async def _recover_one(short_id: str, folder: Path, deep: bool, dry_run: bool) -> dict:
    """Insert one uploads row reflecting the on-disk folder."""
    classified = _classify_files(folder, deep=deep)
    saved_files = {item["filename"]: str(folder / item["filename"]) for item in classified}

    mtime = datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc).isoformat()
    project_name = f"Recovered {mtime[:10]} ({len(classified)} file{'s' if len(classified)!=1 else ''})"

    if dry_run:
        return {"short_id": short_id, "files": len(classified), "would_create": project_name}

    await us.save_upload(short_id, {
        "project_name": project_name,
        "client_name": "",
        "description": "Auto-recovered from orphan disk folder. Rename or move to bin as needed.",
        "files": saved_files,
        "classified": classified,
        "status": "classified",
        "created_at": mtime,
        "results": [],
    })
    return {"short_id": short_id, "files": len(classified), "created": project_name}


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Preview, do not write.")
    parser.add_argument("--deep", action="store_true", help="Run first-page content classification (slower).")
    parser.add_argument("--short-ids", help="Comma-separated short_ids to recover (default: all orphans).")
    args = parser.parse_args(argv)

    orphans = await us.find_orphan_disk_folders()
    if not orphans:
        log.info("No orphans found.")
        return 0

    if args.short_ids:
        wanted = {s.strip() for s in args.short_ids.split(",") if s.strip()}
        orphans = [o for o in orphans if o["short_id"] in wanted]
        if not orphans:
            log.error("No matching orphans for: %s", args.short_ids)
            return 1

    total_files = sum(o["file_count"] for o in orphans)
    log.info("Found %d orphan folder(s) with %d total files", len(orphans), total_files)

    recovered = 0
    failed: list[str] = []
    for o in orphans:
        sid = o["short_id"]
        folder = Path(o["path"])
        if not folder.exists():
            log.warning("Skipping %s: path %s no longer exists", sid, folder)
            continue
        try:
            result = await _recover_one(sid, folder, deep=args.deep, dry_run=args.dry_run)
            label = result.get("created") or result.get("would_create")
            log.info("[%s] %s files=%d  '%s'",
                     "DRY-RUN" if args.dry_run else "RECOVERED",
                     sid, result["files"], label)
            recovered += 1
        except Exception as e:
            log.error("Failed to recover %s: %s", sid, e)
            failed.append(sid)

    log.info("Done. Recovered=%d  Failed=%d (%s)", recovered, len(failed), failed if failed else "none")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
