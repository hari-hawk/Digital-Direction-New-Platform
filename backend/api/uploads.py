"""Upload API — file upload, classification, extraction pipeline.

Upload state lives in Postgres (Phase B refactor, Apr-2026). Redis is now
used only for the live `files_processed` counter during extraction; everything
else — project name, classifications, results, bin status — is durable.

The seven thin helpers below are async wrappers over `backend.services.upload_store`.
They keep the existing call sites in this file readable (one-line awaits) and
preserve the `(short) upload_id` dict keys the frontend has always seen.
"""

import uuid
import json
import logging
import asyncio
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from backend.settings import settings
from backend.config_loader import get_config_store
from backend.pipeline.classifier import classify_by_filename, classify_by_content
from backend.pipeline.parser import parse_document
from backend.pipeline.extractor import extract_document, score_confidence
from backend.models.database import async_session
from backend.models.orm import ExtractionRun, ExtractedRow as ExtractedRowDB
from backend.services import upload_store as us
from backend.services.storage import get_storage

logger = logging.getLogger(__name__)


# ============================================
# Postgres-backed upload state (Phase B)
#
# Thin async wrappers around upload_store. Every helper returns the same
# dict shape the legacy Redis-backed helpers used, so call sites only had
# to gain an `await`.
# ============================================


# Account-level fields that propagate within a carrier_account_number group.
# Mirrors backend/pipeline/merger.py::_ACCOUNT_LEVEL_FIELDS — kept in sync
# manually because the extraction path uses dicts (post-merge uses ORM).
_ACCOUNT_LEVEL_FIELDS_DICT = (
    "billing_name", "service_address_1", "service_address_2",
    "city", "state", "zip", "country",
    "carrier_name", "master_account", "carrier_account_number",
    "invoice_file_name", "contract_file_name", "currency",
    "contract_term_months", "contract_begin_date", "contract_expiration_date",
    "contract_number", "contract_number_2",
    "currently_month_to_month", "mtm_or_less_than_year",
    "billing_per_contract", "auto_renew", "auto_renewal_notes",
    "contract_info_received",
)


def _propagate_account_level_fields_in_dicts(rows: list[dict]) -> None:
    """Fill account-level fields from any row that has them to rows that don't.

    Groups rows by carrier_account_number. For each group, picks the
    first non-empty value for each account-level field and back-fills
    blanks. Also derives `country` from a US/Canadian zip pattern when
    `country` is blank — purely structural, no hardcoded client data.
    Mutates rows in place.
    """
    if not rows:
        return
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        # Normalize: strip + lower so "12345" and " 12345 " group together.
        # Empty account_number rows form their own group ("") — those usually
        # are document-level summaries and won't propagate cross-document.
        key = (r.get("carrier_account_number") or "").strip()
        groups[key].append(r)

    filled = 0
    for key, group in groups.items():
        if len(group) <= 1 and key == "":
            continue
        best: dict[str, object] = {}
        for field in _ACCOUNT_LEVEL_FIELDS_DICT:
            for r in group:
                v = r.get(field)
                if v is not None and str(v).strip() != "":
                    best[field] = v
                    break
        for r in group:
            for field, value in best.items():
                cur = r.get(field)
                if cur is None or (isinstance(cur, str) and cur.strip() == ""):
                    r[field] = value
                    filled += 1

    # Derive country from zip (US 5-digit, Canadian alphanumeric)
    derived = 0
    import re as _re
    for r in rows:
        if r.get("country"):
            continue
        z = (r.get("zip") or "").strip()
        if not z:
            continue
        if _re.match(r"^\d{5}(-\d{4})?$", z):
            r["country"] = "USA"
            derived += 1
        elif _re.match(r"^[A-Za-z]\d[A-Za-z][\s-]?\d[A-Za-z]\d$", z):
            r["country"] = "CAN"
            derived += 1

    if filled or derived:
        logger.info(
            "Extraction propagation: filled %d account-level fields, "
            "derived %d countries from zip",
            filled, derived,
        )


async def _save_upload(upload_id: str, data: dict) -> None:
    """Insert (or upsert) the upload row keyed by short_id."""
    await us.save_upload(upload_id, data)


async def _get_upload(upload_id: str) -> dict | None:
    return await us.get_upload(upload_id)


async def _get_raw_results(upload_id: str) -> list | None:
    return await us.get_raw_results(upload_id)


async def _update_upload_field(upload_id: str, **fields) -> None:
    await us.update_upload_field(upload_id, **fields)


async def _update_upload_results(upload_id: str, results: list) -> None:
    await us.update_upload_results(upload_id, results)


async def _list_uploads(include_deleted: bool = False, only_deleted: bool = False) -> list[dict]:
    """List uploads, ordered by created_at desc.

    Uses the Postgres-backed upload_store (Phase B). Soft-deleted uploads
    are hidden by default; pass only_deleted=True for the bin or
    include_deleted=True for both.

    NOTE: unlike the previous implementation, this does NOT silently
    bootstrap fresh entries for orphaned disk folders — that was the second
    half of the "(unnamed) uploads" bug. Use POST /api/uploads/orphans to
    surface unlinked disk folders explicitly.
    """
    rows = await us.list_uploads(include_deleted=include_deleted, only_deleted=only_deleted)
    out = []
    for data in rows:
        # Card-friendly carrier set: prefer post-extraction computed_carriers,
        # fall back to classify-stage names (skipping "Unknown").
        classified_carriers = sorted({
            c.get("carrier") for c in data.get("classified", [])
            if c.get("carrier") and (c.get("carrier") or "").lower() != "unknown"
        })
        computed_carriers = data.get("computed_carriers") or []
        out.append({
            "upload_id": data["upload_id"],
            "project_name": data.get("project_name", ""),
            "client_name": data.get("client_name", ""),
            "status": data.get("status", "unknown"),
            "total_rows": len(data.get("results", [])),
            "files_total": len(data.get("file_assignments") or data.get("classified") or []),
            "files_processed": data.get("files_processed", 0),
            "created_at": data.get("created_at", ""),
            "deleted_at": data.get("deleted_at"),
            "classified": data.get("classified", []),
            "rows_with_issues": data.get("rows_with_issues", 0),
            "rows_error_level": data.get("rows_error_level", 0),
            "unique_accounts": data.get("unique_accounts", 0),
            "rows_needing_carrier_validation": data.get("rows_needing_carrier_validation", 0),
            "extraction_errors": data.get("extraction_errors", []),
            "carriers": computed_carriers or classified_carriers,
        })
    return out


