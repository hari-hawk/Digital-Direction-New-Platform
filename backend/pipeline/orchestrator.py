"""Pipeline orchestrator — ties classify → parse → extract → merge → validate.

Processes an entire upload batch: classifies all files, groups by account,
extracts each document, merges per-account, validates, outputs final rows.
"""

import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, field

from backend.pipeline.classifier import classify_document, validate_carrier_post_extraction
from backend.pipeline.parser import parse_document
from backend.pipeline.extractor import extract_document, score_confidence
from backend.pipeline.structured_extractor import extract_structured, can_extract_structured
from backend.pipeline.merger import rule_based_merge, cross_granularity_merge, llm_resolve_conflicts
from backend.pipeline.validator import validate_rows
from backend.pipeline.compliance import check_compliance, flags_to_jsonb, ComplianceResult
from backend.models.schemas import ExtractedRow, ClassificationResult
from backend.services.llm import LLMResponse
from backend.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class DocumentResult:
    file_path: str
    classification: ClassificationResult
    rows: list[ExtractedRow] = field(default_factory=list)
    responses: list[LLMResponse] = field(default_factory=list)
    error: str | None = None


@dataclass
class AccountGroup:
    carrier: str
    account_number: str
    documents: dict[str, list[DocumentResult]] = field(default_factory=dict)  # doc_type → [results]


@dataclass
class PipelineResult:
    upload_dir: str
    documents_processed: int = 0
    documents_failed: int = 0
    documents_skipped: int = 0
    total_rows: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    merged_rows: list[ExtractedRow] = field(default_factory=list)
    validation_results: list[dict] = field(default_factory=list)
    compliance_result: ComplianceResult | None = None
    document_results: list[DocumentResult] = field(default_factory=list)
    account_groups: dict[str, AccountGroup] = field(default_factory=dict)


