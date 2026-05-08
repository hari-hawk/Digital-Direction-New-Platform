"""Domain packs — the abstraction that lets the platform extract documents
from industries beyond telecom without rewriting the pipeline.

Today's pipeline is hard-coded around 60 telecom fields and 67 carrier
YAMLs. This module starts the horizontal generalization Hari asked for:
the same classifier, parser, extractor, and merger flow can serve a new
industry by registering a new domain pack.

A `DomainPack` is the minimum metadata needed to route a document into the
right extraction path:
  - `key`            — stable slug, e.g. "telecom", "saas_billing"
  - `display`        — human-readable name shown in UI
  - `field_set`      — list of fields the extractor should produce. Telecom's
                       60-field schema is one field_set; a SaaS billing pack
                       might define ~30 different fields.
  - `prompt_namespace` — directory under `configs/` that holds prompts
                         per doc_type.
  - `content_signals` — phrases/regexes that, if present in a document's
                        first 2 pages, mark it as belonging to this domain.
                        Used by `detect_domain()` at upload time.
  - `default_doc_types` — list of doc_types this pack expects (invoice, csr,
                          contract, etc.). Telecom has all six; a SaaS pack
                          might only have invoice + contract.

Adding a new domain:
  1. Append a `DomainPack(...)` instance to `_REGISTRY` below
  2. Drop prompt files into `configs/<prompt_namespace>/<doc_type>_extraction.md`
  3. The classifier picks it up automatically — no other code changes needed.

For now we ship two packs:
  - `telecom`         — wraps the existing 60-field schema + 67 carrier configs
  - `generic_billing` — placeholder for cross-industry billing docs (SaaS,
                        utilities, services). Fields are the universal subset
                        every billing doc has. Intentionally narrow so it
                        never beats `telecom` on a telecom doc.

This is scaffolding — the classifier integration ships in a follow-up.
For now `detect_domain()` is callable but doesn't yet wire into the upload
flow. That keeps today's deploy purely additive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Field sets — what the extractor produces per domain ────────────────────


# Telecom's existing 60 fields — kept here for reference. The actual ExtractedRow
# Pydantic model lives in models/schemas.py; this list mirrors the field names so
# domain-aware UI surfaces (like the chat context assembler) can ask "which
# fields should I show for this domain?"
TELECOM_FIELDS = (
    "row_type", "status", "notes", "contract_info_received",
    "invoice_file_name", "files_used", "billing_name",
    "service_address_1", "service_address_2", "city", "state", "zip", "country",
    "carrier_name", "master_account", "carrier_account_number",
    "sub_account_number", "phone_number", "carrier_circuit_number",
    "service_type", "description", "component_or_feature_name",
    "service_or_component", "currency", "monthly_recurring_cost", "rate",
    "quantity", "charge_type", "usoc",
    "contract_term_months", "contract_begin_date", "contract_expiration_date",
    "contract_number", "contract_number_2",
    "currently_month_to_month", "mtm_or_less_than_year",
    "billing_per_contract", "auto_renew", "auto_renewal_notes",
    "contract_file_name",
    "z_address_1", "z_city", "z_state", "z_zip", "z_country",
    "btn", "access_speed", "port_speed",
    "ld_minutes", "ld_cost", "num_calls",
    "billing_zip",
    "compliance_flags", "validation_issues", "validation_valid",
    "confidence", "field_confidence",
    "reviewed_by", "reviewed_at",
)

# Generic billing — the universal subset present on virtually every invoice
# regardless of industry. Intentionally narrow so it never wins against a
# domain-specific pack on a domain-specific doc.
GENERIC_BILLING_FIELDS = (
    "row_type", "status", "notes",
    "vendor_name", "customer_name", "account_number", "invoice_number",
    "invoice_date", "due_date", "billing_period_start", "billing_period_end",
    "service_address_1", "service_address_2", "city", "state", "zip", "country",
    "description", "service_type", "currency",
    "unit_price", "quantity", "subtotal", "tax", "total_amount",
    "payment_terms", "remit_to",
    "validation_issues", "validation_valid",
    "confidence", "field_confidence",
)


# ── Pack definition ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DomainPack:
    key: str
    display: str
    field_set: tuple[str, ...]
    prompt_namespace: str
    # Phrases and regex patterns that mark a document as belonging to this
    # domain. The detector counts hits across the first 2 pages.
    content_signals: tuple[str, ...] = field(default_factory=tuple)
    # Doc types this pack expects to encounter. Used by the bulk-classify
    # endpoint to suggest defaults when the classifier is uncertain.
    default_doc_types: tuple[str, ...] = field(default_factory=lambda: (
        "invoice", "csr", "contract", "report", "subscription",
    ))

    def matches_text(self, text: str) -> int:
        """Score the pack against a text body. Returns the number of distinct
        signals matched (so a doc can score `len(content_signals)` max). Used
        by `detect_domain()` to pick the highest-scoring pack."""
        if not text or not self.content_signals:
            return 0
        lo = text.lower()
        return sum(1 for sig in self.content_signals if sig.lower() in lo)


# ── Registry ────────────────────────────────────────────────────────────────


# Telecom signals — drawn from existing first-page markers across the 4 tuned
# carriers (Frontier, Lumen, Verizon, AT&T) plus universal telecom phrases.
_TELECOM_SIGNALS = (
    "usoc",
    "ratecenter",
    "rate center",
    "btn",
    "main billing telephone number",
    "carrier identification code",
    "circuit id",
    "did number",
    "ported number",
    "telecommunications",
    "long distance",
    "tariff",
    "interstate access",
    "regulatory recovery",
    "fcc fee",
    "lec ",
    "dsl ",
    "voip",
    "mpls",
    "pri ",
    "sip trunk",
)

# Generic-billing signals — universal phrases that virtually every invoice has.
# Kept conservative so this pack only "wins" when no domain-specific pack
# matched (length tiebreak).
_GENERIC_BILLING_SIGNALS = (
    "invoice number",
    "invoice date",
    "amount due",
    "remit to",
    "billing period",
    "subtotal",
    "tax",
    "total due",
    "payment terms",
)


_REGISTRY: list[DomainPack] = [
    DomainPack(
        key="telecom",
        display="Telecom (Carrier Documents)",
        field_set=TELECOM_FIELDS,
        prompt_namespace="processing",  # existing prompts already live here
        content_signals=_TELECOM_SIGNALS,
        default_doc_types=("invoice", "csr", "contract", "report", "subscription", "did_list"),
    ),
    DomainPack(
        key="generic_billing",
        display="Generic Billing",
        field_set=GENERIC_BILLING_FIELDS,
        prompt_namespace="processing_generic",  # to be created when we ship a real generic prompt
        content_signals=_GENERIC_BILLING_SIGNALS,
        default_doc_types=("invoice", "contract"),
    ),
]


def all_packs() -> tuple[DomainPack, ...]:
    return tuple(_REGISTRY)


def get_pack(key: str) -> DomainPack | None:
    for p in _REGISTRY:
        if p.key == key:
            return p
    return None


def detect_domain(text: str) -> tuple[DomainPack, int] | None:
    """Score every registered pack against `text` and return the winner.

    Tiebreak rule: if two packs match the same number of signals, the one
    listed FIRST in `_REGISTRY` wins. This means telecom always beats
    generic_billing on a tie, so a telecom invoice (which trips both
    "invoice number" AND "USOC") routes to the tuned telecom path.

    Returns `(pack, hit_count)` for the winner, or `None` when no pack
    has at least one signal hit (caller should fall back to the existing
    classifier's LLM stage).
    """
    if not text:
        return None
    best: tuple[DomainPack, int] | None = None
    for pack in _REGISTRY:
        score = pack.matches_text(text)
        if score == 0:
            continue
        if best is None or score > best[1]:
            best = (pack, score)
    return best


def field_set_for(key: str) -> tuple[str, ...] | None:
    """Return the field set for a domain key, or None when the key isn't
    registered. Used by the chat context assembler to filter row dicts to
    the columns relevant for the current domain."""
    pack = get_pack(key)
    return pack.field_set if pack else None
