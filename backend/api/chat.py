"""Conversational AI endpoint — the platform's "brain" surface.

Two modes (selected by the caller):
  - "project": grounded in one upload's rows + compliance flags + per-project
    pattern findings. Side-drawer use case on the Results page.
  - "platform": grounded in cross-project patterns + per-client portfolio
    summaries. Dedicated /chat page use case.

The chat layer never extracts or transforms data itself — it composes
context from existing artifacts (extracted rows, validation, compliance,
pattern detectors, optional graphify subgraph), drops it into a system
prompt, and streams Claude's response back. This means:
  - Every claim Claude makes is grounded in numbers the detectors computed.
  - Claude can't hallucinate row counts or anomaly thresholds — they're in
    the system prompt verbatim.
  - Adding new context (e.g. a new pattern detector) is one import line.

Streaming is SSE (text/event-stream). The frontend reads the body as a
ReadableStream and appends `data:` chunks to the live message bubble.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.services import upload_store as us
from backend.services import patterns as pat
from backend.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Request/response models ──────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    mode: str = "project"  # "project" | "platform"
    project_id: Optional[str] = None  # required when mode="project"
    client_filter: Optional[str] = None  # case-insensitive name match for platform mode


# ── Context assembly ─────────────────────────────────────────────────────────
#
# The system prompt has three layers, smallest to largest:
#   1. Persona/tone — who Claude is acting as.
#   2. Pattern findings — JSON the detectors produced. Claude cites these.
#   3. Row sample — first N rows from the project (or summary stats for
#      platform mode). Capped so we don't blow the context budget.
#
# Total budget target: ~25K tokens for context, leaving ~50K for chat history
# and ~8K for the response. Sonnet's 200K window means we can grow this.


_PERSONA = """\
You are the analyst-grade assistant for Digital Direction, a telecom-document
extraction platform. You answer questions grounded in:
  - Extracted billing/contract/CSR rows from uploaded carrier documents
  - Validated facts and compliance flags surfaced by the pipeline
  - Pattern findings computed by deterministic detectors (recurring vendors,
    pricing anomalies, contract clusters, multi-carrier accounts, M2M risk)

Rules:
1. Cite specific evidence — row IDs, dollar amounts, account numbers — that
   are in the context provided. Never invent numbers.
2. When the user asks about a pattern (e.g. "show me pricing anomalies"),
   reference the findings JSON below directly. Each finding has a `title`,
   `detail`, `metric` and `evidence_row_ids` field — quote them.
3. Be concise. Telecom analysts read fast and value precise dollar figures
   and counts over prose.
4. When numbers don't appear in the context, say so plainly. Don't guess.
5. If asked about code/architecture (e.g. "how does the merger work"), the
   GRAPHIFY KNOWLEDGE GRAPH section may have BFS results — cite the source
   files and function names.
