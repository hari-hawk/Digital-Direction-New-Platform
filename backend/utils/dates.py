"""Shared date comparison helpers — used by validator and compliance."""

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


def is_expired(expiration_date: date, today: date | None = None) -> bool:
    """Check if a contract expiration date has passed."""
    today = today or date.today()
    return expiration_date < today


def term_matches(
    begin_date: date,
    term_months: int,
    expiration_date: date,
    tolerance_days: int = 31,
) -> bool:
    """Check if begin_date + term_months approximately equals expiration_date.

    Allows tolerance_days of drift to account for billing cycle alignment,
    month-length variation, and carrier rounding.
    """
    expected = begin_date + relativedelta(months=term_months)
    delta = abs((expected - expiration_date).days)
    return delta <= tolerance_days


def months_remaining(expiration_date: date, today: date | None = None) -> int:
    """Calculate months remaining on a contract. Negative if expired."""
    today = today or date.today()
    rd = relativedelta(expiration_date, today)
    return rd.years * 12 + rd.months
