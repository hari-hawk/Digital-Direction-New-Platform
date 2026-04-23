"""Validation + confidence scoring for extracted rows.

Cross-field checks, format validation, summary matching.
"""

import logging
import re
from decimal import Decimal

from backend.models.schemas import ExtractedRow, ConfidenceLevel, FIELD_CATEGORIES, FieldCategory

logger = logging.getLogger(__name__)


def validate_rows(
    rows: list[ExtractedRow],
    validation_data: dict | None = None,
) -> list[dict]:
    """Run all validation checks. Returns per-row validation results."""
    results = []

    for row in rows:
        issues = []

        # Cross-field: qty * unit_cost == MRC
        issues.extend(_check_mrc_math(row))

        # Format validation
        issues.extend(_check_phone_format(row))
        issues.extend(_check_zip_format(row))
        issues.extend(_check_date_logic(row))

        # Completeness
        issues.extend(_check_required_fields(row))

        results.append({
            "row_index": rows.index(row),
            "issues": issues,
            "valid": len([i for i in issues if i["severity"] == "error"]) == 0,
        })

    # Summary matching (Windstream Location Summary)
    if validation_data and "location_summary" in validation_data:
        summary_issues = _check_location_summary(rows, validation_data["location_summary"])
        if summary_issues:
            results.append({"summary_validation": summary_issues})

    return results


def _check_mrc_math(row: ExtractedRow) -> list[dict]:
    """Check: quantity * cost_per_unit should equal monthly_recurring_cost."""
    if row.quantity and row.cost_per_unit and row.monthly_recurring_cost:
        try:
            expected = float(row.quantity) * float(row.cost_per_unit)
            actual = float(row.monthly_recurring_cost)
            if abs(expected - actual) > 0.02:  # Allow 2 cent rounding
                return [{
                    "field": "monthly_recurring_cost",
                    "severity": "warning",
                    "message": f"qty({row.quantity}) × unit(${row.cost_per_unit}) = ${expected:.2f}, but MRC = ${actual:.2f}",
                }]
        except (ValueError, TypeError):
            pass
    return []


def _check_phone_format(row: ExtractedRow) -> list[dict]:
    """Validate phone number formats."""
    issues = []
    for field in ["phone_number", "btn", "point_to_number"]:
        val = getattr(row, field, None)
        if val and not re.match(r'^[\d\s\-().+]{7,20}$', str(val)):
            issues.append({
                "field": field,
                "severity": "warning",
                "message": f"Unusual phone format: {val}",
            })
    return issues


def _check_zip_format(row: ExtractedRow) -> list[dict]:
    """Validate zip code format (US)."""
    issues = []
    for field in ["zip", "z_zip"]:
        val = getattr(row, field, None)
        if val and not re.match(r'^\d{5}(-\d{4})?$', str(val)):
            issues.append({
                "field": field,
                "severity": "info",
                "message": f"Non-standard zip: {val}",
            })
    return issues


def _check_date_logic(row: ExtractedRow) -> list[dict]:
    """Check: begin_date < expiration_date. Equal dates are valid (month-to-month)."""
    if row.contract_begin_date and row.contract_expiration_date:
        if row.contract_begin_date == row.contract_expiration_date:
            return [{
                "field": "contract_expiration_date",
                "severity": "info",
                "message": f"Begin == Expiration ({row.contract_begin_date}) — likely month-to-month",
            }]
        if row.contract_begin_date > row.contract_expiration_date:
            return [{
                "field": "contract_expiration_date",
                "severity": "error",
                "message": f"Begin ({row.contract_begin_date}) > Expiration ({row.contract_expiration_date})",
            }]
    return []


def _check_required_fields(row: ExtractedRow) -> list[dict]:
    """Flag critical missing fields."""
    issues = []
    critical = ["carrier_name", "carrier_account_number"]
    for field in critical:
        if not getattr(row, field, None):
            issues.append({
                "field": field,
                "severity": "warning",
                "message": f"Required field missing: {field}",
            })
    return issues


def _check_location_summary(
    rows: list[ExtractedRow],
    summary: dict[str, float],
) -> list[dict]:
    """Validate extracted MRCs against Windstream Location Summary totals."""
    issues = []

    # Sum MRC per sub-account from extracted rows
    extracted_totals: dict[str, float] = {}
    for row in rows:
        acct = row.sub_account_number_1 or row.carrier_account_number
        if acct and row.monthly_recurring_cost:
            try:
                extracted_totals[acct] = extracted_totals.get(acct, 0) + float(row.monthly_recurring_cost)
            except (ValueError, TypeError):
                pass

    # Compare against summary
    for acct, expected_total in summary.items():
        extracted = extracted_totals.get(acct, 0)
        if abs(extracted - expected_total) > 1.00:  # Allow $1 rounding tolerance
            issues.append({
                "sub_account": acct,
                "severity": "warning",
                "message": f"MRC mismatch: extracted ${extracted:.2f} vs summary ${expected_total:.2f}",
                "delta": round(extracted - expected_total, 2),
            })

    matched = len(summary) - len(issues)
    logger.info(f"Location Summary validation: {matched}/{len(summary)} sub-accounts match")

    return issues
