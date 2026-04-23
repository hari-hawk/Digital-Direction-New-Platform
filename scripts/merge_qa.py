#!/usr/bin/env python3
"""Cross-doc merge QA — AT&T invoice + CSR through the merger.

Extracts both documents independently, runs rule_based_merge, then validates:
1. Field priority (MRC from invoice, phone/USOC from CSR)
2. Gap-filling (CSR phones fill invoice gaps, invoice addresses fill CSR gaps)
3. Conflict detection (same-priority different-value fields flagged)
4. S/C row logic (S rows merge, C rows deduplicate)
5. Validation (cross-field checks on merged output)
"""

import asyncio
import json
import logging
import sys
from collections import defaultdict, Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

# Setup path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.classifier import classify_document
from backend.pipeline.parser import parse_document
from backend.pipeline.extractor import extract_document, score_confidence
from backend.pipeline.merger import rule_based_merge, get_field_priority, _doc_type_base_priority, _build_merge_key, _normalize_for_key, _normalize_account, FIELD_PRIORITIES
from backend.config_loader import get_config_store, MergeRulesConfig
from backend.pipeline.validator import validate_rows
from backend.models.schemas import ExtractedRow

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("merge_qa")


# ── Test account pair ──
DATA_DIR = Path(__file__).parent.parent / "data" / "Consolidated - City of Dublin Project Files"
INVOICE_FILE = DATA_DIR / "ATT - 6143361586496.pdf"
CSR_FILE = DATA_DIR / "614 336 1586 496  CSR  Aug 2025.pdf"

# Load carrier merge rules for AT&T (the test carrier)
CARRIER = "att"
ATT_MERGE_RULES = get_config_store().get_merge_rules(CARRIER)

# Tracking
issues: list[dict] = []
findings: list[str] = []


def issue(severity: str, area: str, msg: str, detail: Any = None):
    """Record a QA issue."""
    issues.append({"severity": severity, "area": area, "message": msg, "detail": detail})
    icon = {"CRITICAL": "!!!", "BUG": "**BUG**", "WARNING": "WARN", "INFO": "info"}[severity]
    logger.warning(f"[{icon}] [{area}] {msg}")


def finding(msg: str):
    """Record a positive finding."""
    findings.append(msg)
    logger.info(f"[OK] {msg}")


def dump_row(row: ExtractedRow, label: str = "") -> str:
    """Compact row dump for debugging."""
    d = row.model_dump(exclude_none=True)
    # Only show key fields
    keys = ["row_type", "carrier_account_number", "sub_account_number_1", "phone_number",
            "btn", "usoc", "component_or_feature_name", "monthly_recurring_cost",
            "charge_type", "service_type", "billing_name", "service_address_1",
            "city", "state", "zip"]
    compact = {k: d[k] for k in keys if k in d}
    return f"{label}: {compact}" if label else str(compact)


# ================================================================
# Phase 1: Extract both documents independently
# ================================================================

async def extract_doc(file_path: Path) -> tuple[list[ExtractedRow], str, str]:
    """Classify, parse, extract a single document. Returns (rows, carrier, doc_type)."""
    logger.info(f"\n{'='*60}\nExtracting: {file_path.name}\n{'='*60}")

    classification = await classify_document(str(file_path))
    logger.info(f"  Classified: carrier={classification.carrier}, type={classification.document_type}, "
                f"format={classification.format_variant}, account={classification.account_number}")

    parsed = parse_document(
        str(file_path),
        classification.carrier,
        classification.document_type,
        classification.format_variant,
    )
    logger.info(f"  Parsed: {len(parsed.sections)} sections, {parsed.total_pages} pages")

    rows, responses = await extract_document(parsed)
    total_cost = sum(r.estimated_cost_usd for r in responses)
    logger.info(f"  Extracted: {len(rows)} rows, {len(responses)} API calls, ${total_cost:.4f}")

    return rows, classification.carrier, classification.document_type


# ================================================================
# Phase 2: Pre-merge analysis — understand what each doc contributes
# ================================================================

