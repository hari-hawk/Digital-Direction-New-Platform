"""Cross-document merge — combines extractions from multiple documents for one account.

Strategy (from eng review OV-4):
1. Rule-based first pass: join on account_number + phone_number, apply priority matrix
2. Same-doc-type conflicts resolved by completeness heuristic (pick fuller row)
3. LLM (Claude) for remaining true cross-doc conflicts
4. Source attribution per field
"""

import asyncio
import json
import logging
import re
from collections import defaultdict
from typing import Optional

from backend.config_loader import (
    get_config_store,
    AccountNormalizationConfig,
    PhoneNormalizationConfig,
    MergeRulesConfig,
)
from backend.models.schemas import ExtractedRow, ConfidenceLevel

logger = logging.getLogger(__name__)


# ============================================
# Normalization — config-driven, no carrier logic in code
# ============================================


def _normalize_for_key(value: str) -> str:
    """Normalize a value for merge key matching — strips non-digit chars."""
    return re.sub(r'[^0-9]', '', value)


def _normalize_account(
    value: str,
    acct_config: AccountNormalizationConfig | None = None,
) -> str:
    """Normalize account number using carrier-specific rules.

    Rules are loaded from carrier.yaml merge_rules.account_normalization.
    When no config is provided, strips to digits only with no truncation (safe default).
    """
    if acct_config is None:
        acct_config = AccountNormalizationConfig()

    if acct_config.char_class == "alphanumeric":
        normalized = re.sub(r'[^a-zA-Z0-9]', '', value)
    else:  # "digits" (default)
        normalized = re.sub(r'[^0-9]', '', value)

    # Check digit truncation: if length is canonical+1 and a check digit position is configured
    if (acct_config.canonical_length
            and acct_config.check_digit_position
            and len(normalized) == acct_config.canonical_length + 1):
        if acct_config.check_digit_position == "trailing":
            normalized = normalized[:acct_config.canonical_length]

    return normalized


# ============================================
# Priority matrix — defaults with per-carrier overrides
# ============================================

# Default priority matrix: which document type wins for each field group
# Higher number = higher priority. Carriers can override via merge_rules.field_priority_overrides.
FIELD_PRIORITIES = {
    # Amounts — invoice is the source of truth (what the client actually pays).
    # Subscription/portal exports may show contracted base rates that exclude
    # surcharges. Invoice includes all regulatory fees, so it reflects the true
    # billed cost.
    "monthly_recurring_cost": {"invoice": 10, "subscription": 6, "csr": 5, "contract": 7, "report": 6},
    "cost_per_unit": {"invoice": 10, "subscription": 6, "csr": 5, "contract": 7},
    "quantity": {"invoice": 10, "subscription": 8, "csr": 7, "contract": 8},

    # Phone numbers come from CSRs
    "phone_number": {"csr": 10, "invoice": 7, "did_list": 9},
    "btn": {"csr": 10, "invoice": 7},

    # Service details — subscription portals often have the fullest names
    "usoc": {"csr": 10, "invoice": 3, "subscription": 4},
    "service_type": {"csr": 9, "invoice": 7, "subscription": 8, "contract": 6},
    "component_or_feature_name": {"csr": 9, "invoice": 7, "subscription": 8},

    # Addresses — CSR primary, contract secondary, invoice tertiary.
    # CSR lists per-service locations; invoices typically show the billing/mailing
    # address (corporate HQ), which is not the service address. Contract usually
    # includes the site list as scheduled locations.
    "billing_name":      {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},
    "service_address_1": {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},
    "service_address_2": {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},
    "city":              {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},
    "state":             {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},
    "zip":               {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},
    "country":           {"csr": 10, "contract": 8, "invoice": 6, "subscription": 5},

    # Contract fields — contract primary, CSR secondary, email tertiary
    "contract_term_months": {"contract": 10, "csr": 8, "subscription": 7, "email": 6},
    "contract_begin_date": {"contract": 10, "csr": 8, "subscription": 7, "email": 6},
    "contract_expiration_date": {"contract": 10, "csr": 8, "subscription": 7, "email": 6},
    "auto_renew": {"contract": 10, "csr": 7, "email": 6},
    "contract_number": {"contract": 10, "csr": 5},

    # Circuit info
    "carrier_circuit_number": {"csr": 10, "invoice": 8, "report": 7},
    "access_speed": {"csr": 9, "invoice": 8, "contract": 7},
    "port_speed": {"csr": 9, "invoice": 8},
}


def get_field_priority(
    field_name: str,
    doc_type: str,
    merge_rules: MergeRulesConfig | None = None,
) -> int:
    """Get priority score for a field from a specific document type."""
    # Start with default
    priorities = FIELD_PRIORITIES.get(field_name, {})
    score = priorities.get(doc_type, 5)

    # Apply carrier-specific override if present
    if merge_rules and field_name in merge_rules.field_priority_overrides:
        override = merge_rules.field_priority_overrides[field_name].get(doc_type)
        if override is not None:
            score = override

    return score


# ============================================
# Merge key construction — config-driven
# ============================================


def _build_merge_key(
    row: ExtractedRow,
    doc_id: str,
    merge_rules: MergeRulesConfig | None = None,
) -> str:
    """Build a normalized merge key for same-doc-type matching.

    Normalizes account and phone numbers using carrier-specific rules so that
    format differences between doc types don't prevent matching.

    Key structure: account|sub_account|phone
    Sub-account is included so rows from different sub-accounts within the
    same master account group separately (e.g., Windstream Enterprise bills
    with 90+ sub-accounts under one master). Without it, all sub-accounts
    collapse into one merge group and C-row dedup deletes valid rows.
    Cross-doc matching (invoice↔CSR) uses tiered enrichment separately.
    """
    if merge_rules is None:
        merge_rules = MergeRulesConfig()
    acct_config = merge_rules.account_normalization
    phone_config = merge_rules.phone_normalization

    key_parts = []
    if row.carrier_account_number:
        key_parts.append(_normalize_account(row.carrier_account_number, acct_config))

    # Include sub_account_number_1 for sub-account-level grouping.
    # This ensures rows from different sub-accounts under the same master
    # don't get incorrectly deduped against each other.
    # Disabled via merge_rules.sub_account_in_merge_key=False when invoice and
    # subscription use incompatible sub-account schemes — in that case, service
    # identity matching handles row-level pairing within the account group.
    if row.sub_account_number_1 and merge_rules.sub_account_in_merge_key:
        key_parts.append(_normalize_account(row.sub_account_number_1, acct_config))

    if row.phone_number:
        normalized_phone = _normalize_for_key(row.phone_number)
        # Config-driven phone padding for short (7-digit) numbers
        if (phone_config.pad_short_phones
                and phone_config.pad_source == "account_prefix"
                and len(normalized_phone) == 7):
            acct_digits = _normalize_for_key(row.carrier_account_number or "")
            if len(acct_digits) >= phone_config.pad_digits:
                normalized_phone = acct_digits[:phone_config.pad_digits] + normalized_phone
        key_parts.append(normalized_phone)
    elif row.btn:
        key_parts.append(_normalize_for_key(row.btn))

    return "|".join(key_parts) if key_parts else f"unkeyed_{doc_id}_{id(row)}"


