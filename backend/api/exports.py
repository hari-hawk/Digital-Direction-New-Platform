"""Export/Import API — Excel download and corrected Excel upload.

The Excel export is driven by `configs/processing/export_template.yaml` so
it always matches the customer's master inventory template:
  Sheet 1 "Extracted Data" — the rows in customer column order, with a
                              "Required level" annotation row above headers
  Sheet 2 "Checklist"      — 30 QA items (Agent / QA Yes-No columns blank
                              for the analyst to fill in)
  Sheet 3 "Column Explanations" — verbatim column definitions
  Sheet 4 "Mandatory Fields"    — requirement matrix grouped by area
"""

import io
import logging
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db
from backend.models.orm import ExtractedRow, ExtractionRun, Correction
from backend.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/exports", tags=["exports"])


@lru_cache(maxsize=1)
def _export_template() -> dict:
    """Load + cache the customer's export template config."""
    path = Path(settings.configs_dir) / "processing" / "export_template.yaml"
    if not path.exists():
        logger.warning("export_template.yaml missing — falling back to legacy column list")
        return {}
    return yaml.safe_load(path.read_text()) or {}


# Legacy column list — used as a fallback if the YAML is unavailable, and
# kept aliased to _EXCEL_FIELDS so /corrections/import (which expects this
# exact order) continues to work even after we switch to the YAML for export.
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
    """Download extracted rows as a multi-sheet Excel matching the customer's
    master inventory template (configs/processing/export_template.yaml).

    Sheets:
      1. Extracted Data        — rows in customer column order + requirement
                                  level annotation row + confidence color coding
      2. Checklist             — 30 QA items, Agent/QA Yes-No columns
      3. Column Explanations   — verbatim column definitions
      4. Mandatory Fields      — requirement matrix grouped by area
    """
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment

        # Find extraction run(s) for this upload
        run_result = await db.execute(
            select(ExtractionRun).where(ExtractionRun.config_version == upload_id)
        )
        runs = run_result.scalars().all()
        if not runs:
            return JSONResponse(status_code=404, content={"error": "No extraction data found"})

        run_ids = [r.id for r in runs]
        row_result = await db.execute(
            select(ExtractedRow)
            .where(ExtractedRow.extraction_run_id.in_(run_ids))
            .order_by(ExtractedRow.created_at)
        )
        rows = row_result.scalars().all()

        # Load the customer template (column order + checklist + explanations)
        template = _export_template()
        column_specs = template.get("columns") or [
            {"field": f, "label": l, "area": "", "required": ""}
            for f, l in _EXCEL_FIELDS
        ]
        explanations = template.get("explanations") or {}
        checklist_items = template.get("checklist") or []
        req_labels = template.get("requirement_labels") or {}

        wb = openpyxl.Workbook()
        _build_extracted_data_sheet(wb.active, rows, column_specs, req_labels)
        _build_checklist_sheet(wb.create_sheet("Checklist"), checklist_items)
        _build_column_explanations_sheet(
            wb.create_sheet("Column Explanations"), column_specs, explanations
        )
        _build_mandatory_fields_sheet(
            wb.create_sheet("Mandatory Fields"), column_specs, req_labels
        )

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=inventory_{upload_id}.xlsx"},
        )

    except Exception as e:
        logger.exception("Excel export failed for %s", upload_id)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ────────────────────────────────────────────────────────────────────────
# Sheet builders
# ────────────────────────────────────────────────────────────────────────


