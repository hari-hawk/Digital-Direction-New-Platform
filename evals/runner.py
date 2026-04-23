"""Eval runner — orchestrates golden comparison: match rows, score fields, generate report.

Usage:
    # From CLI
    python -m evals.runner --extracted path.json --golden path.xlsx

    # From API
    report = await run_eval(extracted_rows, golden_rows, carrier="att")
"""

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from backend.models.schemas import FIELD_CATEGORIES, FieldCategory
from backend.services.golden import classify_field_extractability, load_eval_config
from evals.judge import Score, RootCause, eval_row_pair, eval_fuzzy_batch, compare_field

logger = logging.getLogger(__name__)


# ── Data structures ──

@dataclass
class FieldResult:
    field_name: str
    category: str                # structured, semi_structured, fuzzy, contract
    extractability: str          # extractable, analyst_judgment, derived
    scores: Counter = field(default_factory=Counter)   # Score → count
    root_causes: Counter = field(default_factory=Counter)  # RootCause → count
    mismatches: list[dict] = field(default_factory=list)   # sample mismatches (up to 5)


@dataclass
class EvalReport:
    # Metadata
    timestamp: str = ""
    carrier: str = ""
    golden_source: str = ""
    extracted_source: str = ""

    # Row matching
    golden_count: int = 0
    extracted_count: int = 0
    matched_count: int = 0
    golden_only_count: int = 0      # rows we missed
    extracted_only_count: int = 0   # extra rows we produced

    # Per-field results
    field_results: dict[str, FieldResult] = field(default_factory=dict)

    # Category-level accuracy
    category_accuracy: dict[str, float] = field(default_factory=dict)

    # Targets comparison
    targets: dict[str, float] = field(default_factory=dict)
    targets_met: dict[str, bool] = field(default_factory=dict)

    # Worst fields
    worst_fields: list[dict] = field(default_factory=list)

    # Unmatched samples
    golden_only_samples: list[dict] = field(default_factory=list)
    extracted_only_samples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "timestamp": self.timestamp,
            "carrier": self.carrier,
            "golden_source": self.golden_source,
            "extracted_source": self.extracted_source,
            "row_matching": {
                "golden_count": self.golden_count,
                "extracted_count": self.extracted_count,
                "matched_count": self.matched_count,
                "golden_only": self.golden_only_count,
                "extracted_only": self.extracted_only_count,
                "match_rate": round(self.matched_count / self.golden_count * 100, 1) if self.golden_count else 0,
            },
            "category_accuracy": self.category_accuracy,
            "targets": self.targets,
            "targets_met": self.targets_met,
            "worst_fields": self.worst_fields,
            "field_results": {
                name: {
                    "category": fr.category,
                    "extractability": fr.extractability,
                    "scores": dict(fr.scores),
                    "accuracy": _field_accuracy(fr),
                    "root_causes": dict(fr.root_causes),
                    "sample_mismatches": fr.mismatches[:5],
                }
                for name, fr in self.field_results.items()
            },
            "golden_only_samples": self.golden_only_samples[:20],
            "extracted_only_samples": self.extracted_only_samples[:20],
        }


def _field_accuracy(fr: FieldResult) -> float | None:
    """Calculate accuracy for a single field. Returns None if no scorable comparisons."""
    if fr.extractability != "extractable":
        return None
    correct = fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.PARTIAL, 0) * 0.5
    total = (fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.WRONG, 0)
             + fr.scores.get(Score.MISSING, 0) + fr.scores.get(Score.PARTIAL, 0))
    if total == 0:
        return None
    return round(correct / total * 100, 1)


# ── Row matching ──

def _normalize_phone_for_match(value) -> str:
    """Normalize phone for matching: strip .0, strip non-digits."""
    s = str(value or "").replace(".0", "")
    return re.sub(r"[^0-9]", "", s)


def _normalize_account_for_match(value) -> str:
    """Normalize account for matching: strip non-digits, truncate to 13."""
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    if len(digits) > 13:
        digits = digits[:13]
    return digits