def analyze_pre_merge(invoice_rows: list[ExtractedRow], csr_rows: list[ExtractedRow]):
    """Analyze rows BEFORE merge to understand inputs."""
    logger.info(f"\n{'='*60}\nPRE-MERGE ANALYSIS\n{'='*60}")

    # Row type breakdown
    inv_types = Counter(r.row_type for r in invoice_rows)
    csr_types = Counter(r.row_type for r in csr_rows)
    logger.info(f"Invoice row types: {dict(inv_types)}")
    logger.info(f"CSR row types: {dict(csr_types)}")

    # Merge key analysis — what keys does each doc produce?
    def get_merge_keys(rows: list[ExtractedRow], label: str) -> dict[str, list[ExtractedRow]]:
        groups = defaultdict(list)
        for row in rows:
            key = _build_merge_key(row, label, ATT_MERGE_RULES)
            groups[key].append(row)
        logger.info(f"\n{label} merge keys ({len(groups)} unique):")
        for key, rows_in_key in sorted(groups.items()):
            types = Counter(r.row_type for r in rows_in_key)
            logger.info(f"  {key:60s} → {len(rows_in_key)} rows {dict(types)}")
        return dict(groups)

    inv_keys = get_merge_keys(invoice_rows, "INVOICE")
    csr_keys = get_merge_keys(csr_rows, "CSR")

    # Key overlap analysis
    inv_key_set = set(inv_keys.keys())
    csr_key_set = set(csr_keys.keys())
    overlap = inv_key_set & csr_key_set
    inv_only = inv_key_set - csr_key_set
    csr_only = csr_key_set - inv_key_set

    logger.info(f"\nMerge key overlap:")
    logger.info(f"  Matching keys (will merge): {len(overlap)} — {overlap}")
    logger.info(f"  Invoice-only keys (pass through): {len(inv_only)} — {inv_only}")
    logger.info(f"  CSR-only keys (pass through): {len(csr_only)} — {csr_only}")

    if not overlap:
        issue("CRITICAL", "merge_keys", "NO OVERLAPPING MERGE KEYS between invoice and CSR — merge will be ineffective!",
              {"invoice_keys": list(inv_key_set), "csr_keys": list(csr_key_set)})
    elif len(overlap) < min(len(inv_key_set), len(csr_key_set)) * 0.3:
        issue("WARNING", "merge_keys", f"Low merge key overlap: {len(overlap)}/{len(inv_key_set)} invoice, {len(overlap)}/{len(csr_key_set)} CSR",
              {"overlap": list(overlap)})
    else:
        finding(f"Merge key overlap: {len(overlap)} keys match between invoice and CSR")

    # Field coverage analysis — what does each doc provide?
    def field_coverage(rows: list[ExtractedRow]) -> dict[str, int]:
        coverage = {}
        for row in rows:
            d = row.model_dump()
            for field, val in d.items():
                if val is not None and field != "row_type":
                    coverage[field] = coverage.get(field, 0) + 1
        return coverage

    inv_fields = field_coverage(invoice_rows)
    csr_fields = field_coverage(csr_rows)

    # Key complementary fields
    complementary = {
        "phone_number": ("CSR should dominate", "csr"),
        "btn": ("CSR should dominate", "csr"),
        "usoc": ("CSR should dominate", "csr"),
        "monthly_recurring_cost": ("Invoice should dominate", "invoice"),
        "charge_type": ("Invoice should dominate", "invoice"),
        "billing_name": ("Invoice primary, CSR fills gaps", "invoice"),
        "service_address_1": ("Invoice primary, CSR fills gaps", "invoice"),
    }

    logger.info(f"\nComplementary field coverage:")
    for field, (desc, primary) in complementary.items():
        inv_count = inv_fields.get(field, 0)
        csr_count = csr_fields.get(field, 0)
        logger.info(f"  {field:30s}: invoice={inv_count:3d}, csr={csr_count:3d} — {desc}")

        # Check that primary source has data
        if primary == "invoice" and inv_count == 0:
            issue("WARNING", "field_coverage", f"Invoice has NO {field} data (expected primary source)")
        if primary == "csr" and csr_count == 0:
            issue("WARNING", "field_coverage", f"CSR has NO {field} data (expected primary source)")

    return inv_keys, csr_keys, overlap


# ================================================================
# Phase 3: Run the merger
# ================================================================

def run_merge(
    invoice_rows: list[ExtractedRow],
    csr_rows: list[ExtractedRow],
) -> list[ExtractedRow]:
    """Run the merger and capture detailed output."""
    logger.info(f"\n{'='*60}\nRUNNING MERGER\n{'='*60}")

    extractions = {
        "att_invoice": invoice_rows,
        "att_csr": csr_rows,
    }
    doc_types = {
        "att_invoice": "invoice",
        "att_csr": "csr",
    }

    merged, conflicts = rule_based_merge(extractions, doc_types, carrier=CARRIER)

    logger.info(f"\nMerge result: {len(invoice_rows)} + {len(csr_rows)} input → {len(merged)} merged rows, {len(conflicts)} conflicts")

    return merged