async def _delete_upload(upload_id: str) -> bool:
    """Hard-delete: removes the DB row, the live counter, and on-disk files."""
    return await us.hard_delete_upload(upload_id)


# ============================================
# Carrier name mapping
# ============================================


def _carrier_key_to_display(key: str | None) -> str | None:
    """Map carrier directory key ('att') to display name ('AT&T')."""
    if not key:
        return None
    store = get_config_store()
    config = store.get_carrier(key)
    return config.name if config else key


def _carrier_display_to_key(display: str) -> str:
    """Map carrier display name ('AT&T') to directory key ('att').

    For configured carriers, returns the canonical key. For carriers detected
    by the LLM (e.g., "Frontier", "Lumen") or manually typed, returns a
    normalized slug so the downstream pipeline can route it generically.
    """
    import re as _re
    store = get_config_store()
    for key, config in store.get_all_carriers().items():
        if config.name.lower() == display.lower() or key == display.lower():
            return key
    # Normalize: lowercase, strip non-alphanumerics → stable slug.
    return _re.sub(r'[^a-z0-9]', '', display.lower())


# ============================================
# Pydantic models
# ============================================

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


class ClassifiedFile(BaseModel):
    filename: str
    carrier: Optional[str]
    doc_type: Optional[str]
    format_variant: Optional[str]
    file_size: int


class ClassifyResponse(BaseModel):
    upload_id: str
    files: list[ClassifiedFile]


class ExtractRequest(BaseModel):
    upload_id: str
    files: list[dict]  # [{filename, carrier}] — with user's carrier assignments


class ExtractedRowResponse(BaseModel):
    """All 60 extracted fields + metadata. Passes through the full ExtractedRow schema."""
    id: str
    source_file: str
    carrier: str
    confidence: str = "medium"

    # All 60 fields from ExtractedRow — Optional so missing fields are null
    row_type: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    contract_info_received: Optional[str] = None
    invoice_file_name: Optional[str] = None
    files_used: Optional[str] = None
    billing_name: Optional[str] = None
    service_address_1: Optional[str] = None
    service_address_2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    country: Optional[str] = None
    carrier_name: Optional[str] = None
    master_account: Optional[str] = None
    carrier_account_number: Optional[str] = None
    sub_account_number_1: Optional[str] = None
    sub_account_number_2: Optional[str] = None
    btn: Optional[str] = None
    phone_number: Optional[str] = None
    carrier_circuit_number: Optional[str] = None
    additional_circuit_ids: Optional[str] = None
    service_type: Optional[str] = None
    service_type_2: Optional[str] = None
    usoc: Optional[str] = None
    service_or_component: Optional[str] = None
    component_or_feature_name: Optional[str] = None
    monthly_recurring_cost: Optional[float] = None
    quantity: Optional[int] = None
    cost_per_unit: Optional[float] = None
    currency: Optional[str] = None
    conversion_rate: Optional[float] = None
    mrc_per_currency: Optional[float] = None
    charge_type: Optional[str] = None
    num_calls: Optional[int] = None
    ld_minutes: Optional[float] = None
    ld_cost: Optional[float] = None
    rate: Optional[float] = None
    ld_flat_rate: Optional[float] = None
    point_to_number: Optional[str] = None
    port_speed: Optional[str] = None
    access_speed: Optional[str] = None
    upload_speed: Optional[str] = None
    z_location_name: Optional[str] = None
    z_address_1: Optional[str] = None
    z_address_2: Optional[str] = None
    z_city: Optional[str] = None
    z_state: Optional[str] = None
    z_zip: Optional[str] = None
    z_country: Optional[str] = None
    contract_term_months: Optional[int] = None
    contract_begin_date: Optional[str] = None
    contract_expiration_date: Optional[str] = None
    billing_per_contract: Optional[str] = None
    currently_month_to_month: Optional[str] = None
    mtm_or_less_than_year: Optional[str] = None
    contract_file_name: Optional[str] = None
    contract_number: Optional[str] = None
    contract_number_2: Optional[str] = None
    auto_renew: Optional[str] = None
    auto_renewal_notes: Optional[str] = None


# ============================================
# Endpoints
# ============================================


@router.get("")
async def list_uploads(include_deleted: bool = False):
    """List active uploads. Pass include_deleted=true to get both active and deleted."""
    return {"uploads": await _list_uploads(include_deleted=include_deleted)}


@router.get("/bin")
async def list_bin():
    """List soft-deleted uploads (projects in the bin)."""
    return {"uploads": await _list_uploads(only_deleted=True)}


@router.get("/orphans")
async def list_orphans():
    """Disk folders in storage/temp/ that have no DB row.

    Surfaced explicitly (not silently bootstrapped) so an admin can decide
    whether to re-link, rename, or purge each one. Replaces the silent
    disk-bootstrap path that caused the original "(unnamed) uploads" bug.
    """
    return {"orphans": await us.find_orphan_disk_folders()}