def _get_account_candidates(row: dict) -> list[str]:
    """Get all possible account identifiers for a row.

    Returns normalized account numbers from carrier_account_number,
    sub_account_number_1, and sub_account_number_2 — enables cross-matching
    when golden and extracted use different fields for the same account.
    """
    candidates = []
    for field in ("carrier_account_number", "sub_account_number_1", "sub_account_number_2"):
        val = _normalize_account_for_match(row.get(field, ""))
        if val and val not in candidates:
            candidates.append(val)
    return candidates or [""]


def _normalize_charge_type(row: dict) -> str:
    """Normalize charge_type for match key — groups equivalent charge types.

    MRC/Usage/Tax are the primary buckets. This is more reliable than
    service_or_component (S/C/U) which varies between documents and
    extraction methods.
    """
    ct = str(row.get("charge_type") or "").strip().upper()
    if ct in ("MRC", "MONTHLY RECURRING", "RECURRING"):
        return "MRC"
    if ct in ("USAGE", "VOICE", "LD"):
        return "USAGE"
    if ct in ("TAX", "TAXES", "SURCHARGE", "FEE"):
        return "TAX"
    if ct in ("OCC", "ONE-TIME", "NRC"):
        return "OCC"
    return ct or "UNK"


def _build_match_key(row: dict, strict: bool = True) -> str:
    """Build composite match key for row pairing.

    strict=True:  account | sub_account | phone | charge_type | row_type | usoc
    strict=False: account | sub_account | phone (relaxed fallback)

    Uses charge_type as the primary row-type discriminator because it's
    consistently populated across all document types and carriers.
    An MRC row should only pair with an MRC golden row, never with a Usage row.
    """
    acct = _normalize_account_for_match(row.get("carrier_account_number", ""))
    sub = _normalize_account_for_match(row.get("sub_account_number_1", ""))
    phone = _normalize_phone_for_match(
        row.get("phone_number") or row.get("btn") or ""
    )

    # 7→10 digit padding
    if len(phone) == 7 and len(acct) >= 3:
        phone = acct[:3] + phone

    if not strict:
        return f"{acct}|{sub}|{phone}"

    # Charge type — most reliable row-type discriminator
    ct = _normalize_charge_type(row)

    # Row type normalization (secondary signal)
    rt = str(row.get("service_or_component") or row.get("row_type") or "").strip().upper()
    if rt in ("S", "C"):
        rt = "SC"  # Treat S and C as same bucket (both are service/component level)
    elif "T" in rt or "OCC" in rt:
        rt = "T"
    elif rt == "U":
        rt = "U"
    else:
        rt = ""

    # USOC for component-level rows
    usoc = str(row.get("usoc") or "").strip().lower()

    return f"{acct}|{sub}|{phone}|{ct}|{rt}|{usoc}"


def _build_match_keys_with_sub_accounts(row: dict, strict: bool = True) -> list[str]:
    """Build match keys using all account candidates (primary + sub-accounts).

    Enables cross-matching when golden uses master_account in carrier_account_number
    and extracted uses sub_account there, or vice versa.
    """
    candidates = _get_account_candidates(row)
    phone = _normalize_phone_for_match(
        row.get("phone_number") or row.get("btn") or ""
    )

    keys = []
    for acct in candidates:
        p = phone
        if len(p) == 7 and len(acct) >= 3:
            p = acct[:3] + p

        if not strict:
            keys.append(f"{acct}|{p}")
            continue

        ct = _normalize_charge_type(row)
        rt = str(row.get("service_or_component") or row.get("row_type") or "").strip().upper()
        if rt in ("S", "C"):
            rt = "SC"
        elif "T" in rt or "OCC" in rt:
            rt = "T"
        elif rt == "U":
            rt = "U"
        else:
            rt = ""

        usoc = str(row.get("usoc") or "").strip().lower()
        keys.append(f"{acct}|{p}|{ct}|{rt}|{usoc}")

    return keys


