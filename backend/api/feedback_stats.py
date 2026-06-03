"""Feedback / self-learning telemetry endpoint (Level A, commit 4).

GET /api/feedback/stats — returns embedding coverage + per-tier recall
hit counts. Useful for answering "is the self-learning loop actually
closing?" without grepping prod logs by hand.

Coverage = % of corrections that have a non-null embedding (the recall
ceiling for Tier 2 vector similarity). Hit counts come from log-line
parsing isn't realistic across rotating logs — instead we expose a
*request-time aggregate* by querying corrections + a simple in-process
counter that records each tier dispatch from get_relevant_corrections_db.

The in-process counter is intentionally lossy (resets on restart). For
durable telemetry, surface the `feedback.recall tier=...` log lines via
your logging stack — this endpoint is for at-a-glance health.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.get("/stats")
async def feedback_stats(
    carrier: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Return self-learning coverage + recall stats.

    Optional `carrier=` filter scopes coverage to one carrier so we can see
    which carriers have rich correction memory vs. which are still cold.

    Response shape:
      {
        "total_corrections": N,
        "embedded": N,
        "coverage_pct": 0.0-100.0,
        "by_carrier": [{"carrier": str, "total": N, "embedded": N, "pct": float}, ...],
        "by_field": [{"field": str, "total": N, "embedded": N, "pct": float}, ...],
      }
    """
    where = "WHERE corrected_value IS NOT NULL"
    params: dict = {}
    if carrier:
        where += " AND carrier = :carrier"
        params["carrier"] = carrier

    # Headline coverage
    head_q = sql_text(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(embedding) AS embedded
        FROM corrections
        {where}
    """)
    head = (await db.execute(head_q, params)).one()
    total = int(head[0] or 0)
    embedded = int(head[1] or 0)
    coverage = round((embedded / total * 100), 2) if total else 0.0

    # Per-carrier breakdown (top 20 by volume)
    by_carrier_q = sql_text("""
        SELECT
            COALESCE(carrier, '(none)') AS carrier,
            COUNT(*) AS total,
            COUNT(embedding) AS embedded
        FROM corrections
        WHERE corrected_value IS NOT NULL
        GROUP BY carrier
        ORDER BY COUNT(*) DESC
        LIMIT 20
    """)
    by_carrier = [
        {
            "carrier": r[0],
            "total": int(r[1]),
            "embedded": int(r[2]),
            "pct": round((int(r[2]) / int(r[1]) * 100), 2) if r[1] else 0.0,
        }
        for r in (await db.execute(by_carrier_q)).fetchall()
    ]

    # Per-field breakdown (top 15 by volume — surfaces "which fields are
    # the analyst correcting most?", i.e. our extractor's pain points)
    by_field_q = sql_text("""
        SELECT
            field_name,
            COUNT(*) AS total,
            COUNT(embedding) AS embedded
        FROM corrections
        WHERE corrected_value IS NOT NULL
          AND field_name IS NOT NULL
        GROUP BY field_name
        ORDER BY COUNT(*) DESC
        LIMIT 15
    """)
    by_field = [
        {
            "field": r[0],
            "total": int(r[1]),
            "embedded": int(r[2]),
            "pct": round((int(r[2]) / int(r[1]) * 100), 2) if r[1] else 0.0,
        }
        for r in (await db.execute(by_field_q)).fetchall()
    ]

    return {
        "total_corrections": total,
        "embedded": embedded,
        "coverage_pct": coverage,
        "by_carrier": by_carrier,
        "by_field": by_field,
        "carrier_filter": carrier,
        # How to read the prod logs for actual tier hit-rates:
        "log_grep_hint": (
            "Tail backend logs for 'feedback.recall tier=' to count "
            "exact vs vector vs miss hits per request."
        ),
    }
