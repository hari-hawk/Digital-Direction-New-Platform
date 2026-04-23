"""Analytics API — field-level quality metrics from extraction data."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db
from backend.models.orm import ExtractedRow, Correction
from backend.models.schemas import FIELD_CATEGORIES, FieldCategory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# All 60 extractable field names (column names on extracted_rows)
_ALL_FIELDS = list(FIELD_CATEGORIES.keys())


@router.get("/stats")
async def get_analytics(db: AsyncSession = Depends(get_db)):
    """Field-level fill rates, category scores, and correction patterns."""

    # Total row count
    total_result = await db.execute(select(func.count(ExtractedRow.id)))
    total_rows = total_result.scalar() or 0

    if total_rows == 0:
        return {
            "total_rows": 0,
            "field_fill_rates": [],
            "category_fill_rates": {},
            "top_corrected_fields": [],
            "corrections_by_carrier": [],
        }

    # Field fill rates — count non-null values per field
    # Numeric fields can't be compared to empty string
    from sqlalchemy import inspect as sa_inspect, String, Text
    field_counts = {}
    for field_name in _ALL_FIELDS:
        col = getattr(ExtractedRow, field_name, None)
        if col is None:
            continue
        col_prop = col.property.columns[0]
        is_string = isinstance(col_prop.type, (String, Text))
        query = select(func.count()).where(col.isnot(None))
        if is_string:
            query = query.where(col != "")
        result = await db.execute(query)
        count = result.scalar() or 0
        field_counts[field_name] = count

    # Build field fill rate list with categories
    field_fill_rates = []
    category_totals: dict[str, dict] = {}

    for field_name, count in field_counts.items():
        category = FIELD_CATEGORIES.get(field_name, FieldCategory.FUZZY).value
        fill_rate = round(count / total_rows * 100, 1) if total_rows > 0 else 0

        field_fill_rates.append({
            "field": field_name,
            "category": category,
            "filled": count,
            "total": total_rows,
            "fill_rate": fill_rate,
        })

        if category not in category_totals:
            category_totals[category] = {"filled_sum": 0, "field_count": 0}
        category_totals[category]["filled_sum"] += fill_rate
        category_totals[category]["field_count"] += 1

    # Sort by fill rate descending
    field_fill_rates.sort(key=lambda x: x["fill_rate"], reverse=True)

    # Average fill rate per category
    category_fill_rates = {}
    for cat, data in category_totals.items():
        avg = round(data["filled_sum"] / data["field_count"], 1) if data["field_count"] else 0
        category_fill_rates[cat] = {
            "avg_fill_rate": avg,
            "field_count": data["field_count"],
        }

    # Top corrected fields
    correction_result = await db.execute(
        select(
            Correction.field_name,
            func.count(Correction.id).label("count"),
        )
        .group_by(Correction.field_name)
        .order_by(func.count(Correction.id).desc())
        .limit(15)
    )
    top_corrected = [
        {"field": field, "corrections": count}
        for field, count in correction_result.all()
    ]

    # Corrections by carrier
    carrier_corr_result = await db.execute(
        select(
            Correction.carrier,
            func.count(Correction.id).label("count"),
        )
        .where(Correction.carrier.isnot(None))
        .group_by(Correction.carrier)
        .order_by(func.count(Correction.id).desc())
    )
    corrections_by_carrier = [
        {"carrier": carrier, "corrections": count}
        for carrier, count in carrier_corr_result.all()
    ]

    return {
        "total_rows": total_rows,
        "field_fill_rates": field_fill_rates,
        "category_fill_rates": category_fill_rates,
        "top_corrected_fields": top_corrected,
        "corrections_by_carrier": corrections_by_carrier,
    }
