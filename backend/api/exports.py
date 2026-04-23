"""Export/Import API — Excel download and corrected Excel upload."""

import io
import logging

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db
from backend.models.orm import ExtractedRow, ExtractionRun, Correction

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/exports", tags=["exports"])

# Field order for Excel export — matches the 60-field DD2 template
_EXCEL_FIELDS = [
    ("status", "Status"),
    ("notes", "Notes"),
    ("contract_info_received", "Contract Info Received"),
    ("invoice_file_name", "Invoice File Name"),
    ("files_used", "Files Used"),
    ("billing_name", "Billing Name"),
    ("service_address_1", "Service Address 1"),
    ("service_address_2", "Service Address 2"),
    ("city", "City"),
    ("state", "State"),
    ("zip", "Zip"),
    ("country", "Country"),
    ("carrier_name", "Carrier"),
    ("master_account", "Master Account"),
    ("carrier_account_number", "Carrier Account Number"),
    ("sub_account_number_1", "Sub-Account Number"),
    ("sub_account_number_2", "Sub-Account Number 2"),
    ("btn", "BTN"),
    ("phone_number", "Phone Number"),
    ("carrier_circuit_number", "Carrier Circuit Number"),
    ("additional_circuit_ids", "Additional Circuit IDs"),
    ("service_type", "Service Type"),
    ("service_type_2", "Service Type 2"),
    ("usoc", "USOC"),
    ("service_or_component", "Service or Component"),
    ("component_or_feature_name", "Component or Feature Name"),
    ("monthly_recurring_cost", "Monthly Recurring Cost"),
    ("quantity", "Quantity"),
    ("cost_per_unit", "Cost Per Unit"),
    ("currency", "Currency"),
    ("conversion_rate", "Conversion Rate"),
    ("mrc_per_currency", "MRC per Currency"),
    ("charge_type", "Charge Type"),
    ("num_calls", "# Calls"),
    ("ld_minutes", "LD Minutes"),
    ("ld_cost", "LD Cost"),
    ("rate", "Rate"),
    ("ld_flat_rate", "LD Flat Rate"),
    ("point_to_number", "Point to Number"),
    ("port_speed", "Port Speed"),
    ("access_speed", "Access Speed"),
    ("upload_speed", "Upload Speed"),
    ("z_location_name", "Z Location Name"),
    ("z_address_1", "Z Address 1"),
    ("z_address_2", "Z Address 2"),
    ("z_city", "Z City"),
    ("z_state", "Z State"),
    ("z_zip", "Z Zip"),
    ("z_country", "Z Country"),
    ("contract_term_months", "Contract Term Months"),
    ("contract_begin_date", "Contract Begin Date"),
    ("contract_expiration_date", "Contract Expiration Date"),
    ("billing_per_contract", "Billing Per Contract"),
    ("currently_month_to_month", "Currently Month-to-Month"),
    ("mtm_or_less_than_year", "MTM or Less Than Year"),
    ("contract_file_name", "Contract File Name"),
    ("contract_number", "Contract Number"),
    ("contract_number_2", "2nd Contract Number"),
    ("auto_renew", "Auto Renew"),
    ("auto_renewal_notes", "Auto Renewal Notes"),
]


@router.get("/{upload_id}/excel")
async def export_excel(upload_id: str, db: AsyncSession = Depends(get_db)):
    """Download extracted rows as Excel with confidence color coding."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill

        # Find extraction run(s) for this upload
        run_result = await db.execute(
            select(ExtractionRun).where(ExtractionRun.config_version == upload_id)
        )
        runs = run_result.scalars().all()

        if not runs:
            return JSONResponse(status_code=404, content={"error": "No extraction data found"})

        run_ids = [r.id for r in runs]

        # Query rows
        row_result = await db.execute(
            select(ExtractedRow)
            .where(ExtractedRow.extraction_run_id.in_(run_ids))
            .order_by(ExtractedRow.created_at)
        )
        rows = row_result.scalars().all()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Extracted Data"

        # Confidence color fills
        fills = {
            "high": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
            "medium": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
            "low": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
        }

        # Headers
        headers = [display for _, display in _EXCEL_FIELDS]
        ws.append(headers)

        # Data rows
        for row in rows:
            confidence_map = row.field_confidence or {}
            values = []
            for field_name, _ in _EXCEL_FIELDS:
                val = getattr(row, field_name, None)
                if val is not None:
                    val = str(val)
                values.append(val)
            ws.append(values)

            # Apply confidence color coding per cell
            row_idx = ws.max_row
            for col_idx, (field_name, _) in enumerate(_EXCEL_FIELDS, start=1):
                field_conf = confidence_map.get(field_name)
                if field_conf and field_conf in fills:
                    ws.cell(row=row_idx, column=col_idx).fill = fills[field_conf]

        # Format phone number columns as text
        for col_letter in ["R", "S"]:  # BTN, Phone Number
            for cell in ws[col_letter]:
                cell.number_format = "@"

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=extraction_{upload_id}.xlsx"},
        )

    except Exception as e:
        logger.error(f"Excel export failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/corrections/import")
async def import_corrections(
    upload_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload corrected Excel, diff against extraction, store corrections."""
    try:
        import openpyxl

        content = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(content))
        ws = wb.active

        # Find extraction run(s)
        run_result = await db.execute(
            select(ExtractionRun).where(ExtractionRun.config_version == upload_id)
        )
        runs = run_result.scalars().all()
        if not runs:
            return JSONResponse(status_code=404, content={"error": "No extraction data found"})

        run_ids = [r.id for r in runs]

        # Load existing rows ordered by creation
        row_result = await db.execute(
            select(ExtractedRow)
            .where(ExtractedRow.extraction_run_id.in_(run_ids))
            .order_by(ExtractedRow.created_at)
        )
        db_rows = row_result.scalars().all()

        if not db_rows:
            return JSONResponse(status_code=404, content={"error": "No rows to compare against"})

        # Parse Excel rows (skip header)
        excel_rows = list(ws.iter_rows(min_row=2, values_only=True))
        corrections_created = 0

        for idx, excel_values in enumerate(excel_rows):
            if idx >= len(db_rows):
                break
            db_row = db_rows[idx]

            for col_idx, (field_name, _) in enumerate(_EXCEL_FIELDS):
                if col_idx >= len(excel_values):
                    break
                excel_val = excel_values[col_idx]
                db_val = getattr(db_row, field_name, None)

                # Compare — normalize to string for comparison
                excel_str = str(excel_val).strip() if excel_val is not None else None
                db_str = str(db_val).strip() if db_val is not None else None

                if excel_str != db_str and excel_str:
                    corr = Correction(
                        extracted_row_id=db_row.id,
                        extraction_run_id=db_row.extraction_run_id,
                        field_name=field_name,
                        extracted_value=db_str,
                        corrected_value=excel_str,
                        correction_type="excel_import",
                        carrier=db_row.carrier,
                    )
                    db.add(corr)

                    # Apply correction to row
                    setattr(db_row, field_name, excel_str)
                    corrections_created += 1

            db_row.review_status = "corrected"

        await db.commit()

        return {
            "upload_id": upload_id,
            "rows_compared": min(len(excel_rows), len(db_rows)),
            "corrections_created": corrections_created,
        }

    except Exception as e:
        logger.error(f"Corrections import failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})
