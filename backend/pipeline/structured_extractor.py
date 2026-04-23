"""Structured data extractor — direct column mapping for XLSX/XLS/CSV files.

No LLM needed. Reads spreadsheet column headers, fuzzy-matches them to our
60-field schema, and produces ExtractedRow objects. This handles carrier reports,
portal exports, service inventories, and similar structured data.

Each carrier's spreadsheets have different column naming conventions. The mapper
uses a two-tier approach:
1. Exact/alias match against a known column→field mapping table
2. Fuzzy similarity fallback for unknown column names

The mapping table is built from reading real carrier data (not hardcoded values).
"""

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl
import pandas as pd
import yaml

from backend.models.schemas import ExtractedRow
from backend.settings import settings

logger = logging.getLogger(__name__)


# ── Column header → schema field mapping (loaded from config) ──

_header_map_cache: dict[str, str] | None = None


def _load_header_map(carrier: str | None = None) -> dict[str, str]:
    """Load column→field mapping from configs/processing/column_mapping.yaml.

    Merges with carrier-specific overrides from configs/carriers/{carrier}/column_mapping.yaml
    if present.
    """
    global _header_map_cache

    # Load base mapping
    if _header_map_cache is None:
        base_path = Path(settings.configs_dir) / "processing" / "column_mapping.yaml"
        if base_path.exists():
            with open(base_path) as f:
                _header_map_cache = yaml.safe_load(f) or {}
        else:
            logger.warning(f"Column mapping config not found at {base_path}")
            _header_map_cache = {}

    result = dict(_header_map_cache)

    # Merge carrier-specific overrides
    if carrier:
        carrier_path = Path(settings.configs_dir) / "carriers" / carrier / "column_mapping.yaml"
        if carrier_path.exists():
            with open(carrier_path) as f:
                carrier_map = yaml.safe_load(f) or {}
            result.update(carrier_map)

    return result