@router.post("/classify", response_model=ClassifyResponse)
async def classify_upload(
    files: list[UploadFile] = File(...),
    project_name: str = Form(""),
    client_name: str = Form(""),
    description: str = Form(""),
):
    """Upload files and classify by carrier. Returns classification for user review.

    Files are written through `get_storage()` (Phase C, Apr-2026) so the
    same flow works with local disk in dev and GCS in prod. The returned
    `storage_path` is opaque — could be `/abs/path/...` for LocalStorage or
    `gs://bucket/...` for GCSStorage. Pipeline reads via `storage.open_local()`.
    """
    logger.info(f"Classify request: {len(files)} files, project={project_name}")
    upload_id = str(uuid.uuid4())[:8]
    storage = get_storage()

    classified = []
    saved_files: dict[str, str] = {}

    # Save all files via the storage abstraction.
    # `temp/{id}/{filename}` keeps the existing on-disk layout intact for
    # LocalStorage (so legacy paths still resolve), and gives us a clean
    # prefix for GCS (`gs://bucket/temp/{id}/...`).
    file_data: list[tuple[str, str, int]] = []  # (safe_name, storage_path, size)
    for file in files:
        safe_name = Path(file.filename).name
        if not safe_name:
            continue
        content = await file.read()
        storage_path = storage.save(content, f"temp/{upload_id}/{safe_name}")
        saved_files[safe_name] = storage_path
        file_data.append((safe_name, storage_path, len(content)))

    # Classify all files in parallel (pdfplumber is CPU-bound → thread pool).
    # Strategy: A (filename regex) → B (content regex) → C (open-ended LLM).
    # Stage C catches carriers outside the 4 configured ones (Frontier, Lumen, etc.)
    # so multi-carrier batches classify correctly per-file.
    from backend.pipeline.classifier import classify_by_llm, extract_first_pages_text

    async def _classify_one(safe_name: str, sp: str, fsize: int) -> ClassifiedFile:
        # open_local is zero-copy for LocalStorage and downloads-to-tempfile
        # for GCS. We hold the context across all reads so a GCS file is
        # downloaded once per classify, not once per stage.
        with storage.open_local(sp) as local_path:
            local_str = str(local_path)
            stage_a = classify_by_filename(safe_name)
            stage_b = await asyncio.to_thread(classify_by_content, local_str)
            carrier_key = stage_b.carrier or stage_a.carrier
            doc_type = stage_b.document_type or stage_a.document_type
            format_variant = stage_b.format_variant

            # Stage C: LLM fallback when A and B couldn't name a carrier.
            # The LLM is open-ended — it returns the actual carrier printed on the doc.
            if not carrier_key:
                try:
                    text = await asyncio.to_thread(extract_first_pages_text, local_str)
                    if text:
                        stage_c = await classify_by_llm(local_str, text)
                        carrier_key = stage_c.carrier or carrier_key
                        doc_type = doc_type or stage_c.document_type
                except Exception as e:
                    logger.warning(f"LLM classify failed for {safe_name}: {e}")

        # Configured carriers use their canonical display name ("AT&T"),
        # detected/unknown ones show title-cased slug ("frontier" -> "Frontier").
        store = get_config_store()
        cfg = store.get_carrier(carrier_key) if carrier_key else None
        if cfg:
            display = cfg.name
        elif carrier_key:
            display = carrier_key.title()
        else:
            display = None
        return ClassifiedFile(
            filename=safe_name,
            carrier=display,
            doc_type=doc_type,
            format_variant=format_variant,
            file_size=fsize,
        )

    classified = await asyncio.gather(*[
        _classify_one(name, path, size) for name, path, size in file_data
    ])

    # Client linkage (§2.1): if a client_name was provided, find-or-create the
    # Client row and stash its id in Redis. The merger consults client_id to
    # pull master-data. Empty client_name → no linkage, pipeline behaves as-is.
    client_id: str | None = None
    if client_name and client_name.strip():
        try:
            from backend.api.clients import find_or_create_client
            async with async_session() as session:
                async with session.begin():
                    client = await find_or_create_client(client_name, session)
                    if client:
                        client_id = str(client.id)
        except Exception as e:
            logger.warning(f"Client find-or-create failed for {client_name!r}: {e}")

    # Persist upload state to Postgres (Phase B). Survives restarts/redeploys.
    from datetime import datetime, timezone
    await _save_upload(upload_id, {
        "project_name": project_name,
        "client_name": client_name,
        "client_id": client_id,
        "description": description,
        "files": saved_files,
        "classified": [c.model_dump() for c in classified],
        "status": "classified",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "results": [],
    })

    return ClassifyResponse(upload_id=upload_id, files=classified)


@router.post("/extract")
async def extract_upload(request: ExtractRequest, background_tasks: BackgroundTasks):
    """Start extraction for selected files with user's carrier assignments."""
    upload = await _get_upload(request.upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    await _update_upload_field(request.upload_id,
                               status="extracting",
                               file_assignments=[dict(f) for f in request.files],
                               files_processed=0)

    # Run extraction in background
    background_tasks.add_task(_run_extraction, request.upload_id, request.files)

    return {"upload_id": request.upload_id, "status": "extracting", "file_count": len(request.files)}


@router.get("/{upload_id}/status")
async def get_status(upload_id: str):
    """Poll extraction progress."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    return {
        "upload_id": upload_id,
        "status": upload["status"],
        "total_rows": len(upload.get("results", [])),
        "files_processed": upload.get("files_processed", 0),
        "files_total": len(upload.get("file_assignments", [])),
    }


@router.get("/{upload_id}/results")
async def get_results(upload_id: str, view: str | None = None):
    """Get extracted rows for this upload. ?view=raw returns pre-merge results."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    if view == "raw":
        raw_results = await _get_raw_results(upload_id)
        if raw_results is not None:
            return {
                "upload_id": upload_id,
                "project_name": upload.get("project_name", ""),
                "status": upload["status"],
                "total_rows": len(raw_results),
                "rows": raw_results,
                "view": "raw",
                "has_merged": True,
            }

    results = upload.get("results", [])
    return {
        "upload_id": upload_id,
        "project_name": upload.get("project_name", ""),
        "status": upload["status"],
        "total_rows": len(results),
        "rows": results,
        "view": "merged" if upload.get("has_raw_results") else "default",
        "has_merged": upload.get("has_raw_results", False),
    }


@router.get("/{upload_id}/download")
async def download_all_files(upload_id: str):
    """Download every uploaded source file for a project as a single ZIP.

    Reads through the storage abstraction so the same handler works against
    LocalStorage (zero-copy) and GCSStorage (downloads each blob to a tempfile
    long enough to copy into the ZIP).
    """
    import io
    import zipfile

    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    saved_files: dict[str, str] = upload.get("files") or {}
    if not saved_files:
        return JSONResponse(status_code=404, content={"error": "No files on record for this upload"})

    storage = get_storage()
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, storage_path in sorted(saved_files.items()):
            try:
                with storage.open_local(storage_path) as local_path:
                    zf.write(local_path, arcname=filename)
                    added += 1
            except FileNotFoundError:
                logger.warning("ZIP skipped missing file %s (%s)", filename, storage_path)
            except Exception as e:
                logger.warning("ZIP skipped %s: %s", filename, e)

    if added == 0:
        return JSONResponse(status_code=404, content={"error": "All source files are missing from storage"})

    buf.seek(0)
    project_name = upload.get("project_name") or upload_id
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_name)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_files.zip"'},
    )


