"""Dashboard API — aggregated metrics from extraction data."""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, case, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db
from backend.models.orm import ExtractionRun, ExtractedRow, Correction
from backend.config_loader import get_config_store
from backend.services.spend_ledger import current_total
from backend.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # accept both Z-suffix and offset-suffix timestamps
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@router.get("/live")
async def get_live():
    """Real-time operational state — active projects, spend, carriers, bin.

    Pulls from Redis + spend ledger + config store (no Postgres dependency),
    so it works even before any extractions have persisted.
    """
    # Import locally to avoid circular deps with uploads.py
    from backend.api.uploads import _get_redis, _upload_key

    r = _get_redis()
    upload_ids = list(r.smembers("dd:uploads"))

    active_count = 0
    active_files_in_flight = 0
    oldest_active_started: datetime | None = None
    bin_count = 0
    completed_count = 0
    failed_count = 0

    now = datetime.now(timezone.utc)

    for uid in upload_ids:
        raw = r.get(_upload_key(uid))
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        status = data.get("status", "")
        if data.get("deleted_at"):
            bin_count += 1
            continue
        if status in ("extracting", "classifying", "cancel_requested"):
            active_count += 1
            files = data.get("file_assignments", data.get("classified", []))
            active_files_in_flight += len(files)
            started = _parse_iso(data.get("created_at"))
            if started and (oldest_active_started is None or started < oldest_active_started):
                oldest_active_started = started
        elif status == "done":
            completed_count += 1
        elif status in ("error", "interrupted", "cancelled"):
            failed_count += 1

    oldest_active_age_seconds = (
        int((now - oldest_active_started).total_seconds()) if oldest_active_started else 0
    )

    # Carriers
    store = get_config_store()
    carriers = []
    for key, cfg in store.get_all_carriers().items():
        fmt_count = len(store.get_formats(key))
        carriers.append({
            "key": key,
            "name": cfg.name,
            "format_count": fmt_count,
        })

    # Spend
    total_spent = current_total()
    cap = settings.max_spend_usd
    pct = (total_spent / cap * 100) if cap > 0 else 0.0

    return {
        "active": {
            "count": active_count,
            "files_in_flight": active_files_in_flight,
            "oldest_age_seconds": oldest_active_age_seconds,
        },
        "completed_count": completed_count,
        "failed_count": failed_count,
        "bin_count": bin_count,
        "spend": {
            "total_usd": round(total_spent, 4),
            "cap_usd": cap,
            "pct_used": round(pct, 2),
            "status": "danger" if pct >= 100 else ("warn" if pct >= (settings.spend_warn_pct * 100) else "ok"),
        },
        "carriers": sorted(carriers, key=lambda c: c["name"]),
    }


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Aggregated dashboard metrics across all extraction data."""

    # Total extraction runs
    runs_result = await db.execute(
        select(
            func.count(ExtractionRun.id).label("total_runs"),
            func.coalesce(func.sum(ExtractionRun.rows_extracted), 0).label("total_rows"),
            func.coalesce(func.sum(ExtractionRun.documents_processed), 0).label("total_documents"),
            func.coalesce(func.sum(ExtractionRun.estimated_cost_usd), 0).label("total_cost"),
        )
    )
    run_stats = runs_result.one()

    # Row-level stats from extracted_rows
    row_result = await db.execute(
        select(
            func.count(ExtractedRow.id).label("total_rows"),
            func.coalesce(func.sum(ExtractedRow.monthly_recurring_cost), 0).label("total_mrc"),
            func.count(func.nullif(ExtractedRow.review_status, "pending")).label("reviewed_rows"),
        )
    )
    row_stats = row_result.one()

    # Review status breakdown
    review_result = await db.execute(
        select(
            ExtractedRow.review_status,
            func.count(ExtractedRow.id),
        ).group_by(ExtractedRow.review_status)
    )
    review_breakdown = {status: count for status, count in review_result.all()}

    # Confidence breakdown from JSONB
    confidence_result = await db.execute(
        select(
            ExtractedRow.field_confidence["overall"].astext.label("level"),
            func.count(ExtractedRow.id),
        )
        .where(ExtractedRow.field_confidence["overall"].astext.isnot(None))
        .group_by(text("level"))
    )
    confidence_breakdown = {level: count for level, count in confidence_result.all()}

    # Carrier breakdown
    carrier_result = await db.execute(
        select(
            ExtractedRow.carrier,
            func.count(ExtractedRow.id).label("row_count"),
            func.coalesce(func.sum(ExtractedRow.monthly_recurring_cost), 0).label("mrc"),
        )
        .where(ExtractedRow.carrier.isnot(None))
        .group_by(ExtractedRow.carrier)
        .order_by(func.count(ExtractedRow.id).desc())
    )
    carriers = [
        {"carrier": carrier, "row_count": count, "mrc": float(mrc)}
        for carrier, count, mrc in carrier_result.all()
    ]

    # Corrections count
    corrections_result = await db.execute(select(func.count(Correction.id)))
    total_corrections = corrections_result.scalar() or 0

    # Recent extraction runs (last 10)
    recent_result = await db.execute(
        select(ExtractionRun)
        .order_by(ExtractionRun.created_at.desc())
        .limit(10)
    )
    recent_runs = [
        {
            "id": str(r.id),
            "upload_id": r.config_version,  # Redis upload_id
            "status": r.status,
            "documents_processed": r.documents_processed,
            "rows_extracted": r.rows_extracted,
            "estimated_cost_usd": float(r.estimated_cost_usd) if r.estimated_cost_usd else 0,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in recent_result.scalars().all()
    ]

    return {
        "extraction_runs": {
            "total": run_stats.total_runs,
            "total_documents": int(run_stats.total_documents),
            "total_rows": int(run_stats.total_rows),
            "total_cost_usd": float(run_stats.total_cost),
        },
        "rows": {
            "total": row_stats.total_rows,
            "total_mrc": float(row_stats.total_mrc),
            "reviewed": row_stats.reviewed_rows,
        },
        "review_status": review_breakdown,
        "confidence": confidence_breakdown,
        "carriers": carriers,
        "corrections": total_corrections,
        "recent_runs": recent_runs,
    }