# ============================================
# Value equivalence — config-driven normalization
# ============================================


def _values_equivalent(
    field_name: str,
    val_a,
    val_b,
    acct_config: AccountNormalizationConfig | None = None,
) -> bool:
    """Check if two field values are equivalent despite formatting differences.

    Uses carrier-specific normalization for account numbers and phones.
    """
    if field_name in ("carrier_account_number", "sub_account_number_1",
                      "sub_account_number_2", "master_account"):
        return _normalize_account(str(val_a), acct_config) == _normalize_account(str(val_b), acct_config)

    if field_name in ("phone_number", "btn", "point_to_number"):
        return _normalize_for_key(str(val_a)) == _normalize_for_key(str(val_b))

    # Carrier name: case-insensitive, strip common variations
    if field_name == "carrier_name":
        a = re.sub(r'[^a-zA-Z0-9]', '', str(val_a)).upper()
        b = re.sub(r'[^a-zA-Z0-9]', '', str(val_b)).upper()
        return a == b

    return False


# ============================================
# Document type priority — defaults with overrides
# ============================================


def _doc_type_base_priority(
    doc_type: str,
    merge_rules: MergeRulesConfig | None = None,
) -> int:
    """Base priority for document types (used for initial sort)."""
    defaults = {
        "invoice": 10,
        "csr": 8,
        "contract": 7,
        "report": 6,
        "did_list": 5,
        "subscription": 5,
        "email": 3,
        "service_guide": 2,
    }
    score = defaults.get(doc_type, 1)

    if merge_rules and doc_type in merge_rules.doc_type_priority_overrides:
        score = merge_rules.doc_type_priority_overrides[doc_type]

    return score


# ============================================
# Core merge logic
# ============================================


def rule_based_merge(
    extractions: dict[str, list[ExtractedRow]],
    doc_types: dict[str, str],
    carrier: str | None = None,
) -> list[ExtractedRow]:
    """Merge rows from multiple documents using priority rules.

    Args:
        extractions: {document_id: [rows]} — rows from each document
        doc_types: {document_id: doc_type} — "invoice", "csr", "contract", etc.
        carrier: carrier name (e.g. "att") — loads carrier-specific merge rules

    Returns:
        Merged rows with source attribution.
    """
    if not extractions:
        return []

    # Load carrier-specific merge rules (or safe defaults)
    merge_rules = get_config_store().get_merge_rules(carrier) if carrier else MergeRulesConfig()

    # Group rows by account + phone number (merge key)
    groups: dict[str, list[tuple[str, str, ExtractedRow]]] = defaultdict(list)

    for doc_id, rows in extractions.items():
        doc_type = doc_types.get(doc_id, "unknown")
        for row in rows:
            key = _build_merge_key(row, doc_id, merge_rules)
            groups[key].append((doc_id, doc_type, row))

    # Merge each group
    merged_rows = []
    conflicts = []

    for key, group_rows in groups.items():
        if len(group_rows) == 1:
            # Single source — no merge needed
            _, _, row = group_rows[0]
            merged_rows.append(row)
        else:
            # Multiple sources — apply priority merge
            merged, group_conflicts = _merge_group(group_rows, merge_rules)
            merged_rows.extend(merged)
            conflicts.extend(group_conflicts)

    if conflicts:
        logger.warning(f"Merge produced {len(conflicts)} true cross-doc conflicts for LLM resolution")

    # ── Post-merge: propagate account-level fields ──
    # Fields like billing_name, address, city, state, zip, country, carrier_name
    # are the same for all rows in an account group. If ANY merged row has them,
    # fill them in on rows that don't (e.g., CSR rows missing invoice fields).
    merged_rows = _propagate_account_fields(merged_rows, merge_rules)

    logger.info(f"Rule-based merge: {sum(len(r) for r in extractions.values())} input rows "
                f"→ {len(merged_rows)} merged rows, {len(conflicts)} cross-doc conflicts")

    return merged_rows, conflicts


# Fields that are the same across all rows in an account — safe to propagate
_ACCOUNT_LEVEL_FIELDS = [
    "billing_name", "service_address_1", "service_address_2",
    "city", "state", "zip", "country",
    "carrier_name", "master_account", "carrier_account_number",
    "invoice_file_name", "currency", "charge_type",
]


def _propagate_account_fields(
    rows: list[ExtractedRow],
    merge_rules: MergeRulesConfig,
) -> list[ExtractedRow]:
    """Fill account-level fields from any row that has them to rows that don't.

    Groups rows by normalized account number. Within each group, finds the
    best value for each account-level field (from the row with the most
    populated fields), then fills gaps on other rows.
    """
    acct_config = merge_rules.account_normalization

    # Group by normalized account
    acct_groups: dict[str, list[ExtractedRow]] = defaultdict(list)
    for row in rows:
        acct = _normalize_account(row.carrier_account_number or "", acct_config)
        acct_groups[acct].append(row)

    filled_count = 0
    # Exclude fields that should NOT propagate at account level (multi-location carriers)
    non_propagating = set(merge_rules.non_propagating_fields) if merge_rules else set()
    # Include carrier-specific extra propagation fields (e.g., AT&T contract dates)
    all_fields = list(_ACCOUNT_LEVEL_FIELDS) + list(merge_rules.extra_propagation_fields)
    propagatable = [f for f in all_fields if f not in non_propagating]
    for acct, group in acct_groups.items():
        if len(group) <= 1:
            continue

        # Collect best value for each account-level field
        best_values: dict[str, str] = {}
        for field in propagatable:
            for row in group:
                val = getattr(row, field, None)
                if val is not None and str(val).strip():
                    # Take the first non-empty value found (rows are already
                    # priority-sorted from merge, so higher-priority sources come first)
                    best_values[field] = val
                    break

        # Propagate to rows missing the field
        for row in group:
            for field, value in best_values.items():
                current = getattr(row, field, None)
                if current is None or str(current).strip() == "":
                    setattr(row, field, value)
                    filled_count += 1

    if filled_count:
        logger.info(f"Account-level propagation: filled {filled_count} fields "
                    f"across {len(acct_groups)} account groups")

    return rows


# ============================================
# LLM Conflict Resolution (Claude)
# ============================================


_CONFLICT_RESOLUTION_PROMPT = """\
You are resolving data conflicts from a telecom document extraction pipeline.
Two documents extracted different values for the same field on the same account/phone.

For each conflict, pick the more likely correct value based on:
- Which document type typically has that field (e.g., invoices have amounts, CSRs have phone details)
- Data quality signals (more specific > more generic, complete > truncated)
- If both seem equally valid, pick "a" (the higher-priority source)

Return a JSON array with one object per conflict:
[
  {"conflict_id": 0, "pick": "a" or "b", "reason": "brief reason"}
]

CONFLICTS:
"""


