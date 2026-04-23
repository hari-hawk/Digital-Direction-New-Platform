"""Match an LLM-extracted carrier name against the configured carrier registry.

Used after extraction to decide:
- Registered carrier → canonicalize to the registry's display name (e.g., "att"
  alias → "AT&T")
- LLM-detected but not in registry → keep the extracted string, flag the row
  with status="Validate carrier" so an analyst can confirm + register later
- No carrier name at all → neither — row stays empty and shows as "Unknown"
  on the project card

Kept deliberately small: no ML, no fuzzy, just substring containment + alias
lookup. The registry already covers 60+ carriers with hand-curated aliases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.config_loader import get_config_store


@dataclass(frozen=True)
class CarrierMatch:
    canonical_name: str      # what to display (registry display name OR the extracted string)
    is_registered: bool      # true when matched against a carrier.yaml entry
    carrier_key: str | None  # slug of matched config, when registered


def _normalize(s: str) -> str:
    """Lower-case, strip non-alphanumeric, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def match_carrier_name(extracted: str | None) -> CarrierMatch | None:
    """Return a CarrierMatch for an extracted carrier_name, or None if input is empty.

    Matching strategy (precedence order):
      1. Exact alias match (normalized).
      2. Extracted name contains a registry alias (handles "AT&T California" → AT&T).
      3. Registry alias contains extracted name (handles "NTT" → NTT Communications).
      4. No match → return CarrierMatch(extracted, is_registered=False, None).
    """
    if not extracted or not extracted.strip():
        return None

    ext = extracted.strip()
    ext_norm = _normalize(ext)
    if not ext_norm:
        return CarrierMatch(canonical_name=ext, is_registered=False, carrier_key=None)

    store = get_config_store()
    carriers = store.get_all_carriers()

    # Pass 1: exact normalized match on name or alias.
    for key, cfg in carriers.items():
        if _normalize(cfg.name) == ext_norm:
            return CarrierMatch(canonical_name=cfg.name, is_registered=True, carrier_key=key)
        for alias in cfg.aliases:
            if _normalize(alias) == ext_norm:
                return CarrierMatch(canonical_name=cfg.name, is_registered=True, carrier_key=key)

    # Pass 2: extracted CONTAINS a registered alias (prefer longest alias for specificity).
    best: tuple[int, str, str] | None = None  # (alias_len, canonical_name, key)
    for key, cfg in carriers.items():
        candidates = [cfg.name, *cfg.aliases]
        for c in candidates:
            cn = _normalize(c)
            if not cn or len(cn) < 3:
                continue
            if cn in ext_norm:
                if best is None or len(cn) > best[0]:
                    best = (len(cn), cfg.name, key)
    if best:
        return CarrierMatch(canonical_name=best[1], is_registered=True, carrier_key=best[2])

    # Pass 3: a registered alias contains the extracted name (e.g., "NTT" in "NTT Com").
    if len(ext_norm) >= 3:
        for key, cfg in carriers.items():
            candidates = [cfg.name, *cfg.aliases]
            for c in candidates:
                cn = _normalize(c)
                if cn and ext_norm in cn:
                    return CarrierMatch(canonical_name=cfg.name, is_registered=True, carrier_key=key)

    # Not in registry — keep the extracted name for analyst validation.
    return CarrierMatch(canonical_name=ext, is_registered=False, carrier_key=None)