async def process_upload(
    input_dir: str,
    client_name: str = "",
) -> PipelineResult:
    """Full pipeline: classify → parse → extract → merge → validate for all files in a directory."""

    result = PipelineResult(upload_dir=input_dir)
    p = Path(input_dir)

    # ── Stage 0: Classify all files ──
    logger.info(f"Stage 0: Classifying files in {input_dir}")
    file_results: list[DocumentResult] = []

    # Patterns for output/golden files that should never be processed as input
    _OUTPUT_FILE_PATTERNS = ["inventory file", "inventory_file", "_wip_"]

    for f in sorted(p.rglob("*")):
        if f.is_dir() or f.name == ".DS_Store":
            continue

        # Skip output/golden files — these are eval targets, not extraction sources
        if any(p in f.name.lower() for p in _OUTPUT_FILE_PATTERNS):
            logger.info(f"Skipping output file: {f.name}")
            continue

        try:
            classification = await classify_document(str(f))
            doc_result = DocumentResult(file_path=str(f), classification=classification)

            if not classification.carrier:
                logger.warning(f"Unclassified: {f.name}")
                result.documents_skipped += 1
                doc_result.error = "unclassified"

            file_results.append(doc_result)
        except Exception as e:
            logger.error(f"Classification failed for {f.name}: {e}")
            result.documents_failed += 1

    classified = [r for r in file_results if r.classification.carrier and not r.error]
    logger.info(f"Classified {len(classified)}/{len(file_results)} files")

    # ── Load correction hints for self-healing feedback ──
    # Query past corrections (with 2+ agreement guardrail) to inject into prompts.
    # Loaded once per carrier, shared across all documents for that carrier.
    correction_hints_by_carrier: dict[str, list] = {}
    try:
        from backend.services.feedback import get_relevant_corrections
        carriers_in_batch = set(r.classification.carrier for r in classified if r.classification.carrier)
        for carrier in carriers_in_batch:
            hints = get_relevant_corrections(carrier)
            if hints:
                correction_hints_by_carrier[carrier] = hints
                logger.info(f"Loaded {len(hints)} correction hints for {carrier}")
    except Exception as e:
        logger.debug(f"Correction hint loading skipped: {e}")

    # ── Stage 1+2: Parse and extract ALL documents in parallel ──
    logger.info("Stage 1+2: Parsing and extracting documents (parallel)")

    # Per-file extraction cache — saves each file's results as it completes.
    # If the pipeline crashes, we don't lose already-extracted data.
    # Also lets us re-run merge/eval without re-extracting.
    import json as _json
    cache_dir = Path(settings.data_dir) / "cache" / "extractions"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_extraction(filename: str, carrier: str, doc_type: str, rows: list):
        """Save per-file extraction to cache for later reuse."""
        try:
            safe_name = filename.replace("/", "_").replace(" ", "_")
            cache_path = cache_dir / f"{carrier}_{doc_type}_{safe_name}.json"
            row_dicts = [r.model_dump(mode="json") for r in rows]
            cache_path.write_text(_json.dumps(row_dicts, indent=2, default=str))
        except Exception as e:
            logger.debug(f"Cache write failed for {filename}: {e}")

    async def _process_one_file(doc_result: DocumentResult):
        """Process a single file: parse → extract → cache. Runs in parallel with other files.
        The GeminiClient semaphore (200 concurrent) gates total LLM calls across all files."""
        f = Path(doc_result.file_path)
        c = doc_result.classification

        logger.info(f"Processing: {f.name} → {c.carrier}/{c.document_type}")

        try:
            # Route: structured files (XLSX/CSV/XLS) use direct column mapping (no LLM)
            if can_extract_structured(str(f)):
                rows, warnings = extract_structured(
                    str(f), c.carrier, c.document_type or "report"
                )
                for w in warnings:
                    logger.warning(f"  {w}")

                # Post-extraction carrier validation for structured files:
                # if data contains carrier_name that contradicts classification, reclassify
                correct_carrier = validate_carrier_post_extraction(
                    c.carrier, rows, str(f)
                )
                if correct_carrier:
                    logger.warning(f"  Reclassifying {f.name}: {c.carrier} → {correct_carrier}")
                    c.carrier = correct_carrier
                    # Re-extract with correct carrier (column mapping may differ)
                    rows, warnings = extract_structured(
                        str(f), c.carrier, c.document_type or "report"
                    )
                    for w in warnings:
                        logger.warning(f"  {w}")

                doc_result.rows = rows
                doc_result.responses = []
                result.documents_processed += 1
                result.total_rows += len(rows)
                logger.info(f"  → {len(rows)} rows (structured, $0 LLM cost)")

            else:
                # Parse + LLM extraction (PDFs, emails, contracts, etc.)
                parsed = parse_document(
                    str(f), c.carrier, c.document_type or "unknown", c.format_variant
                )

                if not parsed.sections:
                    logger.warning(f"No sections parsed from {f.name}")
                    doc_result.error = "no_sections"
                    result.documents_skipped += 1
                    return

                # Extract — sections within this file run in parallel,
                # AND other files' sections run in parallel too (shared semaphore)
                carrier_hints = correction_hints_by_carrier.get(c.carrier)
                rows, responses = await extract_document(
                    parsed, correction_hints=carrier_hints
                )

                doc_result.rows = rows
                doc_result.responses = responses
                result.documents_processed += 1
                result.total_rows += len(rows)

                # Track cost
                for resp in responses:
                    result.total_input_tokens += resp.input_tokens
                    result.total_output_tokens += resp.output_tokens
                    result.total_cost_usd += resp.estimated_cost_usd

                logger.info(f"  → {len(rows)} rows, {len(responses)} API calls")

            # Cache per-file extraction (saves as we go — crash-safe)
            if doc_result.rows:
                _cache_extraction(f.name, c.carrier, c.document_type or "unknown", doc_result.rows)

        except Exception as e:
            logger.error(f"Extract failed for {f.name}: {e}")
            doc_result.error = str(e)
            result.documents_failed += 1

    # Fire all files in parallel — GeminiClient semaphore (200) gates total LLM calls.
    # This means sections from different files share the same concurrent pool.
    # A 150-section Windstream invoice runs alongside AT&T CSRs simultaneously.
    gather_results = await asyncio.gather(
        *[_process_one_file(doc) for doc in classified],
        return_exceptions=True,
    )

    # Check for any unhandled exceptions that escaped _process_one_file
    for i, gr in enumerate(gather_results):
        if isinstance(gr, Exception):
            logger.error(f"Unexpected error processing {classified[i].file_path}: {gr}")
            result.documents_failed += 1

    result.document_results = file_results

    # ── Group by carrier ──
    # All documents for the same carrier merge together. The merger handles
    # sub-grouping internally via account normalization and tiered merge keys.
    logger.info("Grouping documents by carrier")
    groups: dict[str, AccountGroup] = {}

    for doc_result in classified:
        if doc_result.error or not doc_result.rows:
            continue

        c = doc_result.classification
        key = c.carrier  # carrier-only grouping for cross-doc merge

        if key not in groups:
            groups[key] = AccountGroup(carrier=c.carrier, account_number="all")

        doc_type = c.document_type or "unknown"
        if doc_type not in groups[key].documents:
            groups[key].documents[doc_type] = []
        groups[key].documents[doc_type].append(doc_result)

    result.account_groups = groups
    logger.info(f"Grouped into {len(groups)} carrier groups")

    # ── Stage 3: Cross-granularity merge per carrier ──
    logger.info("Stage 3: Cross-granularity merge")
    all_merged = []

    for key, group in groups.items():
        # Flatten all document results across doc types
        all_doc_results = []
        for doc_type, doc_list in group.documents.items():
            for doc_result in doc_list:
                all_doc_results.append((doc_type, doc_result))

        total_docs = len(all_doc_results)

        if total_docs <= 1:
            # Single document — no merge needed, pass through
            for _, doc_result in all_doc_results:
                all_merged.extend(doc_result.rows)
        else:
            # Multiple docs — cross-granularity merge
            extractions = {}
            doc_types = {}
            for doc_type, doc_result in all_doc_results:
                doc_id = Path(doc_result.file_path).name
                if doc_id in extractions:
                    base_id = doc_id
                    counter = 2
                    while doc_id in extractions:
                        doc_id = f"{base_id}_{counter}"
                        counter += 1
                extractions[doc_id] = doc_result.rows
                doc_types[doc_id] = doc_type

            merged, conflicts = cross_granularity_merge(extractions, doc_types, carrier=group.carrier)

            # Resolve true cross-doc conflicts via Claude
            if conflicts:
                merged = await llm_resolve_conflicts(merged, conflicts, carrier=group.carrier)

            all_merged.extend(merged)
            input_rows = sum(len(dr.rows) for _, dr in all_doc_results)
            logger.info(f"  {key}: {input_rows} rows from {total_docs} docs → {len(merged)} merged ({len(conflicts)} conflicts)")

    result.merged_rows = all_merged

    # ── Stage 4: Validate ──
    logger.info("Stage 4: Validating")
    result.validation_results = validate_rows(all_merged)

    valid_count = sum(1 for v in result.validation_results if isinstance(v, dict) and v.get("valid", False))
    logger.info(f"Validation: {valid_count}/{len(result.validation_results)} rows valid")

    # ── Stage 5: Compliance ──
    logger.info("Stage 5: Contract compliance checking")
    result.compliance_result = check_compliance(all_merged)

    # Attach compliance flags to each row for downstream persistence
    for row_idx, flags in result.compliance_result.flags_by_row.items():
        if row_idx < len(all_merged):
            all_merged[row_idx].compliance_flags = flags_to_jsonb(flags)

    flagged = len(result.compliance_result.flags_by_row)
    total_flags = sum(len(f) for f in result.compliance_result.flags_by_row.values())
    logger.info(f"Compliance: {total_flags} flags on {flagged}/{len(all_merged)} rows")

    # ── Summary ──
    logger.info(f"""
Pipeline complete:
  Files processed: {result.documents_processed}
  Files failed: {result.documents_failed}
  Files skipped: {result.documents_skipped}
  Raw rows: {result.total_rows}
  Merged rows: {len(result.merged_rows)}
  Cost: ${result.total_cost_usd:.4f}
  Tokens: {result.total_input_tokens} in + {result.total_output_tokens} out
""")

    return result
