"""Map free-form extracted values to the customer's canonical dropdown values.

Sourced from configs/processing/column_dropdowns.yaml — the dropdowns the
customer ships in their Excel inventory templates. The LLM may emit close-
but-not-exact values ("Tax" vs "Taxes", "monthly" vs "Yes" for MTM, etc.);
this layer reconciles them so the final inventory column values match
the customer's vocabularies exactly.

Behavior:
  * Exact match (case-insensitive)         → canonical value, no flag
  * Synonym table match                    → canonical value, no flag
  * Best fuzzy match within tight ratio    → canonical value, info flag
  * No match                                → leave value as-is, warn flag

The normalizer is intentionally conservative: it only rewrites when the
mapping is unambiguous. Anything we can't map confidently is preserved
verbatim and surfaced to the analyst via a `_dropdown_warnings` list on
the row.
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from backend.settings import settings

logger = logging.getLogger(__name__)


_FUZZY_THRESHOLD = 0.92  # only auto-fix near-exact matches; below this, leave alone


@lru_cache(maxsize=1)
def _load_vocab() -> dict:
    """Load + cache the dropdown vocabularies. Returns the parsed YAML."""
    path = Path(settings.configs_dir) / "processing" / "column_dropdowns.yaml"
    if not path.exists():
        logger.warning("dropdown_normalizer: %s missing — normalization disabled", path)
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _normalize_one(field: str, value: object, vocab: dict, syn: dict) -> tuple[object, str | None]:
    """Map one (field, value) pair to canonical. Returns (new_value, warning_or_None)."""
    if value is None:
        return value, None
    s = str(value).strip()
    if not s:
        return value, None

    options = vocab.get(field) or []
    syns = syn.get(field) or {}
    if not options:
        return value, None  # field not constrained

    # 1. Exact case-insensitive match
    s_lower = s.lower()
    for opt in options:
        if str(opt).lower() == s_lower:
            return opt, None  # already canonical (modulo case)

    # 2. Synonym table
    if s_lower in {k.lower() for k in syns}:
        for k, canonical in syns.items():
            if k.lower() == s_lower:
                return canonical, None

    # 3. Fuzzy near-match within threshold
    best_opt = None
    best_score = 0.0
    for opt in options:
        score = SequenceMatcher(None, s_lower, str(opt).lower()).ratio()
        if score > best_score:
            best_score, best_opt = score, opt
    if best_opt is not None and best_score >= _FUZZY_THRESHOLD:
        return best_opt, f"{field}: '{s}' → '{best_opt}' (fuzzy match {best_score:.2f})"

    # 4. No confident mapping — leave as-is, warn
    return value, f"{field}: '{s}' is not in the dropdown vocabulary ({len(options)} valid values)"


def normalize_row(row: dict) -> list[str]:
    """Mutate `row` in place: snap dropdown-constrained fields to canonical
    values where unambiguous; return any warnings produced.

    Fields covered (any present in the YAML's top-level keys, except
    `synonyms`). Currently: service_type, service_type_2, status,
    charge_type, service_or_component, currently_month_to_month,
    mtm_or_less_than_year, auto_renew, contract_info_received.

    For service_type_2 we re-use the service_type vocabulary.
    """
    vocab = _load_vocab()
    if not vocab:
        return []
    syn = vocab.get("synonyms") or {}

    fields_to_check = [k for k in vocab.keys() if k != "synonyms"]
    # service_type_2 mirrors service_type's vocabulary
    if "service_type" in vocab and "service_type_2" not in vocab:
        fields_to_check.append("service_type_2")

    warnings: list[str] = []
    for field in fields_to_check:
        # Look up vocab + synonyms under the canonical key
        vocab_key = "service_type" if field == "service_type_2" else field
        local_vocab = {vocab_key: vocab.get(vocab_key, [])}
        local_syn = {vocab_key: syn.get(vocab_key, {})}
        new_val, warn = _normalize_one(vocab_key, row.get(field), local_vocab, local_syn)
        if new_val != row.get(field):
            row[field] = new_val
        if warn:
            warnings.append(warn)
    return warnings


def normalize_rows(rows: Iterable[dict]) -> dict[str, int]:
    """Apply normalize_row to every row. Returns counts:
        {"normalized": N, "warned": M}
    Adds row['_dropdown_warnings'] when a field couldn't be mapped.
    """
    n = 0
    w = 0
    for r in rows:
        warnings = normalize_row(r)
        if warnings:
            r.setdefault("_dropdown_warnings", []).extend(warnings)
            w += 1
        else:
            n += 1
    return {"normalized": n, "warned": w}