async def llm_resolve_conflicts(
    merged_rows: list[ExtractedRow],
    conflicts: list[dict],
    carrier: str | None = None,
) -> list[ExtractedRow]:
    """Resolve true cross-doc conflicts using Claude.

    Batches all conflicts into a single prompt for efficiency.
    Applies resolutions back to the merged rows.
    """
    if not conflicts:
        return merged_rows

    from backend.services.llm import get_claude

    # Build conflict descriptions
    conflict_lines = []
    for i, c in enumerate(conflicts):
        conflict_lines.append(
            f"Conflict {i}: field={c['field']}, "
            f"source_a={c['source_a']}({c.get('doc_a', '?')}): {c['value_a']!r}, "
            f"source_b={c['source_b']}({c.get('doc_b', '?')}): {c['value_b']!r}"
        )

    prompt = _CONFLICT_RESOLUTION_PROMPT + "\n".join(conflict_lines)

    try:
        claude = get_claude()
        response = await claude.call(
            prompt=prompt,
            system=f"Telecom data extraction conflict resolver. Carrier: {carrier or 'unknown'}.",
        )

        # Parse response
        resolutions = json.loads(response.content)
        if not isinstance(resolutions, list):
            resolutions = [resolutions]

        # Build resolution map
        resolution_map = {}
        for r in resolutions:
            cid = r.get("conflict_id")
            pick = r.get("pick", "a")
            reason = r.get("reason", "")
            if cid is not None and 0 <= cid < len(conflicts):
                resolution_map[cid] = (pick, reason)

        # Apply resolutions — find the merged row for each conflict and update the field
        applied = 0
        for cid, (pick, reason) in resolution_map.items():
            conflict = conflicts[cid]
            field = conflict["field"]
            new_value = conflict["value_b"] if pick == "b" else conflict["value_a"]
            merge_key = conflict.get("merge_key", "")

            # Find the merged row this conflict belongs to (match by account)
            for row in merged_rows:
                acct = row.carrier_account_number or ""
                if merge_key and merge_key in acct or acct in merge_key:
                    if hasattr(row, field):
                        setattr(row, field, new_value)
                        applied += 1
                        break

        logger.info(f"LLM conflict resolution: {len(conflicts)} conflicts, "
                    f"{len(resolution_map)} resolved by Claude, {applied} applied")

        for cid, (pick, reason) in resolution_map.items():
            c = conflicts[cid]
            chosen = c["value_b"] if pick == "b" else c["value_a"]
            logger.debug(f"  Conflict {cid} ({c['field']}): picked {pick} = {chosen!r} — {reason}")

    except Exception as e:
        logger.error(f"LLM conflict resolution failed: {e}. Keeping rule-based results.")

    return merged_rows


def _normalize_service_name(name: str) -> str:
    """Normalize a service/component name to its core identity for merge matching.

    Strips decorations that don't change WHAT the service is:
    - Quantity suffixes: "- Qty 15", "- Qty 2", "Qty: 10"
    - Trailing whitespace, punctuation
    - Case folding

    Does NOT strip location names or service qualifiers — those distinguish
    different services (e.g., "Channel Fee - SDC" vs "Channel Fee - Tops 108").
    """
    if not name:
        return ""
    s = name.strip().lower()
    # Strip "- Qty N" or "(Qty N)" or "Qty: N" suffixes
    s = re.sub(r'\s*[-–]\s*qty\s*:?\s*\d+\s*$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*\(\s*qty\s*:?\s*\d+\s*\)\s*$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*qty\s*:?\s*\d+\s*$', '', s, flags=re.IGNORECASE)
    # Normalize whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _service_identity_match(row_a: ExtractedRow, row_b: ExtractedRow) -> bool:
    """Determine if two rows describe the same service.

    This is the core merge identity question: "are these the same line item
    appearing in two different documents?" Used for S-row dedup within the
    same account/phone group.

    Returns True if matched by any of these signals (strongest first):
    1. USOC match — when both rows have a USOC code and they're equal
    2. Normalized component match — same name after stripping decorations
    3. Containment match — one name is a prefix/substring of the other

    All signals require service_type compatibility (both empty, or equal).
    """
    # Service type gate: if both have service_type and they differ, not same service
    st_a = (row_a.service_type or "").strip().lower()
    st_b = (row_b.service_type or "").strip().lower()
    if st_a and st_b and st_a != st_b:
        return False

    # Signal 1: USOC match (most reliable — carrier-assigned code)
    usoc_a = (row_a.usoc or "").strip().lower()
    usoc_b = (row_b.usoc or "").strip().lower()
    if usoc_a and usoc_b:
        return usoc_a == usoc_b

    # Signal 2: Normalized component name match
    comp_a = _normalize_service_name(row_a.component_or_feature_name or "")
    comp_b = _normalize_service_name(row_b.component_or_feature_name or "")

    if not comp_a or not comp_b:
        return False

    if comp_a == comp_b:
        return True

    # Signal 3: Containment match — one is a prefix of the other.
    # "Monthly Channel Fee - SDC" matches "Monthly Channel Fee - SDC - extra detail"
    # but "Monthly Channel Fee" does NOT match "Monthly Channel Fee - SDC"
    # (too generic — would false-match different locations).
    # Require the shorter string to be at least 60% of the longer to avoid
    # overly generic matches.
    shorter, longer = (comp_a, comp_b) if len(comp_a) <= len(comp_b) else (comp_b, comp_a)
    if len(shorter) >= len(longer) * 0.6 and longer.startswith(shorter):
        return True

    return False


