"""Review API — get extracted rows, submit corrections, bulk approve."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, text, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db
from backend.models.orm import ExtractedRow, Correction, ExtractionRun, Document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])


class CorrectionRequest(BaseModel):
    field_name: str
    extracted_value: str | None
    corrected_value: str
    correction_notes: str = ""


class BulkApproveRequest(BaseModel):
    row_ids: list[str]


def _row_to_dict(row: ExtractedRow) -> dict:
    """Convert an ORM ExtractedRow to a JSON-serializable dict."""
    return {
        "id": str(row.id),
        "extraction_run_id": str(row.extraction_run_id) if row.extraction_run_id else None,
        "carrier": row.carrier,
        "review_status": row.review_status,
        "field_confidence": row.field_confidence or {},
        "field_sources": row.field_sources or {},
        "source_documents": row.source_documents or [],
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        # 60 fields
        "row_type": row.row_type,
        "status": row.status,
        "notes": row.notes,
        "contract_info_received": row.contract_info_received,
        "invoice_file_name": row.invoice_file_name,
        "files_used": row.files_used,
        "billing_name": row.billing_name,
        "service_address_1": row.service_address_1,
        "service_address_2": row.service_address_2,
        "city": row.city,
        "state": row.state,
        "zip": row.zip,
        "country": row.country,
        "carrier_name": row.carrier_name,
        "master_account": row.master_account,
        "carrier_account_number": row.carrier_account_number,
        "sub_account_number_1": row.sub_account_number_1,
        "sub_account_number_2": row.sub_account_number_2,
        "btn": row.btn,
        "phone_number": row.phone_number,
        "carrier_circuit_number": row.carrier_circuit_number,
        "additional_circuit_ids": row.additional_circuit_ids,
        "service_type": row.service_type,
        "service_type_2": row.service_type_2,
        "usoc": row.usoc,
        "service_or_component": row.service_or_component,
        "component_or_feature_name": row.component_or_feature_name,
        "monthly_recurring_cost": float(row.monthly_recurring_cost) if row.monthly_recurring_cost else None,
        "quantity": row.quantity,
        "cost_per_unit": float(row.cost_per_unit) if row.cost_per_unit else None,
        "currency": row.currency,
        "conversion_rate": float(row.conversion_rate) if row.conversion_rate else None,
        "mrc_per_currency": float(row.mrc_per_currency) if row.mrc_per_currency else None,
        "charge_type": row.charge_type,
        "num_calls": row.num_calls,
        "ld_minutes": float(row.ld_minutes) if row.ld_minutes else None,
        "ld_cost": float(row.ld_cost) if row.ld_cost else None,
        "rate": float(row.rate) if row.rate else None,
        "ld_flat_rate": float(row.ld_flat_rate) if row.ld_flat_rate else None,
        "point_to_number": row.point_to_number,
        "port_speed": row.port_speed,
        "access_speed": row.access_speed,
        "upload_speed": row.upload_speed,
        "z_location_name": row.z_location_name,
        "z_address_1": row.z_address_1,
        "z_address_2": row.z_address_2,
        "z_city": row.z_city,
        "z_state": row.z_state,
        "z_zip": row.z_zip,
        "z_country": row.z_country,
        "contract_term_months": row.contract_term_months,
        "contract_begin_date": row.contract_begin_date.isoformat() if row.contract_begin_date else None,
        "contract_expiration_date": row.contract_expiration_date.isoformat() if row.contract_expiration_date else None,
        "billing_per_contract": row.billing_per_contract,
        "currently_month_to_month": row.currently_month_to_month,
        "mtm_or_less_than_year": row.mtm_or_less_than_year,
        "contract_file_name": row.contract_file_name,
        "contract_number": row.contract_number,
        "contract_number_2": row.contract_number_2,
        "auto_renew": row.auto_renew,
        "auto_renewal_notes": row.auto_renewal_notes,
    }


# ============================================
# Endpoints
# ============================================


@router.get("/{upload_id}/rows")
async def get_rows(
    upload_id: str,
    confidence: str | None = None,
    review_status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get extracted rows for review. Filter by confidence level or review status.

    The upload_id here is the Redis short-id (stored in extraction_runs.config_version).
    """
    # Find the extraction run(s) for this upload
    run_query = select(ExtractionRun).where(ExtractionRun.config_version == upload_id)
    run_result = await db.execute(run_query)
    runs = run_result.scalars().all()

    if not runs:
        return {"upload_id": upload_id, "rows": [], "total": 0}

    run_ids = [r.id for r in runs]

    # Query extracted rows
    query = select(ExtractedRow).where(ExtractedRow.extraction_run_id.in_(run_ids))

    if review_status:
        query = query.where(ExtractedRow.review_status == review_status)

    if confidence:
        # Filter by overall confidence in the JSONB field
        query = query.where(
            ExtractedRow.field_confidence["overall"].astext == confidence
        )

    query = query.order_by(ExtractedRow.created_at)
    result = await db.execute(query)
    rows = result.scalars().all()

    return {
        "upload_id": upload_id,
        "total": len(rows),
        "rows": [_row_to_dict(r) for r in rows],
    }