# ================================================================
# Phase 4: QA — Field Priority
# ================================================================

def qa_field_priority(
    merged: list[ExtractedRow],
    invoice_rows: list[ExtractedRow],
    csr_rows: list[ExtractedRow],
):
    """Verify field priority rules are respected in merged output."""
    logger.info(f"\n{'='*60}\nQA: FIELD PRIORITY\n{'='*60}")

    # Build lookup tables for invoice and CSR values by merge key
    def build_lookup(rows: list[ExtractedRow]) -> dict[str, dict]:
        lookup = {}
        for row in rows:
            key = _build_merge_key(row, "lookup", ATT_MERGE_RULES)
            if key and not key.startswith("unkeyed") and row.row_type == "S":
                lookup[key] = row.model_dump()
        return lookup

    inv_lookup = build_lookup(invoice_rows)
    csr_lookup = build_lookup(csr_rows)
    merged_lookup = build_lookup(merged)

    # For each overlapping key, check priority rules
    overlap_keys = set(inv_lookup.keys()) & set(csr_lookup.keys()) & set(merged_lookup.keys())
    logger.info(f"Checking {len(overlap_keys)} overlapping S-row keys for priority")

    priority_checks = {
        # field: (expected_winner, description)
        "monthly_recurring_cost": ("invoice", "MRC should come from invoice (priority 10 vs 5)"),
        "phone_number": ("csr", "Phone should come from CSR (priority 10 vs 7)"),
        "btn": ("csr", "BTN should come from CSR (priority 10 vs 7)"),
        "usoc": ("csr", "USOC should come from CSR (priority 10 vs 3)"),
        "service_type": ("csr", "Service type should prefer CSR (priority 9 vs 7)"),
        "component_or_feature_name": ("csr", "Component name should prefer CSR (priority 9 vs 7)"),
        "billing_name": ("invoice", "Billing name should prefer invoice (priority 9 vs 8)"),
        "service_address_1": ("invoice", "Address should prefer invoice (priority 9 vs 8)"),
    }

    for key in overlap_keys:
        inv_row = inv_lookup[key]
        csr_row = csr_lookup[key]
        merged_row = merged_lookup[key]

        for field, (expected_winner, desc) in priority_checks.items():
            inv_val = inv_row.get(field)
            csr_val = csr_row.get(field)
            merged_val = merged_row.get(field)

            if inv_val is None or csr_val is None:
                continue  # Can't check priority if one side has no data
            if inv_val == csr_val:
                continue  # Same value — no conflict to check

            # Normalize for format-insensitive comparison (phone, account numbers)
            norm_inv = _normalize_for_key(str(inv_val)) if field in ("phone_number", "btn") else str(inv_val)
            norm_csr = _normalize_for_key(str(csr_val)) if field in ("phone_number", "btn") else str(csr_val)
            norm_merged = _normalize_for_key(str(merged_val)) if field in ("phone_number", "btn") else str(merged_val)

            if norm_inv == norm_csr:
                continue  # Same value after normalization — no real conflict

            # Both have genuinely different values — check which one won
            if expected_winner == "invoice":
                if norm_merged == norm_inv:
                    finding(f"  {field} @ {key[:30]}: invoice wins (correct)")
                elif norm_merged == norm_csr:
                    issue("BUG", "field_priority", f"{field} @ {key[:30]}: CSR won but invoice should have! {desc}",
                          {"invoice": str(inv_val), "csr": str(csr_val), "merged": str(merged_val)})
                else:
                    issue("WARNING", "field_priority", f"{field} @ {key[:30]}: merged value matches neither source",
                          {"invoice": str(inv_val), "csr": str(csr_val), "merged": str(merged_val)})
            else:  # expected_winner == "csr"
                if norm_merged == norm_csr:
                    finding(f"  {field} @ {key[:30]}: CSR wins (correct)")
                elif norm_merged == norm_inv:
                    issue("BUG", "field_priority", f"{field} @ {key[:30]}: invoice won but CSR should have! {desc}",
                          {"invoice": str(inv_val), "csr": str(csr_val), "merged": str(merged_val)})
                else:
                    issue("WARNING", "field_priority", f"{field} @ {key[:30]}: merged value matches neither source",
                          {"invoice": str(inv_val), "csr": str(csr_val), "merged": str(merged_val)})