def _merge_group(
    group_rows: list[tuple[str, str, ExtractedRow]],
    merge_rules: MergeRulesConfig,
) -> tuple[list[ExtractedRow], list[dict]]:
    """Merge rows from the same account/phone from different documents.

    Service identity determines which rows describe the same line item:
    - S rows: matched by USOC, normalized component name, or containment
    - C rows: matched by USOC + component name

    When two documents describe the same service, the priority matrix decides
    which field values win. Invoice always wins for amounts because the invoice
    is what the client actually pays (billed rate includes surcharges that
    portal exports or contracts may exclude).

    Windstream Enterprise invoices have 3-5 S rows per sub-account (one per
    service group: UCaaS, SDWAN, Ethernet, Data). These have distinct component
    names so they're correctly kept separate.
    """
    conflicts = []

    # Separate by row type
    s_rows = [(did, dt, r) for did, dt, r in group_rows if r.row_type == "S"]
    c_rows = [(did, dt, r) for did, dt, r in group_rows if r.row_type == "C"]
    other = [(did, dt, r) for did, dt, r in group_rows if r.row_type not in ("S", "C")]

    merged = []

    # S rows — group by service identity using multi-signal matching.
    # Within the same account/phone group, two S rows from different documents
    # represent the same service if _service_identity_match returns True.
    if s_rows:
        s_groups: list[list[tuple[str, str, ExtractedRow]]] = []

        for did, dt, r in s_rows:
            placed = False
            for group in s_groups:
                # Check against the first row in the group (representative)
                _, _, rep = group[0]
                if _service_identity_match(r, rep):
                    group.append((did, dt, r))
                    placed = True
                    break
            if not placed:
                s_groups.append([(did, dt, r)])

        for s_group in s_groups:
            if len(s_group) == 1:
                merged.append(s_group[0][2])
            else:
                # Multiple docs have the same service — merge fields
                base_row, row_conflicts = _merge_fields(s_group, merge_rules)
                merged.append(base_row)
                conflicts.extend(row_conflicts)

    # C rows — dedup by component identity (usoc + name), merge fields across docs.
    # Does NOT include MRC in the key: same component from report ($30) and
    # invoice ($0 bundled) should merge, with priority matrix picking the MRC.
    if c_rows:
        c_groups: dict[tuple, list] = {}
        for did, dt, r in c_rows:
            c_key = (
                str(r.usoc or "").strip().lower(),
                _normalize_service_name(r.component_or_feature_name or ""),
            )
            if c_key not in c_groups:
                c_groups[c_key] = []
            c_groups[c_key].append((did, dt, r))

        for c_key, c_group in c_groups.items():
            if len(c_group) == 1:
                merged.append(c_group[0][2])
            else:
                # Multiple docs have the same C row — merge fields via priority
                base_row, row_conflicts = _merge_fields(c_group, merge_rules)
                merged.append(base_row)
                conflicts.extend(row_conflicts)

    # Other rows pass through
    for _, _, row in other:
        merged.append(row)

    # ── Phone-level address propagation ──
    # Within this merge group (same account+phone), all rows are for the same
    # physical service location. S-rows (service headers) typically carry the
    # address from the CSR/invoice TN section, but C-rows (USOC/component lines)
    # underneath don't repeat it. Propagate address from S-rows to C-rows so
    # every row has the correct service location.
    # Only location-specific fields — NOT billing_name (which is account-level
    # and should propagate at account level, not phone level).
    _LOCATION_FIELDS = [
        "service_address_1", "service_address_2", "city", "state", "zip",
    ]
    best_location: dict[str, str] = {}
    for row in merged:
        soc = getattr(row, "service_or_component", "") or ""
        rt = getattr(row, "row_type", None)
        is_service = soc == "S" or (rt is not None and str(rt) == "S")
        if is_service:
            for field in _LOCATION_FIELDS:
                val = getattr(row, field, None)
                if val and field not in best_location:
                    best_location[field] = val

    if best_location:
        filled = 0
        for row in merged:
            for field, val in best_location.items():
                if not getattr(row, field, None):
                    setattr(row, field, val)
                    filled += 1
        if filled:
            logger.debug(f"Phone-level propagation: filled {filled} location fields within group")

    return merged, conflicts


# Fields that are processing metadata, not extracted data — never conflict
_MERGE_SKIP_FIELDS = frozenset({
    "field_confidence", "field_sources", "review_status",
    "invoice_file_name", "row_type", "service_or_component",
})


def _row_completeness(row: ExtractedRow) -> int:
    """Count non-None fields — used to pick the 'fuller' row in same-doc conflicts."""
    return sum(1 for v in row.model_dump().values() if v is not None)


def _merge_fields(
    rows: list[tuple[str, str, ExtractedRow]],
    merge_rules: MergeRulesConfig,
) -> tuple[ExtractedRow, list[dict]]:
    """Merge fields from multiple rows using priority matrix.

    Three-tier conflict resolution:
    1. Priority matrix (invoice MRC > CSR MRC, CSR phone > invoice phone, etc.)
    2. Same-doc-type completeness heuristic (when CSR-vs-CSR or invoice-vs-invoice,
       pick the row with more populated fields as base — the fuller extraction wins)
    3. Remaining true cross-doc conflicts → collected for LLM resolution

    Returns the merged row and any unresolvable conflicts.
    """
    conflicts = []
    acct_config = merge_rules.account_normalization

    # When all rows come from the same doc_type (e.g., CSR-vs-CSR from different chunks),
    # sort by completeness instead of doc_type priority so the fuller row wins.
    doc_types_in_group = set(dt for _, dt, _ in rows)
    if len(doc_types_in_group) == 1:
        rows_sorted = sorted(rows, key=lambda x: -_row_completeness(x[2]))
    else:
        rows_sorted = sorted(rows, key=lambda x: -_doc_type_base_priority(x[1], merge_rules))

    base_doc_id, base_doc_type, base_row = rows_sorted[0]
    merged_dict = base_row.model_dump()

    # Capture invoice-sourced addresses even when they don't win the primary
    # field. Sidecar columns (billing_address_1, billing_city, ...) preserve
    # what the invoice showed so analysts can see both when they diverge.
    invoice_sidecar_map = {
        "service_address_1": "billing_address_1",
        "city":              "billing_city",
        "state":             "billing_state",
        "zip":               "billing_zip",
        "billing_name":      "billing_name_from_invoice",
    }
    # Pre-populate sidecars from the invoice source in the group (if any).
    for _doc_id, _doc_type, _row in rows_sorted:
        if _doc_type != "invoice":
            continue
        for primary_field, sidecar_field in invoice_sidecar_map.items():
            inv_val = getattr(_row, primary_field, None)
            if inv_val and not merged_dict.get(sidecar_field):
                merged_dict[sidecar_field] = inv_val

    # For each field, check if a lower-priority document has a value that should override
    for doc_id, doc_type, row in rows_sorted[1:]:
        row_dict = row.model_dump()
        for field_name, value in row_dict.items():
            if value is None:
                continue
            if field_name.startswith("_") or field_name in _MERGE_SKIP_FIELDS:
                continue

            current_value = merged_dict.get(field_name)

            if current_value is None:
                # Fill gap — base didn't have this field
                merged_dict[field_name] = value
            elif current_value != value:
                # Check if values are equivalent after normalization
                if _values_equivalent(field_name, current_value, value, acct_config):
                    continue  # Same value, just different formatting

                # Same doc type (e.g., CSR-vs-CSR) — base already has the fuller row,
                # so keep the base value. No conflict to report.
                if doc_type == base_doc_type:
                    continue

                # Cross-doc conflict — check priority
                current_priority = get_field_priority(field_name, base_doc_type, merge_rules)
                new_priority = get_field_priority(field_name, doc_type, merge_rules)

                if new_priority > current_priority:
                    merged_dict[field_name] = value
                elif new_priority == current_priority:
                    # Same priority, genuinely different values — true cross-doc conflict
                    conflicts.append({
                        "field": field_name,
                        "merge_key": merged_dict.get("carrier_account_number", "?"),
                        "value_a": current_value,
                        "source_a": base_doc_type,
                        "doc_a": base_doc_id,
                        "value_b": value,
                        "source_b": doc_type,
                        "doc_b": doc_id,
                    })

    # Flag rows where the invoice's billing address diverges from the primary
    # service address. Analyst can see both columns in the grid and resolve.
    def _norm(s):
        return str(s).strip().upper() if s else ""
    divergence = False
    for primary_field, sidecar_field in invoice_sidecar_map.items():
        pri = _norm(merged_dict.get(primary_field))
        alt = _norm(merged_dict.get(sidecar_field))
        if pri and alt and pri != alt:
            divergence = True
            break
    if divergence:
        # Don't overwrite an existing status (e.g., "Active", "Inactive")
        if not merged_dict.get("status"):
            merged_dict["status"] = "Needs Review"

    # Remove None-only fields for clean Pydantic
    clean = {k: v for k, v in merged_dict.items() if k in ExtractedRow.model_fields}
    return ExtractedRow(**clean), conflicts


