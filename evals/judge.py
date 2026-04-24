"""Eval judge — deterministic + LLM scoring for field-level accuracy.

Two modes:
1. Deterministic (eval_structured): exact/normalized match for structured + semi-structured fields
2. LLM (eval_fuzzy): Claude judges semantic equivalence for fuzzy fields (optional, costs money)

Each field gets a FieldScore: CORRECT, WRONG, MISSING, EXTRA, PARTIAL, SKIPPED, BOTH_EMPTY.

LangFuse tracing: All Claude eval calls are traced to LangFuse for observability.
"""

import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

logger = logging.getLogger(__name__)


class Score(str, Enum):
    CORRECT = "CORRECT"         # Values match (exact or normalized)
    PARTIAL = "PARTIAL"         # Fuzzy match — semantically equivalent but different text
    WRONG = "WRONG"             # Values differ
    MISSING = "MISSING"         # Golden has value, extraction doesn't
    EXTRA = "EXTRA"             # Extraction has value, golden doesn't
    SKIPPED = "SKIPPED"         # Field is analyst_judgment or derived — not scored
    BOTH_EMPTY = "BOTH_EMPTY"   # Neither has a value


class RootCause(str, Enum):
    """Why a field scored WRONG — helps prioritize fixes."""
    NORMALIZATION = "normalization"    # Same value, format differs (att vs AT&T)
    OCR_ERROR = "ocr_error"           # Extraction garbled the text
    PARSING_ERROR = "parsing_error"   # Parser lost or split the value
    CROSS_REF_ERROR = "cross_ref"     # Value from wrong document section
    HALLUCINATION = "hallucination"   # LLM fabricated a value
    DOMAIN_ERROR = "domain_error"     # Needs domain knowledge (USOC→name lookup)
    CALCULATION = "calculation"       # Golden has calculated/derived amount
    UNKNOWN = "unknown"


# ── Normalization helpers ──

# Analyst placeholder strings that semantically mean "field not populated in the
# source document". Our pipeline emits null for these; golden spreadsheets often
# carry explicit literals. Treat them as equivalent to empty for scoring only —
# pipeline output is untouched.
_EMPTY_PLACEHOLDERS = {
    "not mentioned", "not mention", "not mentioned in document", "na", "n/a",
    "none", "null", "nil", "-", "--", "---",
}


def _is_empty_placeholder(value) -> bool:
    """True if the string is a golden-data convention for "no value"."""
    if value is None:
        return True
    s = str(value).strip().lower()
    return not s or s in _EMPTY_PLACEHOLDERS


def _to_str(value) -> str:
    """Normalize any value to a stripped string. Returns '' for None/empty or
    for analyst-convention placeholder strings like "Not mentioned" / "NA"."""
    if _is_empty_placeholder(value):
        return ""
    s = str(value).strip()
    # Strip Excel .0 artifact
    if s.endswith(".0") and s[:-2].replace("-", "").replace(" ", "").isdigit():
        s = s[:-2]
    return s


def _normalize_phone(value) -> str:
    """Normalize phone: strip .0, strip non-digits."""
    s = _to_str(value).replace(".0", "")
    return re.sub(r"[^0-9]", "", s)


def _normalize_account(value) -> str:
    """Normalize account: strip non-digits."""
    return re.sub(r"[^0-9]", "", _to_str(value))


