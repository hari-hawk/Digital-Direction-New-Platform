"""Self-healing feedback service — root-cause diagnosis, correction querying, pattern analysis.

Flow:
  1. User corrects a field in review UI → correction saved with metadata
  2. diagnose_correction() traces the error back through pipeline stages
  3. get_relevant_corrections() provides corrections for next extraction
  4. analyze_correction_patterns() suggests config updates from patterns
"""

import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from backend.settings import settings

logger = logging.getLogger(__name__)


# ============================================
# Root Cause Types
# ============================================

class RootCause:
    EXTRACTION = "EXTRACTION"      # LLM misread the source text
    MERGE = "MERGE"                # Merge priority/matching error
    ENRICHMENT = "ENRICHMENT"      # Cross-doc enrichment failed
    DATA_GAP = "DATA_GAP"          # Field not in any source document
    ANALYST_JUDGMENT = "ANALYST"   # Golden value is analyst opinion, not extractable
    UNKNOWN = "UNKNOWN"            # Could not determine root cause


@dataclass
class Diagnosis:
    root_cause: str
    explanation: str
    raw_extraction_value: str | None = None
    merged_value: str | None = None
    source_doc: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "explanation": self.explanation,
            "raw_extraction_value": self.raw_extraction_value,
            "merged_value": self.merged_value,
            "source_doc": self.source_doc,
            **self.details,
        }


@dataclass
class CorrectionHint:
    """A correction formatted for injection into the extraction prompt."""
    field_name: str
    wrong_value: str
    correct_value: str
    carrier: str
    occurrence_count: int


@dataclass
class KnowledgeSuggestion:
    """A suggested config update derived from correction patterns."""
    suggestion_type: str  # "usoc_mapping", "service_type", "merge_rule", "prompt_fix"
    carrier: str
    field_name: str
    suggested_value: str
    correction_count: int
    example_extracted_values: list[str]
    explanation: str


# ============================================
# Root-Cause Diagnosis
# ============================================