# ============================================
# Cross-Granularity Merge (v2)
# ============================================

# Account-level fields: safe to propagate at Tier 3 (account-only match)
# because they're the same for all rows in an account.
_ACCOUNT_LEVEL_ENRICHMENT_FIELDS = {
    # Contract terms — same for all services under one account
    "contract_term_months", "contract_begin_date", "contract_expiration_date",
    "billing_per_contract", "currently_month_to_month", "mtm_or_less_than_year",
    "contract_file_name", "contract_number", "contract_number_2",
    "auto_renew", "auto_renewal_notes",
    # Address/identity — same for all rows in an account
    "billing_name", "service_address_1", "service_address_2",
    "city", "state", "zip", "country",
    "carrier_name", "master_account", "currency",
}

# ALL schema fields — used for Tier 1/2 enrichment (phone/circuit-level match)
# where we know the enrichment row matches a specific service line.
_ALL_ENRICHMENT_FIELDS = set(ExtractedRow.model_fields.keys())


def _build_account_equivalence(
    all_rows: list[ExtractedRow],
    merge_rules: MergeRulesConfig,
) -> dict[str, str]:
    """Build account equivalence map from cross-referencing account fields.

    If a row has master_account=X and carrier_account_number=Y,
    then X and Y are equivalent. Returns {normalized_account: canonical_account}.
    """
    acct_config = merge_rules.account_normalization
    equiv_fields = merge_rules.account_equivalence_fields

    # Union-Find via dict
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Shorter string becomes canonical (master accounts tend to be shorter)
            if len(ra) <= len(rb):
                parent[rb] = ra
            else:
                parent[ra] = rb

    for row in all_rows:
        accounts = set()
        for field in equiv_fields:
            val = getattr(row, field, None)
            if val:
                norm = _normalize_account(str(val), acct_config)
                if norm:
                    accounts.add(norm)

        # All accounts on this row are equivalent
        accounts_list = sorted(accounts)
        for i in range(1, len(accounts_list)):
            union(accounts_list[0], accounts_list[i])

    # Build final map
    result = {}
    for acct in parent:
        canonical = find(acct)
        if acct != canonical:
            result[acct] = canonical

    if result:
        logger.info(f"Account equivalence: {len(result)} aliases → "
                     f"{len(set(result.values()))} canonical accounts")

    return result


