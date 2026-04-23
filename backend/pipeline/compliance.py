"""Contract compliance checking — post-merge analysis.

Flags billing-vs-contract discrepancies on merged rows. Runs AFTER merge + validate
in the orchestrator. Does NOT modify the extraction pipeline.

Five checks:
1. rate_mismatch: invoice MRC vs contract rate
2. expired_contract: contract past expiration date
3. mtm_inconsistency: MTM flag inconsistent with contract dates
4. term_date_mismatch: begin + term_months != expiration
5. no_contract: active billing with zero contract fields
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from backend.models.schemas import ExtractedRow
from backend.utils.dates import is_expired, term_matches

logger = logging.getLogger(__name__)


@dataclass
class ComplianceFlag:
    check: str          # rate_mismatch, expired_contract, etc.
    severity: str       # error, warning, info
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class ComplianceResult:
    flags_by_row: dict[int, list[ComplianceFlag]] = field(default_factory=dict)
    summary: dict[str, int] = field(default_factory=dict)
    checked_at: str = ""


def check_compliance(
    merged_rows: list[ExtractedRow],
    today: date | None = None,
) -> ComplianceResult:
    """Run all compliance checks on merged rows.

    Args:
        merged_rows: post-merge, post-validate rows
        today: override for testing (default: date.today())

    Returns:
        ComplianceResult with per-row flags and summary counts.
    """
    today = today or date.today()
    result = ComplianceResult(checked_at=today.isoformat())

    for i, row in enumerate(merged_rows):
        flags = []
        flags.extend(_check_rate_mismatch(row))
        flags.extend(_check_expired_contract(row, today))
        flags.extend(_check_mtm_inconsistency(row, today))
        flags.extend(_check_term_date_mismatch(row))
        flags.extend(_check_no_contract(row))

        if flags:
            result.flags_by_row[i] = flags

    # Build summary
    all_flags = [f for flags in result.flags_by_row.values() for f in flags]
    for f in all_flags:
        key = f"{f.check}:{f.severity}"
        result.summary[key] = result.summary.get(key, 0) + 1

    total = len(all_flags)
    rows_flagged = len(result.flags_by_row)
    logger.info(f"Compliance: {total} flags on {rows_flagged}/{len(merged_rows)} rows "
                f"({result.summary})")

    return result


def _parse_dollar_amount(text: str) -> Decimal | None:
    """Extract a dollar amount from a string like '$95', '$95.00/mo', 'rate $13'.

    Returns None if no parseable amount found.
    """
    if not text:
        return None
    # Find dollar amounts: $95, $95.00, 95.00, etc.
    match = re.search(r'\$?([\d,]+(?:\.\d{1,2})?)', str(text))
    if match:
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
    return None


def _check_rate_mismatch(row: ExtractedRow) -> list[ComplianceFlag]:
    """Check: invoice MRC vs contract rate from billing_per_contract.

    The billing_per_contract field may contain analyst notes like:
    - "Yes" (billing matches)
    - "N/A" (no contract to compare)
    - "No(Underbilling-contract rate $13)" (contains a dollar amount)

    We parse the dollar amount from billing_per_contract and compare to MRC.
    If billing_per_contract has no parseable amount, we skip.
    """
    if not row.monthly_recurring_cost or not row.billing_per_contract:
        return []

    bpc = str(row.billing_per_contract).strip()

    # Skip known non-rate values
    if bpc.lower() in ("yes", "n/a", "na", "none", ""):
        return []
    if bpc.lower().startswith("n/a"):
        return []

    contract_rate = _parse_dollar_amount(bpc)
    if contract_rate is None:
        return []

    try:
        mrc = Decimal(str(row.monthly_recurring_cost))
    except (InvalidOperation, TypeError):
        return []

    if contract_rate == 0 or mrc == 0:
        return []

    diff = abs(mrc - contract_rate)
    if diff <= Decimal("0.50"):
        return []  # Within tolerance

    pct_diff = (diff / contract_rate * 100) if contract_rate > 0 else Decimal(0)

    if pct_diff > 10:
        severity = "error"
    elif pct_diff > 1:
        severity = "warning"
    else:
        return []

    return [ComplianceFlag(
        check="rate_mismatch",
        severity=severity,
        message=f"Invoice MRC ${mrc} vs contract rate ${contract_rate} "
                f"(diff ${diff}, {pct_diff:.1f}%)",
        details={
            "invoice_mrc": str(mrc),
            "contract_rate": str(contract_rate),
            "difference": str(diff),
            "pct_difference": str(round(pct_diff, 1)),
            "billing_per_contract_raw": bpc,
        },
    )]


def _check_expired_contract(row: ExtractedRow, today: date) -> list[ComplianceFlag]:
    """Check: contract expired but still billing.

    Downgrades to 'info' if MTM or auto-renew explains the continued billing.
    """
    if not row.contract_expiration_date:
        return []
    if not row.monthly_recurring_cost or row.monthly_recurring_cost <= 0:
        return []

    if not is_expired(row.contract_expiration_date, today):
        return []

    # Contract is expired and there's active billing — check mitigating factors
    mtm = str(row.currently_month_to_month or "").strip().lower()
    auto = str(row.auto_renew or "").strip().lower()

    if mtm == "yes" or auto == "yes":
        return [ComplianceFlag(
            check="expired_contract",
            severity="info",
            message=f"Contract expired {row.contract_expiration_date} but "
                    f"{'MTM' if mtm == 'yes' else 'auto-renew'} explains continued billing",
            details={
                "expiration_date": str(row.contract_expiration_date),
                "currently_mtm": mtm,
                "auto_renew": auto,
                "mrc": str(row.monthly_recurring_cost),
            },
        )]

    return [ComplianceFlag(
        check="expired_contract",
        severity="warning",
        message=f"Contract expired {row.contract_expiration_date}, "
                f"still billing ${row.monthly_recurring_cost}/mo "
                f"with no MTM or auto-renew flag",
        details={
            "expiration_date": str(row.contract_expiration_date),
            "currently_mtm": mtm,
            "auto_renew": auto,
            "mrc": str(row.monthly_recurring_cost),
        },
    )]


def _check_mtm_inconsistency(row: ExtractedRow, today: date) -> list[ComplianceFlag]:
    """Check: currently_month_to_month flag inconsistent with contract dates.

    MTM=Yes but contract is still active (not expired) → warning.
    """
    mtm = str(row.currently_month_to_month or "").strip().lower()
    if mtm != "yes":
        return []

    if not row.contract_expiration_date:
        return []  # No dates to compare against

    if not is_expired(row.contract_expiration_date, today):
        # MTM=Yes but contract hasn't expired yet
        return [ComplianceFlag(
            check="mtm_inconsistency",
            severity="warning",
            message=f"MTM=Yes but contract expires {row.contract_expiration_date} "
                    f"(still active)",
            details={
                "expiration_date": str(row.contract_expiration_date),
                "currently_mtm": "yes",
            },
        )]

    return []


def _check_term_date_mismatch(row: ExtractedRow) -> list[ComplianceFlag]:
    """Check: begin_date + term_months should approximately equal expiration_date.

    Allows 31 days tolerance for billing cycle alignment.
    """
    if not (row.contract_begin_date and row.contract_term_months
            and row.contract_expiration_date):
        return []

    if term_matches(
        row.contract_begin_date,
        row.contract_term_months,
        row.contract_expiration_date,
        tolerance_days=31,
    ):
        return []

    from dateutil.relativedelta import relativedelta
    expected = row.contract_begin_date + relativedelta(months=row.contract_term_months)
    delta_days = abs((expected - row.contract_expiration_date).days)

    return [ComplianceFlag(
        check="term_date_mismatch",
        severity="info",
        message=f"Begin {row.contract_begin_date} + {row.contract_term_months}mo "
                f"= {expected}, but expiration = {row.contract_expiration_date} "
                f"({delta_days} days off)",
        details={
            "begin_date": str(row.contract_begin_date),
            "term_months": row.contract_term_months,
            "expected_expiration": str(expected),
            "actual_expiration": str(row.contract_expiration_date),
            "delta_days": delta_days,
        },
    )]


def _check_no_contract(row: ExtractedRow) -> list[ComplianceFlag]:
    """Check: active billing but zero contract fields populated.

    Only flags rows with real charges (MRC > 0), not zero-cost included features.
    """
    if not row.monthly_recurring_cost or row.monthly_recurring_cost <= 0:
        return []

    contract_fields = [
        row.contract_term_months,
        row.contract_begin_date,
        row.contract_expiration_date,
        row.contract_number,
        row.currently_month_to_month,
        row.auto_renew,
    ]

    has_any_contract = any(f is not None for f in contract_fields)
    if has_any_contract:
        return []

    return [ComplianceFlag(
        check="no_contract",
        severity="info",
        message=f"Billing ${row.monthly_recurring_cost}/mo but no contract "
                f"fields populated",
        details={
            "mrc": str(row.monthly_recurring_cost),
            "phone": row.phone_number or "",
            "account": row.carrier_account_number or "",
        },
    )]


def flags_to_jsonb(flags: list[ComplianceFlag]) -> list[dict]:
    """Convert ComplianceFlags to JSON-serializable dicts for DB storage."""
    return [
        {
            "check": f.check,
            "severity": f.severity,
            "message": f.message,
            "details": f.details,
        }
        for f in flags
    ]