# ================================================================
# Phase 5: QA — Gap-Filling
# ================================================================

def qa_gap_filling(
    merged: list[ExtractedRow],
    invoice_rows: list[ExtractedRow],
    csr_rows: list[ExtractedRow],
):
    """Verify that gaps from one doc are filled by the other."""
    logger.info(f"\n{'='*60}\nQA: GAP-FILLING\n{'='*60}")

    # Check: CSR phone numbers should fill invoice rows that lack phones
    inv_phones_missing = sum(1 for r in invoice_rows if not r.phone_number and r.row_type == "S")
    merged_phones_present = sum(1 for r in merged if r.phone_number and r.row_type == "S")
    logger.info(f"Invoice S-rows missing phone: {inv_phones_missing}")
    logger.info(f"Merged S-rows with phone: {merged_phones_present}")

    # Check: Invoice addresses should fill CSR rows that lack addresses
    csr_addr_missing = sum(1 for r in csr_rows if not r.service_address_1 and r.row_type == "S")
    merged_addr_present = sum(1 for r in merged if r.service_address_1 and r.row_type == "S")
    logger.info(f"CSR S-rows missing address: {csr_addr_missing}")
    logger.info(f"Merged S-rows with address: {merged_addr_present}")

    # Check: Invoice billing_name should fill CSR gaps
    csr_name_missing = sum(1 for r in csr_rows if not r.billing_name and r.row_type == "S")
    merged_name_present = sum(1 for r in merged if r.billing_name and r.row_type == "S")
    logger.info(f"CSR S-rows missing billing_name: {csr_name_missing}")
    logger.info(f"Merged S-rows with billing_name: {merged_name_present}")

    # CSR USOC should fill invoice gaps
    inv_usoc_missing = sum(1 for r in invoice_rows if not r.usoc and r.row_type == "C")
    merged_usoc_present = sum(1 for r in merged if r.usoc and r.row_type == "C")
    logger.info(f"Invoice C-rows missing USOC: {inv_usoc_missing}")
    logger.info(f"Merged C-rows with USOC: {merged_usoc_present}")

    # Quantify overall gap-filling effectiveness
    def field_fill_rate(rows: list[ExtractedRow], fields: list[str]) -> dict[str, float]:
        rates = {}
        for field in fields:
            total = len(rows)
            filled = sum(1 for r in rows if getattr(r, field, None) is not None)
            rates[field] = filled / total if total > 0 else 0
        return rates

    key_fields = ["phone_number", "btn", "usoc", "monthly_recurring_cost",
                  "billing_name", "service_address_1", "city", "state", "zip",
                  "charge_type", "service_type", "component_or_feature_name"]

    inv_rates = field_fill_rate(invoice_rows, key_fields)
    csr_rates = field_fill_rate(csr_rows, key_fields)
    merged_rates = field_fill_rate(merged, key_fields)

    logger.info(f"\nField fill rates (invoice → CSR → MERGED):")
    for field in key_fields:
        inv_pct = inv_rates[field] * 100
        csr_pct = csr_rates[field] * 100
        merged_pct = merged_rates[field] * 100
        improvement = merged_pct - max(inv_pct, csr_pct)
        marker = "+" if improvement > 0 else " "
        logger.info(f"  {field:30s}: inv={inv_pct:5.1f}% csr={csr_pct:5.1f}% → merged={merged_pct:5.1f}% {marker}{improvement:+.1f}%")

        if merged_pct < max(inv_pct, csr_pct) - 1:
            # This is expected when CSR has many more rows than invoice —
            # overall fill rate drops because CSR-only rows dilute it.
            # Only flag as bug if the rate dropped significantly for the SMALLER set.
            if min(inv_pct, csr_pct) > 50 and merged_pct < min(inv_pct, csr_pct) - 5:
                issue("BUG", "gap_filling", f"Merge REDUCED fill rate for {field}: min({inv_pct:.1f}%, {csr_pct:.1f}%) → {merged_pct:.1f}%",
                      {"invoice": inv_pct, "csr": csr_pct, "merged": merged_pct})
            else:
                logger.info(f"    (expected: CSR-only rows dilute {field} fill rate)")
        elif improvement > 5:
            finding(f"Gap-filling improved {field} by {improvement:.1f}%")