def _build_tiered_merge_key(
    row: ExtractedRow,
    doc_id: str,
    merge_rules: MergeRulesConfig,
    account_equiv: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Build a tiered merge key for cross-granularity matching.

    Returns (key, tier) where tier indicates match specificity:
      Tier 1: account|phone — strongest, phone-level matching
      Tier 2: sub_account|circuit or sub_account|service_type — service-level
      Tier 3: account — account-level (enrichment only)
      Tier 0: unkeyed — no usable identifiers
    """
    acct_config = merge_rules.account_normalization

    raw_acct = _normalize_account(row.carrier_account_number or "", acct_config)
    raw_sub = _normalize_account(row.sub_account_number_1 or "", acct_config)

    # Resolve account equivalence
    if account_equiv:
        raw_acct = account_equiv.get(raw_acct, raw_acct)
        raw_sub = account_equiv.get(raw_sub, raw_sub)

    # Use sub-account if available, fall back to main account
    acct = raw_sub or raw_acct

    # Tier 1: account + phone
    phone = _normalize_for_key(row.phone_number or "") or _normalize_for_key(row.btn or "")
    if acct and phone:
        return (f"{acct}|{phone}", 1)

    # Tier 2: account + circuit or service_type
    circuit = (row.carrier_circuit_number or "").strip()
    if acct and circuit:
        return (f"{acct}|cir:{circuit}", 2)
    svc_type = (row.service_type or "").strip().upper()
    svc_comp = (row.service_or_component or "").strip().upper()
    if acct and svc_type and svc_comp in ("S", ""):
        return (f"{acct}|svc:{svc_type}", 2)

    # Tier 3: account only
    if acct:
        return (f"{acct}", 3)

    return (f"unkeyed_{doc_id}_{id(row)}", 0)


def _enrich_from_account_sources(
    target_rows: list[ExtractedRow],
    enrichment_rows: list[tuple[str, str, ExtractedRow]],
    merge_rules: MergeRulesConfig,
    account_equiv: dict[str, str] | None = None,
) -> list[ExtractedRow]:
    """Propagate fields from enrichment-role rows to target rows.

    Two-tier enrichment:
      - Tier 1/2 match (phone or circuit): propagate ALL fields (row-level match)
      - Tier 3 match (account only): propagate only account-level fields

    Only fills fields where the target is empty and the source has a value.
    Uses field priority to resolve conflicts when multiple enrichment sources
    contribute the same field.
    """
    if not enrichment_rows:
        return target_rows

    acct_config = merge_rules.account_normalization

    # Additional account-level fields from carrier config
    account_fields = set(_ACCOUNT_LEVEL_ENRICHMENT_FIELDS)
    account_fields.update(merge_rules.enrichment_fields)

    # Build enrichment indices by tiered key
    # Tier 1/2: phone/circuit-level → propagate all fields
    enrichment_by_key: dict[str, list[tuple[str, str, ExtractedRow]]] = defaultdict(list)
    # Tier 3: account-level → propagate only account-level fields
    enrichment_by_acct: dict[str, list[tuple[str, str, ExtractedRow]]] = defaultdict(list)

    for doc_id, doc_type, row in enrichment_rows:
        key, tier = _build_tiered_merge_key(row, doc_id, merge_rules, account_equiv)
        if tier in (1, 2):
            enrichment_by_key[key].append((doc_id, doc_type, row))
        # Always index by account for Tier 3 fallback
        acct = _normalize_account(row.carrier_account_number or "", acct_config)
        sub = _normalize_account(row.sub_account_number_1 or "", acct_config)
        if account_equiv:
            acct = account_equiv.get(acct, acct)
            sub = account_equiv.get(sub, sub)
        if acct:
            enrichment_by_acct[acct].append((doc_id, doc_type, row))
        if sub and sub != acct:
            enrichment_by_acct[sub].append((doc_id, doc_type, row))

    if not enrichment_by_key and not enrichment_by_acct:
        return target_rows

    filled_count = 0
    tier1_2_matches = 0
    tier3_matches = 0

    for target in target_rows:
        # Try Tier 1/2 match first (row-level — propagate ALL fields)
        t_key, t_tier = _build_tiered_merge_key(target, "", merge_rules, account_equiv)
        sources = enrichment_by_key.get(t_key, []) if t_tier in (1, 2) else []

        if sources:
            tier1_2_matches += 1
            eligible_fields = _ALL_ENRICHMENT_FIELDS
        else:
            # Fall back to Tier 3 (account-level — propagate only account fields)
            t_acct = _normalize_account(target.carrier_account_number or "", acct_config)
            t_sub = _normalize_account(target.sub_account_number_1 or "", acct_config)
            if account_equiv:
                t_acct = account_equiv.get(t_acct, t_acct)
                t_sub = account_equiv.get(t_sub, t_sub)
            sources = enrichment_by_acct.get(t_sub, []) if t_sub else []
            if not sources:
                sources = enrichment_by_acct.get(t_acct, [])
            if not sources:
                continue
            tier3_matches += 1
            eligible_fields = account_fields

        for field_name in eligible_fields:
            current = getattr(target, field_name, None)
            if current is not None and str(current).strip():
                continue  # Target already has this field

            # Find best value from enrichment sources.
            # Prefer sources that directly match the target's sub-account
            # (more specific) over sources that match via equivalence.
            best_value = None
            best_priority = -1
            best_specificity = 0  # 2=direct sub match, 1=direct acct, 0=equivalence
            for doc_id, doc_type, src_row in sources:
                val = getattr(src_row, field_name, None)
                if val is None or not str(val).strip():
                    continue
                priority = get_field_priority(field_name, doc_type, merge_rules)
                # Specificity: source has same sub_account as target → more specific.
                # Resolve src_sub through account_equiv so both sides use the
                # same canonical form. Without this, a raw sub like "ACME-030"
                # (→ "030") could match the resolved t_sub while numeric subs
                # (→ resolved to "030" too) would fail the raw comparison.
                src_sub = _normalize_account(src_row.sub_account_number_1 or "", acct_config)
                if account_equiv:
                    src_sub = account_equiv.get(src_sub, src_sub)
                specificity = 2 if (t_sub and src_sub == t_sub) else 1
                # Prefer higher specificity, then higher priority
                if (specificity > best_specificity) or \
                   (specificity == best_specificity and priority > best_priority):
                    best_priority = priority
                    best_value = val
                    best_specificity = specificity

            if best_value is not None:
                setattr(target, field_name, best_value)
                filled_count += 1

    if filled_count:
        logger.info(f"Cross-granularity enrichment: filled {filled_count} fields "
                     f"(tier1/2={tier1_2_matches} row-level, tier3={tier3_matches} account-level)")

    return target_rows


def _apply_pre_merge_normalization(
    extractions: dict[str, list[ExtractedRow]],
    doc_types: dict[str, str],
    carrier: str | None = None,
) -> None:
    """Apply carrier-specific normalizations BEFORE merge keys are computed.

    Currently supports (all config-driven from carrier.yaml):
    - account_from_filename: extract account ID from the document filename and
      set it on rows that lack a carrier_account_number. Fixes scanned invoice
      OCR that fails to capture the account number from the document body.
    - location_to_sub_account: move location names (e.g., "Tops Markets 720")
      from carrier_account_number to sub_account_number_1, replacing the account
      with the canonical account extracted from the filename.

    Mutates rows in-place.
    """
    if not carrier:
        return

    from pathlib import Path
    from backend.settings import settings
    import yaml as _yaml

    config_path = Path(settings.configs_dir) / "carriers" / carrier / "carrier.yaml"
    if not config_path.exists():
        return

    with open(config_path) as f:
        raw_config = _yaml.safe_load(f) or {}

    # ── account_from_filename + location_to_sub_account ──
    pre_merge = raw_config.get("pre_merge_normalization", {})
    acct_pattern = pre_merge.get("account_from_filename", {}).get("pattern") if pre_merge else None
    location_pattern = pre_merge.get("location_to_sub_account", {}).get("pattern") if pre_merge else None

    if acct_pattern:
        acct_re = re.compile(acct_pattern)
        location_re = re.compile(location_pattern) if location_pattern else None
        acct_set_count = 0
        loc_moved_count = 0

        for doc_id, rows in extractions.items():
            # Extract account from filename
            m = acct_re.search(doc_id)
            if not m:
                continue
            canonical_account = m.group(1) if m.lastindex else m.group(0)

            for row in rows:
                current_acct = (row.carrier_account_number or "").strip()

                # Move location names to sub_account
                if location_re and current_acct and location_re.match(current_acct):
                    if not row.sub_account_number_1:
                        row.sub_account_number_1 = current_acct
                    row.carrier_account_number = canonical_account
                    loc_moved_count += 1
                # Fill empty accounts from filename
                elif not current_acct:
                    row.carrier_account_number = canonical_account
                    acct_set_count += 1

        if acct_set_count or loc_moved_count:
            logger.info(f"Pre-merge account normalization: set {acct_set_count} accounts from filename, "
                        f"moved {loc_moved_count} locations to sub_account")

    # ── promote_master_account: fix account hierarchy for consolidated bills ──
    # Carriers with hierarchical billing (Spectrum, Charter) have a control/billing
    # account and per-location sub-accounts. The standard field semantics are:
    #   carrier_account_number = billing/control account (what the carrier bills under)
    #   sub_account_number_1   = per-location identifier
    # Some extraction prompts invert this (sub-account in carrier_account_number,
    # control in master_account). This normalization corrects the hierarchy.
    if pre_merge and pre_merge.get("promote_master_account"):
        promote_count = 0
        for _doc_id, rows in extractions.items():
            for row in rows:
                master = (row.master_account or "").strip()
                acct = (row.carrier_account_number or "").strip()
                if master and acct and master != acct:
                    # Current: carrier_account = sub-account, master = control
                    # Correct: carrier_account = control, sub_account = sub-account
                    if not row.sub_account_number_1:
                        row.sub_account_number_1 = acct
                    row.carrier_account_number = master
                    promote_count += 1
        if promote_count:
            logger.info(f"Pre-merge account hierarchy: promoted master_account to "
                        f"carrier_account_number on {promote_count} rows")

    # ── row_type_from_charge_type: fix row_type BEFORE merge ──
    # The merge groups S-rows vs C-rows for different dedup strategies.
    # If the LLM extraction misclassifies Usage rows as C (Component), they
    # get C-row dedup (by component name) which incorrectly collapses per-
    # location rows with the same service name (e.g., 155 "Voice Domestic MOU"
    # rows → 1). Applying row_type from charge_type before merge ensures
    # correct classification.
    rt_map = raw_config.get("row_type_from_charge_type", {})
    rt_count = 0
    if rt_map:
        from backend.models.schemas import RowType
        _rt_enum = {"S": RowType.SERVICE, "C": RowType.COMPONENT}
        for _doc_id, rows in extractions.items():
            for row in rows:
                ct = row.charge_type or ""
                if ct not in rt_map:
                    continue
                expected_soc = rt_map[ct]
                # Map to RowType enum — None for non-S/C values (U, T\S\OCC)
                # which removes them from S-row/C-row grouping in merge
                expected_rt = _rt_enum.get(expected_soc)  # None for "U", "T\S\OCC"

                if row.service_or_component != expected_soc or row.row_type != expected_rt:
                    row.service_or_component = expected_soc
                    row.row_type = expected_rt  # Explicitly None for non-S/C
                    rt_count += 1
    if rt_count:
        logger.info(f"Pre-merge row type normalization: {rt_count} values corrected from charge_type")


def cross_granularity_merge(
    extractions: dict[str, list[ExtractedRow]],
    doc_types: dict[str, str],
    carrier: str | None = None,
) -> tuple[list[ExtractedRow], list[dict]]:
    """Cross-granularity merge — handles documents at different row granularities.

    Three-phase merge:
      Phase A: Same-granularity merge on primary-role rows (Tier 1/2 keys)
      Phase B: Enrich from enrichment-role rows (Tier 3 account-level propagation)
      Phase C: Append supplemental-role rows that didn't match

    Args:
        extractions: {document_id: [rows]} — rows from each document
        doc_types: {document_id: doc_type} — "invoice", "csr", "contract", etc.
        carrier: carrier name — loads carrier-specific merge rules

    Returns:
        (merged_rows, conflicts)
    """
    if not extractions:
        return [], []

    # Step 0: Pre-merge normalization — fix account numbers, move locations to
    # sub_account, etc. BEFORE merge keys are computed.
    _apply_pre_merge_normalization(extractions, doc_types, carrier)

    merge_rules = get_config_store().get_merge_rules(carrier) if carrier else MergeRulesConfig()
    roles = merge_rules.doc_type_roles  # doc_type → "primary"|"enrichment"|"supplemental"

    def get_role(doc_type: str) -> str:
        return roles.get(doc_type, "primary")  # default: primary (backward compat)

    # Collect all rows for account equivalence
    all_rows = []
    for rows in extractions.values():
        all_rows.extend(rows)

    # Step 1: Build account equivalence map
    account_equiv = _build_account_equivalence(all_rows, merge_rules)

    # Step 2: Separate rows by role
    primary_extractions: dict[str, list[ExtractedRow]] = {}
    primary_doc_types: dict[str, str] = {}
    enrichment_rows: list[tuple[str, str, ExtractedRow]] = []  # (doc_id, doc_type, row)
    supplemental_rows: list[tuple[str, str, ExtractedRow]] = []

    for doc_id, rows in extractions.items():
        doc_type = doc_types.get(doc_id, "unknown")
        role = get_role(doc_type)

        if role == "enrichment":
            for row in rows:
                enrichment_rows.append((doc_id, doc_type, row))
        elif role == "supplemental":
            for row in rows:
                supplemental_rows.append((doc_id, doc_type, row))
        else:  # primary
            primary_extractions[doc_id] = rows
            primary_doc_types[doc_id] = doc_type

    logger.info(f"Cross-granularity merge: {len(primary_extractions)} primary docs, "
                f"{len(enrichment_rows)} enrichment rows, {len(supplemental_rows)} supplemental rows")

    # Step 3 — Phase A: Same-granularity merge on primary rows
    if primary_extractions:
        merged_rows, conflicts = rule_based_merge(
            primary_extractions, primary_doc_types, carrier=carrier
        )
    else:
        merged_rows, conflicts = [], []

    # Step 4 — Phase B: Enrich from enrichment-role rows
    if enrichment_rows:
        merged_rows = _enrich_from_account_sources(
            merged_rows, enrichment_rows, merge_rules, account_equiv
        )

    # Step 5 — Phase C: Append supplemental rows that didn't match any primary
    if supplemental_rows:
        # Check if supplemental rows match any existing merged row by Tier 1 key
        existing_keys = set()
        for row in merged_rows:
            key, tier = _build_tiered_merge_key(row, "", merge_rules, account_equiv)
            if tier <= 2:
                existing_keys.add(key)

            appended = 0
        for doc_id, doc_type, row in supplemental_rows:
            key, tier = _build_tiered_merge_key(row, doc_id, merge_rules, account_equiv)
            if key not in existing_keys:
                merged_rows.append(row)
                appended += 1

        if appended:
            logger.info(f"Appended {appended} supplemental rows "
                         f"(skipped {len(supplemental_rows) - appended} already matched)")

    # Step 6: Account-level field propagation (existing logic)
    merged_rows = _propagate_account_fields(merged_rows, merge_rules)

    # Step 7: Apply carrier-specific field normalization.
    # Normalizes LLM extraction output that uses product variant names instead
    # of the standard categories defined in the extraction prompt.
    merged_rows = _apply_field_normalization(merged_rows, carrier)

    # Step 8: Re-propagate account fields after normalization.
    # Normalization may clear placeholder values (e.g., billing_name "--" → None)
    # that blocked propagation in step 6. A second pass fills those gaps.
    merged_rows = _propagate_account_fields(merged_rows, merge_rules)

    total_input = sum(len(r) for r in extractions.values())
    logger.info(f"Cross-granularity merge complete: {total_input} input → "
                f"{len(merged_rows)} output, {len(conflicts)} conflicts")

    return merged_rows, conflicts


def _apply_field_normalization(
    rows: list[ExtractedRow],
    carrier: str | None = None,
) -> list[ExtractedRow]:
    """Apply carrier-specific field value normalization from carrier.yaml.

    Currently supports:
    - service_type_normalization: maps product variant names to standard categories

    This is config-driven (carrier YAML), not hardcoded. Normalizations are based
    on what source documents call these services, not golden data.
    """
    if not carrier:
        return rows

    from pathlib import Path
    from backend.settings import settings
    import yaml as _yaml

    config_path = Path(settings.configs_dir) / "carriers" / carrier / "carrier.yaml"
    if not config_path.exists():
        return rows

    with open(config_path) as f:
        raw_config = _yaml.safe_load(f) or {}

    # Service type normalization
    st_map = raw_config.get("service_type_normalization", {})
    st_count = 0
    if st_map:
        for row in rows:
            if row.service_type and row.service_type in st_map:
                row.service_type = st_map[row.service_type]
                st_count += 1

    if st_count:
        logger.info(f"Service type normalization: {st_count} values normalized")

    # USOC → service_type inference.
    # When a row has no service_type but has a USOC code, infer service_type
    # from the carrier's usoc_service_type mapping. Source: carrier service catalog.
    usoc_st_map = raw_config.get("usoc_service_type", {})
    usoc_st_count = 0
    if usoc_st_map:
        for row in rows:
            if row.service_type:
                continue  # Don't override existing service_type
            usoc = (row.usoc or "").strip()
            if usoc and usoc in usoc_st_map:
                row.service_type = usoc_st_map[usoc]
                usoc_st_count += 1

    if usoc_st_count:
        logger.info(f"USOC → service_type inference: {usoc_st_count} values inferred")

    # Phone-level service_type propagation.
    # Surcharge/fee USOCs (9ZR, NSR, UXT25, etc.) appear on any line type and
    # aren't in usoc_service_type. Propagate the service_type from sibling rows
    # that share the same phone_number. Groups by normalized phone and fills
    # empty service_type from any sibling that has one.
    if usoc_st_map:  # Only do this when USOC mapping is configured
        phone_groups: dict[str, list[ExtractedRow]] = defaultdict(list)
        for row in rows:
            phone = _normalize_for_key(row.phone_number or row.btn or "")
            if phone:
                phone_groups[phone].append(row)

        phone_st_count = 0
        for phone, group in phone_groups.items():
            # Find the service_type from rows that have one
            group_st = None
            for row in group:
                if row.service_type:
                    group_st = row.service_type
                    break
            if group_st:
                for row in group:
                    if not row.service_type:
                        row.service_type = group_st
                        phone_st_count += 1

        if phone_st_count:
            logger.info(f"Phone-level service_type propagation: {phone_st_count} values propagated")

    # USOC → description enrichment for component_or_feature_name.
    # When the LLM extraction echoes a raw USOC code as the component name
    # (e.g., "CPXHF" instead of "CTX Central Office Termination"), resolve it
    # using the carrier's domain_knowledge/usoc_codes.yaml lookup table.
    usoc_path = Path(settings.configs_dir) / "carriers" / carrier / "domain_knowledge" / "usoc_codes.yaml"
    usoc_count = 0
    if usoc_path.exists():
        with open(usoc_path) as f:
            usoc_map = _yaml.safe_load(f) or {}
        if usoc_map:
            for row in rows:
                comp = row.component_or_feature_name
                if not comp:
                    continue
                # Only resolve if component looks like a raw USOC code:
                # short (<=10 chars), uppercase, and exists in the lookup table
                comp_stripped = comp.strip()
                if (len(comp_stripped) <= 10
                        and comp_stripped == comp_stripped.upper()
                        and comp_stripped in usoc_map):
                    row.component_or_feature_name = usoc_map[comp_stripped]
                    usoc_count += 1

    if usoc_count:
        logger.info(f"USOC enrichment: {usoc_count} component names resolved from codes")

    # ── Service type inference from component_name patterns ──
    # Config: service_type_inference: {"DID": "DID", "Channel Fee": "SIP Trunk", ...}
    # Each key is a regex pattern matched against component_or_feature_name.
    sti_map = raw_config.get("service_type_inference", {})
    sti_count = 0
    if sti_map:
        for row in rows:
            if row.service_type:
                continue  # Don't override existing service_type
            comp = row.component_or_feature_name or ""
            for pattern, svc_type in sti_map.items():
                if re.search(pattern, comp, re.IGNORECASE):
                    row.service_type = svc_type
                    sti_count += 1
                    break
    if sti_count:
        logger.info(f"Service type inference: {sti_count} values inferred from component names")

    # ── Row type inference from charge_type ──
    # Config: row_type_from_charge_type: {"MRC": "S", "Usage": "U", "Tax": "T\\S\\OCC"}
    rt_map = raw_config.get("row_type_from_charge_type", {})
    rt_count = 0
    if rt_map:
        for row in rows:
            ct = row.charge_type or ""
            if ct in rt_map:
                expected = rt_map[ct]
                current = row.service_or_component or row.row_type or ""
                if current != expected:
                    row.service_or_component = expected
                    row.row_type = expected
                    rt_count += 1
    if rt_count:
        logger.info(f"Row type inference: {rt_count} values corrected from charge_type")

    # ── Charge type normalization ──
    # Config: charge_type_normalization: {"Tax": "Taxes"}
    ct_map = raw_config.get("charge_type_normalization", {})
    ct_count = 0
    if ct_map:
        for row in rows:
            if row.charge_type and row.charge_type in ct_map:
                row.charge_type = ct_map[row.charge_type]
                ct_count += 1
    if ct_count:
        logger.info(f"Charge type normalization: {ct_count} values normalized")

    # ── Billing name placeholder cleanup ──
    # Config: billing_name_placeholders: ["--", "Default", "RemoteWorker"]
    # Clear placeholder values so account-level propagation can fill them
    # from rows that have the real billing name (e.g., invoice rows).
    bn_placeholders = raw_config.get("billing_name_placeholders", [])
    bn_count = 0
    if bn_placeholders:
        for row in rows:
            if row.billing_name and row.billing_name.strip() in bn_placeholders:
                row.billing_name = None
                bn_count += 1
    if bn_count:
        logger.info(f"Billing name cleanup: {bn_count} placeholder values cleared")

    # ── Derive currently_month_to_month from contract dates ──
    # If a row has contract_expiration_date and it's in the past, the service
    # is operating month-to-month. If it's in the future, it's under contract.
    # This is a logical derivation, not analyst judgment.
    if raw_config.get("derive_currently_month_to_month", False):
        from datetime import date
        today = date.today()
        mtm_count = 0
        for row in rows:
            if row.currently_month_to_month:
                continue  # Don't override existing
            exp = row.contract_expiration_date
            if exp:
                try:
                    exp_date = date.fromisoformat(str(exp))
                    if exp_date < today:
                        row.currently_month_to_month = "Yes"
                    else:
                        row.currently_month_to_month = "No"
                    mtm_count += 1
                except (ValueError, TypeError):
                    pass
        if mtm_count:
            logger.info(f"Month-to-month derivation: {mtm_count} values derived from contract dates")

    # ── Derive cost_per_unit from MRC / quantity ──
    # Config: derive_cost_per_unit: true
    if raw_config.get("derive_cost_per_unit", False):
        from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
        cpu_count = 0
        for row in rows:
            if row.cost_per_unit is not None:
                continue  # Don't override existing
            mrc = row.monthly_recurring_cost
            qty = row.quantity
            if mrc is not None and qty is not None and qty > 0:
                try:
                    mrc_d = Decimal(str(mrc))
                    cpu = mrc_d / Decimal(qty)
                    row.cost_per_unit = float(cpu.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    cpu_count += 1
                except (InvalidOperation, ZeroDivisionError):
                    pass
        if cpu_count:
            logger.info(f"Cost per unit derivation: {cpu_count} values computed from MRC/quantity")

    return rows