def _content_pair_rows(
    g_rows: list[dict],
    e_rows: list[dict],
) -> list[tuple[dict, dict]]:
    """Pair rows within a matched group using content similarity.

    When multiple rows share the same match key (e.g., 20 C-rows in one
    sub-account), positional pairing (1st-to-1st) produces wrong pairs.
    Instead, pair by component_or_feature_name match, then by MRC match,
    then positionally for leftovers.
    """
    if len(g_rows) <= 1 and len(e_rows) <= 1:
        # Single row — no need for content pairing
        return [(e_rows[0], g_rows[0])] if g_rows and e_rows else []

    pairs = []
    used_g = set()
    used_e = set()

    # Pass A: exact component_or_feature_name match
    e_by_comp: dict[str, list[int]] = defaultdict(list)
    for i, e in enumerate(e_rows):
        comp = str(e.get("component_or_feature_name", "") or "").strip().lower()
        if comp:
            e_by_comp[comp].append(i)

    for gi, g in enumerate(g_rows):
        if gi in used_g:
            continue
        g_comp = str(g.get("component_or_feature_name", "") or "").strip().lower()
        if not g_comp or g_comp not in e_by_comp:
            continue
        # Find first unused extracted row with matching component
        for ei in e_by_comp[g_comp]:
            if ei not in used_e:
                pairs.append((e_rows[ei], g_rows[gi]))
                used_g.add(gi)
                used_e.add(ei)
                break

    # Pass A.5: normalized component match (handles camelCase, spacing differences)
    # e.g., "UnlimitedLocalUsage" ↔ "Unlimited Local Usage",
    #       "COTerminationWithTouchtone" ↔ "CO Termination With Touchtone"
    if len(used_g) < len(g_rows) and len(used_e) < len(e_rows):
        e_by_norm: dict[str, list[int]] = defaultdict(list)
        for i, e in enumerate(e_rows):
            if i in used_e:
                continue
            comp = str(e.get("component_or_feature_name", "") or "").strip()
            if comp:
                # Split camelCase then normalize (handles acronyms like "COTermination")
                norm = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', comp)
                norm = re.sub(r'([a-z])([A-Z])', r'\1 \2', norm)
                norm = re.sub(r'[^a-zA-Z0-9 ]', ' ', norm).lower()
                norm = re.sub(r'\s+', ' ', norm).strip()
                if norm:
                    e_by_norm[norm].append(i)

        for gi, g in enumerate(g_rows):
            if gi in used_g:
                continue
            g_comp = str(g.get("component_or_feature_name", "") or "").strip()
            if not g_comp:
                continue
            norm = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', g_comp)
            norm = re.sub(r'([a-z])([A-Z])', r'\1 \2', norm)
            norm = re.sub(r'[^a-zA-Z0-9 ]', ' ', norm).lower()
            norm = re.sub(r'\s+', ' ', norm).strip()
            if not norm or norm not in e_by_norm:
                continue
            for ei in e_by_norm[norm]:
                if ei not in used_e:
                    pairs.append((e_rows[ei], g_rows[gi]))
                    used_g.add(gi)
                    used_e.add(ei)
                    break

    # Pass B: MRC match for remaining unpaired rows
    remaining_g = [(gi, g) for gi, g in enumerate(g_rows) if gi not in used_g]
    remaining_e = [(ei, e) for ei, e in enumerate(e_rows) if ei not in used_e]

    if remaining_g and remaining_e:
        for gi, g in remaining_g:
            g_mrc = str(g.get("monthly_recurring_cost", "") or "").strip()
            if not g_mrc:
                continue
            best_ei = None
            for ei, e in remaining_e:
                if ei in used_e:
                    continue
                e_mrc = str(e.get("monthly_recurring_cost", "") or "").strip()
                if e_mrc == g_mrc:
                    best_ei = ei
                    break
            if best_ei is not None:
                pairs.append((e_rows[best_ei], g_rows[gi]))
                used_g.add(gi)
                used_e.add(best_ei)

    # Pass C: positional for leftovers — but only pair same charge type.
    # An MRC row should never pair with a Usage golden row even as a last resort.
    remaining_g = [gi for gi in range(len(g_rows)) if gi not in used_g]
    remaining_e = [ei for ei in range(len(e_rows)) if ei not in used_e]

    # First pass: pair same charge type positionally
    still_remaining_g = []
    for gi in remaining_g:
        g_ct = _normalize_charge_type(g_rows[gi])
        matched = False
        for ei in remaining_e:
            if ei in used_e:
                continue
            e_ct = _normalize_charge_type(e_rows[ei])
            if g_ct == e_ct or not g_ct or not e_ct or g_ct == "UNK" or e_ct == "UNK":
                pairs.append((e_rows[ei], g_rows[gi]))
                used_e.add(ei)
                matched = True
                break
        if not matched:
            still_remaining_g.append(gi)

    # Final fallback: pair any remaining (only if charge_type is unknown/empty)
    remaining_e2 = [ei for ei in remaining_e if ei not in used_e]
    for i in range(min(len(still_remaining_g), len(remaining_e2))):
        pairs.append((e_rows[remaining_e2[i]], g_rows[still_remaining_g[i]]))

    return pairs


