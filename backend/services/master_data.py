"""Per-client master-data lookup applied at merge time (PENDING §2.1).

Consulted by _run_merge after cross_granularity_merge produces merged rows.
When a row's key (phone_number / account_number / circuit id) matches a
client_reference_data entry, that entry's authoritative values override the
extracted values. Priority is effectively 15 — above CSR(10) — matching the
analyst's confirmed fact over any single document.

Empty-store behavior: when the client has no reference-data rows (day one),
this is a no-op and the pipeline behaves identically to today.

Kept intentionally narrow: one function, pure Python, no Pydantic coercion
beyond dict access. The merger calls it via a single entry point.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Which key_fields tie a master-data entry to an extracted row. The entry's
# `kind` selects which match function runs; first hit wins.
_KIND_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "address": ("phone_number", "account_number"),
    "account_alias": ("account_number",),
    "circuit": ("carrier_circuit_number", "phone_number"),
    "contract_term": ("account_number", "carrier"),
}


def _row_val(row: Any, field: str) -> str | None:
    """Read a field off either a dict row or a Pydantic ExtractedRow."""
    if isinstance(row, dict):
        v = row.get(field)
    else:
        v = getattr(row, field, None)
    return str(v).strip() if v is not None and str(v).strip() else None


def _set_row_val(row: Any, field: str, value: Any) -> None:
    if isinstance(row, dict):
        row[field] = value
    else:
        try:
            setattr(row, field, value)
        except (AttributeError, TypeError):
            pass


def _entry_matches_row(entry: dict, row: Any) -> bool:
    """Does this reference-data entry identify this row?

    `entry` is the raw dict-shape of one `client_reference_data` row with:
      - kind: 'address' | 'account_alias' | 'circuit' | ...
      - carrier: optional carrier scope
      - account_number: optional scope
      - key_fields: dict of identifier fields (e.g., {"phone_number": "614..."})
    """
    kind = entry.get("kind")
    kf = entry.get("key_fields") or {}
    if not isinstance(kf, dict) or not kf:
        return False

    # Carrier scope — if the entry is carrier-scoped, row must match carrier
    entry_carrier = entry.get("carrier")
    if entry_carrier:
        row_carrier = _row_val(row, "carrier_name") or _row_val(row, "carrier")
        if not row_carrier or row_carrier.lower() != entry_carrier.lower():
            return False

    # Account scope
    entry_acct = entry.get("account_number")
    if entry_acct:
        row_acct = _row_val(row, "carrier_account_number") or _row_val(row, "account_number")
        if not row_acct or _norm_acct(row_acct) != _norm_acct(entry_acct):
            return False

    # Key-field match (at least one key_fields key must match the row)
    for key, expected in kf.items():
        row_val = _row_val(row, key)
        if row_val is None:
            continue
        if key == "phone_number" or key.endswith("_phone"):
            if _norm_phone(row_val) == _norm_phone(str(expected)):
                return True
        elif key.endswith("account_number") or key == "account_number":
            if _norm_acct(row_val) == _norm_acct(str(expected)):
                return True
        elif row_val.lower() == str(expected).lower():
            return True

    # Kind-specific fallback (e.g., address keyed by account only)
    if kind in _KIND_KEY_FIELDS:
        for fallback_key in _KIND_KEY_FIELDS[kind]:
            if fallback_key in kf:
                row_val = _row_val(row, fallback_key)
                expected = kf[fallback_key]
                if row_val and str(expected):
                    if fallback_key == "phone_number":
                        if _norm_phone(row_val) == _norm_phone(str(expected)):
                            return True
                    elif fallback_key.endswith("account_number"):
                        if _norm_acct(row_val) == _norm_acct(str(expected)):
                            return True
    return False


def _norm_phone(s: str) -> str:
    import re
    return re.sub(r"\D", "", s or "")


def _norm_acct(s: str) -> str:
    import re
    return re.sub(r"\D", "", s or "")


def store_correction_to_master_data(
    db_session,
    client_id: str,
    row: Any,
    field_name: str,
    corrected_value: Any,
    confirmed_by: str | None = None,
) -> bool:
    """Upsert an analyst correction into client_reference_data.

    Only location/identity fields write back — we don't overwrite MRC or
    one-time amounts. Returns True if a row was written/updated.

    No-op when client_id is None.
    """
    if not client_id or not field_name or corrected_value is None:
        return False

    # Which corrections flow into master-data, and under which `kind`
    _KIND_BY_FIELD = {
        "service_address_1": "address",
        "service_address_2": "address",
        "city": "address",
        "state": "address",
        "zip": "address",
        "country": "address",
        "billing_name": "address",
        "carrier_circuit_number": "circuit",
        "contract_term_months": "contract_term",
        "contract_begin_date": "contract_term",
        "contract_expiration_date": "contract_term",
        "auto_renew": "contract_term",
    }
    kind = _KIND_BY_FIELD.get(field_name)
    if not kind:
        return False

    phone = _row_val(row, "phone_number")
    account = _row_val(row, "carrier_account_number") or _row_val(row, "account_number")
    carrier = _row_val(row, "carrier_name") or _row_val(row, "carrier")

    # Build the key that scopes this entry — phone preferred, account fallback
    if kind == "address":
        key_fields = {"phone_number": phone} if phone else ({"account_number": account} if account else {})
    elif kind == "circuit":
        key_fields = {"carrier_circuit_number": _row_val(row, "carrier_circuit_number") or corrected_value}
    else:  # contract_term
        key_fields = {"account_number": account} if account else {}

    if not key_fields:
        return False

    import json
    import uuid
    from datetime import datetime, timezone
    from sqlalchemy import text as _text

    try:
        cid = uuid.UUID(client_id) if isinstance(client_id, str) else client_id
    except ValueError:
        return False

    # Look up existing entry to merge new value into its values dict
    existing = db_session.execute(
        _text(
            "SELECT id, values FROM client_reference_data "
            "WHERE client_id = :cid AND kind = :kind "
            "AND (carrier IS NOT DISTINCT FROM :carrier) "
            "AND (account_number IS NOT DISTINCT FROM :acct) "
            "AND key_fields = CAST(:kf AS jsonb) "
            "LIMIT 1"
        ),
        {
            "cid": str(cid),
            "kind": kind,
            "carrier": carrier,
            "acct": account,
            "kf": json.dumps(key_fields),
        },
    ).fetchone()

    now = datetime.now(timezone.utc)
    new_value_json = json.dumps({field_name: str(corrected_value) if corrected_value is not None else None})

    if existing:
        existing_values = existing[1] or {}
        if not isinstance(existing_values, dict):
            existing_values = {}
        existing_values[field_name] = str(corrected_value) if corrected_value is not None else None
        db_session.execute(
            _text(
                "UPDATE client_reference_data "
                "SET values = CAST(:vals AS jsonb), "
                "    source = :source, confirmed_by = :who, confirmed_at = :when "
                "WHERE id = :id"
            ),
            {
                "vals": json.dumps(existing_values),
                "source": "analyst_correction",
                "who": confirmed_by or "analyst",
                "when": now,
                "id": existing[0],
            },
        )
        logger.info(f"master-data: updated {kind} entry for client {client_id} (field={field_name})")
    else:
        db_session.execute(
            _text(
                "INSERT INTO client_reference_data "
                "(client_id, kind, carrier, account_number, key_fields, values, source, confirmed_by, confirmed_at) "
                "VALUES (:cid, :kind, :carrier, :acct, CAST(:kf AS jsonb), CAST(:vals AS jsonb), "
                "        :source, :who, :when)"
            ),
            {
                "cid": str(cid),
                "kind": kind,
                "carrier": carrier,
                "acct": account,
                "kf": json.dumps(key_fields),
                "vals": new_value_json,
                "source": "analyst_correction",
                "who": confirmed_by or "analyst",
                "when": now,
            },
        )
        logger.info(f"master-data: created {kind} entry for client {client_id} (field={field_name})")
    return True


def apply_master_data_overrides(
    rows: list[Any],
    client_id: str | None,
    db_session,
) -> tuple[list[Any], int]:
    """Apply client master-data values as overrides to merged rows.

    Returns (rows, override_count). Rows are mutated in place for efficiency.
    No-op when `client_id` is None or the client has no reference data.
    """
    if not client_id or not rows:
        return rows, 0

    # Load all reference data for this client in one query
    import uuid
    try:
        cid = uuid.UUID(client_id) if isinstance(client_id, str) else client_id
    except ValueError:
        return rows, 0

    from sqlalchemy import text as _text
    q = _text(
        "SELECT kind, carrier, account_number, key_fields, values "
        "FROM client_reference_data WHERE client_id = :cid"
    )
    try:
        result = db_session.execute(q, {"cid": str(cid)})
        entries = [
            {
                "kind": r[0],
                "carrier": r[1],
                "account_number": r[2],
                "key_fields": r[3],
                "values": r[4],
            }
            for r in result.fetchall()
        ]
    except Exception as e:
        logger.warning(f"master-data lookup failed: {e}")
        return rows, 0

    if not entries:
        return rows, 0

    # Apply overrides — master-data values win over extracted values
    overridden = 0
    for row in rows:
        for entry in entries:
            if not _entry_matches_row(entry, row):
                continue
            values = entry.get("values") or {}
            if not isinstance(values, dict):
                continue
            for field, value in values.items():
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                _set_row_val(row, field, value)
                overridden += 1

    if overridden:
        logger.info(f"master-data: applied {overridden} overrides from {len(entries)} entries")
    return rows, overridden
