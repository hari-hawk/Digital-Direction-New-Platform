"""Upload API — file upload, classification, extraction pipeline.

Upload state is persisted in Redis so it survives page refreshes and server restarts.
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
import redis

from backend.settings import settings
from backend.config_loader import get_config_store
from backend.pipeline.classifier import classify_by_filename, classify_by_content
from backend.pipeline.parser import parse_document
from backend.pipeline.extractor import extract_document, score_confidence
from backend.models.database import async_session
from backend.models.orm import ExtractionRun, ExtractedRow as ExtractedRowDB

logger = logging.getLogger(__name__)


# ============================================
# Redis-backed upload storage
# ============================================

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def _upload_key(upload_id: str) -> str:
    return f"dd:upload:{upload_id}"


def _save_upload(upload_id: str, data: dict) -> None:
    """Save upload state to Redis. Results stored separately to keep hash small."""
    r = _get_redis()
    results = data.pop("results", [])
    r.set(_upload_key(upload_id), json.dumps(data), ex=86400)  # 24h TTL
    r.set(f"{_upload_key(upload_id)}:results", json.dumps(results), ex=86400)
    # Track in uploads index
    r.sadd("dd:uploads", upload_id)


def _get_upload(upload_id: str) -> dict | None:
    """Load upload state from Redis."""
    r = _get_redis()
    raw = r.get(_upload_key(upload_id))
    if not raw:
        return None
    data = json.loads(raw)
    results_raw = r.get(f"{_upload_key(upload_id)}:results")
    data["results"] = json.loads(results_raw) if results_raw else []
    # Check if raw (pre-merge) results exist
    raw_results = r.get(f"{_upload_key(upload_id)}:results:raw")
    data["has_raw_results"] = raw_results is not None
    return data


def _get_raw_results(upload_id: str) -> list | None:
    """Load pre-merge raw results from Redis."""
    r = _get_redis()
    raw = r.get(f"{_upload_key(upload_id)}:results:raw")
    return json.loads(raw) if raw else None


def _update_upload_field(upload_id: str, **fields) -> None:
    """Update specific fields on an upload without rewriting results."""
    r = _get_redis()
    raw = r.get(_upload_key(upload_id))
    if not raw:
        return
    data = json.loads(raw)
    data.update(fields)
    r.set(_upload_key(upload_id), json.dumps(data), ex=86400)


def _update_upload_results(upload_id: str, results: list) -> None:
    """Update just the results list."""
    r = _get_redis()
    r.set(f"{_upload_key(upload_id)}:results", json.dumps(results), ex=86400)


def _list_uploads(include_deleted: bool = False, only_deleted: bool = False) -> list[dict]:
    """List all uploads from disk, enriched with Redis state where available.

    Disk (storage/temp/) is the source of truth for which uploads exist.
    Redis provides active state (status, progress, results, classified metadata).
    Uploads without Redis entries are shown with files discovered from disk.

    Soft-deleted uploads (with deleted_at timestamp) are excluded by default.
    Pass only_deleted=True to list the bin; include_deleted=True to get both.
    """
    r = _get_redis()
    temp_dir = Path(settings.storage_base_dir) / "temp"
    uploads = []
    seen_ids = set()

    # First: all uploads that have Redis state
    redis_ids = r.smembers("dd:uploads")
    for uid in redis_ids:
        raw = r.get(_upload_key(uid))
        if not raw:
            r.srem("dd:uploads", uid)
            continue
        data = json.loads(raw)
        is_deleted = bool(data.get("deleted_at"))
        if is_deleted and not (include_deleted or only_deleted):
            seen_ids.add(uid)
            continue
        if only_deleted and not is_deleted:
            seen_ids.add(uid)
            continue
        results_raw = r.get(f"{_upload_key(uid)}:results")
        result_count = len(json.loads(results_raw)) if results_raw else 0
        # Carriers from classify stage (may contain "Unknown" while extraction pending).
        # Prefer computed_carriers (from actual row data) when available — those
        # reflect the LLM-detected carrier names and avoid "Unknown" labels.
        classified_carriers = sorted({
            c.get("carrier") for c in data.get("classified", [])
            if c.get("carrier") and c.get("carrier").lower() != "unknown"
        })
        computed_carriers = data.get("computed_carriers") or []
        uploads.append({
            "upload_id": uid,
            "project_name": data.get("project_name", ""),
            "client_name": data.get("client_name", ""),
            "status": data.get("status", "unknown"),
            "total_rows": result_count,
            "files_total": len(data.get("file_assignments", data.get("classified", []))),
            "files_processed": data.get("files_processed", 0),
            "created_at": data.get("created_at", ""),
            "deleted_at": data.get("deleted_at"),
            "classified": data.get("classified", []),
            # Richer card stats — zero when not yet computed (pending extraction).
            "rows_with_issues": data.get("rows_with_issues", 0),
            "rows_error_level": data.get("rows_error_level", 0),
            "unique_accounts": data.get("unique_accounts", 0),
            # Effective carrier list: post-extraction names when available; else classify stage.
            "carriers": computed_carriers or classified_carriers,
        })
        seen_ids.add(uid)

    # Second: disk folders without Redis entries (pre-Redis uploads or expired keys)
    if temp_dir.exists():
        for folder in sorted(temp_dir.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name in seen_ids:
                continue
            # Discover files on disk and build a minimal classified list
            disk_files = []
            for f in sorted(folder.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    disk_files.append({
                        "filename": f.name,
                        "carrier": None,
                        "doc_type": None,
                        "format_variant": None,
                        "file_size": f.stat().st_size,
                    })
            if not disk_files:
                continue
            # Classify these files to get carrier/doc_type
            for cf in disk_files:
                stage_a = classify_by_filename(cf["filename"])
                stage_b = classify_by_content(str(folder / cf["filename"]))
                carrier_key = stage_b.carrier or stage_a.carrier
                cf["carrier"] = _carrier_key_to_display(carrier_key)
                cf["doc_type"] = stage_b.document_type or stage_a.document_type
                cf["format_variant"] = stage_b.format_variant

            # Bootstrap Redis entry so future operations (extract, delete) work
            import os
            from datetime import datetime, timezone
            folder_mtime = datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc)
            saved_files = {f["filename"]: str(folder / f["filename"]) for f in disk_files}
            _save_upload(folder.name, {
                "project_name": "",
                "client_name": "",
                "description": "",
                "files": saved_files,
                "classified": disk_files,
                "status": "classified",
                "created_at": folder_mtime.isoformat(),
                "results": [],
            })

            uploads.append({
                "upload_id": folder.name,
                "project_name": "",
                "client_name": "",
                "status": "classified",
                "total_rows": 0,
                "files_total": len(disk_files),
                "files_processed": 0,
                "created_at": folder_mtime.isoformat(),
                "classified": disk_files,
            })

    # Sort by created_at descending
    uploads.sort(key=lambda u: u.get("created_at", ""), reverse=True)
    return uploads


def _delete_upload(upload_id: str) -> bool:
    """Delete upload from Redis and clean up temp files on disk."""
    r = _get_redis()
    r.delete(_upload_key(upload_id))
    r.delete(f"{_upload_key(upload_id)}:results")
    r.srem("dd:uploads", upload_id)
    # Clean up temp files
    import shutil
    upload_dir = Path(settings.storage_base_dir) / "temp" / upload_id
    if upload_dir.exists():
        shutil.rmtree(upload_dir)
    return True


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
    return {"uploads": _list_uploads(include_deleted=include_deleted)}


@router.get("/bin")
async def list_bin():
    """List soft-deleted uploads (projects in the bin)."""
    return {"uploads": _list_uploads(only_deleted=True)}


@router.post("/classify", response_model=ClassifyResponse)
async def classify_upload(
    files: list[UploadFile] = File(...),
    project_name: str = Form(""),
    client_name: str = Form(""),
    description: str = Form(""),
):
    """Upload files and classify by carrier. Returns classification for user review."""
    logger.info(f"Classify request: {len(files)} files, project={project_name}")
    upload_id = str(uuid.uuid4())[:8]
    upload_dir = Path(settings.storage_base_dir) / "temp" / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    classified = []
    saved_files = {}

    # Save all files to disk first (fast I/O)
    file_data: list[tuple[str, str, int]] = []  # (safe_name, file_path, size)
    for file in files:
        safe_name = Path(file.filename).name
        if not safe_name:
            continue
        file_path = upload_dir / safe_name
        content = await file.read()
        file_path.write_bytes(content)
        saved_files[safe_name] = str(file_path)
        file_data.append((safe_name, str(file_path), len(content)))

    # Classify all files in parallel (pdfplumber is CPU-bound → thread pool).
    # Strategy: A (filename regex) → B (content regex) → C (open-ended LLM).
    # Stage C catches carriers outside the 4 configured ones (Frontier, Lumen, etc.)
    # so multi-carrier batches classify correctly per-file.
    from backend.pipeline.classifier import classify_by_llm, extract_first_pages_text

    async def _classify_one(safe_name: str, fpath: str, fsize: int) -> ClassifiedFile:
        stage_a = classify_by_filename(safe_name)
        stage_b = await asyncio.to_thread(classify_by_content, fpath)
        carrier_key = stage_b.carrier or stage_a.carrier
        doc_type = stage_b.document_type or stage_a.document_type
        format_variant = stage_b.format_variant

        # Stage C: LLM fallback when A and B couldn't name a carrier.
        # The LLM is open-ended — it returns the actual carrier printed on the doc.
        if not carrier_key:
            try:
                text = await asyncio.to_thread(extract_first_pages_text, fpath)
                if text:
                    stage_c = await classify_by_llm(fpath, text)
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

    # Store upload state in Redis
    from datetime import datetime, timezone
    _save_upload(upload_id, {
        "project_name": project_name,
        "client_name": client_name,
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
    upload = _get_upload(request.upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    _update_upload_field(request.upload_id,
                         status="extracting",
                         file_assignments=[dict(f) for f in request.files],
                         files_processed=0)

    # Run extraction in background
    background_tasks.add_task(_run_extraction, request.upload_id, request.files)

    return {"upload_id": request.upload_id, "status": "extracting", "file_count": len(request.files)}


@router.get("/{upload_id}/status")
async def get_status(upload_id: str):
    """Poll extraction progress."""
    upload = _get_upload(upload_id)
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
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})

    if view == "raw":
        raw_results = _get_raw_results(upload_id)
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
    """Download every uploaded source file for a project as a single ZIP."""
    import io
    import zipfile
    upload_dir = Path(settings.storage_base_dir) / "temp" / upload_id
    if not upload_dir.exists():
        return JSONResponse(status_code=404, content={"error": "Upload files not found"})

    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(upload_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                zf.write(f, arcname=f.name)
                added += 1
    if added == 0:
        return JSONResponse(status_code=404, content={"error": "No files on disk for this upload"})

    buf.seek(0)
    upload = _get_upload(upload_id)
    project_name = (upload.get("project_name") if upload else None) or upload_id
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_name)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_files.zip"'},
    )


@router.get("/{upload_id}/files/{filename:path}")
async def get_file(upload_id: str, filename: str):
    """Serve an uploaded file (PDF, XLSX, etc.) for viewing in the browser."""
    file_path = Path(settings.storage_base_dir) / "temp" / upload_id / filename
    if not file_path.exists():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    # Determine media type
    suffix = file_path.suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".msg": "application/vnd.ms-outlook",
        ".eml": "message/rfc822",
    }
    return FileResponse(file_path, media_type=media_types.get(suffix, "application/octet-stream"))


@router.post("/{upload_id}/cancel")
async def cancel_extraction(upload_id: str):
    """Request cancellation of an in-progress extraction."""
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] != "extracting":
        return JSONResponse(status_code=400, content={"error": f"Cannot cancel upload in status '{upload['status']}'"})
    _update_upload_field(upload_id, status="cancel_requested")
    return {"upload_id": upload_id, "status": "cancel_requested"}


@router.post("/{upload_id}/retry")
async def retry_extraction(upload_id: str, background_tasks: BackgroundTasks):
    """Re-run extraction on a project — allowed for completed runs too so users
    can re-extract after prompt/config updates. Not allowed while a run is in progress."""
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] in ("extracting", "classifying", "cancel_requested"):
        return JSONResponse(status_code=400, content={"error": f"Extraction already in progress (status={upload['status']}). Cancel first or wait."})
    file_assignments = upload.get("file_assignments") or upload.get("classified", [])
    if not file_assignments:
        return JSONResponse(status_code=400, content={"error": "No file assignments found — re-upload the files"})
    _update_upload_field(upload_id, status="extracting", files_processed=0)
    _update_upload_results(upload_id, [])
    background_tasks.add_task(_run_extraction, upload_id, file_assignments)
    return {"upload_id": upload_id, "status": "extracting"}


@router.delete("/{upload_id}")
async def delete_upload(upload_id: str):
    """Soft-delete an upload (moves it to the bin). Data is preserved and can be restored or purged."""
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] == "extracting":
        return JSONResponse(status_code=400, content={"error": "Cannot delete while extracting. Cancel first."})
    from datetime import datetime, timezone
    _update_upload_field(upload_id, deleted_at=datetime.now(timezone.utc).isoformat())
    # Remove Redis TTL so binned items survive past the active-upload expiry
    r = _get_redis()
    r.persist(_upload_key(upload_id))
    r.persist(f"{_upload_key(upload_id)}:results")
    return {"upload_id": upload_id, "deleted": True, "soft": True}


@router.post("/{upload_id}/restore")
async def restore_upload(upload_id: str):
    """Restore a soft-deleted upload from the bin."""
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if not upload.get("deleted_at"):
        return JSONResponse(status_code=400, content={"error": "Upload is not in the bin"})
    _update_upload_field(upload_id, deleted_at=None)
    return {"upload_id": upload_id, "restored": True}


@router.post("/{upload_id}/purge")
async def purge_upload(upload_id: str):
    """Permanently delete an upload — data cannot be recovered. Removes Redis, disk, and Postgres rows."""
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload["status"] == "extracting":
        return JSONResponse(status_code=400, content={"error": "Cannot purge while extracting. Cancel first."})

    # Delete from PostgreSQL (extraction_runs cascade-deletes extracted_rows)
    try:
        from sqlalchemy import delete
        async with async_session() as session:
            async with session.begin():
                await session.execute(
                    delete(ExtractionRun).where(ExtractionRun.config_version == upload_id)
                )
    except Exception as e:
        logger.error(f"Failed to delete PostgreSQL data for {upload_id}: {e}")

    # Delete from Redis + disk
    _delete_upload(upload_id)
    return {"upload_id": upload_id, "purged": True}


@router.post("/cleanup")
async def cleanup_orphaned():
    """Delete temp folders that have no corresponding Redis entry."""
    import shutil
    temp_dir = Path(settings.storage_base_dir) / "temp"
    if not temp_dir.exists():
        return {"cleaned": 0}
    r = _get_redis()
    known_ids = r.smembers("dd:uploads")
    cleaned = 0
    for folder in temp_dir.iterdir():
        if folder.is_dir() and folder.name not in known_ids:
            shutil.rmtree(folder)
            cleaned += 1
            logger.info(f"Cleaned orphaned temp folder: {folder.name}")
    return {"cleaned": cleaned}


@router.post("/backfill-db")
async def backfill_db():
    """Migrate all existing Redis extraction results into PostgreSQL.

    Safe to run multiple times — clears previous backfill data first.
    """
    r = _get_redis()
    upload_ids = r.smembers("dd:uploads")
    total_backfilled = 0

    for uid in upload_ids:
        upload = _get_upload(uid)
        if not upload or upload.get("status") != "done":
            continue

        results = upload.get("results", [])
        if not results:
            continue

        files_processed = upload.get("files_processed", 0)
        await _persist_to_db(uid, results, files_processed)
        total_backfilled += len(results)
        logger.info(f"Backfilled {len(results)} rows for upload {uid}")

    return {"backfilled_uploads": len([u for u in upload_ids if _get_upload(u) and _get_upload(u).get("status") == "done"]), "backfilled_rows": total_backfilled}


# ============================================
# Cross-doc merge
# ============================================


@router.post("/{upload_id}/merge")
async def merge_upload(upload_id: str, background_tasks: BackgroundTasks):
    """Run cross-granularity merge on already-extracted results.

    Groups rows by carrier, runs merge + LLM conflict resolution,
    then replaces per-file rows with merged rows.
    """
    upload = _get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Upload not found"})
    if upload.get("status") != "done":
        return JSONResponse(status_code=400, content={"error": f"Upload must be in 'done' status, got '{upload.get('status')}'"})

    results = upload.get("results", [])
    if not results:
        return JSONResponse(status_code=400, content={"error": "No extraction results to merge"})

    _update_upload_field(upload_id, status="merging")
    background_tasks.add_task(_run_merge, upload_id)
    return {"upload_id": upload_id, "status": "merging"}


async def _run_merge(upload_id: str):
    """Background task: group by carrier, run cross_granularity_merge + LLM conflict resolution."""
    from backend.pipeline.merger import cross_granularity_merge, llm_resolve_conflicts
    from backend.models.schemas import ExtractedRow

    try:
        upload = _get_upload(upload_id)
        results = upload.get("results", [])
        file_assignments = upload.get("file_assignments", [])

        # Save raw results before merge (so user can toggle back)
        r = _get_redis()
        r.set(f"{_upload_key(upload_id)}:results:raw", json.dumps(results), ex=86400)

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

        # Update Redis with merged results
        _update_upload_results(upload_id, all_merged)
        _update_upload_field(upload_id, status="done")

        # Re-persist to PostgreSQL
        await _persist_to_db(upload_id, all_merged, len(carrier_groups))

    except Exception as e:
        logger.error(f"Merge task crashed for {upload_id}: {e}")
        _update_upload_field(upload_id, status="done")  # Revert to done, not error — original results still intact


# ============================================
# Stuck extraction detection (called on startup)
# ============================================


def detect_stuck_extractions():
    """On startup, mark any uploads stuck in 'extracting' as 'interrupted'."""
    try:
        r = _get_redis()
        upload_ids = r.smembers("dd:uploads")
        for uid in upload_ids:
            raw = r.get(_upload_key(uid))
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("status") in ("extracting", "cancel_requested"):
                logger.warning(f"Upload {uid} was stuck in '{data['status']}', marking as interrupted")
                data["status"] = "interrupted"
                r.set(_upload_key(uid), json.dumps(data), ex=86400)
    except Exception as e:
        logger.error(f"Failed to detect stuck extractions: {e}")


# ============================================
# Background extraction task
# ============================================


async def _run_extraction(upload_id: str, file_assignments: list[dict]):
    """Background task: parse → extract each file. Progress persisted to Redis."""
    try:
        upload = _get_upload(upload_id)
        if not upload:
            logger.error(f"Upload {upload_id} not found in Redis")
            return

        saved_files = upload["files"]
        all_rows = []
        files_processed = 0

        for assignment in file_assignments:
            # Check if cancel was requested
            upload_check = _get_upload(upload_id)
            if upload_check and upload_check.get("status") == "cancel_requested":
                _update_upload_field(upload_id, status="cancelled", files_processed=files_processed)
                _update_upload_results(upload_id, all_rows)
                logger.info(f"Upload {upload_id} cancelled after {files_processed} files")
                return

            filename = assignment["filename"]
            carrier_display = assignment["carrier"]
            carrier_key = _carrier_display_to_key(carrier_display) or "unknown"
            file_path = saved_files.get(filename)

            if not file_path:
                continue

            try:
                logger.info(f"Extracting: {filename} (carrier={carrier_key})")

                # Parse
                parsed = parse_document(file_path, carrier_key, assignment.get("doc_type", "invoice"))

                if not parsed.sections:
                    logger.warning(f"No sections from {filename}")
                    continue

                # Extract
                rows, responses = await extract_document(parsed)

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
                # Persist progress to Redis after each file
                _update_upload_field(upload_id, files_processed=files_processed)
                _update_upload_results(upload_id, all_rows)
                logger.info(f"  → {len(rows)} rows from {filename}")

            except Exception as e:
                logger.error(f"Extraction failed for {filename}: {e}")
                continue

        # Summary stats — validation issues, unique accounts, carrier names
        # computed from extracted rows. Surfaced on the Previous Uploads cards.
        rows_with_issues = sum(1 for r in all_rows if r.get("validation_issues"))
        rows_error_level = sum(1 for r in all_rows
                               if any(i.get("severity") == "error" for i in (r.get("validation_issues") or [])))
        unique_accounts = len({r.get("carrier_account_number") for r in all_rows if r.get("carrier_account_number")})
        # Prefer detected carrier_name from row data — falls back to routing key.
        carrier_names = {
            (r.get("carrier_name") or r.get("carrier") or "").strip()
            for r in all_rows
            if (r.get("carrier_name") or r.get("carrier"))
        }
        computed_carriers = sorted(c for c in carrier_names if c and c.lower() not in ("unknown", ""))
        logger.info(f"Validation: {rows_with_issues} of {len(all_rows)} rows have issues ({rows_error_level} at error severity)")
        logger.info(f"Computed: {unique_accounts} unique accounts, carriers={computed_carriers}")

        _update_upload_field(
            upload_id,
            status="done",
            files_processed=files_processed,
            rows_with_issues=rows_with_issues,
            rows_error_level=rows_error_level,
            unique_accounts=unique_accounts,
            computed_carriers=computed_carriers,
        )
        _update_upload_results(upload_id, all_rows)
        logger.info(f"Upload {upload_id} complete: {len(all_rows)} total rows from {files_processed} files")

        # Persist to PostgreSQL for permanent storage
        await _persist_to_db(upload_id, all_rows, files_processed)

    except Exception as e:
        logger.error(f"Extraction task crashed for {upload_id}: {e}")
        _update_upload_field(upload_id, status="error")


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