@router.get("/{upload_id}/files/{filename:path}")
async def get_file(upload_id: str, filename: str):
    """Serve an uploaded file (PDF, XLSX, etc.) for viewing in the browser.

    Local storage: serves the file inline via FileResponse.
    GCS storage: redirects to a short-lived signed URL, so the browser
    streams directly from GCS without going through the FastAPI process.
    """
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    saved_files: dict[str, str] = upload.get("files") or {}
    storage_path = saved_files.get(filename)
    if not storage_path:
        return JSONResponse(status_code=404, content={"error": "File not found for this upload"})

    storage = get_storage()
    if not storage.exists(storage_path):
        return JSONResponse(status_code=404, content={"error": "File missing from storage"})

    # GCS-backed: redirect to a signed URL (5-min TTL is plenty for a viewer).
    if storage_path.startswith("gs://"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=storage.public_url(storage_path, ttl_seconds=300), status_code=302)

    # Local-backed: serve directly with the right media type.
    suffix = Path(filename).suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".msg": "application/vnd.ms-outlook",
        ".eml": "message/rfc822",
    }
    with storage.open_local(storage_path) as local_path:
        # local_path is the actual on-disk file for LocalStorage (zero-copy);
        # FileResponse holds it open through the response lifecycle.
        return FileResponse(local_path, media_type=media_types.get(suffix, "application/octet-stream"))


@router.post("/{upload_id}/cancel")
async def cancel_extraction(upload_id: str):
    """Request cancellation of an in-progress extraction."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] != "extracting":
        return JSONResponse(status_code=400, content={"error": f"Cannot cancel upload in status '{upload['status']}'"})
    await _update_upload_field(upload_id, status="cancel_requested")
    return {"upload_id": upload_id, "status": "cancel_requested"}


@router.post("/{upload_id}/retry")
async def retry_extraction(upload_id: str, background_tasks: BackgroundTasks):
    """Re-run extraction on a project — allowed for completed runs too so users
    can re-extract after prompt/config updates. Not allowed while a run is in progress."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] in ("extracting", "classifying", "cancel_requested"):
        return JSONResponse(status_code=400, content={"error": f"Extraction already in progress (status={upload['status']}). Cancel first or wait."})
    file_assignments = upload.get("file_assignments") or upload.get("classified", [])
    if not file_assignments:
        return JSONResponse(status_code=400, content={"error": "No file assignments found — re-upload the files"})
    await _update_upload_field(upload_id, status="extracting", files_processed=0)
    await _update_upload_results(upload_id, [])
    background_tasks.add_task(_run_extraction, upload_id, file_assignments)
    return {"upload_id": upload_id, "status": "extracting"}


@router.delete("/{upload_id}")
async def delete_upload(upload_id: str):
    """Soft-delete an upload (moves it to the bin). Data is preserved and can be restored or purged."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] == "extracting":
        return JSONResponse(status_code=400, content={"error": "Cannot delete while extracting. Cancel first."})
    from datetime import datetime, timezone
    await _update_upload_field(upload_id, deleted_at=datetime.now(timezone.utc).isoformat())
    return {"upload_id": upload_id, "deleted": True, "soft": True}


@router.post("/{upload_id}/restore")
async def restore_upload(upload_id: str):
    """Restore a soft-deleted upload from the bin."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if not upload.get("deleted_at"):
        return JSONResponse(status_code=400, content={"error": "Upload is not in the bin"})
    await _update_upload_field(upload_id, deleted_at=None)
    return {"upload_id": upload_id, "restored": True}


@router.post("/{upload_id}/purge")
async def purge_upload(upload_id: str):
    """Permanently delete an upload — data cannot be recovered. Removes the
    Postgres row, the Redis live counter, on-disk files, and any related
    extraction_runs/extracted_rows."""
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] == "extracting":
        return JSONResponse(status_code=400, content={"error": "Cannot purge while extracting. Cancel first."})

    # Delete pipeline-side ExtractionRun rows that link back via config_version
    # (legacy linkage from the Redis era — kept for backward compat with rows
    # created before client_id linkage).
    try:
        from sqlalchemy import delete
        async with async_session() as session:
            async with session.begin():
                await session.execute(
                    delete(ExtractionRun).where(ExtractionRun.config_version == upload_id)
                )
    except Exception as e:
        logger.error(f"Failed to delete PostgreSQL ExtractionRuns for {upload_id}: {e}")

    # Hard-delete the upload row, on-disk files, and Redis counter.
    await _delete_upload(upload_id)
    return {"upload_id": upload_id, "purged": True}


@router.post("/cleanup")
async def cleanup_orphaned():
    """Delete on-disk temp folders that have no matching DB row.

    Now backed by upload_store.find_orphan_disk_folders() — which is the
    same data surfaced by GET /api/uploads/orphans. Use this endpoint when
    you've already decided every orphan should be purged. For per-folder
    decisions, call /orphans first.
    """
    import shutil
    orphans = await us.find_orphan_disk_folders()
    cleaned = 0
    for o in orphans:
        try:
            shutil.rmtree(o["path"])
            cleaned += 1
            logger.info("Cleaned orphaned temp folder: %s", o["short_id"])
        except Exception as e:
            logger.warning("Failed to clean orphan %s: %s", o["short_id"], e)
    return {"cleaned": cleaned}


@router.post("/backfill-db")
async def backfill_db():
    """Migrate completed extraction results into the typed extracted_rows table.

    Iterates every non-deleted upload (durable Postgres rows now, post Phase B)
    and copies finished results into extracted_rows for analytics queries.
    Idempotent — _persist_to_db handles dedupe.
    """
    uploads = await us.list_uploads(include_deleted=True)
    total_backfilled = 0
    backfilled_uploads = 0

    for upload in uploads:
        if upload.get("status") != "done":
            continue
        results = upload.get("results", [])
        if not results:
            continue
        files_processed = upload.get("files_processed", 0)
        await _persist_to_db(upload["upload_id"], results, files_processed)
        total_backfilled += len(results)
        backfilled_uploads += 1
        logger.info("Backfilled %d rows for upload %s", len(results), upload["upload_id"])

    return {"backfilled_uploads": backfilled_uploads, "backfilled_rows": total_backfilled}


# ============================================
# Cross-doc merge
# ============================================


@router.post("/{upload_id}/merge")
async def merge_upload(upload_id: str, background_tasks: BackgroundTasks):
    """Run cross-granularity merge on already-extracted results.

    Groups rows by carrier, runs merge + LLM conflict resolution,
    then replaces per-file rows with merged rows.
    """
    upload = await _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload.get("status") != "done":
        return JSONResponse(status_code=400, content={"error": f"Upload must be in 'done' status, got '{upload.get('status')}'"})

    results = upload.get("results", [])
    if not results:
        return JSONResponse(status_code=400, content={"error": "No extraction results to merge"})

    await _update_upload_field(upload_id, status="merging")
    background_tasks.add_task(_run_merge, upload_id)
    return {"upload_id": upload_id, "status": "merging"}