"""


def _format_findings(findings: list[dict]) -> str:
    """Render findings as compact JSON for the system prompt — Claude reads
    JSON well and we get exact field names back in citations."""
    if not findings:
        return "No pattern findings."
    lite = []
    for f in findings:
        lite.append({
            "kind": f.get("kind"),
            "severity": f.get("severity"),
            "title": f.get("title"),
            "detail": f.get("detail"),
            "metric": f.get("metric") or {},
            "evidence_count": len(f.get("evidence_row_ids") or []),
        })
    return json.dumps(lite, indent=2)


_ROW_FIELDS_FOR_CONTEXT = (
    "id", "source_file", "carrier", "carrier_name",
    "carrier_account_number", "phone_number", "service_type",
    "description", "usoc", "monthly_recurring_cost", "rate", "quantity",
    "contract_term_months", "contract_begin_date", "contract_expiration_date",
    "currently_month_to_month", "billing_per_contract", "auto_renew",
    "city", "state", "zip", "billing_name",
    "status", "validation_issues", "compliance_flags",
)


def _slim_row(row: dict) -> dict:
    return {k: row.get(k) for k in _ROW_FIELDS_FOR_CONTEXT if row.get(k) not in (None, "", [], {})}


async def _build_project_context(project_id: str, max_rows: int = 80) -> tuple[str, dict]:
    """Build the system-prompt body for project-scoped chat. Returns
    `(system_prompt, meta)` where meta holds counters the API surfaces back
    to the client for telemetry."""
    upload = await us.get_upload(project_id)
    if not upload:
        return "", {"error": "Project not found"}
    rows = upload.get("results") or []
    findings = pat.detect_all(rows, uploads=None)

    # Sample rows: keep all if small enough, else stratify by source_file so
    # both the contract and the invoice rows are represented.
    if len(rows) <= max_rows:
        sample = rows
    else:
        from collections import defaultdict
        by_src: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_src[r.get("source_file") or "?"].append(r)
        per_src = max(1, max_rows // max(len(by_src), 1))
        sample = []
        for src_rows in by_src.values():
            sample.extend(src_rows[:per_src])
        sample = sample[:max_rows]

    slim_rows = [_slim_row(r) for r in sample]
    body_parts = [
        _PERSONA,
        "",
        f"PROJECT: {upload.get('project_name', '(unnamed)')!r}",
        f"CLIENT:  {upload.get('client_name') or '(none)'}",
        f"CARRIERS: {', '.join(upload.get('computed_carriers') or [])}",
        f"TOTAL ROWS: {len(rows)}  ·  SAMPLED IN CONTEXT: {len(slim_rows)}",
        "",
        "PATTERN FINDINGS (deterministic detector output — cite these directly):",
        _format_findings(findings),
        "",
        f"ROW SAMPLE (slim, first {len(slim_rows)} rows):",
        json.dumps(slim_rows, indent=2, default=str),
    ]
    return "\n".join(body_parts), {
        "row_count": len(rows),
        "sampled": len(slim_rows),
        "findings": pat.summarize(findings),
    }


async def _build_platform_context(client_filter: str | None) -> tuple[str, dict]:
    """Build the system-prompt body for platform-scoped chat. Aggregates
    across every (non-deleted, done) upload and runs the cross-project
    detectors on the union."""
    uploads = await us.list_uploads(include_deleted=False)
    uploads = [u for u in uploads if (u.get("status") == "done")]
    if client_filter:
        cf = client_filter.lower().strip()
        uploads = [u for u in uploads if (u.get("client_name") or "").lower() == cf]

    # Lazy-load rows only when the user actually asks platform-level questions
    # — keeps cost low on first /chat open. We pull a slim summary per upload
    # plus the union of rows for cross-project detectors.
    union_rows: list[dict] = []
    summaries: list[dict] = []
    for u in uploads[:30]:  # top 30 most recent — keeps prompt size sane
        rows = (await us.get_raw_results(u.get("upload_id"))) or []
        union_rows.extend(rows)
        summaries.append({
            "upload_id": u.get("upload_id"),
            "project_name": u.get("project_name"),
            "client_name": u.get("client_name"),
            "computed_carriers": u.get("computed_carriers") or [],
            "total_rows": len(rows),
            "created_at": u.get("created_at"),
        })

    findings = pat.detect_all(union_rows, uploads=uploads)
    body_parts = [
        _PERSONA,
        "",
        "PLATFORM-WIDE CONTEXT" + (f" (filtered to client={client_filter})" if client_filter else ""),
        f"PROJECTS: {len(summaries)}",
        f"TOTAL ROWS ACROSS PROJECTS: {len(union_rows)}",
        "",
        "PROJECT SUMMARIES:",
        json.dumps(summaries, indent=2, default=str),
        "",
        "PATTERN FINDINGS (cross-project + per-client):",
        _format_findings(findings),
    ]
    return "\n".join(body_parts), {
        "project_count": len(summaries),
        "row_count": len(union_rows),
        "findings": pat.summarize(findings),
    }


# ── Streaming endpoint ───────────────────────────────────────────────────────


@router.post("/stream")
async def chat_stream(req: ChatRequest = Body(...)):
    """SSE-streamed chat reply. Body uses the same shape Claude expects so
    the frontend can append messages directly. Mode determines context."""
    if req.mode not in ("project", "platform"):
        return JSONResponse(status_code=400, content={"error": "mode must be 'project' or 'platform'"})
    if req.mode == "project" and not req.project_id:
        return JSONResponse(status_code=400, content={"error": "project_id required for mode='project'"})
    if not req.messages or not any(m.role == "user" for m in req.messages):
        return JSONResponse(status_code=400, content={"error": "at least one user message required"})

    if req.mode == "project":
        system, meta = await _build_project_context(req.project_id)  # type: ignore[arg-type]
    else:
        system, meta = await _build_platform_context(req.client_filter)
    if "error" in meta:
        return JSONResponse(status_code=404, content=meta)

    if not settings.anthropic_api_key:
        return JSONResponse(status_code=503, content={"error": "ANTHROPIC_API_KEY not configured"})

    # Convert chat history to Anthropic SDK shape. Claude's SDK rejects
    # consecutive same-role messages — squash any that appear (defensive).
    history: list[dict] = []
    for m in req.messages:
        if history and history[-1]["role"] == m.role:
            history[-1]["content"] += "\n\n" + m.content
        else:
            history.append({"role": m.role, "content": m.content})

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def event_stream():
        try:
            # Send a one-time "meta" event so the UI can render context size,
            # row counts, etc. before the first token arrives.
            yield f"event: meta\ndata: {json.dumps(meta)}\n\n"
            async with client.messages.stream(
                model=settings.claude_merge_model,
                max_tokens=settings.claude_max_tokens,
                system=system,
                messages=history,
            ) as stream:
                async for text_chunk in stream.text_stream:
                    if text_chunk:
                        # SSE: each chunk is a single `data:` line. JSON-encode
                        # to keep newlines and quotes intact.
                        yield f"data: {json.dumps({'type': 'token', 'text': text_chunk})}\n\n"
                final = await stream.get_final_message()
                yield (
                    "event: done\n"
                    f"data: {json.dumps({'input_tokens': final.usage.input_tokens, 'output_tokens': final.usage.output_tokens})}\n\n"
                )
        except Exception as e:
            logger.exception(f"chat stream failed: {e}")
            yield f"event: error\ndata: {json.dumps({'error': str(e)[:200]})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Patterns endpoint (used by both chat + the dashboard) ────────────────────


@router.get("/patterns/{project_id}")
async def patterns_for_project(project_id: str):
    """Run the 5 detectors against a single project. Cheap: pure compute,
    no LLM. Used by the chat warm-up + the per-project insights card."""
    upload = await us.get_upload(project_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    rows = upload.get("results") or []
    findings = pat.detect_all(rows, uploads=None)
    return {
        "project_id": project_id,
        "project_name": upload.get("project_name"),
        "row_count": len(rows),
        "findings": findings,
        "summary": pat.summarize(findings),
    }


@router.get("/patterns")
async def patterns_platform(client: Optional[str] = None):
    """Run cross-project detectors. Optionally scope to one client."""
    uploads = await us.list_uploads(include_deleted=False)
    uploads = [u for u in uploads if u.get("status") == "done"]
    if client:
        cf = client.lower().strip()
        uploads = [u for u in uploads if (u.get("client_name") or "").lower() == cf]

    union_rows: list[dict] = []
    for u in uploads[:50]:
        rows = (await us.get_raw_results(u.get("upload_id"))) or []
        union_rows.extend(rows)
    findings = pat.detect_all(union_rows, uploads=uploads)
    return {
        "project_count": len(uploads),
        "row_count": len(union_rows),
        "findings": findings,
        "summary": pat.summarize(findings),
        "client_filter": client,
    }
