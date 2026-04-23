"""Quick diagnostic: compare our extraction to golden data for City of Dublin.

This is NOT the formal eval framework. It's a one-time diagnostic to see
where we stand before building Task 6.1-6.4.

Usage:
    python scripts/golden_comparison.py --extracted /tmp/dublin_extraction_results.json
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl


# ── Column name mapping: golden Excel → our schema field names ──

GOLDEN_TO_SCHEMA = {
    "Status": "status",
    "Inventory creation questions, concerns, or notes.": "notes",
    "*Contract Info received": "contract_info_received",
    "Invoice File Name": "invoice_file_name",
    "Files Used For Inventory": "files_used",
    "Billing Name": "billing_name",
    "Service Address 1": "service_address_1",
    "Service Address 2": "service_address_2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Country": "country",
    "Carrier": "carrier_name",
    "Master Account": "master_account",
    "Carrier Account Number": "carrier_account_number",
    "Sub-Account Number": "sub_account_number_1",
    "Sub-Account Number 2": "sub_account_number_2",
    "BTN": "btn",
    "Phone Number": "phone_number",
    "Carrier Circuit Number": "carrier_circuit_number",
    "Additional Circuit IDs": "additional_circuit_ids",
    "Service Type": "service_type",
    "Service Type 2": "service_type_2",
    "USOC": "usoc",
    "Service or Component": "service_or_component",
    "Component or Feature Name": "component_or_feature_name",
    "Monthly Recurring Cost": "monthly_recurring_cost",
    "Quanity": "quantity",
    "Cost Per Unit": "cost_per_unit",
    "Currency": "currency",
    "Conversion Rate": "conversion_rate",
    "Monthly Recurring Cost per Currency": "mrc_per_currency",
    "Charge Type": "charge_type",
    "# Calls": "num_calls",
    "LD Minutes": "ld_minutes",
    "LD Cost": "ld_cost",
    "Rate": "rate",
    "LD Flat Rate": "ld_flat_rate",
    "Port Speed": "port_speed",
    "Access Speed": "access_speed",
    "Upload Speed": "upload_speed",
    "Poin to Number": "point_to_number",
    "Z Location Name If One Given By Carrier": "z_location_name",
    "Z Address 1": "z_address_1",
    "Z Address 2": "z_address_2",
    "Z City": "z_city",
    "Z State": "z_state",
    "Z Zip Code": "z_zip",
    "Z Country": "z_country",
    "*Contract - Term Months": "contract_term_months",
    "*Contract - Begin Date": "contract_begin_date",
    "*Contract - Expiration Date": "contract_expiration_date",
    "Billing Per Contract": "billing_per_contract",
    "*Currently Month-to-Month": "currently_month_to_month",
    "Month to Month or Less Than a Year Remaining": "mtm_or_less_than_year",
    "Contract File Name": "contract_file_name",
    "Contract Number": "contract_number",
    "2nd Contract Number": "contract_number_2",
    "*Auto Renew": "auto_renew",
    "Auto Renewal Notes and Removal Requirements": "auto_renewal_notes",
}

# Fields that are analyst judgment — we measure them but don't fix mismatches
ANALYST_FIELDS = {
    "status", "notes", "contract_info_received", "files_used",
    "billing_per_contract", "mtm_or_less_than_year", "contract_file_name",
}

# Field categories for reporting
STRUCTURED_FIELDS = {
    "carrier_account_number", "sub_account_number_1", "sub_account_number_2",
    "master_account", "btn", "phone_number", "carrier_circuit_number",
    "monthly_recurring_cost", "quantity", "cost_per_unit", "mrc_per_currency",
    "num_calls", "ld_minutes", "ld_cost", "rate", "ld_flat_rate", "zip", "z_zip",
}

CONTRACT_FIELDS = {
    "contract_term_months", "contract_begin_date", "contract_expiration_date",
    "currently_month_to_month", "contract_number", "contract_number_2",
    "auto_renew", "auto_renewal_notes", "conversion_rate",
}


def normalize_phone(value: str) -> str:
    """Normalize phone: strip non-digits, remove Excel .0 artifact."""
    s = str(value).replace(".0", "")
    return re.sub(r"[^0-9]", "", s)


def normalize_account(value: str) -> str:
    """Normalize account: strip to digits."""
    return re.sub(r"[^0-9]", "", str(value))


def normalize_amount(value) -> str:
    """Normalize monetary amount to 2 decimal places."""
    if value is None:
        return ""
    try:
        d = Decimal(str(value).replace("$", "").replace(",", "").strip())
        return str(d.quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return str(value).strip()


def normalize_str(value) -> str:
    """Basic string normalization: lowercase, strip whitespace."""
    if value is None:
        return ""
    s = str(value).strip()
    # Remove Excel .0 for numeric-looking strings
    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        s = s[:-2]
    return s.lower()


def normalize_date(value) -> str:
    """Normalize date to YYYY-MM-DD."""
    if value is None:
        return ""
    import datetime
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.date):
        return value.isoformat()
    s = str(value).strip()
    # Try parsing common formats
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]:
        try:
            return datetime.datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def compare_field(field_name: str, extracted_val, golden_val) -> str:
    """Compare a single field. Returns: CORRECT, WRONG, MISSING, EXTRA, BOTH_EMPTY."""
    e_str = normalize_str(extracted_val)
    g_str = normalize_str(golden_val)

    if not e_str and not g_str:
        return "BOTH_EMPTY"
    if not e_str and g_str:
        return "MISSING"
    if e_str and not g_str:
        return "EXTRA"

    # Field-specific comparison
    if field_name in {"phone_number", "btn", "point_to_number"}:
        e_norm = normalize_phone(extracted_val)
        g_norm = normalize_phone(golden_val)
        # Allow 7-digit match to last 7 of 10-digit
        if e_norm == g_norm:
            return "CORRECT"
        if len(e_norm) == 7 and len(g_norm) == 10 and g_norm.endswith(e_norm):
            return "CORRECT"
        if len(g_norm) == 7 and len(e_norm) == 10 and e_norm.endswith(g_norm):
            return "CORRECT"
        return "WRONG"

    if field_name in {"carrier_account_number", "master_account", "sub_account_number_1",
                       "sub_account_number_2", "contract_number", "contract_number_2"}:
        e_norm = normalize_account(extracted_val)
        g_norm = normalize_account(golden_val)
        if e_norm == g_norm:
            return "CORRECT"
        # Allow prefix/suffix match for account variations
        if e_norm in g_norm or g_norm in e_norm:
            return "CORRECT"
        return "WRONG"

    if field_name in {"monthly_recurring_cost", "cost_per_unit", "mrc_per_currency",
                       "ld_cost", "rate", "ld_flat_rate"}:
        e_amt = normalize_amount(extracted_val)
        g_amt = normalize_amount(golden_val)
        if e_amt == g_amt:
            return "CORRECT"
        try:
            if abs(Decimal(e_amt) - Decimal(g_amt)) <= Decimal("0.01"):
                return "CORRECT"
        except (InvalidOperation, ValueError):
            pass
        return "WRONG"

    if field_name in {"contract_begin_date", "contract_expiration_date"}:
        e_date = normalize_date(extracted_val)
        g_date = normalize_date(golden_val)
        if e_date == g_date:
            return "CORRECT"
        return "WRONG"

    if field_name in {"quantity", "contract_term_months", "num_calls"}:
        try:
            if int(float(str(extracted_val))) == int(float(str(golden_val))):
                return "CORRECT"
        except (ValueError, TypeError):
            pass
        return "WRONG"

    # Default: case-insensitive string match
    if e_str == g_str:
        return "CORRECT"

    # Fuzzy: check containment for service types, component names
    if e_str in g_str or g_str in e_str:
        return "PARTIAL"

    return "WRONG"


def load_golden_data(xlsx_path: str, carriers: set[str] | None = None) -> list[dict]:
    """Load golden data from Baseline sheet, return list of {field: value} dicts."""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["Baseline"]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    headers = rows[2]  # Row 3 is field names
    h_list = [str(h).strip() if h else "" for h in headers]

    golden_rows = []
    for row in rows[3:]:
        record = {}
        for i, h in enumerate(h_list):
            if h in GOLDEN_TO_SCHEMA and i < len(row):
                schema_field = GOLDEN_TO_SCHEMA[h]
                record[schema_field] = row[i]

        # Filter by carrier if specified
        if carriers:
            carrier = normalize_str(record.get("carrier_name", ""))
            if not any(c.lower() in carrier for c in carriers):
                continue

        golden_rows.append(record)

    return golden_rows


def load_extracted_data(json_path: str) -> list[dict]:
    """Load extracted data from pipeline output JSON."""
    with open(json_path) as f:
        data = json.load(f)

    if isinstance(data, dict):
        return data.get("merged_rows", data.get("rows", []))
    return data


def normalize_account_for_match(acct: str) -> str:
    """Normalize account for matching — strip to digits, truncate to 13 (AT&T check digit)."""
    digits = re.sub(r"[^0-9]", "", str(acct))
    # AT&T accounts: golden sometimes has 16 digits (with check digit suffix),
    # extracted has 13. Use first 13 as canonical.
    if len(digits) > 13:
        digits = digits[:13]
    return digits


def build_match_key(row: dict, is_golden: bool = False) -> str:
    """Build composite match key: account + phone + row_type + usoc (for C-rows only)."""
    acct = normalize_account_for_match(str(row.get("carrier_account_number", "")))
    phone = normalize_phone(str(row.get("phone_number", "") or row.get("btn", "") or ""))

    # Row type
    rt = normalize_str(row.get("service_or_component", "") or row.get("row_type", ""))
    if rt in ("s", "c"):
        rt = rt.upper()
    elif "t" in rt or "occ" in rt:
        rt = "T"  # Taxes/surcharges
    else:
        rt = "U"

    # For C-rows ONLY, add USOC to disambiguate (S-rows often don't have USOC in golden)
    usoc = ""
    if rt == "C":
        usoc = normalize_str(row.get("usoc", ""))

    # 7-digit phone: try to make it 10-digit using account prefix
    if len(phone) == 7 and len(acct) >= 3:
        phone = acct[:3] + phone

    return f"{acct}|{phone}|{rt}|{usoc}"


def run_comparison(golden_rows: list[dict], extracted_rows: list[dict]):
    """Compare extracted vs golden, print diagnostic report."""

    # Build indexes
    golden_by_key = defaultdict(list)
    for r in golden_rows:
        key = build_match_key(r, is_golden=True)
        golden_by_key[key].append(r)

    extracted_by_key = defaultdict(list)
    for r in extracted_rows:
        key = build_match_key(r)
        extracted_by_key[key].append(r)

    all_keys = set(golden_by_key.keys()) | set(extracted_by_key.keys())
    matched_keys = set(golden_by_key.keys()) & set(extracted_by_key.keys())
    golden_only = set(golden_by_key.keys()) - set(extracted_by_key.keys())
    extracted_only = set(extracted_by_key.keys()) - set(golden_by_key.keys())

    print(f"\n{'='*70}")
    print(f"CITY OF DUBLIN — EXTRACTION vs GOLDEN DIAGNOSTIC")
    print(f"{'='*70}")
    print(f"Golden rows:     {len(golden_rows)}")
    print(f"Extracted rows:  {len(extracted_rows)}")
    print(f"Unique keys:     {len(all_keys)}")
    print(f"Matched keys:    {len(matched_keys)}")
    print(f"Golden-only:     {len(golden_only)} (rows we missed)")
    print(f"Extracted-only:  {len(extracted_only)} (extra rows)")

    # Field-by-field comparison on matched rows
    field_scores = defaultdict(Counter)  # field → {CORRECT, WRONG, MISSING, ...}
    field_mismatches = defaultdict(list)  # field → [(extracted, golden), ...]

    compared_pairs = 0
    for key in matched_keys:
        g_rows = golden_by_key[key]
        e_rows = extracted_by_key[key]
        # Take first match from each
        g = g_rows[0]
        e = e_rows[0]
        compared_pairs += 1

        all_fields = set(GOLDEN_TO_SCHEMA.values())
        for field in all_fields:
            result = compare_field(field, e.get(field), g.get(field))
            field_scores[field][result] += 1
            if result in ("WRONG", "MISSING") and field not in ANALYST_FIELDS:
                field_mismatches[field].append({
                    "key": key,
                    "extracted": str(e.get(field, "")),
                    "golden": str(g.get(field, "")),
                    "result": result,
                })

    print(f"\nCompared pairs:  {compared_pairs}")

    # Category-level accuracy
    print(f"\n{'─'*70}")
    print("ACCURACY BY CATEGORY (extractable fields only)")
    print(f"{'─'*70}")

    categories = {
        "Structured": STRUCTURED_FIELDS - ANALYST_FIELDS,
        "Semi-structured": (set(GOLDEN_TO_SCHEMA.values()) - STRUCTURED_FIELDS
                           - CONTRACT_FIELDS - ANALYST_FIELDS
                           - {"service_or_component", "row_type"}),
        "Contract": CONTRACT_FIELDS - ANALYST_FIELDS,
        "Analyst (info only)": ANALYST_FIELDS,
    }

    for cat_name, cat_fields in categories.items():
        correct = sum(field_scores[f]["CORRECT"] + field_scores[f]["PARTIAL"] * 0.5
                      for f in cat_fields)
        total = sum(field_scores[f]["CORRECT"] + field_scores[f]["WRONG"]
                    + field_scores[f]["MISSING"] + field_scores[f]["PARTIAL"]
                    for f in cat_fields)
        both_empty = sum(field_scores[f]["BOTH_EMPTY"] for f in cat_fields)
        pct = (correct / total * 100) if total > 0 else 0
        marker = "  (reference only)" if cat_name == "Analyst (info only)" else ""
        print(f"  {cat_name:25s}  {pct:5.1f}%  ({int(correct)}/{total} scored, {both_empty} both-empty){marker}")

    # Per-field detail
    print(f"\n{'─'*70}")
    print("PER-FIELD SCORES (extractable fields, sorted by accuracy)")
    print(f"{'─'*70}")
    print(f"  {'Field':35s} {'Correct':>8s} {'Wrong':>8s} {'Missing':>8s} {'Extra':>8s} {'Acc%':>6s}")

    field_results = []
    for field in sorted(GOLDEN_TO_SCHEMA.values()):
        if field in ANALYST_FIELDS:
            continue
        scores = field_scores[field]
        correct = scores["CORRECT"] + scores.get("PARTIAL", 0)
        wrong = scores["WRONG"]
        missing = scores["MISSING"]
        extra = scores["EXTRA"]
        total = correct + wrong + missing
        pct = (correct / total * 100) if total > 0 else -1
        field_results.append((field, correct, wrong, missing, extra, pct))

    for field, correct, wrong, missing, extra, pct in sorted(field_results, key=lambda x: x[5]):
        if pct < 0:
            continue  # All empty
        flag = " <<<" if pct < 80 else ""
        print(f"  {field:35s} {int(correct):8d} {wrong:8d} {missing:8d} {extra:8d} {pct:5.1f}%{flag}")

    # Top mismatches
    print(f"\n{'─'*70}")
    print("TOP MISMATCHES (extractable fields, first 3 per field)")
    print(f"{'─'*70}")

    for field in sorted(field_mismatches.keys()):
        examples = field_mismatches[field][:3]
        if examples:
            print(f"\n  {field} ({len(field_mismatches[field])} mismatches):")
            for ex in examples:
                print(f"    {ex['result']:7s}  ours={repr(ex['extracted'][:50])}  golden={repr(ex['golden'][:50])}")

    # Unmatched golden rows — what are we missing?
    if golden_only:
        print(f"\n{'─'*70}")
        print(f"UNMATCHED GOLDEN ROWS ({len(golden_only)} keys)")
        print(f"{'─'*70}")
        sample = sorted(golden_only)[:15]
        for key in sample:
            g = golden_by_key[key][0]
            print(f"  {key}  carrier={g.get('carrier_name')}  svc={g.get('service_type')}")

    # Unmatched extracted rows
    if extracted_only:
        print(f"\n{'─'*70}")
        print(f"EXTRA EXTRACTED ROWS ({len(extracted_only)} keys)")
        print(f"{'─'*70}")
        sample = sorted(extracted_only)[:15]
        for key in sample:
            e = extracted_by_key[key][0]
            print(f"  {key}  carrier={e.get('carrier_name')}  svc={e.get('service_type')}")

    print(f"\n{'='*70}")
    print("END DIAGNOSTIC")
    print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(description="Compare extraction to golden data")
    parser.add_argument("--extracted", required=True, help="Path to extraction results JSON")
    parser.add_argument("--golden", default=None, help="Path to golden data Excel")
    parser.add_argument("--carriers", default="AT&T,Spectrum", help="Comma-separated carriers to compare")
    args = parser.parse_args()

    golden_path = args.golden or str(
        Path.home() / "Downloads" / "Input Packets"
        / "City of Dublin POC Inputs and Output"
        / "Digital Direction_City of Dublin_ Inventory File_01.20.2026_PRE FINAL INVENTORY-Techjays.xlsx"
    )

    carriers = set(c.strip() for c in args.carriers.split(","))

    print(f"Loading golden data from: {golden_path}")
    golden_rows = load_golden_data(golden_path, carriers)
    print(f"Loaded {len(golden_rows)} golden rows for carriers: {carriers}")

    print(f"Loading extracted data from: {args.extracted}")
    extracted_rows = load_extracted_data(args.extracted)
    print(f"Loaded {len(extracted_rows)} extracted rows")

    run_comparison(golden_rows, extracted_rows)


if __name__ == "__main__":
    main()