async def _run_merge(upload_id: str):
    """Background task: group by carrier, run cross_granularity_merge + LLM conflict resolution."""
    from backend.pipeline.merger import cross_granularity_merge, llm_resolve_conflicts
    from backend.models.schemas import ExtractedRow

    try:
        upload = await _get_upload(upload_id)
        results = upload.get("results", [])
        file_assignments = upload.get("file_assignments", [])

        # Save raw results before merge (so user can toggle back to the per-file
        # view in the UI). Stored on the same uploads row in Postgres.
        await us.set_raw_results(upload_id, results)

        # Build doc_type lookup from file_assignments
        doc_type_map = {}
        for fa in file_assignments:
            doc_type_map[fa["filename"]] = fa.get("doc_type", "invoice")

        # Group rows by carrier
        carrier_groups: dict[str, dict[str, list[ExtractedRow]]] = {}
        carrier_doc_types: dict[str, dict[str, str]] = {}

        for row_dict in results:
            carrier_display = row_dict.get("carrier", "unknown")
            source_file = row_dict.get("source_file", "unknown")
            carrier_key = _carrier_display_to_key(carrier_display)

            if carrier_key not in carrier_groups:
                carrier_groups[carrier_key] = {}
                carrier_doc_types[carrier_key] = {}

            if source_file not in carrier_groups[carrier_key]:
                carrier_groups[carrier_key][source_file] = []
                carrier_doc_types[carrier_key][source_file] = doc_type_map.get(source_file, "invoice")

            # Convert dict back to ExtractedRow model (strip metadata fields)
            row_fields = {k: v for k, v in row_dict.items()
                         if k not in ("id", "source_file", "carrier", "confidence", "field_confidence", "_confidence", "_row_index")}
            try:
                carrier_groups[carrier_key][source_file].append(ExtractedRow(**row_fields))
            except Exception:
                # Skip rows that can't be parsed back
                continue

        # Merge per carrier
        all_merged = []
        total_conflicts = 0
        for carrier_key, extractions in carrier_groups.items():
            carrier_display = _carrier_key_to_display(carrier_key) or carrier_key

            if len(extractions) <= 1:
                # Single doc — no merge needed, pass through
                for source_file, rows in extractions.items():
                    for i, row in enumerate(rows):
                        row_dict = row.model_dump(mode="json")
                        row_dict["source_file"] = source_file
                        row_dict["carrier"] = carrier_display
                        row_dict["id"] = f"{upload_id}-merged-{carrier_key}-{i}"
                        # Re-score confidence
                        conf_scores = score_confidence([row])
                        if conf_scores:
                            field_conf = {k: v.value if hasattr(v, 'value') else str(v) for k, v in conf_scores[0].items()}
                            row_dict["field_confidence"] = field_conf
                            non_missing = [v for v in field_conf.values() if v != "missing"]
                            row_dict["confidence"] = Counter(non_missing).most_common(1)[0][0] if non_missing else "medium"
                        all_merged.append(row_dict)
                continue

            logger.info(f"Merging {len(extractions)} docs for carrier {carrier_key}")
            merged_rows, conflicts = cross_granularity_merge(extractions, carrier_doc_types[carrier_key], carrier=carrier_key)
            total_conflicts += len(conflicts)

            # LLM conflict resolution if any
            if conflicts:
                logger.info(f"  → {len(conflicts)} conflicts, calling LLM resolver")
                try:
                    merged_rows = await llm_resolve_conflicts(merged_rows, conflicts, carrier=carrier_key)
                except Exception as e:
                    logger.warning(f"LLM conflict resolution failed: {e}, using rule-based results")

            # Score confidence and convert back to dicts
            conf_scores = score_confidence(merged_rows)
            for i, row in enumerate(merged_rows):
                row_dict = row.model_dump(mode="json")
                row_dict["carrier"] = carrier_display
                row_dict["source_file"] = row_dict.get("invoice_file_name") or row_dict.get("files_used") or "merged"
                row_dict["id"] = f"{upload_id}-merged-{carrier_key}-{i}"

                if i < len(conf_scores):
                    field_conf = {k: v.value if hasattr(v, 'value') else str(v) for k, v in conf_scores[i].items()}
                    row_dict["field_confidence"] = field_conf
                    non_missing = [v for v in field_conf.values() if v != "missing"]
                    row_dict["confidence"] = Counter(non_missing).most_common(1)[0][0] if non_missing else "medium"

                all_merged.append(row_dict)

        logger.info(f"Merge complete: {len(results)} rows → {len(all_merged)} merged rows ({total_conflicts} conflicts resolved)")

        # Auto-fill contract_info_received based on what was uploaded for
        # this project. Customer asked for this so the inventory column
        # reflects whether contracts exist on file:
        #   "Yes"   → at least one file in the upload had doc_type=contract
        #   "Email" → only email/.msg/.eml present (info came from a message)
        #   "No"    → only invoices/CSRs/reports (no contract on file)
        # Per-row override is preserved when the LLM already extracted a
        # specific value (e.g. "Pending from carrier"); we only fill blanks.
        try:
            doc_types = {(fa.get("doc_type") or "").lower() for fa in file_assignments}
            filenames = [(fa.get("filename") or "") for fa in file_assignments]
            has_contract_doc = "contract" in doc_types
            has_email = ("email" in doc_types) or any(
                fn.lower().endswith((".msg", ".eml")) for fn in filenames
            )
            if has_contract_doc:
                cir_default = "Yes"
            elif has_email:
                cir_default = "Email"
            else:
                cir_default = "No"
            cir_filled = 0
            for rd in all_merged:
                if not (rd.get("contract_info_received") or "").strip():
                    rd["contract_info_received"] = cir_default
                    cir_filled += 1
            if cir_filled:
                logger.info(
                    "contract_info_received auto-filled '%s' on %d rows "
                    "(based on uploaded doc types: %s)",
                    cir_default, cir_filled, sorted(doc_types - {""}),
                )
        except Exception as e:
            logger.warning("contract_info_received auto-fill skipped: %s", e)

        # §2.1 Master-data overrides — analyst-confirmed values win over
        # extracted/merged values for this client. No-op when the client has
        # no reference-data entries (day-one state).
        try:
            client_id = upload.get("client_id")
            if client_id:
                from backend.services.master_data import apply_master_data_overrides
                from backend.models.database import async_session as _async_session
                async with _async_session() as s:
                    # apply_master_data_overrides is sync; use sync-adapter via run_sync
                    def _apply(sync_session):
                        return apply_master_data_overrides(all_merged, client_id, sync_session)
                    _, n = await s.run_sync(_apply)
                    if n:
                        logger.info(f"master-data: {n} field overrides applied for client {client_id}")
        except Exception as e:
            logger.warning(f"master-data overrides skipped: {e}")

        # Post-merge validation + compliance — auto-run every time.
        # Validator re-checks format / cross-field math on merged rows.
        # Compliance flags rate_mismatch / expired_contract / MTM / term-math / no_contract.
        try:
            from backend.pipeline.validator import validate_rows
            from backend.pipeline.compliance import check_compliance, flags_to_jsonb

            model_rows = []
            for rd in all_merged:
                fields = {k: v for k, v in rd.items() if k in ExtractedRow.model_fields}
                try:
                    model_rows.append(ExtractedRow(**fields))
                except Exception:
                    model_rows.append(None)

            # Validation
            valid_model_rows = [r for r in model_rows if r is not None]
            validation_results = validate_rows(valid_model_rows)
            vi = 0
            for rd, vr in zip([rd for rd, mr in zip(all_merged, model_rows) if mr is not None], validation_results):
                if isinstance(vr, dict) and "issues" in vr:
                    issues = vr.get("issues", [])
                    rd["validation_issues"] = issues
                    rd["validation_valid"] = vr.get("valid", True)
                    if issues and any(i.get("severity") == "error" for i in issues):
                        vi += 1
                        if not rd.get("status"):
                            rd["status"] = "Needs Review"

            # Compliance
            compliance_result = check_compliance(valid_model_rows)
            ci = 0
            for idx, flags in compliance_result.flags_by_row.items():
                if idx < len(valid_model_rows):
                    # Map back to the original all_merged index
                    # (valid_model_rows skips rows that failed to rebuild)
                    original_idx = 0
                    valid_seen = 0
                    for j, mr in enumerate(model_rows):
                        if mr is not None:
                            if valid_seen == idx:
                                original_idx = j
                                break
                            valid_seen += 1
                    all_merged[original_idx]["compliance_flags"] = flags_to_jsonb(flags)
                    if any(f.severity == "error" for f in flags):
                        ci += 1
                        if not all_merged[original_idx].get("status"):
                            all_merged[original_idx]["status"] = "Needs Review"

            logger.info(f"Post-merge validation: {vi} rows with error-level issues, {ci} rows with error-level compliance flags")
        except Exception as e:
            logger.warning(f"Post-merge validation/compliance skipped: {e}")

        # Persist merged results to the durable uploads row.
        await _update_upload_results(upload_id, all_merged)
        await _update_upload_field(upload_id, status="done")

        # Re-persist to the typed extracted_rows table for analytics.
        await _persist_to_db(upload_id, all_merged, len(carrier_groups))

    except Exception as e:
        logger.error(f"Merge task crashed for {upload_id}: {e}")
        await _update_upload_field(upload_id, status="done")  # Revert to done, not error — original results still intact


