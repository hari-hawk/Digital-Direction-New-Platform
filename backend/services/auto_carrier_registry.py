"""Auto-register newly-discovered carriers (Apr-2026).

When the LLM extracts rows for a carrier that's not yet in the registry,
write a minimal carrier.yaml so future uploads recognize the carrier
immediately — without any manual setup, PR, or restart.

Design:
* The registry already has 67 hand-curated carriers (4 with full prompts +
  domain knowledge, 63 with name + aliases). New carriers added here join
  the second tier — they extract via the generic prompts at
  `configs/processing/{doc_type}_extraction.md` (which already work for
  any carrier; the Verizon test extracted 460 rows this way).
* We only auto-register when match_carrier_name returns is_registered=False
  AND the candidate name passes a sanity filter (length, no obvious junk).
* Writes are best-effort — failures don't block extraction. Tracked via
  return value so callers can surface in the UI if needed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.config_loader import get_config_store, reset_config_store
from backend.services.carrier_match import match_carrier_name
from backend.settings import settings

logger = logging.getLogger(__name__)

# Sanity filter for candidates. The LLM occasionally hallucinates a row's
# carrier_name as a service or product label ("Voice", "Internet"). We reject
# anything that's too short, too long, contains digits-only, or matches
# obvious non-carrier tokens.
_BLOCKLIST = {
    "voice", "internet", "data", "mobility", "wireless", "wireline",
    "broadband", "fiber", "ethernet", "service", "services",
    "telecom", "telecommunications", "carrier", "vendor", "supplier",
    "monthly charges", "equipment charges", "taxes", "surcharges", "fees",
    "unknown", "n/a", "na", "none", "null",
}
_MIN_LEN = 3
_MAX_LEN = 64


def _slugify(name: str) -> str:
    """carrier name → safe directory slug, matching scripts/generate_carrier_registry.py."""
    s = name.lower().strip()
    s = re.sub(r"[&]+", "_and_", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _is_plausible_carrier_name(name: str) -> bool:
    """Reject obvious non-carrier strings before writing a carrier.yaml."""
    if not name or not name.strip():
        return False
    n = name.strip()
    if len(n) < _MIN_LEN or len(n) > _MAX_LEN:
        return False
    if n.lower() in _BLOCKLIST:
        return False
    # Reject digit-only or symbol-only strings.
    if not any(c.isalpha() for c in n):
        return False
    # Reject "carrier-like" service-detail strings (often have "$" or numbers).
    if "$" in n or re.search(r"\d{3,}", n):
        return False
    return True


def register_discovered_carrier(name: str) -> dict | None:
    """Auto-register `name` as a new carrier if it passes sanity checks AND
    isn't already in the registry.

    Returns:
        {"slug": ..., "name": ..., "path": ..., "created": True}  on creation
        None  if rejected (already registered, blocklisted, or invalid)
    """
    if not _is_plausible_carrier_name(name):
        logger.debug("auto-register: rejecting %r (failed sanity check)", name)
        return None

    # Re-check via the same matcher used downstream — if anything resolves,
    # we don't need a new entry.
    match = match_carrier_name(name)
    if match and match.is_registered:
        return None

    slug = _slugify(name)
    if not slug:
        return None

    carriers_dir = Path(settings.configs_dir) / "carriers" / slug
    yaml_path = carriers_dir / "carrier.yaml"
    if yaml_path.exists():
        # Already on disk but not yet loaded — config store reload below
        # will pick it up.
        reset_config_store()
        return None

    try:
        carriers_dir.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(
            f'name: "{name}"\n'
            f"aliases:\n"
            f"  - {name}\n"
            f"  - {name.lower()}\n"
            f"\n"
            f"# Auto-registered by backend/services/auto_carrier_registry.py.\n"
            f"# No filename_patterns — classifier falls back to content-based alias match.\n"
            f"# No prompts directory — extraction uses the generic prompts at\n"
            f"# configs/processing/{{doc_type}}_extraction.md.\n"
            f"# Add carrier-specific prompts here later if accuracy needs tuning.\n"
        )
        # Hot-reload the singleton so the next get_config_store() includes
        # this carrier without a backend restart.
        reset_config_store()
        get_config_store()
        logger.info("auto-register: created carrier %r at %s", name, yaml_path)
        return {"slug": slug, "name": name, "path": str(yaml_path), "created": True}
    except OSError as e:
        logger.warning("auto-register: failed to write %s: %s", yaml_path, e)
        return None


def auto_register_from_rows(rows: list[dict]) -> list[dict]:
    """Scan extracted rows for unregistered carrier names and register each.

    Returns a list of registration result dicts (one per newly-registered
    carrier, deduped). Empty list when nothing was added.
    """
    if not rows:
        return []

    seen: set[str] = set()
    candidates: list[str] = []
    for r in rows:
        raw = r.get("carrier_name") or r.get("carrier")
        if not raw:
            continue
        match = match_carrier_name(raw)
        if not match or match.is_registered:
            continue
        # Use the canonical_name (which equals the extracted name when not
        # registered) as the dedup key so "Verizon Wireless" and "Verizon"
        # don't both spawn separate folders if both are seen in one run.
        key = match.canonical_name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(match.canonical_name)

    created: list[dict] = []
    for name in candidates:
        result = register_discovered_carrier(name)
        if result:
            created.append(result)
    return created