@router.get("/rows/{row_id}/sources")
async def get_row_sources(row_id: str, db: AsyncSession = Depends(get_db)):
    """Get source document snippets for a row's fields."""
    try:
        row_uuid = uuid.UUID(row_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid row ID format"})

    result = await db.execute(select(ExtractedRow).where(ExtractedRow.id == row_uuid))
    row = result.scalar_one_or_none()

    if not row:
        return JSONResponse(status_code=404, content={"error": "Row not found"})

    return {
        "row_id": row_id,
        "source_documents": row.source_documents or [],
        "field_sources": row.field_sources or {},
        "field_confidence": row.field_confidence or {},
    }


@router.patch("/rows/{row_id}")
async def submit_correction(
    row_id: str,
    correction: CorrectionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Submit a human correction for a field."""
    try:
        row_uuid = uuid.UUID(row_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid row ID format"})

    result = await db.execute(select(ExtractedRow).where(ExtractedRow.id == row_uuid))
    row = result.scalar_one_or_none()

    if not row:
        return JSONResponse(status_code=404, content={"error": "Row not found"})

    # Resolve format_variant from the row's primary document (if available)
    format_variant = None
    source_text_snippet = None
    if row.primary_document_id:
        doc_result = await db.execute(
            select(Document).where(Document.id == row.primary_document_id)
        )
        doc = doc_result.scalar_one_or_none()
        if doc:
            format_variant = doc.format_variant
            # Try to load source section text from cached parsed sections
            if doc.parsed_sections_path:
                try:
                    import json
                    from pathlib import Path
                    sections_path = Path(doc.parsed_sections_path)
                    if sections_path.exists():
                        sections_data = json.loads(sections_path.read_text())
                        # Take first section as context (truncated)
                        if sections_data:
                            source_text_snippet = str(sections_data[0].get("text", ""))[:500]
                except Exception:
                    pass  # Best-effort source text retrieval

    # Build correction context for embedding
    correction_context = (
        f"carrier:{row.carrier or ''} format:{format_variant or ''} "
        f"field:{correction.field_name} "
        f"extracted:{correction.extracted_value or ''} "
        f"corrected:{correction.corrected_value}"
    )
    if source_text_snippet:
        correction_context += f"\nsource_text:{source_text_snippet[:300]}"

    # Insert correction record with full metadata
    corr = Correction(
        extracted_row_id=row.id,
        extraction_run_id=row.extraction_run_id,
        field_name=correction.field_name,
        extracted_value=correction.extracted_value,
        corrected_value=correction.corrected_value,
        correction_type="manual",
        carrier=row.carrier,
        format_variant=format_variant,
        source_text_snippet=source_text_snippet,
        correction_context=correction_context,
        correction_notes=correction.correction_notes,
    )
    db.add(corr)

    # Apply the correction to the row
    if hasattr(row, correction.field_name):
        setattr(row, correction.field_name, correction.corrected_value)
        row.review_status = "corrected"
        row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(corr)

    # Generate embedding for pgvector similarity search (inline, ~200ms)
    # Uses raw SQL because SQLAlchemy ORM doesn't support pgvector column type
    try:
        from backend.services.llm import get_gemini
        gemini = get_gemini()
        embedding = await gemini.embed(correction_context)
        await db.execute(
            text("UPDATE corrections SET embedding = :emb WHERE id = :cid"),
            {"emb": str(embedding), "cid": str(corr.id)},
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to generate correction embedding: {e}")
        # Non-fatal — correction is saved, pgvector fallback degraded

    # §2.1 Master-data writeback — for location/identity/contract fields, save
    # the analyst-confirmed value into the client's reference-data store so
    # future uploads for the same client inherit it. No-op when no client is
    # linked to the upload (legacy rows).
    try:
        # Resolve client_id via the upload Redis state (upload_id is on the run)
        client_id = None
        if row.upload_id:
            from backend.api.uploads import _get_upload as _get_up
            up = _get_up(str(row.upload_id))
            client_id = up.get("client_id") if up else None
        if client_id:
            from backend.services.master_data import store_correction_to_master_data
            def _store(sync_session):
                return store_correction_to_master_data(
                    sync_session,
                    client_id=client_id,
                    row=row,
                    field_name=correction.field_name,
                    corrected_value=correction.corrected_value,
                    confirmed_by="analyst",
                )
            wrote = await db.run_sync(_store)
            if wrote:
                await db.commit()
    except Exception as e:
        logger.warning(f"master-data writeback skipped: {e}")

    return {
        "row_id": row_id,
        "correction_id": str(corr.id),
        "field_name": correction.field_name,
        "status": "correction_saved",
    }


@router.post("/{upload_id}/bulk-approve")
async def bulk_approve(
    upload_id: str,
    request: BulkApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Approve rows by setting review_status to 'approved'."""
    now = datetime.now(timezone.utc)

    try:
        row_uuids = [uuid.UUID(rid) for rid in request.row_ids]
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid row ID format in list"})

    stmt = (
        update(ExtractedRow)
        .where(ExtractedRow.id.in_(row_uuids))
        .values(review_status="approved", reviewed_at=now)
    )
    result = await db.execute(stmt)
    await db.commit()

    return {
        "upload_id": upload_id,
        "approved": result.rowcount,
    }