# ============================================
# Stuck extraction detection (called on startup)
# ============================================


async def detect_stuck_extractions() -> None:
    """On startup, mark any uploads stuck in 'extracting' / 'cancel_requested'
    / 'merging' as 'interrupted'. Now reads/writes the durable Postgres row
    via upload_store, so it survives a Redis flush."""
    try:
        rows = await us.list_uploads(include_deleted=True)
        for u in rows:
            if u.get("status") in ("extracting", "cancel_requested", "merging"):
                logger.warning("Upload %s was stuck in '%s', marking as interrupted", u["upload_id"], u["status"])
                await us.update_upload_field(u["upload_id"], status="interrupted")
    except Exception as e:
        logger.error("Failed to detect stuck extractions: %s", e)


# ============================================
# Background extraction task
# ============================================


async def _run_extraction(upload_id: str, file_assignments: list[dict]):
    """Background task: parse → extract each file. Progress persisted to
    Postgres (rows + status); files_processed counter routed to Redis."""
    try:
        upload = await _get_upload(upload_id)
        if not upload:
            logger.error(f"Upload {upload_id} not found")
            return

        saved_files = upload["files"]
        all_rows = []
        files_processed = 0
        # Per-file failure log — surfaced on the upload card so silent 0-row
        # results stop being silent (e.g. PDF too big for the model).
        extraction_errors: list[dict] = []

        for assignment in file_assignments:
            # Check if cancel was requested
            upload_check = await _get_upload(upload_id)
            if upload_check and upload_check.get("status") == "cancel_requested":
                await _update_upload_field(upload_id, status="cancelled", files_processed=files_processed)
                await _update_upload_results(upload_id, all_rows)
                logger.info(f"Upload {upload_id} cancelled after {files_processed} files")
                return

            filename = assignment["filename"]
            carrier_display = assignment["carrier"]
            carrier_key = _carrier_display_to_key(carrier_display) or "unknown"
            storage_path = saved_files.get(filename)

            if not storage_path:
                continue

            try:
                logger.info(f"Extracting: {filename} (carrier={carrier_key})")

                # Parse via the storage abstraction. open_local is zero-copy
                # for LocalStorage and downloads-to-tempfile for GCS.
                storage = get_storage()
                with storage.open_local(storage_path) as local_path:
                    parsed = parse_document(str(local_path), carrier_key, assignment.get("doc_type", "invoice"))

                if not parsed.sections:
                    logger.warning(f"No sections from {filename}")
                    extraction_errors.append({
                        "filename": filename,
                        "carrier": carrier_display,
                        "reason": "Parser produced no sections — likely an unsupported format or empty PDF.",
                    })
                    continue

                # Extract — collect per-section errors so silent failures
                # (oversized prompt, JSON parse error, timeout) are surfaced.
                file_errors: list[str] = []
                rows, responses = await extract_document(parsed, errors_out=file_errors)
                if file_errors:
                    extraction_errors.append({
                        "filename": filename,
                        "carrier": carrier_display,
                        "reason": "; ".join(file_errors[:3]) + (" …" if len(file_errors) > 3 else ""),
                    })

                # Score per-field confidence
                confidence_scores = score_confidence(rows)

                # Auto-validate — format/cross-field checks run on every upload.
                # Each row gets a list of issues (empty if clean). Row is "Needs Review"
                # when any error-severity issue is present.
                from backend.pipeline.validator import validate_rows
                validation_results = validate_rows(rows)

                # Convert to response format — dump all 60 fields
                for i, row in enumerate(rows):
                    row_dict = row.model_dump(mode="json")  # mode=json serializes dates/decimals
                    row_dict["id"] = f"{upload_id}-{filename}-{i}"
                    row_dict["source_file"] = filename
                    row_dict["carrier"] = carrier_display
                    # extraction_order preserves the LLM's emission order, which
                    # matches the PDF's top-to-bottom reading order. Frontend and
                    # exports sort by this by default so "Total Monthly Service"
                    # stays next to the section it totals, not at the end.
                    row_dict["extraction_order"] = len(all_rows)

                    # Per-field confidence from scorer
                    field_conf = {k: v.value if hasattr(v, 'value') else str(v) for k, v in confidence_scores[i].items()} if i < len(confidence_scores) else {}
                    row_dict["field_confidence"] = field_conf

                    # Overall confidence = majority vote (excluding missing)
                    non_missing = [v for v in field_conf.values() if v != "missing"]
                    if non_missing:
                        row_dict["confidence"] = Counter(non_missing).most_common(1)[0][0]
                    else:
                        row_dict["confidence"] = "medium"

                    # Attach validation results
                    v = validation_results[i] if i < len(validation_results) and isinstance(validation_results[i], dict) and "issues" in validation_results[i] else {"issues": [], "valid": True}
                    issues = v.get("issues", [])
                    row_dict["validation_issues"] = issues
                    row_dict["validation_valid"] = v.get("valid", True)
                    # Flip status to "Needs Review" when validator flagged errors,
                    # unless the row already has a status from the analyst/merger.
                    if issues and any(iss.get("severity") == "error" for iss in issues) and not row_dict.get("status"):
                        row_dict["status"] = "Needs Review"

                    all_rows.append(row_dict)

                files_processed += 1
                # Persist progress after each file. files_processed routes to
                # Redis (live counter); results write to Postgres jsonb.
                await _update_upload_field(upload_id, files_processed=files_processed)
                await _update_upload_results(upload_id, all_rows)
                logger.info(f"  → {len(rows)} rows from {filename}")

            except Exception as e:
                logger.error(f"Extraction failed for {filename}: {e}")
                extraction_errors.append({
                    "filename": filename,
                    "carrier": carrier_display,
                    "reason": str(e)[:200],
                })
                continue

        # Summary stats — validation issues, unique accounts, carrier names
        # computed from extracted rows. Surfaced on the Previous Uploads cards.
        rows_with_issues = sum(1 for r in all_rows if r.get("validation_issues"))
        rows_error_level = sum(1 for r in all_rows
                               if any(i.get("severity") == "error" for i in (r.get("validation_issues") or [])))
        unique_accounts = len({r.get("carrier_account_number") for r in all_rows if r.get("carrier_account_number")})

        # Carrier post-processing — every carrier is supported, no manual setup needed.
        #   1. Auto-register any LLM-detected carrier that's not yet in the registry
        #      (writes a minimal carrier.yaml + hot-reloads the config store).
        #      So next upload of the same carrier sees it as known.
        #   2. Canonicalize each row's carrier_name to the registry display name
        #      (handles "Verizon Wireless" → "Verizon", etc.).
        #   3. Track which carriers appeared so the upload card can list them.
        from backend.services.carrier_match import match_carrier_name
        from backend.services.auto_carrier_registry import auto_register_from_rows

        # Step 1: auto-register first, so the second pass picks up new carriers
        # as registered (canonicalized + no "Validate carrier" friction).
        newly_registered = auto_register_from_rows(all_rows)
        if newly_registered:
            logger.info(
                "Auto-registered %d new carrier(s): %s",
                len(newly_registered),
                [r["name"] for r in newly_registered],
            )

        # Step 2: canonicalize each row's carrier_name now that the registry
        # includes the freshly-registered carriers.
        canonical_names: set[str] = set()
        for r in all_rows:
            raw = r.get("carrier_name") or r.get("carrier")
            match = match_carrier_name(raw)
            if match is None:
                continue
            r["carrier_name"] = match.canonical_name
            canonical_names.add(match.canonical_name)
        computed_carriers = sorted(n for n in canonical_names if n.lower() != "unknown")

        # Step 3: account-level field propagation + zip→country derivation +
        # contract_info_received auto-fill. Runs at extraction time so users
        # who don't run the cross-doc merger still get a fully-populated
        # inventory (every row of an account carries billing_name, address,
        # contract terms, etc., not just the first S row).
        try:
            _propagate_account_level_fields_in_dicts(all_rows)
        except Exception as e:
            logger.warning("account-level propagation skipped: %s", e)
        try:
            doc_types = {(fa.get("doc_type") or "").lower() for fa in file_assignments}
            filenames = [(fa.get("filename") or "") for fa in file_assignments]
            has_contract_doc = "contract" in doc_types
            has_email = ("email" in doc_types) or any(
                fn.lower().endswith((".msg", ".eml")) for fn in filenames
            )
            if has_contract_doc:
                cir_default = "Yes"
            elif has_email:
                cir_default = "Email"
            else:
                cir_default = "No"
            cir_filled = 0
            for r in all_rows:
                if not (r.get("contract_info_received") or "").strip():
                    r["contract_info_received"] = cir_default
                    cir_filled += 1
            if cir_filled:
                logger.info(
                    "contract_info_received auto-filled '%s' on %d rows (doc types: %s)",
                    cir_default, cir_filled, sorted(doc_types - {""}),
                )
        except Exception as e:
            logger.warning("contract_info_received auto-fill skipped: %s", e)
        # needs_validation_count is now always 0 — auto-register closes the loop.
        # Kept as a field for backward compat with the API contract.
        needs_validation_count = 0
        logger.info(f"Validation: {rows_with_issues} of {len(all_rows)} rows have issues ({rows_error_level} at error severity)")
        logger.info(f"Carriers: {len(computed_carriers)} unique canonical "
                    f"({len(newly_registered)} auto-registered), "
                    f"{unique_accounts} unique accounts, names={computed_carriers}")

        await _update_upload_field(
            upload_id,
            status="done",
            files_processed=files_processed,
            rows_with_issues=rows_with_issues,
            rows_error_level=rows_error_level,
            unique_accounts=unique_accounts,
            computed_carriers=computed_carriers,
            rows_needing_carrier_validation=needs_validation_count,
            extraction_errors=extraction_errors,
        )
        await _update_upload_results(upload_id, all_rows)
        logger.info(f"Upload {upload_id} complete: {len(all_rows)} total rows from {files_processed} files")
        if extraction_errors:
            logger.warning(
                "Upload %s finished with %d file-level extraction error(s): %s",
                upload_id, len(extraction_errors),
                [(e['filename'], e['reason'][:120]) for e in extraction_errors],
            )

        # Persist to the typed extracted_rows table for analytics queries.
        await _persist_to_db(upload_id, all_rows, files_processed)

    except Exception as e:
        logger.error(f"Extraction task crashed for {upload_id}: {e}")
        await _update_upload_field(upload_id, status="error")