def _normalize_header(h) -> str:
    """Normalize column header for matching."""
    if not h:
        return ""
    s = str(h).strip().lower()
    # Remove special chars but keep spaces
    s = re.sub(r'[^\w\s]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _map_columns(headers: list, carrier: str | None = None) -> dict[int, str]:
    """Map column indices to schema field names using config-driven mapping.

    Returns {col_index: schema_field_name} for matched columns.
    """
    header_map = _load_header_map(carrier)
    mapping = {}
    used_fields = set()  # Avoid mapping multiple columns to same field

    for i, header in enumerate(headers):
        norm = _normalize_header(header)
        if not norm:
            continue

        # Exact match
        if norm in header_map:
            field = header_map[norm]
            if field not in used_fields:
                mapping[i] = field
                used_fields.add(field)
                continue

        # Try without underscores/hyphens
        alt = norm.replace("_", " ").replace("-", " ")
        if alt in header_map:
            field = header_map[alt]
            if field not in used_fields:
                mapping[i] = field
                used_fields.add(field)

    return mapping


def _normalize_cell(field_name: str, value) -> any:
    """Normalize a cell value for the target schema field."""
    if value is None:
        return None

    # Date fields
    if field_name in ("contract_begin_date", "contract_expiration_date"):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        s = str(value).strip()
        if not s:
            return None
        for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"]:
            try:
                return datetime.strptime(s[:19], fmt).date()
            except ValueError:
                continue
        return None

    # Numeric fields
    if field_name in ("monthly_recurring_cost", "cost_per_unit", "ld_cost", "rate"):
        if isinstance(value, (int, float)):
            return Decimal(str(value))
        s = str(value).strip().replace("$", "").replace(",", "")
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    if field_name in ("quantity", "contract_term_months", "num_calls"):
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        try:
            return int(float(s)) if s else None
        except ValueError:
            return None

    # String fields
    s = str(value).strip()
    # Strip Excel .0 from numeric-like strings
    if s.endswith(".0") and s[:-2].replace("-", "").replace(" ", "").isdigit():
        s = s[:-2]
    return s if s else None


def extract_structured(
    file_path: str,
    carrier: str,
    document_type: str,
    sheet_name: str | None = None,
) -> tuple[list[ExtractedRow], list[str]]:
    """Extract rows from a structured data file (XLSX/XLS/CSV).

    No LLM needed — maps column headers directly to schema fields.

    Args:
        file_path: path to the spreadsheet file
        carrier: classified carrier name
        document_type: classified document type
        sheet_name: specific sheet to read (None = first sheet)

    Returns:
        (rows, warnings) — extracted rows and any mapping warnings
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    warnings = []

    # Read headers and data
    if suffix == ".csv":
        df = pd.read_csv(file_path, nrows=0)
        headers = list(df.columns)
        # Re-read with all data
        df = pd.read_csv(file_path)
    elif suffix in (".xlsx", ".xls"):
        engine = "openpyxl" if suffix == ".xlsx" else "xlrd"
        try:
            # Try reading with pandas for simplicity
            if sheet_name:
                df = pd.read_excel(file_path, sheet_name=sheet_name, engine=engine)
            else:
                df = pd.read_excel(file_path, engine=engine)
            headers = list(df.columns)
        except Exception as e:
            logger.warning(f"Pandas read failed for {file_path}: {e}")
            return [], [f"Read failed: {e}"]
    else:
        return [], [f"Unsupported file type: {suffix}"]

    if df.empty:
        return [], ["Empty file"]

    # Handle files where row 1 isn't the header (like Charter with empty row 1)
    # Check if first row looks like data rather than headers
    if all(isinstance(h, (int, float)) for h in headers if h is not None):
        # Headers are numeric — likely data, not headers. Skip this heuristic for now.
        warnings.append("Headers appear numeric — may need manual mapping")

    # Map columns
    col_map = _map_columns(headers, carrier)

    if not col_map:
        # Try row 2 as headers (some files have a title row)
        if len(df) > 1:
            alt_headers = list(df.iloc[0])
            alt_map = _map_columns(alt_headers, carrier)
            if len(alt_map) > len(col_map):
                col_map = alt_map
                df = df.iloc[1:]  # Skip the title row
                headers = alt_headers

    if not col_map:
        warnings.append(f"No columns mapped for {path.name}. Headers: {headers[:10]}")
        return [], warnings

    mapped_names = [headers[i] for i in col_map.keys()]
    unmapped = [h for i, h in enumerate(headers) if h and i not in col_map and _normalize_header(h)]
    logger.info(f"Structured extraction: mapped {len(col_map)}/{len([h for h in headers if h])} "
                f"columns from {path.name}")
    if unmapped:
        logger.debug(f"Unmapped columns: {unmapped[:10]}")

    # Extract rows
    rows = []
    from backend.config_loader import get_config_store
    store = get_config_store()
    carrier_config = store.get_carrier(carrier)
    carrier_display = carrier_config.name if carrier_config else carrier.upper()

    for _, df_row in df.iterrows():
        row_dict = {}

        for col_idx, field_name in col_map.items():
            raw_val = df_row.iloc[col_idx] if col_idx < len(df_row) else None
            if pd.isna(raw_val):
                continue
            normalized = _normalize_cell(field_name, raw_val)
            if normalized is not None:
                row_dict[field_name] = normalized

        # Skip empty rows
        if not row_dict or len(row_dict) < 2:
            continue

        # Set carrier and file metadata — carrier_name always from config (canonical),
        # never from CSV column values which may use non-standard names
        row_dict["carrier_name"] = carrier_display
        row_dict.setdefault("invoice_file_name", path.name)

        # Normalize carrier_account_number using carrier account patterns.
        # Some structured exports have composite IDs (e.g., "PREFIX_ACCT_SUB")
        # where only part matches the canonical account format. Apply the
        # carrier's account_number_patterns to extract the canonical portion.
        if "carrier_account_number" in row_dict and carrier_config:
            raw_acct = str(row_dict["carrier_account_number"])
            for pat in (carrier_config.account_number_patterns or []):
                m = re.search(pat.pattern, raw_acct)
                if m and m.group(1) != raw_acct:
                    row_dict["carrier_account_number"] = m.group(1)
                    break

        # Parse quantity from component_name if present (e.g., "Channel Fee - Qty 15")
        comp = row_dict.get("component_or_feature_name", "")
        if comp and "quantity" not in row_dict:
            qty_match = re.search(r'Qty\s*(\d+)', str(comp))
            if qty_match:
                row_dict["quantity"] = int(qty_match.group(1))

        # Determine row_type if not mapped
        if "service_or_component" not in row_dict and "row_type" not in row_dict:
            # Default: S for rows with service-level data, C for component-level
            if row_dict.get("monthly_recurring_cost") is not None:
                row_dict["row_type"] = "S"
                row_dict["service_or_component"] = "S"

        # Build ExtractedRow
        known = {k: v for k, v in row_dict.items() if k in ExtractedRow.model_fields}
        try:
            rows.append(ExtractedRow(**known))
        except Exception as e:
            warnings.append(f"Row parse error: {e}")

    logger.info(f"Structured extraction: {len(rows)} rows from {path.name}")
    return rows, warnings


def can_extract_structured(file_path: str) -> bool:
    """Quick check: is this file a spreadsheet we can extract from?"""
    suffix = Path(file_path).suffix.lower()
    return suffix in (".xlsx", ".xls", ".csv")
