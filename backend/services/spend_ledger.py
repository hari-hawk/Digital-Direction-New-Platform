"""Cumulative LLM spend ledger with hard cap.

File-backed atomic counter. Persists across backend restarts.
Check before each LLM call; record after each response.
"""

import json
import logging
import threading
from pathlib import Path

from backend.settings import settings

logger = logging.getLogger(__name__)

_LEDGER_PATH = Path(settings.data_dir) / ".spend_ledger.json"
_lock = threading.Lock()
_spent_usd: float | None = None


class SpendCapExceeded(RuntimeError):
    """Raised when a pending LLM call would take cumulative spend past the configured cap."""


def _load() -> float:
    """Read ledger from disk. Always re-reads so multi-process writes (CLI + backend) stay consistent."""
    global _spent_usd
    try:
        if _LEDGER_PATH.exists():
            data = json.loads(_LEDGER_PATH.read_text())
            _spent_usd = float(data.get("total_usd", 0.0))
        else:
            _spent_usd = 0.0
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Spend ledger read failed ({e}); resetting to 0.")
        _spent_usd = 0.0
    return _spent_usd


def _persist(total: float) -> None:
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LEDGER_PATH.write_text(json.dumps({"total_usd": round(total, 6)}))
    except OSError as e:
        logger.warning(f"Spend ledger write failed: {e}")


def check_budget() -> None:
    """Raise SpendCapExceeded if cumulative spend is already over the configured cap."""
    cap = settings.max_spend_usd
    if cap <= 0:
        return
    with _lock:
        total = _load()
    if total >= cap:
        raise SpendCapExceeded(
            f"LLM spend cap reached: ${total:.2f} >= ${cap:.2f}. "
            f"Raise MAX_SPEND_USD in .env to continue."
        )
    if total >= cap * settings.spend_warn_pct:
        logger.warning(f"LLM spend at ${total:.2f} of ${cap:.2f} cap ({total/cap:.0%}).")


def record(cost_usd: float) -> float:
    """Atomically add cost_usd to the ledger and persist. Returns new total."""
    if cost_usd <= 0:
        return _load() or 0.0
    with _lock:
        total = _load() + cost_usd
        global _spent_usd
        _spent_usd = total
        _persist(total)
    return total


def current_total() -> float:
    with _lock:
        return _load()


def reset() -> None:
    """Zero the ledger. Use only when starting a new billing period."""
    with _lock:
        global _spent_usd
        _spent_usd = 0.0
        _persist(0.0)