def _normalize_amount(value) -> Decimal | None:
    """Normalize monetary amount to Decimal."""
    s = _to_str(value).replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _normalize_date(value) -> str:
    """Normalize date to YYYY-MM-DD string."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    s = _to_str(value)
    if not s:
        return ""
    # Try common formats
    for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m-%d-%Y"]:
        try:
            return datetime.strptime(s[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


# Carrier name aliases — maps variations to a canonical form for comparison
_CARRIER_ALIASES = {
    "ATT": "ATT", "ATANDT": "ATT", "ATTOHIO": "ATT",
    "THEOHIOBELLTELEPHONE": "ATT", "OHIOBELL": "ATT",
    "SPECTRUM": "SPECTRUM", "CHARTER": "SPECTRUM",
    "CHARTERCOMMUNICATIONS": "SPECTRUM", "SPECTRUMBUSINESS": "SPECTRUM",
    "SPECTRUMENTERPRISE": "SPECTRUM",
    "WINDSTREAM": "WINDSTREAM", "WINDSTREAMCOMMUNICATIONS": "WINDSTREAM",
    "WINDSTREAMENTERPRISE": "WINDSTREAM",
    "PEERLESS": "PEERLESS", "PEERLESSNETWORK": "PEERLESS",
    "PEERLESSNETWORKS": "PEERLESS", "THEPEERLESSPORTAL": "PEERLESS",
    "PEERLESSPORTAL": "PEERLESS",
}


def _normalize_carrier(value) -> str:
    """Normalize carrier name: strip non-alphanumeric, uppercase, resolve aliases."""
    raw = re.sub(r"[^a-zA-Z0-9]", "", _to_str(value)).upper()
    # Try exact alias lookup first
    if raw in _CARRIER_ALIASES:
        return _CARRIER_ALIASES[raw]
    # Try prefix match for longer names (e.g., "WINDSTREAMCOMMUNICATIONSINC")
    for alias, canonical in sorted(_CARRIER_ALIASES.items(), key=lambda x: -len(x[0])):
        if raw.startswith(alias):
            return canonical
    return raw


# ── Field comparison ──

# Fields grouped by comparison strategy
_PHONE_FIELDS = {"phone_number", "btn", "point_to_number"}
_ACCOUNT_FIELDS = {"carrier_account_number", "master_account",
                    "sub_account_number_1", "sub_account_number_2",
                    "contract_number", "contract_number_2"}
_AMOUNT_FIELDS = {"monthly_recurring_cost", "cost_per_unit", "mrc_per_currency",
                   "ld_cost", "rate", "ld_flat_rate", "ld_minutes"}
_DATE_FIELDS = {"contract_begin_date", "contract_expiration_date"}
_INT_FIELDS = {"quantity", "num_calls", "contract_term_months"}
_CARRIER_FIELDS = {"carrier_name"}
_COMPONENT_NAME_FIELDS = {"component_or_feature_name"}


# Common telecom-naming abbreviations that analyst text + doc text disagree on.
# Used by the component-name comparator to normalize both sides before match.
_COMPONENT_ABBREVS = {
    "bus": "business", "busi": "business",
    "svc": "service", "srv": "service",
    "mgmt": "management", "mgr": "manager",
    "intl": "international", "natl": "national",
    "ctx": "centrex", "cntx": "centrex",
    "ovc": "overcharge",
    "unltd": "unlimited", "unlim": "unlimited", "unltmt": "unlimited",
    "id": "identification", "ident": "identification",
    "tel": "telephone", "telco": "telephone",
    "ord": "order",
    "indiv": "individual", "ind": "individual",
    "msg": "message", "mesg": "message",
    "forwd": "forwarding", "fwd": "forwarding",
    "add": "additional", "addtl": "additional",
    "assm": "assessment", "asmt": "assessment",
    "surc": "surcharge",
    "chg": "charge", "chrg": "charge",
    "fed": "federal",
    "univ": "universal",
    "reg": "regulatory", "regul": "regulatory",
    "corp": "corporation", "cmty": "community",
    "co": "co", "comm": "communications",  # keep short forms; already common
}


# Lazy-loaded AT&T USOC code → canonical-name map. Only touched during eval;
# zero impact on extraction pipeline.
_USOC_NAME_MAP: dict[str, str] | None = None


def _load_usoc_map() -> dict[str, str]:
    """Load AT&T USOC→name map once, cache for subsequent calls."""
    global _USOC_NAME_MAP
    if _USOC_NAME_MAP is not None:
        return _USOC_NAME_MAP
    _USOC_NAME_MAP = {}
    try:
        import yaml
        from pathlib import Path
        path = Path(__file__).parent.parent / "configs" / "carriers" / "att" / "domain_knowledge" / "usoc_codes.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            # File is a top-level {CODE: name} mapping
            _USOC_NAME_MAP = {str(k).upper(): str(v) for k, v in data.items() if v}
    except Exception:
        pass
    return _USOC_NAME_MAP


def _normalize_component_name(value) -> str:
    """Collapse a component name to a comparable form.

    Steps:
      1. If value is a USOC code (all-caps ≤6 chars), look up canonical name.
      2. Lowercase, strip non-alphanumeric, split into tokens.
      3. Expand common abbreviations (bus→business, ctx→centrex, …).
      4. Re-join as a single alphabetic string for direct compare.
    """
    s = _to_str(value)
    if not s:
        return ""
    # USOC passthrough
    if len(s) <= 6 and s.isupper() and s.isalnum():
        mapped = _load_usoc_map().get(s.upper())
        if mapped:
            s = mapped
    # Tokenize by any non-alpha run; also split camelCase / PascalCase
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)  # camelCase boundary
    tokens = re.split(r"[^a-zA-Z0-9]+", s.lower())
    expanded: list[str] = []
    for t in tokens:
        if not t:
            continue
        expanded.append(_COMPONENT_ABBREVS.get(t, t))
    return "".join(expanded)


def compare_field(
    field_name: str,
    extracted_val,
    golden_val,
    extractability: str = "extractable",
) -> tuple[Score, RootCause | None]:
    """Compare a single field. Returns (Score, optional RootCause for WRONG).

    Args:
        field_name: schema field name
        extracted_val: value from our extraction
        golden_val: value from golden data
        extractability: "extractable", "analyst_judgment", or "derived"

    Returns:
        (Score, RootCause or None)
    """
    if extractability in ("analyst_judgment", "derived"):
        return Score.SKIPPED, None

    e_str = _to_str(extracted_val)
    g_str = _to_str(golden_val)

    # Both empty
    if not e_str and not g_str:
        return Score.BOTH_EMPTY, None

    # One empty
    if not e_str and g_str:
        return Score.MISSING, None
    if e_str and not g_str:
        return Score.EXTRA, None

    # ── Field-type-specific comparison ──

    if field_name in _PHONE_FIELDS:
        return _compare_phone(extracted_val, golden_val)

    if field_name in _ACCOUNT_FIELDS:
        return _compare_account(extracted_val, golden_val)

    if field_name in _AMOUNT_FIELDS:
        return _compare_amount(extracted_val, golden_val)

    if field_name in _DATE_FIELDS:
        return _compare_date(extracted_val, golden_val)

    if field_name in _INT_FIELDS:
        return _compare_int(extracted_val, golden_val)

    if field_name in _CARRIER_FIELDS:
        return _compare_carrier(extracted_val, golden_val)

    if field_name in _COMPONENT_NAME_FIELDS:
        return _compare_component_name(extracted_val, golden_val)

    # Default: case-insensitive string match with containment fallback
    return _compare_string(extracted_val, golden_val)


def _compare_component_name(extracted, golden) -> tuple[Score, RootCause | None]:
    """Compare component/feature names after USOC lookup + abbreviation expansion.

    Handles the CSR↔invoice naming mismatch (e.g., "Business Local Calling
    Unlimited B" vs "BusLocalCallingUnlimitedB" vs USOC code "1MB").
    """
    e_norm = _normalize_component_name(extracted)
    g_norm = _normalize_component_name(golden)
    if not e_norm and not g_norm:
        return Score.BOTH_EMPTY, None
    if e_norm == g_norm:
        return Score.CORRECT, None
    # Substring match after normalization — handles "Three Way Calling" vs
    # "Three-Way Calling Feature" without needing perfect abbreviation maps.
    if len(e_norm) > 4 and len(g_norm) > 4 and (e_norm in g_norm or g_norm in e_norm):
        return Score.PARTIAL, None
    return Score.WRONG, RootCause.DOMAIN_ERROR


def _compare_phone(extracted, golden) -> tuple[Score, RootCause | None]:
    e = _normalize_phone(extracted)
    g = _normalize_phone(golden)
    if e == g:
        return Score.CORRECT, None
    # 7↔10 digit matching
    if len(e) == 7 and len(g) == 10 and g.endswith(e):
        return Score.CORRECT, None
    if len(g) == 7 and len(e) == 10 and e.endswith(g):
        return Score.CORRECT, None
    return Score.WRONG, RootCause.NORMALIZATION


def _compare_account(extracted, golden) -> tuple[Score, RootCause | None]:
    e = _normalize_account(extracted)
    g = _normalize_account(golden)
    if e == g:
        return Score.CORRECT, None
    # Substring match (check digit truncation, prefix differences)
    if len(e) >= 10 and len(g) >= 10:
        if e in g or g in e:
            return Score.CORRECT, None
    return Score.WRONG, RootCause.NORMALIZATION


def _compare_amount(extracted, golden) -> tuple[Score, RootCause | None]:
    e = _normalize_amount(extracted)
    g = _normalize_amount(golden)
    if e is None or g is None:
        return Score.WRONG, RootCause.PARSING_ERROR
    if e == g:
        return Score.CORRECT, None
    if abs(e - g) <= Decimal("0.01"):
        return Score.CORRECT, None
    # Check if golden has a calculated/prorated amount (many decimal places)
    g_str = _to_str(golden)
    if "." in g_str and len(g_str.split(".")[-1]) > 4:
        return Score.WRONG, RootCause.CALCULATION
    return Score.WRONG, None


def _compare_date(extracted, golden) -> tuple[Score, RootCause | None]:
    e = _normalize_date(extracted)
    g = _normalize_date(golden)
    if e == g:
        return Score.CORRECT, None
    return Score.WRONG, None


def _compare_int(extracted, golden) -> tuple[Score, RootCause | None]:
    try:
        e = int(float(_to_str(extracted)))
        g = int(float(_to_str(golden)))
        if e == g:
            return Score.CORRECT, None
        return Score.WRONG, None
    except (ValueError, TypeError):
        return Score.WRONG, RootCause.PARSING_ERROR


def _compare_carrier(extracted, golden) -> tuple[Score, RootCause | None]:
    if _normalize_carrier(extracted) == _normalize_carrier(golden):
        return Score.CORRECT, None
    return Score.WRONG, RootCause.NORMALIZATION


def _compare_string(extracted, golden) -> tuple[Score, RootCause | None]:
    e = _to_str(extracted).lower()
    g = _to_str(golden).lower()
    if e == g:
        return Score.CORRECT, None
    # Containment check for partial matches
    if len(e) > 3 and len(g) > 3:
        if e in g or g in e:
            return Score.PARTIAL, None
    return Score.WRONG, None


# ── Batch evaluation ──

def eval_row_pair(
    extracted: dict,
    golden: dict,
    extractability: dict[str, str],
) -> dict[str, tuple[Score, RootCause | None]]:
    """Compare all fields between an extracted row and a golden row.

    Args:
        extracted: {field_name: value} from our extraction
        golden: {field_name: value} from golden data
        extractability: {field_name: "extractable"|"analyst_judgment"|"derived"}

    Returns:
        {field_name: (Score, RootCause or None)}
    """
    all_fields = set(extractability.keys())
    results = {}

    for field in all_fields:
        ext_val = extracted.get(field)
        gld_val = golden.get(field)
        ext_class = extractability.get(field, "extractable")
        results[field] = compare_field(field, ext_val, gld_val, ext_class)

    return results


# ── LangFuse tracing helper ──

def _trace_eval_call(prompt: str, response: str, carrier: str = "unknown"):
    """Log an eval call to LangFuse if enabled. Non-blocking, best-effort."""
    try:
        from backend.settings import settings
        from backend.services.llm import get_langfuse
        
        lf = get_langfuse()
        if not lf:
            return
        
        trace = lf.trace(name="eval_judge", metadata={"carrier": carrier, "type": "fuzzy_match"})
        trace.generation(
            name="claude_eval_judge",
            model="claude-opus-4-6",
            input=prompt[:2000],  # Truncate for storage
            output=response[:2000],
            metadata={"carrier": carrier},
        )
    except Exception:
        pass  # Tracing is best-effort, never block eval


# ── LLM fuzzy judge (optional, costs money) ──

async def eval_fuzzy_batch(
    pairs: list[tuple[dict, dict]],
    fields: list[str],
    extractability: dict[str, str],
) -> list[dict[str, tuple[Score, RootCause | None]]]:
    """Use Claude to judge fuzzy field matches.

    For fields where deterministic comparison returns WRONG but the values
    might be semantically equivalent (e.g., "BLC" vs "Business Local Calling").

    Args:
        pairs: list of (extracted_row, golden_row) dicts
        fields: which fields to judge (typically fuzzy + contract category)
        extractability: field extractability classification

    Returns:
        list of {field: (Score, RootCause)} dicts, one per pair
    """
    from backend.services.llm import get_claude

    # Build batch prompt
    comparisons = []
    for i, (ext, gld) in enumerate(pairs):
        for field in fields:
            ext_val = _to_str(ext.get(field))
            gld_val = _to_str(gld.get(field))
            if not ext_val or not gld_val:
                continue
            if ext_val.lower() == gld_val.lower():
                continue  # Already matched deterministically
            comparisons.append({
                "pair_idx": i,
                "field": field,
                "extracted": ext_val,
                "golden": gld_val,
            })

    if not comparisons:
        return [{} for _ in pairs]

    # Batch into chunks of 20
    results_by_pair: list[dict] = [{} for _ in pairs]
    chunk_size = 20

    for chunk_start in range(0, len(comparisons), chunk_size):
        chunk = comparisons[chunk_start:chunk_start + chunk_size]

        prompt = (
            "You are judging whether extracted telecom data matches golden reference data.\n"
            "For each comparison, judge if the values are:\n"
            "- CORRECT: semantically the same (e.g., 'BLC' = 'Business Local Calling')\n"
            "- PARTIAL: related but not equivalent (e.g., 'Caller ID' vs 'Calling Name Display')\n"
            "- WRONG: genuinely different values\n\n"
            "Also classify the root cause of WRONG/PARTIAL:\n"
            "- normalization: same value, different format\n"
            "- domain_error: needs USOC/carrier knowledge to match\n"
            "- parsing_error: extraction garbled or split the value\n"
            "- cross_ref: value from wrong section/account\n"
            "- unknown: can't determine cause\n\n"
            "Return JSON array:\n"
            '[{"idx": 0, "score": "CORRECT|PARTIAL|WRONG", "cause": "..."}, ...]\n\n'
            "COMPARISONS:\n"
        )
        for j, comp in enumerate(chunk):
            prompt += (f"{j}. field={comp['field']}: "
                       f"extracted={comp['extracted']!r} vs golden={comp['golden']!r}\n")

        try:
            claude = get_claude()
            start = time.monotonic()
            response = await claude.call(
                prompt=prompt,
                system="Telecom data quality judge. Be precise. Return ONLY the JSON array.",
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            # Trace to LangFuse
            _trace_eval_call(prompt, response.content, carrier="all")

            import json
            # Strip markdown code blocks if present (Claude often wraps JSON in ```json...```)
            content = response.content.strip()
            if content.startswith("```"):
                # Remove opening ```json or ``` and closing ```
                lines = content.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                content = "\n".join(lines)
            judgments = json.loads(content)
            if not isinstance(judgments, list):
                judgments = [judgments]

            for j_result in judgments:
                idx = j_result.get("idx", 0)
                if 0 <= idx < len(chunk):
                    comp = chunk[idx]
                    score_str = j_result.get("score", "WRONG").upper()
                    cause_str = j_result.get("cause", "unknown")

                    score = Score.CORRECT if score_str == "CORRECT" else (
                        Score.PARTIAL if score_str == "PARTIAL" else Score.WRONG
                    )
                    cause = RootCause(cause_str) if cause_str in RootCause.__members__.values() else RootCause.UNKNOWN

                    pair_idx = comp["pair_idx"]
                    field = comp["field"]
                    results_by_pair[pair_idx][field] = (score, cause)

        except Exception as e:
            logger.error(f"LLM fuzzy eval failed: {e}")

    return results_by_pair