def _build_extracted_data_sheet(ws, rows, column_specs, req_labels):
    """Sheet 1 — the actual extracted data, in customer column order.

    Layout:
      row 1: Area headers (Location, Carrier Information, Service, ...)
      row 2: Required level (Required, Required if Applicable, ...)
      row 3: Column display names
      row 4+: Data rows, with confidence color fill per cell
    """
    from openpyxl.styles import PatternFill, Font, Alignment

    ws.title = "Extracted Data"

    fills = {
        "high":   PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
        "medium": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
        "low":    PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    }
    area_fill   = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    req_fill    = PatternFill(start_color="9DC3E6", end_color="9DC3E6", fill_type="solid")
    header_fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    white_bold  = Font(color="FFFFFF", bold=True)
    bold        = Font(bold=True)

    # Row 1 — area headers spanning each contiguous run of same area
    last_area = None
    span_start = 1
    for col_idx, spec in enumerate(column_specs, start=1):
        area = spec.get("area") or ""
        if area != last_area:
            if last_area is not None and col_idx > span_start:
                ws.merge_cells(start_row=1, start_column=span_start, end_row=1, end_column=col_idx - 1)
                cell = ws.cell(row=1, column=span_start)
                cell.value = last_area
                cell.fill = area_fill
                cell.font = white_bold
                cell.alignment = Alignment(horizontal="center")
            last_area = area
            span_start = col_idx
    # close the last area span
    end_col = len(column_specs)
    if end_col >= span_start:
        if end_col > span_start:
            ws.merge_cells(start_row=1, start_column=span_start, end_row=1, end_column=end_col)
        cell = ws.cell(row=1, column=span_start)
        cell.value = last_area
        cell.fill = area_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    # Row 2 — requirement level
    for col_idx, spec in enumerate(column_specs, start=1):
        cell = ws.cell(row=2, column=col_idx)
        cell.value = req_labels.get(spec.get("required", ""), spec.get("required", ""))
        cell.fill = req_fill
        cell.font = bold
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    # Row 3 — column labels
    for col_idx, spec in enumerate(column_specs, start=1):
        cell = ws.cell(row=3, column=col_idx)
        cell.value = spec["label"]
        cell.fill = header_fill
        cell.font = bold

    ws.row_dimensions[2].height = 32

    # Row 4+ — data rows, colored per-cell by confidence
    text_cols = []
    for col_idx, spec in enumerate(column_specs, start=1):
        if spec["field"] in ("btn", "phone_number", "carrier_account_number",
                             "sub_account_number_1", "sub_account_number_2",
                             "zip", "z_zip", "carrier_circuit_number"):
            text_cols.append(col_idx)

    data_start_row = 4
    for r_idx, row in enumerate(rows, start=data_start_row):
        confidence_map = row.field_confidence or {}
        for col_idx, spec in enumerate(column_specs, start=1):
            val = getattr(row, spec["field"], None)
            if val is None:
                continue
            cell = ws.cell(row=r_idx, column=col_idx)
            cell.value = val
            field_conf = confidence_map.get(spec["field"])
            if field_conf in fills:
                cell.fill = fills[field_conf]
            if col_idx in text_cols:
                cell.number_format = "@"

    # Reasonable column widths
    for col_idx, spec in enumerate(column_specs, start=1):
        col_letter = ws.cell(row=3, column=col_idx).column_letter
        # Wider for known long columns
        long_cols = {"notes", "auto_renewal_notes", "files_used", "additional_circuit_ids",
                     "component_or_feature_name", "billing_name", "service_address_1",
                     "z_address_1", "contract_file_name", "z_location_name"}
        ws.column_dimensions[col_letter].width = 30 if spec["field"] in long_cols else 18

    ws.freeze_panes = "A4"


def _build_checklist_sheet(ws, checklist_items):
    """Sheet 2 — 30 QA items + Agent / QA Yes-No columns blank for analyst to fill."""
    from openpyxl.styles import PatternFill, Font, Alignment

    ws.title = "Checklist"
    header_fill = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    white_bold = Font(color="FFFFFF", bold=True)

    headers = ["Checklist", "Agent — Yes/No", "QA — Yes/No"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.value = h
        cell.fill = header_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    for r_idx, item in enumerate(checklist_items, start=2):
        ws.cell(row=r_idx, column=1).value = item
        ws.cell(row=r_idx, column=1).alignment = Alignment(wrap_text=True)
        # Agent + QA columns left blank intentionally; analyst fills in.

    ws.column_dimensions["A"].width = 90
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 18
    ws.row_dimensions[1].height = 22


def _build_column_explanations_sheet(ws, column_specs, explanations):
    """Sheet 3 — column name + explanation, exactly as the customer ships it."""
    from openpyxl.styles import PatternFill, Font, Alignment

    ws.title = "Column Explanations"
    header_fill = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    white_bold = Font(color="FFFFFF", bold=True)

    headers = ["Column Name", "Explanation"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.value = h
        cell.fill = header_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    for r_idx, spec in enumerate(column_specs, start=2):
        ws.cell(row=r_idx, column=1).value = spec["label"]
        explanation = explanations.get(spec["field"]) or ""
        ws.cell(row=r_idx, column=2).value = explanation
        ws.cell(row=r_idx, column=2).alignment = Alignment(wrap_text=True)

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 80


def _build_mandatory_fields_sheet(ws, column_specs, req_labels):
    """Sheet 4 — requirement matrix grouped by area, mirroring the customer's
    "Mandatory Field details" tab structure."""
    from openpyxl.styles import PatternFill, Font, Alignment

    ws.title = "Mandatory Fields"
    area_fill   = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    header_fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
    white_bold  = Font(color="FFFFFF", bold=True)
    bold        = Font(bold=True)

    headers = ["Area", "Column", "Requirement Level"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.value = h
        cell.fill = area_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center")

    last_area = None
    for r_idx, spec in enumerate(column_specs, start=2):
        area = spec.get("area") or ""
        # Only repeat area on first row of each area group
        if area != last_area:
            ws.cell(row=r_idx, column=1).value = area
            ws.cell(row=r_idx, column=1).font = bold
            last_area = area
        ws.cell(row=r_idx, column=2).value = spec["label"]
        ws.cell(row=r_idx, column=3).value = req_labels.get(
            spec.get("required", ""), spec.get("required", "")
        )

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 56


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