# ================================================================
# Phase 6: QA — Conflict Detection
# ================================================================

def qa_conflict_detection(
    invoice_rows: list[ExtractedRow],
    csr_rows: list[ExtractedRow],
):
    """Manually replay merge to count expected conflicts, then compare with actual."""
    logger.info(f"\n{'='*60}\nQA: CONFLICT DETECTION\n{'='*60}")

    # Replay the merge manually to count conflicts
    from backend.pipeline.merger import _merge_group  # noqa: E402

    # Group by normalized merge key
    groups = defaultdict(list)
    for row in invoice_rows:
        key = _build_merge_key(row, "inv", ATT_MERGE_RULES)
        groups[key].append(("inv", "invoice", row))

    for row in csr_rows:
        key = _build_merge_key(row, "csr", ATT_MERGE_RULES)
        groups[key].append(("csr", "csr", row))

    total_conflicts = []
    multi_source_groups = 0
    for key, group in groups.items():
        doc_ids = set(d[0] for d in group)
        if len(doc_ids) > 1:
            multi_source_groups += 1
            merged_rows, conflicts = _merge_group(group, ATT_MERGE_RULES)
            if conflicts:
                total_conflicts.extend(conflicts)
                logger.info(f"  Key {key[:50]}: {len(conflicts)} conflicts")
                for c in conflicts:
                    logger.info(f"    {c['field']}: {c['source_a']}={c['value_a']} vs {c['source_b']}={c['value_b']}")

    logger.info(f"\nConflict summary:")
    logger.info(f"  Multi-source groups: {multi_source_groups}")
    logger.info(f"  Total conflicts: {len(total_conflicts)}")

    if total_conflicts:
        # Analyze conflict patterns
        conflict_fields = Counter(c["field"] for c in total_conflicts)
        logger.info(f"  Conflicts by field: {dict(conflict_fields)}")

        # Check: conflicts should only occur for fields with same priority in both doc types
        for conflict in total_conflicts:
            field = conflict["field"]
            src_a = conflict["source_a"]
            src_b = conflict["source_b"]
            pri_a = get_field_priority(field, src_a)
            pri_b = get_field_priority(field, src_b)
            if src_a == src_b:
                # Same-source conflict (e.g., multiple CSR S-rows for same phone)
                # These happen when a document has duplicate entries — not a merger bug
                logger.info(f"  Same-source conflict ({src_a}): {field} = {conflict['value_a']} vs {conflict['value_b']}")
            elif pri_a != pri_b:
                issue("BUG", "conflicts", f"Conflict on {field} despite different priorities ({src_a}={pri_a}, {src_b}={pri_b})",
                      conflict)
            else:
                finding(f"Conflict correctly detected: {field} (both priority {pri_a})")
    else:
        logger.info("  No conflicts detected — all field priorities resolved cleanly")
        finding("All field conflicts resolved by priority matrix — no ambiguity")


# ================================================================
# Phase 7: QA — S/C Row Logic
# ================================================================

def qa_sc_row_logic(
    merged: list[ExtractedRow],
    invoice_rows: list[ExtractedRow],
    csr_rows: list[ExtractedRow],
):
    """Verify S/C row merge and deduplication logic."""
    logger.info(f"\n{'='*60}\nQA: S/C ROW LOGIC\n{'='*60}")

    inv_s = sum(1 for r in invoice_rows if r.row_type == "S")
    inv_c = sum(1 for r in invoice_rows if r.row_type == "C")
    csr_s = sum(1 for r in csr_rows if r.row_type == "S")
    csr_c = sum(1 for r in csr_rows if r.row_type == "C")
    merged_s = sum(1 for r in merged if r.row_type == "S")
    merged_c = sum(1 for r in merged if r.row_type == "C")
    merged_other = sum(1 for r in merged if r.row_type not in ("S", "C"))

    logger.info(f"Invoice: {inv_s} S-rows, {inv_c} C-rows")
    logger.info(f"CSR:     {csr_s} S-rows, {csr_c} C-rows")
    logger.info(f"Merged:  {merged_s} S-rows, {merged_c} C-rows, {merged_other} other")

    # S-rows should be merged (fewer than sum of inputs for overlapping keys)
    if merged_s > inv_s + csr_s:
        issue("BUG", "sc_logic", f"Merged S-rows ({merged_s}) > total inputs ({inv_s + csr_s})")
    else:
        finding(f"S-rows reduced: {inv_s}+{csr_s}={inv_s+csr_s} → {merged_s} (merged)")

    # C-rows should be deduplicated (not more than sum, ideally less)
    total_c = inv_c + csr_c
    if merged_c > total_c:
        issue("BUG", "sc_logic", f"Merged C-rows ({merged_c}) > total inputs ({total_c})")
    elif merged_c < total_c:
        finding(f"C-rows deduplicated: {inv_c}+{csr_c}={total_c} → {merged_c} ({total_c - merged_c} removed)")
    else:
        logger.info(f"C-rows: no deduplication occurred (all unique)")

    # Check for orphan rows (no account number)
    orphans = [r for r in merged if not r.carrier_account_number]
    if orphans:
        issue("WARNING", "sc_logic", f"{len(orphans)} merged rows missing carrier_account_number")

    # Check for rows with no row_type
    untyped = [r for r in merged if r.row_type is None]
    if untyped:
        issue("WARNING", "sc_logic", f"{len(untyped)} merged rows have no row_type (S/C)")


