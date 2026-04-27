"""Cumulative LLM spend ledger with hard cap + per-backend tallies.

File-backed atomic counters. Persists across backend restarts.
Check before each LLM call; record after each response.

File schema (backward-compatible):
{
  "total_usd": 12.34,
  "by_backend": {"vertex": 8.10, "aistudio": 4.24, "anthropic": 0.00}
}
Older files with only `total_usd` still load — `by_backend` defaults to {}.
"""

import json
import logging
import threading
from pathlib import Path

from backend.settings import settings

logger = logging.getLogger(__name__)

_LEDGER_PATH = Path(settings.data_dir) / ".spend_ledger.json"
_lock = threading.Lock()
# In-memory mirror; always re-loaded from disk to stay multi-process consistent.
_spent_usd: float = 0.0
_by_backend: dict[str, float] = {}


class SpendCapExceeded(RuntimeError):
    """Raised when a pending LLM call would take cumulative spend past the configured cap."""


def _load() -> tuple[float, dict[str, float]]:
    """Read ledger from disk. Returns (total, by_backend dict)."""
    global _spent_usd, _by_backend
    try:
        if _LEDGER_PATH.exists():
            data = json.loads(_LEDGER_PATH.read_text())
            _spent_usd = float(data.get("total_usd", 0.0))
            bb = data.get("by_backend", {})
            _by_backend = {k: float(v) for k, v in bb.items() if isinstance(v, (int, float))}
        else:
            _spent_usd = 0.0
            _by_backend = {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Spend ledger read failed ({e}); resetting to 0.")
        _spent_usd = 0.0
        _by_backend = {}
    return _spent_usd, _by_backend


def _persist(total: float, by_backend: dict[str, float]) -> None:
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LEDGER_PATH.write_text(json.dumps({
            "total_usd": round(total, 6),
            "by_backend": {k: round(v, 6) for k, v in by_backend.items()},
        }))
    except OSError as e:
        logger.warning(f"Spend ledger write failed: {e}")


def check_budget() -> None:
    """Raise SpendCapExceeded if cumulative spend is already over the configured cap."""
    cap = settings.max_spend_usd
    if cap <= 0:
        return
    with _lock:
        total, _ = _load()
    if total >= cap:
        raise SpendCapExceeded(
            f"LLM spend cap reached: ${total:.2f} >= ${cap:.2f}. "
            f"Raise MAX_SPEND_USD in .env to continue."
        )
    if total >= cap * settings.spend_warn_pct:
        logger.warning(f"LLM spend at ${total:.2f} of ${cap:.2f} cap ({total/cap:.0%}).")


def record(cost_usd: float, backend: str = "unknown") -> float:
    """Atomically add cost_usd to the ledger (overall + per-backend). Returns new total.

    `backend` is one of 'vertex' | 'aistudio' | 'anthropic' | 'unknown'.
    """
    if cost_usd <= 0:
        with _lock:
            t, _ = _load()
        return t
    with _lock:
        total, bb = _load()
        total += cost_usd
        bb[backend] = bb.get(backend, 0.0) + cost_usd
        global _spent_usd, _by_backend
        _spent_usd = total
        _by_backend = bb
        _persist(total, bb)
    return total


def current_total() -> float:
    with _lock:
        t, _ = _load()
        return t


def current_by_backend() -> dict[str, float]:
    """Return the per-backend spend tally as a dict."""
    with _lock:
        _, bb = _load()
        return dict(bb)


def reset() -> None:
    """Zero the ledger. Use only when starting a new billing period."""
    with _lock:
        global _spent_usd, _by_backend
        _spent_usd = 0.0
        _by_backend = {}
        _persist(0.0, {})