def match_rows(
    golden_rows: list[dict],
    extracted_rows: list[dict],
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Three-pass row matching: strict → relaxed → sub-account cross-match.

    Within each matched group, uses content-based pairing (component name,
    then MRC) instead of positional pairing to correctly align rows.

    Returns:
        (matched_pairs, golden_only, extracted_only)
        matched_pairs: list of (extracted_row, golden_row) tuples
        golden_only: golden rows with no match
        extracted_only: extracted rows with no match
    """
    # Pass 1: strict key match (account | sub | phone | row_type | usoc)
    golden_by_key: dict[str, list[dict]] = defaultdict(list)
    for row in golden_rows:
        key = _build_match_key(row, strict=True)
        golden_by_key[key].append(row)

    extracted_by_key: dict[str, list[dict]] = defaultdict(list)
    for row in extracted_rows:
        key = _build_match_key(row, strict=True)
        extracted_by_key[key].append(row)

    matched_pairs = []
    matched_golden = set()
    matched_extracted = set()

    for key in golden_by_key:
        if key in extracted_by_key:
            g_rows = golden_by_key[key]
            e_rows = extracted_by_key[key]
            pairs = _content_pair_rows(g_rows, e_rows)
            for ext, gld in pairs:
                matched_pairs.append((ext, gld))
                matched_golden.add(id(gld))
                matched_extracted.add(id(ext))

    pass1_count = len(matched_pairs)

    # Pass 2: relaxed match for unmatched rows (account | sub | phone only)
    unmatched_golden = [r for r in golden_rows if id(r) not in matched_golden]
    unmatched_extracted = [r for r in extracted_rows if id(r) not in matched_extracted]

    if unmatched_golden and unmatched_extracted:
        relaxed_golden: dict[str, list[dict]] = defaultdict(list)
        for row in unmatched_golden:
            key = _build_match_key(row, strict=False)
            relaxed_golden[key].append(row)

        relaxed_extracted: dict[str, list[dict]] = defaultdict(list)
        for row in unmatched_extracted:
            key = _build_match_key(row, strict=False)
            relaxed_extracted[key].append(row)

        newly_matched_golden = set()
        newly_matched_extracted = set()

        for key in relaxed_golden:
            if key in relaxed_extracted:
                g_rows = relaxed_golden[key]
                e_rows = relaxed_extracted[key]
                pairs = _content_pair_rows(g_rows, e_rows)
                for ext, gld in pairs:
                    matched_pairs.append((ext, gld))
                    newly_matched_golden.add(id(gld))
                    newly_matched_extracted.add(id(ext))

        unmatched_golden = [r for r in unmatched_golden if id(r) not in newly_matched_golden]
        unmatched_extracted = [r for r in unmatched_extracted if id(r) not in newly_matched_extracted]

    pass2_count = len(matched_pairs) - pass1_count

    # Pass 3: sub-account cross-match — handles the common case where
    # golden has master_account in carrier_account_number and sub in sub_account_number_1
    # but extracted has the sub-account in carrier_account_number.
    if unmatched_golden and unmatched_extracted:
        # Build a lookup from ALL account candidates (primary + sub-accounts)
        golden_by_any_acct: dict[str, list[dict]] = defaultdict(list)
        for row in unmatched_golden:
            for key in _build_match_keys_with_sub_accounts(row, strict=False):
                golden_by_any_acct[key].append(row)

        extracted_by_any_acct: dict[str, list[dict]] = defaultdict(list)
        for row in unmatched_extracted:
            for key in _build_match_keys_with_sub_accounts(row, strict=False):
                extracted_by_any_acct[key].append(row)

        newly_matched_golden = set()
        newly_matched_extracted = set()

        for key in golden_by_any_acct:
            if key in extracted_by_any_acct:
                g_rows = [r for r in golden_by_any_acct[key] if id(r) not in newly_matched_golden]
                e_rows = [r for r in extracted_by_any_acct[key] if id(r) not in newly_matched_extracted]
                pairs_count = min(len(g_rows), len(e_rows))
                for i in range(pairs_count):
                    matched_pairs.append((e_rows[i], g_rows[i]))
                    newly_matched_golden.add(id(g_rows[i]))
                    newly_matched_extracted.add(id(e_rows[i]))

        unmatched_golden = [r for r in unmatched_golden if id(r) not in newly_matched_golden]
        unmatched_extracted = [r for r in unmatched_extracted if id(r) not in newly_matched_extracted]

    pass3_count = len(matched_pairs) - pass1_count - pass2_count

    logger.info(f"Row matching: {len(matched_pairs)} matched, "
                f"{len(unmatched_golden)} golden-only, {len(unmatched_extracted)} extracted-only "
                f"(pass1={pass1_count}, pass2={pass2_count}, pass3={pass3_count})")

    return matched_pairs, unmatched_golden, unmatched_extracted


# ── Main eval runner ──

def run_eval(
    extracted_rows: list[dict],
    golden_rows: list[dict],
    carrier: str | None = None,
    skip_llm_judge: bool = True,
    golden_source: str = "",
    extracted_source: str = "",
) -> EvalReport:
    """Run full eval: match rows → score fields → generate report.

    For LLM fuzzy judge (skip_llm_judge=False), use run_eval_async() instead.

    Args:
        extracted_rows: list of {field: value} dicts from our extraction
        golden_rows: list of {field: value} dicts from golden data
        carrier: carrier name for extractability config
        skip_llm_judge: if True, skip Claude fuzzy evaluation (free, fast)
        golden_source: description of golden data source (for report metadata)
        extracted_source: description of extraction source (for report metadata)

    Returns:
        EvalReport with all results
    """
    if not skip_llm_judge:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # Already in async context — caller should use run_eval_async
            logger.warning("skip_llm_judge=False in sync context with running loop. "
                           "Use run_eval_async() instead. Falling back to deterministic.")
            skip_llm_judge = True
        else:
            return asyncio.run(run_eval_async(
                extracted_rows, golden_rows, carrier=carrier,
                skip_llm_judge=False, golden_source=golden_source,
                extracted_source=extracted_source,
            ))
    report = EvalReport(
        timestamp=datetime.utcnow().isoformat(),
        carrier=carrier or "",
        golden_source=golden_source,
        extracted_source=extracted_source,
        golden_count=len(golden_rows),
        extracted_count=len(extracted_rows),
    )

    # Load extractability config
    extractability = classify_field_extractability(carrier)

    # Filter non-comparable extracted rows before matching
    config = load_eval_config()
    row_matching_config = config.get("row_matching", {})
    exclude_charge_types = row_matching_config.get("exclude_charge_types", [])
    exclude_untyped = row_matching_config.get("exclude_untyped_rows", False)

    if exclude_charge_types or exclude_untyped:
        pre_filter_count = len(extracted_rows)
        filtered_extracted = []
        for row in extracted_rows:
            charge = str(row.get("charge_type") or "").strip()
            row_type = str(row.get("row_type") or row.get("service_or_component") or "").strip()

            # Exclude specific charge types (Surcharge, Tax)
            if charge and charge in exclude_charge_types:
                continue
            # Exclude rows with BOTH null charge_type AND null row_type
            # (report/structured data rows with no golden equivalent)
            if exclude_untyped and not charge and not row_type:
                continue
            filtered_extracted.append(row)
        excluded = pre_filter_count - len(filtered_extracted)
        if excluded:
            logger.info(f"Row filtering: excluded {excluded}/{pre_filter_count} extracted rows "
                        f"(charge_types={exclude_charge_types}, untyped={exclude_untyped})")
        extracted_rows = filtered_extracted
        report.extracted_count = len(extracted_rows)

    # Match rows
    matched_pairs, golden_only, extracted_only = match_rows(golden_rows, extracted_rows)
    report.matched_count = len(matched_pairs)
    report.golden_only_count = len(golden_only)
    report.extracted_only_count = len(extracted_only)

    # Sample unmatched rows
    report.golden_only_samples = [
        {
            "key": _build_match_key(r),
            "carrier": r.get("carrier_name", ""),
            "phone": str(r.get("phone_number", "")),
            "account": str(r.get("carrier_account_number", "")),
            "service_type": r.get("service_type", ""),
            "row_type": r.get("service_or_component", ""),
        }
        for r in golden_only[:20]
    ]
    report.extracted_only_samples = [
        {
            "key": _build_match_key(r),
            "carrier": r.get("carrier_name", ""),
            "phone": str(r.get("phone_number", "")),
            "account": str(r.get("carrier_account_number", "")),
            "service_type": r.get("service_type", ""),
            "row_type": r.get("service_or_component", "") or r.get("row_type", ""),
        }
        for r in extracted_only[:20]
    ]

    # Initialize field results
    for field_name, category in FIELD_CATEGORIES.items():
        report.field_results[field_name] = FieldResult(
            field_name=field_name,
            category=category.value,
            extractability=extractability.get(field_name, "extractable"),
        )

    # Score matched pairs
    for ext_row, gld_row in matched_pairs:
        row_scores = eval_row_pair(ext_row, gld_row, extractability)

        for field_name, (score, root_cause) in row_scores.items():
            if field_name not in report.field_results:
                continue
            fr = report.field_results[field_name]
            fr.scores[score] += 1

            if root_cause:
                fr.root_causes[root_cause] += 1

            # Collect sample mismatches (up to 5 per field)
            if score in (Score.WRONG, Score.MISSING) and len(fr.mismatches) < 5:
                if fr.extractability == "extractable":
                    fr.mismatches.append({
                        "score": score.value,
                        "extracted": str(ext_row.get(field_name, "")),
                        "golden": str(gld_row.get(field_name, "")),
                        "root_cause": root_cause.value if root_cause else None,
                        "account": str(ext_row.get("carrier_account_number", "")),
                        "phone": str(ext_row.get("phone_number", "")),
                    })

    # Calculate category accuracy
    targets = config.get("accuracy_targets", {})

    category_scores: dict[str, tuple[float, int]] = {}  # category → (correct_sum, total)
    for fr in report.field_results.values():
        if fr.extractability != "extractable":
            continue
        correct = fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.PARTIAL, 0) * 0.5
        total = (fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.WRONG, 0)
                 + fr.scores.get(Score.MISSING, 0) + fr.scores.get(Score.PARTIAL, 0))
        if total == 0:
            continue
        cat = fr.category
        if cat not in category_scores:
            category_scores[cat] = (0.0, 0)
        prev_c, prev_t = category_scores[cat]
        category_scores[cat] = (prev_c + correct, prev_t + total)

    for cat, (correct, total) in category_scores.items():
        pct = round(correct / total * 100, 1) if total > 0 else 0
        report.category_accuracy[cat] = pct
        target = targets.get(cat)
        if target:
            report.targets[cat] = target * 100
            report.targets_met[cat] = pct >= target * 100

    # Worst fields (by accuracy, extractable only)
    field_accuracies = []
    for name, fr in report.field_results.items():
        acc = _field_accuracy(fr)
        if acc is not None:
            total = (fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.WRONG, 0)
                     + fr.scores.get(Score.MISSING, 0) + fr.scores.get(Score.PARTIAL, 0))
            field_accuracies.append({
                "field": name,
                "accuracy": acc,
                "total_scored": total,
                "correct": fr.scores.get(Score.CORRECT, 0),
                "wrong": fr.scores.get(Score.WRONG, 0),
                "missing": fr.scores.get(Score.MISSING, 0),
                "top_root_cause": fr.root_causes.most_common(1)[0][0].value if fr.root_causes else None,
            })

    report.worst_fields = sorted(field_accuracies, key=lambda x: x["accuracy"])[:15]

    return report


async def run_eval_async(
    extracted_rows: list[dict],
    golden_rows: list[dict],
    carrier: str | None = None,
    skip_llm_judge: bool = False,
    golden_source: str = "",
    extracted_source: str = "",
) -> EvalReport:
    """Async eval with optional LLM fuzzy judge for semantic field matching.

    When skip_llm_judge=False, fields scored WRONG by the deterministic judge
    in fuzzy/contract categories are re-evaluated by Claude for semantic equivalence.
    Example: "UnlimitedLocalUsage" vs "BUS CLING" — different naming systems,
    but Claude can judge if they refer to the same telecom service component.

    Cost: ~$0.01–0.05 per eval run (Claude API calls for fuzzy comparisons).
    """
    # Run deterministic eval first
    report = run_eval(
        extracted_rows, golden_rows,
        carrier=carrier, skip_llm_judge=True,
        golden_source=golden_source, extracted_source=extracted_source,
    )

    if skip_llm_judge:
        return report

    # Identify fuzzy fields that had WRONG scores — candidates for LLM re-judgment
    fuzzy_fields = [
        name for name, fr in report.field_results.items()
        if fr.category in ("fuzzy", "contract")
        and fr.extractability == "extractable"
        and fr.scores.get(Score.WRONG, 0) > 0
    ]

    if not fuzzy_fields:
        logger.info("LLM judge: no fuzzy WRONG scores to re-evaluate")
        return report

    # Re-match rows to get the pairs (we need them for the LLM judge)
    extractability = classify_field_extractability(carrier)
    matched_pairs, _, _ = match_rows(golden_rows, extracted_rows)

    # Filter to pairs that have at least one WRONG fuzzy field
    pairs_for_llm = []
    pair_indices = []  # track which original pair each belongs to
    for i, (ext, gld) in enumerate(matched_pairs):
        has_wrong_fuzzy = False
        for field_name in fuzzy_fields:
            e_val = str(ext.get(field_name, "") or "").strip()
            g_val = str(gld.get(field_name, "") or "").strip()
            if e_val and g_val and e_val.lower() != g_val.lower():
                has_wrong_fuzzy = True
                break
        if has_wrong_fuzzy:
            pairs_for_llm.append((ext, gld))
            pair_indices.append(i)

    if not pairs_for_llm:
        logger.info("LLM judge: no pairs with wrong fuzzy fields to re-evaluate")
        return report

    logger.info(f"LLM judge: re-evaluating {len(pairs_for_llm)} pairs across "
                f"{len(fuzzy_fields)} fuzzy fields: {fuzzy_fields}")

    # Call LLM fuzzy judge
    llm_results = await eval_fuzzy_batch(pairs_for_llm, fuzzy_fields, extractability)

    # Apply LLM overrides to the report
    overrides_applied = 0
    for batch_idx, llm_pair_result in enumerate(llm_results):
        if not llm_pair_result:
            continue
        for field_name, (new_score, new_cause) in llm_pair_result.items():
            if field_name not in report.field_results:
                continue
            fr = report.field_results[field_name]

            # Only upgrade: WRONG → CORRECT or PARTIAL (never downgrade)
            if new_score in (Score.CORRECT, Score.PARTIAL):
                fr.scores[Score.WRONG] -= 1
                fr.scores[new_score] += 1
                overrides_applied += 1

                # Update root cause if applicable
                if new_cause and new_cause != RootCause.UNKNOWN:
                    fr.root_causes[new_cause] += 1

    logger.info(f"LLM judge: {overrides_applied} scores upgraded (WRONG → CORRECT/PARTIAL)")

    # Recalculate category accuracy and worst fields after LLM overrides
    config = load_eval_config()
    targets = config.get("accuracy_targets", {})

    category_scores: dict[str, tuple[float, int]] = {}
    for fr in report.field_results.values():
        if fr.extractability != "extractable":
            continue
        correct = fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.PARTIAL, 0) * 0.5
        total = (fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.WRONG, 0)
                 + fr.scores.get(Score.MISSING, 0) + fr.scores.get(Score.PARTIAL, 0))
        if total == 0:
            continue
        cat = fr.category
        if cat not in category_scores:
            category_scores[cat] = (0.0, 0)
        prev_c, prev_t = category_scores[cat]
        category_scores[cat] = (prev_c + correct, prev_t + total)

    for cat, (correct, total) in category_scores.items():
        pct = round(correct / total * 100, 1) if total > 0 else 0
        report.category_accuracy[cat] = pct
        target = targets.get(cat)
        if target:
            report.targets[cat] = target * 100
            report.targets_met[cat] = pct >= target * 100

    field_accuracies = []
    for name, fr in report.field_results.items():
        acc = _field_accuracy(fr)
        if acc is not None:
            total = (fr.scores.get(Score.CORRECT, 0) + fr.scores.get(Score.WRONG, 0)
                     + fr.scores.get(Score.MISSING, 0) + fr.scores.get(Score.PARTIAL, 0))
            field_accuracies.append({
                "field": name,
                "accuracy": acc,
                "total_scored": total,
                "correct": fr.scores.get(Score.CORRECT, 0),
                "wrong": fr.scores.get(Score.WRONG, 0),
                "missing": fr.scores.get(Score.MISSING, 0),
                "top_root_cause": fr.root_causes.most_common(1)[0][0].value if fr.root_causes else None,
            })

    report.worst_fields = sorted(field_accuracies, key=lambda x: x["accuracy"])[:15]

    return report


def print_report(report: EvalReport):
    """Print human-readable eval report to stdout."""
    print(f"\n{'='*70}")
    print(f"EVAL REPORT — {report.carrier or 'all carriers'}")
    print(f"{'='*70}")
    print(f"Timestamp:    {report.timestamp}")
    print(f"Golden:       {report.golden_count} rows ({report.golden_source})")
    print(f"Extracted:    {report.extracted_count} rows ({report.extracted_source})")
    print(f"Matched:      {report.matched_count} ({report.matched_count/report.golden_count*100:.0f}% of golden)" if report.golden_count else "")
    print(f"Golden-only:  {report.golden_only_count}")
    print(f"Extra:        {report.extracted_only_count}")

    print(f"\n{'─'*70}")
    print("ACCURACY BY CATEGORY")
    print(f"{'─'*70}")
    for cat, pct in sorted(report.category_accuracy.items()):
        target = report.targets.get(cat, 0)
        met = report.targets_met.get(cat, False)
        marker = " ✓" if met else f" (target: {target:.0f}%)" if target else ""
        print(f"  {cat:25s}  {pct:5.1f}%{marker}")

    print(f"\n{'─'*70}")
    print("WORST FIELDS (extractable, sorted by accuracy)")
    print(f"{'─'*70}")
    print(f"  {'Field':35s} {'Acc%':>6s} {'Correct':>8s} {'Wrong':>8s} {'Missing':>8s} {'Root Cause':>15s}")
    for wf in report.worst_fields:
        cause = wf['top_root_cause'] or ''
        print(f"  {wf['field']:35s} {wf['accuracy']:5.1f}% {wf['correct']:8d} {wf['wrong']:8d} {wf['missing']:8d} {cause:>15s}")

    print(f"\n{'='*70}")