# ================================================================
# Phase 8: Run Validator
# ================================================================

def qa_validation(merged: list[ExtractedRow]):
    """Run validator on merged output."""
    logger.info(f"\n{'='*60}\nQA: POST-MERGE VALIDATION\n{'='*60}")

    results = validate_rows(merged)

    valid_count = 0
    warning_count = 0
    error_count = 0
    all_issues = []

    for result in results:
        if isinstance(result, dict) and "issues" in result:
            if result.get("valid", False):
                valid_count += 1
            for iss in result["issues"]:
                all_issues.append(iss)
                if iss["severity"] == "error":
                    error_count += 1
                elif iss["severity"] == "warning":
                    warning_count += 1

    logger.info(f"Validation: {valid_count}/{len(results)} rows valid")
    logger.info(f"  Errors: {error_count}")
    logger.info(f"  Warnings: {warning_count}")

    # Group issues by type
    issue_types = Counter(i["field"] for i in all_issues)
    for field, count in issue_types.most_common():
        sample = next(i for i in all_issues if i["field"] == field)
        logger.info(f"  {field}: {count}x — {sample['message'][:80]}")

    if error_count > 0:
        issue("CRITICAL", "validation", f"{error_count} validation errors in merged output",
              {"errors": [i for i in all_issues if i["severity"] == "error"]})
    elif warning_count > 0:
        finding(f"Validation: {valid_count} valid, {warning_count} warnings (no errors)")
    else:
        finding(f"All {valid_count} merged rows pass validation")


# ================================================================
# Phase 9: Merged Output Dump
# ================================================================

def dump_merged_output(merged: list[ExtractedRow], invoice_rows: list[ExtractedRow], csr_rows: list[ExtractedRow]):
    """Dump merged output to JSON for inspection + summary table."""
    logger.info(f"\n{'='*60}\nMERGED OUTPUT SUMMARY\n{'='*60}")

    # Summary table
    total_mrc = sum(float(r.monthly_recurring_cost) for r in merged if r.monthly_recurring_cost)
    phones = set(r.phone_number for r in merged if r.phone_number)
    accounts = set(r.carrier_account_number for r in merged if r.carrier_account_number)
    usocs = set(r.usoc for r in merged if r.usoc)

    logger.info(f"Total merged rows: {len(merged)}")
    logger.info(f"Total MRC: ${total_mrc:.2f}")
    logger.info(f"Unique accounts: {len(accounts)}")
    logger.info(f"Unique phones: {len(phones)} — {phones}")
    logger.info(f"Unique USOCs: {len(usocs)} — {usocs}")

    # Dump first 10 rows
    logger.info(f"\nSample merged rows:")
    for i, row in enumerate(merged[:20]):
        logger.info(f"  Row {i}: {dump_row(row)}")

    # Save full output to JSON
    output_path = Path(__file__).parent.parent / "scripts" / "merge_qa_output.json"
    output_data = {
        "summary": {
            "invoice_rows": len(invoice_rows),
            "csr_rows": len(csr_rows),
            "merged_rows": len(merged),
            "total_mrc": float(total_mrc),
            "unique_phones": list(phones),
            "unique_accounts": list(accounts),
            "unique_usocs": list(usocs),
        },
        "merged_rows": [r.model_dump(mode="json") for r in merged],
        "invoice_rows": [r.model_dump(mode="json") for r in invoice_rows],
        "csr_rows": [r.model_dump(mode="json") for r in csr_rows],
    }
    output_path.write_text(json.dumps(output_data, indent=2, default=str))
    logger.info(f"\nFull output saved to: {output_path}")


