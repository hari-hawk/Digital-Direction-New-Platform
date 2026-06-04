"""Generic (non-telecom) extraction path for Level B domain packs.

Sits alongside `backend/pipeline/extractor.py` — never imported from the
telecom code path so telecom risk stays at zero. Active only when an
upload's `domain_pack` is non-telecom (currently means "insurance", since
"generic_billing" still has no prompts).

Why a sibling module instead of weaving conditionals through extractor.py:
  - The telecom extractor is 1300+ lines of tightly-coupled logic
    (Pydantic ExtractedRow shape, spatial-address corrections, per-carrier
    backfills, USOC handling, etc.). Generalizing those would either
    regress telecom accuracy or hide telecom-only behavior behind dead
    conditionals. We split the path; later refactor merges the
    truly-shared bits (Gemini call, JSON parse) if a third pack arrives.
  - The non-telecom path returns plain `list[dict]` rows — no Pydantic
    enforcement, no field-name canonicalization. The Pack's `field_set`
    is advisory; the LLM may emit additional fields and we store them
    verbatim. Telecom's typed `extracted_rows` table is not used — rows
    land only in `uploads.results` JSONB. A later commit adds a typed
    `extracted_rows_generic` table once we have a real customer.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from backend.models.schemas import ParsedDocument, ParsedSection
from backend.services.domain_packs import get_pack

logger = logging.getLogger(__name__)


# ── Prompt loading ───────────────────────────────────────────────────────────


def load_pack_prompt(pack_key: str, doc_type: str) -> str | None:
    """Load the LLM prompt for a (pack, doc_type) pair.

    Resolution order:
      1. configs/{pack.prompt_namespace}/{doc_type}_extraction.md
      2. configs/{pack.prompt_namespace}/default_extraction.md  (fallback)

    Returns None when nothing's on disk — caller should error gracefully.
    """
    pack = get_pack(pack_key)
    if pack is None:
        logger.warning("load_pack_prompt: unknown pack %r", pack_key)
        return None
    repo_root = Path(__file__).resolve().parent.parent.parent
    # Try doc-type-specific first, then a 'default' file as a fallback.
    for candidate in (
        repo_root / "configs" / pack.prompt_namespace / f"{doc_type}_extraction.md",
        repo_root / "configs" / pack.prompt_namespace / "default_extraction.md",
    ):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    logger.warning(
        "load_pack_prompt: no prompt found for pack=%s doc_type=%s "
        "(looked in configs/%s/)",
        pack_key, doc_type, pack.prompt_namespace,
    )
    return None


# ── JSON parsing ─────────────────────────────────────────────────────────────


_JSON_ARRAY_RE = re.compile(r"\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\]", re.DOTALL)


def _parse_json_array(text: str) -> list[dict]:
    """Best-effort JSON-array extraction from an LLM response.

    The prompt asks for a bare array, but models occasionally wrap in
    markdown fences or prepend explanation. We extract the first array we
    find and silently drop anything else.
    """
    if not text:
        return []
    # Strip common markdown fence variants
    cleaned = re.sub(r"```(?:json)?\s*", "", text).rstrip("`").strip()
    # Try direct parse first
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        if isinstance(parsed, dict):
            return [parsed]  # single-object response — wrap
    except json.JSONDecodeError:
        pass
    # Fallback: find the first array-shaped substring
    match = _JSON_ARRAY_RE.search(cleaned)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
        return [r for r in parsed if isinstance(r, dict)]
    except json.JSONDecodeError as e:
        logger.warning(f"_parse_json_array: salvage failed: {e}")
        return []


# ── Per-section extraction ───────────────────────────────────────────────────


async def _extract_section_generic(
    section: ParsedSection,
    pack_key: str,
    doc_type: str,
    prompt_template: str,
    correction_hints: list | None = None,
) -> tuple[list[dict], list[str]]:
    """Call Gemini on one section with the pack's prompt. Returns (rows, errors)."""
    from backend.services.llm import get_gemini

    parts = [prompt_template]

    # Inject correction hints (Level A self-learning) — same format as
    # telecom path so analyst edits cross-flow.
    if correction_hints:
        lines = ["KNOWN CORRECTIONS (from past reviews — avoid these mistakes):"]
        for hint in correction_hints[:10]:
            lines.append(
                f"  - Field '{hint.field_name}': do NOT extract as "
                f"'{hint.wrong_value}' → correct value is "
                f"'{hint.correct_value}' (corrected {hint.occurrence_count} times)"
            )
        parts.append("\n" + "\n".join(lines))

    if section.global_context:
        parts.append(f"\nDOCUMENT CONTEXT:\n{section.global_context}")
    parts.append(f"\nDOCUMENT TEXT:\n---\n{section.text}\n---")

    prompt = "\n".join(parts)
    errors: list[str] = []
    try:
        gemini = get_gemini()
        response = await gemini.extract(prompt)
        rows = _parse_json_array(response.content)
        if not rows:
            errors.append("LLM returned no parseable JSON array")
        return rows, errors
    except Exception as e:
        errors.append(f"LLM call failed: {str(e)[:200]}")
        return [], errors


# ── Document-level entry point ───────────────────────────────────────────────


async def extract_document_generic(
    parsed: ParsedDocument,
    pack_key: str,
    doc_type: str,
    errors_out: list[str] | None = None,
    correction_hints: list | None = None,
) -> list[dict]:
    """Extract a document under a non-telecom pack. Returns plain list[dict].

    Each row is whatever JSON the LLM emitted, minus any field that's
    explicitly `null`. The caller persists into uploads.results JSONB
    verbatim — no Pydantic enforcement, no telecom-shape coercion.

    `errors_out` is appended-to (mutable) so the caller can surface
    per-document failures on the upload card.
    """
    if errors_out is None:
        errors_out = []

    if not parsed or not parsed.sections:
        errors_out.append("Generic extract: parser produced no sections")
        return []

    prompt = load_pack_prompt(pack_key, doc_type)
    if not prompt:
        errors_out.append(
            f"Generic extract: no prompt found for pack={pack_key} doc_type={doc_type}. "
            f"Add configs/processing_{pack_key.replace('_billing', '')}/{doc_type}_extraction.md"
        )
        return []

    all_rows: list[dict] = []
    for section in parsed.sections:
        rows, section_errors = await _extract_section_generic(
            section, pack_key, doc_type, prompt, correction_hints,
        )
        errors_out.extend(section_errors)
        all_rows.extend(rows)

    # Strip explicit-null fields from every row so the persisted JSONB
    # stays compact and the UI doesn't render empty cells.
    cleaned = []
    for row in all_rows:
        cleaned.append({k: v for k, v in row.items() if v not in (None, "", [], {})})
    return cleaned