async def _persist_to_db(upload_id: str, rows: list[dict], files_processed: int):
    """Save extraction results to PostgreSQL after a successful extraction run."""
    from datetime import datetime, timezone, date

    def _parse_date(val):
        """Parse date string to date object, or return None."""
        if val is None:
            return None
        if isinstance(val, date):
            return val
        try:
            return date.fromisoformat(str(val)[:10])
        except (ValueError, TypeError):
            return None

    try:
        from sqlalchemy import delete

        async with async_session() as session:
            async with session.begin():
                # Remove previous run for this upload_id (idempotent)
                await session.execute(
                    delete(ExtractionRun).where(ExtractionRun.config_version == upload_id)
                )

                # Create extraction run record
                now = datetime.utcnow()
                run = ExtractionRun(
                    upload_id=None,  # upload_id in Redis is short (8-char), not a UUID FK
                    started_at=now,
                    completed_at=now,
                    status="completed",
                    documents_processed=files_processed,
                    rows_extracted=len(rows),
                    config_version=upload_id,  # store Redis upload_id for cross-reference
                )
                session.add(run)
                await session.flush()  # get run.id

                for row_dict in rows:
                    # Extract metadata fields that aren't part of the 60-field schema
                    source_file = row_dict.get("source_file")
                    carrier = row_dict.get("carrier")
                    confidence = row_dict.get("confidence", "medium")

                    db_row = ExtractedRowDB(
                        extraction_run_id=run.id,
                        source_documents=[{"source_file": source_file, "redis_id": row_dict.get("id")}],
                        carrier=carrier,
                        field_confidence=row_dict.get("field_confidence") or {"overall": confidence},
                        review_status="pending",
                        invoice_file_name=source_file,
                        # 60-field mapping
                        row_type=row_dict.get("row_type"),
                        status=row_dict.get("status"),
                        notes=row_dict.get("notes"),
                        contract_info_received=row_dict.get("contract_info_received"),
                        files_used=row_dict.get("files_used"),
                        billing_name=row_dict.get("billing_name"),
                        service_address_1=row_dict.get("service_address_1"),
                        service_address_2=row_dict.get("service_address_2"),
                        city=row_dict.get("city"),
                        state=row_dict.get("state"),
                        zip=row_dict.get("zip"),
                        country=row_dict.get("country"),
                        carrier_name=row_dict.get("carrier_name"),
                        master_account=row_dict.get("master_account"),
                        carrier_account_number=row_dict.get("carrier_account_number"),
                        sub_account_number_1=row_dict.get("sub_account_number_1"),
                        sub_account_number_2=row_dict.get("sub_account_number_2"),
                        btn=row_dict.get("btn"),
                        phone_number=row_dict.get("phone_number"),
                        carrier_circuit_number=row_dict.get("carrier_circuit_number"),
                        additional_circuit_ids=row_dict.get("additional_circuit_ids"),
                        service_type=row_dict.get("service_type"),
                        service_type_2=row_dict.get("service_type_2"),
                        usoc=row_dict.get("usoc"),
                        service_or_component=row_dict.get("service_or_component"),
                        component_or_feature_name=row_dict.get("component_or_feature_name"),
                        monthly_recurring_cost=row_dict.get("monthly_recurring_cost"),
                        quantity=row_dict.get("quantity"),
                        cost_per_unit=row_dict.get("cost_per_unit"),
                        currency=row_dict.get("currency"),
                        conversion_rate=row_dict.get("conversion_rate"),
                        mrc_per_currency=row_dict.get("mrc_per_currency"),
                        charge_type=row_dict.get("charge_type"),
                        num_calls=row_dict.get("num_calls"),
                        ld_minutes=row_dict.get("ld_minutes"),
                        ld_cost=row_dict.get("ld_cost"),
                        rate=row_dict.get("rate"),
                        ld_flat_rate=row_dict.get("ld_flat_rate"),
                        point_to_number=row_dict.get("point_to_number"),
                        port_speed=row_dict.get("port_speed"),
                        access_speed=row_dict.get("access_speed"),
                        upload_speed=row_dict.get("upload_speed"),
                        z_location_name=row_dict.get("z_location_name"),
                        z_address_1=row_dict.get("z_address_1"),
                        z_address_2=row_dict.get("z_address_2"),
                        z_city=row_dict.get("z_city"),
                        z_state=row_dict.get("z_state"),
                        z_zip=row_dict.get("z_zip"),
                        z_country=row_dict.get("z_country"),
                        contract_term_months=row_dict.get("contract_term_months"),
                        contract_begin_date=_parse_date(row_dict.get("contract_begin_date")),
                        contract_expiration_date=_parse_date(row_dict.get("contract_expiration_date")),
                        billing_per_contract=row_dict.get("billing_per_contract"),
                        currently_month_to_month=row_dict.get("currently_month_to_month"),
                        mtm_or_less_than_year=row_dict.get("mtm_or_less_than_year"),
                        contract_file_name=row_dict.get("contract_file_name"),
                        contract_number=row_dict.get("contract_number"),
                        contract_number_2=row_dict.get("contract_number_2"),
                        auto_renew=row_dict.get("auto_renew"),
                        auto_renewal_notes=row_dict.get("auto_renewal_notes"),
                    )
                    session.add(db_row)

        logger.info(f"Persisted {len(rows)} rows to PostgreSQL for upload {upload_id}")
    except Exception as e:
        logger.error(f"Failed to persist to PostgreSQL for upload {upload_id}: {e}")
        # Don't fail the extraction — Redis still has the data
