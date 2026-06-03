#!/usr/bin/env python3
"""Backfill embeddings for historical corrections (Level A, commit 3).

Reads every correction with embedding IS NULL, regenerates the option-C
synthesis sentence from its stored fields, embeds via Gemini, and persists
through the same `store_correction_embedding` helper that runs in the live
save path. Idempotent: re-runs against the same rows are no-ops.

Usage:
    # See how many would be backfilled, no writes
    python scripts/backfill_correction_embeddings.py --dry-run

    # Backfill everything
    python scripts/backfill_correction_embeddings.py

    # Cap to a small batch (smoke test)
    python scripts/backfill_correction_embeddings.py --limit 50

    # Bigger concurrency for prod (default 4)
    python scripts/backfill_correction_embeddings.py --concurrency 8

Cost: ~$0.0001 per correction at Gemini embedding pricing. Expect <$0.20
total even on a corpus of 2K historical corrections.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make the backend importable when running this script directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text as sql_text  # noqa: E402

from backend.models.database import async_session  # noqa: E402
from backend.services.learning import store_correction_embedding  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("backfill")


async def _fetch_pending(limit: int | None) -> list[dict]:
    """Pull every correction missing an embedding. Returns the dict shape
    we need to call store_correction_embedding."""
    q = """
        SELECT
            c.id, c.carrier, c.field_name,
            c.extracted_value, c.corrected_value,
            c.source_text_snippet, c.format_variant,
            d.document_type AS doc_type
        FROM corrections c
        LEFT JOIN documents d ON c.source_document_id = d.id
        WHERE c.embedding IS NULL
          AND c.corrected_value IS NOT NULL
        ORDER BY c.created_at ASC NULLS LAST
    """
    if limit is not None:
        q += f"\nLIMIT {int(limit)}"

    async with async_session() as sess:
        result = await sess.execute(sql_text(q))
        rows = result.mappings().all()
        return [dict(r) for r in rows]


async def _embed_one(record: dict, semaphore: asyncio.Semaphore) -> bool:
    """Backfill one correction. Returns True on success."""
    async with semaphore:
        return await store_correction_embedding(
            correction_id=record["id"],
            carrier=record.get("carrier"),
            doc_type=record.get("doc_type"),
            field_name=record["field_name"],
            extracted_value=record.get("extracted_value"),
            corrected_value=record.get("corrected_value"),
            source_text_snippet=record.get("source_text_snippet"),
            format_variant=record.get("format_variant"),
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Only count how many would be backfilled")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap backfill to N records (smoke test)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Max parallel Gemini embed calls (default 4)")
    args = parser.parse_args()

    logger.info("Scanning corrections for missing embeddings…")
    pending = await _fetch_pending(args.limit)
    if not pending:
        logger.info("No pending corrections. Nothing to do.")
        return 0

    logger.info("Found %d correction(s) without embeddings.", len(pending))
    if args.dry_run:
        # Print a small sample so the operator sees what would land
        for r in pending[:5]:
            logger.info(
                "  sample: id=%s carrier=%s field=%s",
                r["id"], r.get("carrier"), r["field_name"],
            )
        logger.info("Dry-run — no writes. Re-run without --dry-run to backfill.")
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    tasks = [_embed_one(r, sem) for r in pending]

    # Run with progress every 50 completions so long backfills aren't silent.
    successes = 0
    failures = 0
    for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
        ok = await coro
        if ok:
            successes += 1
        else:
            failures += 1
        if i % 50 == 0 or i == len(tasks):
            logger.info("Progress: %d / %d  (✅ %d  ❌ %d)",
                        i, len(tasks), successes, failures)

    logger.info("Backfill complete. ✅ %d embedded, ❌ %d failed.",
                successes, failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