def diagnose_correction(
    carrier: str,
    field_name: str,
    extracted_value: str | None,
    corrected_value: str,
    account_number: str | None = None,
    phone_number: str | None = None,
) -> Diagnosis:
    """Trace a correction back through pipeline stages to find the root cause.

    Compares the corrected value against:
    1. Raw extraction cache (per-file, pre-merge) → was extraction correct?
    2. Merged output → did merge change the value?
    3. All source documents → does the corrected value exist anywhere?

    Returns a Diagnosis with root_cause classification and explanation.
    """
    cache_dir = Path(settings.data_dir) / "cache" / "extractions"
    if not cache_dir.exists():
        return Diagnosis(
            root_cause=RootCause.UNKNOWN,
            explanation="No extraction cache available for diagnosis",
        )

    norm_phone = re.sub(r'[^0-9]', '', phone_number or '')
    norm_acct = re.sub(r'[^0-9]', '', account_number or '')
    corrected_upper = (corrected_value or '').upper().strip()
    extracted_upper = (extracted_value or '').upper().strip()

    # Search through all cached extractions for this carrier
    raw_values_found = []  # (file, value) pairs where the field had any value
    corrected_found_in = []  # files where the corrected value exists in raw extraction

    for cache_file in sorted(cache_dir.iterdir()):
        if not cache_file.name.lower().startswith(carrier) or not cache_file.suffix == '.json':
            continue

        try:
            rows = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        for row in rows:
            # Match by phone or account
            row_phone = re.sub(r'[^0-9]', '', row.get('phone_number', '') or '')
            row_acct = re.sub(r'[^0-9]', '', row.get('carrier_account_number', '') or '')

            phone_match = norm_phone and row_phone and (
                norm_phone == row_phone or
                norm_phone[-7:] == row_phone[-7:]  # 7-digit match
            )
            acct_match = norm_acct and row_acct and (
                norm_acct in row_acct or row_acct in norm_acct
            )

            if not phone_match and not acct_match:
                continue

            raw_val = (str(row.get(field_name, '') or '')).upper().strip()
            if raw_val:
                raw_values_found.append((cache_file.name, raw_val))

            if raw_val == corrected_upper:
                corrected_found_in.append(cache_file.name)

    # ── Decision tree ──

    # Case 1: Raw extraction had the correct value → merge/enrichment changed it
    if corrected_found_in:
        # The correct value existed in raw extraction but was lost during merge
        return Diagnosis(
            root_cause=RootCause.MERGE,
            explanation=(
                f"Raw extraction in {corrected_found_in[0]} had the correct value "
                f"'{corrected_value}', but merge/enrichment changed it to "
                f"'{extracted_value}'. Check merge priority or propagation rules."
            ),
            raw_extraction_value=corrected_value,
            merged_value=extracted_value,
            source_doc=corrected_found_in[0],
        )

    # Case 2: Raw extraction had a different value → extraction error
    if raw_values_found:
        raw_val = raw_values_found[0][1]
        if raw_val == extracted_upper:
            # Extraction produced the wrong value, merge kept it
            return Diagnosis(
                root_cause=RootCause.EXTRACTION,
                explanation=(
                    f"LLM extraction produced '{extracted_value}' from "
                    f"{raw_values_found[0][0]}, but correct value is "
                    f"'{corrected_value}'. The value was not found in any "
                    f"raw extraction — may need prompt improvement."
                ),
                raw_extraction_value=extracted_value,
                source_doc=raw_values_found[0][0],
            )
        else:
            # Extraction had something different, and merge changed it further
            return Diagnosis(
                root_cause=RootCause.MERGE,
                explanation=(
                    f"Raw extraction had '{raw_val}' in {raw_values_found[0][0]}, "
                    f"merge produced '{extracted_value}', but correct is "
                    f"'{corrected_value}'. Multiple pipeline stages involved."
                ),
                raw_extraction_value=raw_val,
                merged_value=extracted_value,
                source_doc=raw_values_found[0][0],
            )

    # Case 3: No raw extraction found for this row → data gap or enrichment
    if not raw_values_found:
        # Check if the corrected value exists in ANY file for this carrier
        # (might be a cross-doc enrichment that should have happened)
        for cache_file in sorted(cache_dir.iterdir()):
            if not cache_file.name.lower().startswith(carrier) or not cache_file.suffix == '.json':
                continue
            try:
                rows = json.loads(cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            for row in rows:
                raw_val = (str(row.get(field_name, '') or '')).upper().strip()
                if raw_val == corrected_upper:
                    return Diagnosis(
                        root_cause=RootCause.ENRICHMENT,
                        explanation=(
                            f"Corrected value '{corrected_value}' exists in "
                            f"{cache_file.name} but was not enriched to this row. "
                            f"Cross-doc enrichment may need a broader lookup."
                        ),
                        source_doc=cache_file.name,
                    )

        return Diagnosis(
            root_cause=RootCause.DATA_GAP,
            explanation=(
                f"Corrected value '{corrected_value}' for field '{field_name}' "
                f"not found in any raw extraction for carrier '{carrier}'. "
                f"The value may come from an external source not in the pipeline."
            ),
        )

    return Diagnosis(root_cause=RootCause.UNKNOWN, explanation="Could not determine root cause")


# ============================================
# Correction Querying (for extraction prompts)
# ============================================


def get_relevant_corrections(
    carrier: str,
    format_variant: str | None = None,
    corrections_dir: str | None = None,
) -> list[CorrectionHint]:
    """Query past corrections for injection into extraction prompts.

    Two-tier lookup:
    1. Primary: exact-match on carrier + field_name, grouped by corrected_value,
       filtered by 2+ agreeing corrections (guardrail)
    2. Fallback: pgvector cosine similarity (requires DB session, done separately)

    Only returns corrections with root_cause=EXTRACTION (the only ones where
    prompt injection helps — merge/enrichment errors need different fixes).

    For POC: reads from a corrections JSON file (no DB dependency for CLI usage).
    For production: queries the corrections table directly.
    """
    # For now, this works with the corrections stored in a JSON export.
    # Production version would query the DB with:
    #   SELECT field_name, corrected_value, COUNT(*) as cnt,
    #          array_agg(DISTINCT extracted_value) as examples
    #   FROM corrections
    #   WHERE carrier = :carrier
    #     AND (format_variant = :format OR format_variant IS NULL)
    #     AND root_cause = 'EXTRACTION'
    #   GROUP BY field_name, corrected_value
    #   HAVING COUNT(*) >= 2
    #   ORDER BY cnt DESC
    #   LIMIT 20

    corrections_path = Path(corrections_dir or os.path.join(settings.data_dir, "corrections"))
    corrections_file = corrections_path / f"{carrier}_corrections.json"

    if not corrections_file.exists():
        return []

    try:
        corrections = json.loads(corrections_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    # Group by field_name + corrected_value, filter by extraction root cause
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in corrections:
        if c.get("root_cause") and c["root_cause"] != RootCause.EXTRACTION:
            continue
        key = (c.get("field_name", ""), c.get("corrected_value", ""))
        groups[key].append(c)

    # Apply 2+ agreement guardrail
    hints = []
    for (field_name, corrected_value), group in groups.items():
        if len(group) < 2:
            continue
        wrong_values = list(set(c.get("extracted_value", "") for c in group if c.get("extracted_value")))
        hints.append(CorrectionHint(
            field_name=field_name,
            wrong_value=wrong_values[0] if wrong_values else "",
            correct_value=corrected_value,
            carrier=carrier,
            occurrence_count=len(group),
        ))

    # Sort by frequency (most common corrections first)
    hints.sort(key=lambda h: -h.occurrence_count)
    return hints[:20]  # Cap at 20 to avoid prompt bloat


async def get_relevant_corrections_db(
    db,
    carrier: str,
    format_variant: str | None = None,
    context_text: str | None = None,
) -> list[CorrectionHint]:
    """Query corrections from the database with pgvector fallback.

    1. Exact-match on carrier + field_name with 2+ agreement
    2. If no results and context_text provided, fall back to pgvector similarity
    """
    from sqlalchemy import text as sql_text

    # Tier 1: Exact-match grouped query
    query = sql_text("""
        SELECT field_name, corrected_value, COUNT(*) as cnt,
               array_agg(DISTINCT extracted_value) as examples
        FROM corrections
        WHERE carrier = :carrier
          AND root_cause = 'EXTRACTION'
          AND corrected_value IS NOT NULL
        GROUP BY field_name, corrected_value
        HAVING COUNT(*) >= 2
        ORDER BY cnt DESC
        LIMIT 20
    """)
    result = await db.execute(query, {"carrier": carrier})
    rows = result.fetchall()

    if rows:
        return [
            CorrectionHint(
                field_name=r[0],
                wrong_value=r[3][0] if r[3] else "",
                correct_value=r[1],
                carrier=carrier,
                occurrence_count=r[2],
            )
            for r in rows
        ]

    # Tier 2: pgvector similarity fallback (if context text provided)
    if context_text:
        try:
            from backend.services.llm import get_gemini
            gemini = get_gemini()
            embedding = await gemini.embed(context_text[:500])

            similarity_query = sql_text("""
                SELECT field_name, extracted_value, corrected_value,
                       1 - (embedding <=> :emb::vector) as similarity
                FROM corrections
                WHERE carrier = :carrier
                  AND root_cause = 'EXTRACTION'
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> :emb::vector
                LIMIT 10
            """)
            result = await db.execute(similarity_query, {
                "carrier": carrier,
                "emb": str(embedding),
            })
            sim_rows = result.fetchall()

            # Group similar corrections by field+value and apply 2+ guardrail
            groups: dict[tuple, list] = defaultdict(list)
            for r in sim_rows:
                groups[(r[0], r[2])].append(r)

            hints = []
            for (field_name, corrected_value), group in groups.items():
                if len(group) < 2:
                    continue
                hints.append(CorrectionHint(
                    field_name=field_name,
                    wrong_value=group[0][1] or "",
                    correct_value=corrected_value,
                    carrier=carrier,
                    occurrence_count=len(group),
                ))
            return hints[:20]

        except Exception as e:
            logger.warning(f"pgvector similarity search failed: {e}")

    return []


# ============================================
# Pattern Analysis (for config suggestions)
# ============================================


def analyze_correction_patterns(
    carrier: str,
    corrections_dir: str | None = None,
    min_count: int = 3,
) -> list[KnowledgeSuggestion]:
    """Analyze correction patterns and suggest config updates.

    Groups corrections by root_cause and field, identifies repeated patterns,
    and generates actionable suggestions:
    - EXTRACTION errors → suggest prompt improvements or USOC mappings
    - MERGE errors → suggest merge rule changes
    - ENRICHMENT errors → suggest enrichment config updates
    - DATA_GAP → flag for client discussion
    """
    corrections_path = Path(corrections_dir or os.path.join(settings.data_dir, "corrections"))
    corrections_file = corrections_path / f"{carrier}_corrections.json"

    if not corrections_file.exists():
        return []

    try:
        corrections = json.loads(corrections_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    suggestions = []

    # Group by root_cause → field_name → corrected_value
    by_cause: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for c in corrections:
        cause = c.get("root_cause", RootCause.UNKNOWN)
        field = c.get("field_name", "")
        value = c.get("corrected_value", "")
        if field and value:
            by_cause[cause][field][value] += 1

    # EXTRACTION errors → suggest prompt or domain knowledge fixes
    for field, value_counts in by_cause.get(RootCause.EXTRACTION, {}).items():
        for value, count in value_counts.items():
            if count >= min_count:
                # Collect the wrong values that were extracted
                wrong_values = list(set(
                    c.get("extracted_value", "")
                    for c in corrections
                    if c.get("field_name") == field
                    and c.get("corrected_value") == value
                    and c.get("root_cause") == RootCause.EXTRACTION
                    and c.get("extracted_value")
                ))

                # Determine suggestion type based on field
                if field == "service_type":
                    stype = "service_type"
                    explanation = (
                        f"LLM consistently extracts wrong service_type. "
                        f"Add normalization: {wrong_values} → '{value}'"
                    )
                elif field == "component_or_feature_name" and len(value) <= 10:
                    stype = "usoc_mapping"
                    explanation = (
                        f"Component name '{value}' extracted incorrectly {count} times. "
                        f"Consider adding to USOC lookup table."
                    )
                else:
                    stype = "prompt_fix"
                    explanation = (
                        f"Field '{field}' consistently wrong: extracted as "
                        f"{wrong_values[:3]} but should be '{value}'. "
                        f"Consider adding a prompt hint."
                    )

                suggestions.append(KnowledgeSuggestion(
                    suggestion_type=stype,
                    carrier=carrier,
                    field_name=field,
                    suggested_value=value,
                    correction_count=count,
                    example_extracted_values=wrong_values[:5],
                    explanation=explanation,
                ))

    # MERGE errors → suggest merge rule changes
    for field, value_counts in by_cause.get(RootCause.MERGE, {}).items():
        total = sum(value_counts.values())
        if total >= min_count:
            suggestions.append(KnowledgeSuggestion(
                suggestion_type="merge_rule",
                carrier=carrier,
                field_name=field,
                suggested_value=value_counts.most_common(1)[0][0],
                correction_count=total,
                example_extracted_values=[],
                explanation=(
                    f"Field '{field}' has {total} merge-related corrections. "
                    f"Review field_priority_overrides or propagation rules."
                ),
            ))

    # DATA_GAP → flag for client discussion
    for field, value_counts in by_cause.get(RootCause.DATA_GAP, {}).items():
        total = sum(value_counts.values())
        if total >= min_count:
            suggestions.append(KnowledgeSuggestion(
                suggestion_type="data_gap",
                carrier=carrier,
                field_name=field,
                suggested_value="",
                correction_count=total,
                example_extracted_values=list(value_counts.keys())[:5],
                explanation=(
                    f"Field '{field}' corrected {total} times with values not "
                    f"in source documents. Needs supplemental data from client."
                ),
            ))

    # Sort by correction count (most impactful first)
    suggestions.sort(key=lambda s: -s.correction_count)
    return suggestions


async def analyze_correction_patterns_db(
    db,
    carrier: str,
    min_count: int = 3,
) -> list[KnowledgeSuggestion]:
    """Analyze patterns directly from the corrections DB table."""
    from sqlalchemy import text as sql_text

    query = sql_text("""
        SELECT root_cause, field_name, corrected_value, COUNT(*) as cnt,
               array_agg(DISTINCT extracted_value) as examples
        FROM corrections
        WHERE carrier = :carrier
          AND corrected_value IS NOT NULL
          AND field_name IS NOT NULL
        GROUP BY root_cause, field_name, corrected_value
        HAVING COUNT(*) >= :min_count
        ORDER BY cnt DESC
    """)
    result = await db.execute(query, {"carrier": carrier, "min_count": min_count})
    rows = result.fetchall()

    suggestions = []
    for r in rows:
        root_cause, field_name, corrected_value, count, examples = r

        if root_cause == RootCause.EXTRACTION:
            stype = "prompt_fix"
            explanation = f"LLM extraction error: '{field_name}' wrong {count} times"
        elif root_cause == RootCause.MERGE:
            stype = "merge_rule"
            explanation = f"Merge error: '{field_name}' wrong {count} times"
        elif root_cause == RootCause.DATA_GAP:
            stype = "data_gap"
            explanation = f"Data gap: '{field_name}' not in source docs {count} times"
        else:
            stype = "unknown"
            explanation = f"'{field_name}' corrected {count} times (cause: {root_cause})"

        suggestions.append(KnowledgeSuggestion(
            suggestion_type=stype,
            carrier=carrier,
            field_name=field_name,
            suggested_value=corrected_value,
            correction_count=count,
            example_extracted_values=examples or [],
            explanation=explanation,
        ))

    return suggestions