# ================================================================
# Main
# ================================================================

async def main():
    logger.info(f"Cross-Doc Merge QA — AT&T Invoice + CSR")
    logger.info(f"Invoice: {INVOICE_FILE.name}")
    logger.info(f"CSR:     {CSR_FILE.name}")

    if not INVOICE_FILE.exists():
        logger.error(f"Invoice file not found: {INVOICE_FILE}")
        return
    if not CSR_FILE.exists():
        logger.error(f"CSR file not found: {CSR_FILE}")
        return

    # Phase 1: Load cached extraction results (if available) or extract fresh
    cache_path = Path(__file__).parent / "merge_qa_output.json"
    if cache_path.exists() and "--fresh" not in sys.argv:
        logger.info("Loading cached extraction results from merge_qa_output.json (use --fresh to re-extract)")
        cached = json.loads(cache_path.read_text())
        invoice_rows = [ExtractedRow(**r) for r in cached["invoice_rows"]]
        csr_rows = [ExtractedRow(**r) for r in cached["csr_rows"]]
        logger.info(f"  Loaded: {len(invoice_rows)} invoice rows, {len(csr_rows)} CSR rows")
    else:
        invoice_rows, inv_carrier, inv_type = await extract_doc(INVOICE_FILE)
        csr_rows, csr_carrier, csr_type = await extract_doc(CSR_FILE)

    if not invoice_rows:
        issue("CRITICAL", "extraction", "Invoice extraction produced 0 rows!")
    if not csr_rows:
        issue("CRITICAL", "extraction", "CSR extraction produced 0 rows!")

    if not invoice_rows or not csr_rows:
        logger.error("Cannot proceed without both documents extracted")
        return

    logger.info(f"\nExtraction complete: {len(invoice_rows)} invoice rows, {len(csr_rows)} CSR rows")

    # Phase 2: Pre-merge analysis
    inv_keys, csr_keys, overlap = analyze_pre_merge(invoice_rows, csr_rows)

    # Phase 3: Run merger
    merged = run_merge(invoice_rows, csr_rows)

    # Phase 4-8: QA checks
    qa_field_priority(merged, invoice_rows, csr_rows)
    qa_gap_filling(merged, invoice_rows, csr_rows)
    qa_conflict_detection(invoice_rows, csr_rows)
    qa_sc_row_logic(merged, invoice_rows, csr_rows)
    qa_validation(merged)

    # Phase 9: Output
    dump_merged_output(merged, invoice_rows, csr_rows)

    # ── Final Report ──
    logger.info(f"\n{'='*60}")
    logger.info(f"FINAL QA REPORT")
    logger.info(f"{'='*60}")

    bugs = [i for i in issues if i["severity"] == "BUG"]
    criticals = [i for i in issues if i["severity"] == "CRITICAL"]
    warnings = [i for i in issues if i["severity"] == "WARNING"]

    logger.info(f"\nIssues: {len(criticals)} CRITICAL, {len(bugs)} BUG, {len(warnings)} WARNING")
    logger.info(f"Positive findings: {len(findings)}")

    if criticals:
        logger.info(f"\nCRITICAL ISSUES:")
        for i in criticals:
            logger.info(f"  [{i['area']}] {i['message']}")
    if bugs:
        logger.info(f"\nBUGS:")
        for i in bugs:
            logger.info(f"  [{i['area']}] {i['message']}")
            if i.get("detail"):
                logger.info(f"    Detail: {i['detail']}")
    if warnings:
        logger.info(f"\nWARNINGS:")
        for i in warnings:
            logger.info(f"  [{i['area']}] {i['message']}")

    logger.info(f"\nPOSITIVE FINDINGS:")
    for f in findings:
        logger.info(f"  {f}")

    # Exit code
    if criticals or bugs:
        logger.info(f"\n*** QA FAILED — {len(criticals)} critical, {len(bugs)} bugs ***")
        return 1
    else:
        logger.info(f"\n*** QA PASSED — {len(warnings)} warnings, {len(findings)} positive findings ***")
        return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code or 0)
