"""Extraction engine — converts parsed sections into 60-field rows via LLM.

Strategy:
1. Pre-extract structured fields deterministically (regex for account#, phone#, amounts)
2. Build carrier-specific prompt with domain knowledge + schema + examples
3. Call Gemini for semantic extraction (service type, S/C designation, component names)
4. Merge regex + LLM results, assign confidence scores
"""

import asyncio
import json
import logging
import re
from decimal import Decimal
from pathlib import Path

from backend.config_loader import get_config_store, FormatConfig, DomainKnowledge
from backend.models.schemas import (
    ExtractedRow, FieldConfidence, ConfidenceLevel,
    ParsedDocument, ParsedSection, FIELD_CATEGORIES, FieldCategory,
)
from backend.services.llm import get_gemini, LLMResponse
from backend.settings import settings

logger = logging.getLogger(__name__)


# ============================================
# Prompt Construction
# ============================================

EXTRACTION_SCHEMA = """
Return a JSON array of objects. Each object is one output row with these possible fields.
IMPORTANT: Only include fields that have actual values. OMIT fields that would be null/unknown/not-present.
A sparse object with 5 fields is valid; you do not need to write "null" 40 times.

Possible fields:
{
  "row_type": "S" or "C",  // S=Service summary, C=Component/feature detail
  "billing_name": "string or null",
  "service_address_1": "string or null",
  "service_address_2": "string or null",
  "city": "string or null",
  "state": "string or null",
  "zip": "string or null",
  "country": "string or null",
  "carrier_name": "string or null",
  "master_account": "string or null",
  "carrier_account_number": "string or null",
  "sub_account_number_1": "string or null",
  "sub_account_number_2": "string or null",
  "btn": "string or null",
  "phone_number": "string or null",
  "carrier_circuit_number": "string or null",
  "additional_circuit_ids": "string or null",
  "service_type": "string or null",
  "service_type_2": "string or null",
  "usoc": "string or null",
  "service_or_component": "S" or "C" or null,
  "component_or_feature_name": "string or null",
  "monthly_recurring_cost": number or null,
  "quantity": integer or null,
  "cost_per_unit": number or null,
  "currency": "string or null",
  "charge_type": "MRC" or "NRC" or "Usage" or "Surcharge" or "Tax" or null,
  "num_calls": integer or null,
  "ld_minutes": number or null,
  "ld_cost": number or null,
  "rate": number or null,
  "ld_flat_rate": number or null,
  "point_to_number": "string or null",
  "port_speed": "string or null",
  "access_speed": "string or null",
  "upload_speed": "string or null",
  "z_location_name": "string or null",
  "z_address_1": "string or null",
  "z_city": "string or null",
  "z_state": "string or null",
  "z_zip": "string or null",
  "z_country": "string or null",
  "contract_term_months": integer or null,
  "contract_begin_date": "YYYY-MM-DD or null",
  "contract_expiration_date": "YYYY-MM-DD or null",
  "currently_month_to_month": "Yes" or "No" or null,
  "auto_renew": "Yes" or "No" or null,
  "auto_renewal_notes": "string or null",
  "contract_number": "string or null"
}
"""

EXTRACTION_RULES = """
RULES:
- Extract ONLY what is explicitly stated in the document text below
- Do NOT infer, guess, or hallucinate values
- Dollar amounts must be exact as shown (do not round or recalculate)
- Phone numbers in the format they appear in the document
- Account numbers exactly as shown (preserve spaces, dashes, formatting)
- For each charge line item, determine if it is:
  - S (Service): a summary/package-level charge (e.g., "Business Local Calling $105")
  - C (Component): an individual feature within a service (e.g., "Caller ID", "Line Charge")
- charge_type: "MRC" for monthly recurring service charges, "NRC" for one-time charges, "Usage" for per-call/minute charges, "Surcharge" for regulatory surcharges (USF, E911, Regulatory Assessment, etc.), "Tax" for government taxes (federal, state, local, sales tax)
- If a field cannot be determined from the document, OMIT IT ENTIRELY from the JSON object (do NOT include `null`)
- Only emit fields that have an actual extracted value — sparse objects are valid and expected
- Do NOT extract carrier support/helpline phone numbers as customer phone_number or btn
- Return an empty array [] if no extractable data found
- ZERO-COST ITEMS: If a line item shows $0.00 or 0.00 as its amount, extract it with monthly_recurring_cost: 0.00 (NOT null). Zero-cost items are real included features.
- NAMES AND ADDRESSES: If text appears with spaces removed (e.g., "LOSANGELES" or "520S9THST"), reconstruct natural spacing in your output: "LOS ANGELES", "520 S 9TH ST"
- COMPONENT NAMES: Extract the COMPLETE service/product name including tier descriptors (Gig, Ultra, Pro, Standard, etc.). If the name spans two lines, combine them.
"""


def build_extraction_prompt(
    section: ParsedSection,
    carrier: str,
    document_type: str,
    domain_knowledge: DomainKnowledge | None = None,
    carrier_prompt: str | None = None,
    few_shot_examples: list[dict] | None = None,
    spatial_addresses: dict[str, str] | None = None,
    correction_hints: list | None = None,
) -> str:
    """Construct full extraction prompt from all layers."""
    parts = []

    # Layer 1: System context
    parts.append(f"You are extracting telecom billing data from a {carrier.upper()} {document_type}.")

    # Layer 2: Carrier-specific prompt (from configs/carriers/{name}/prompts/)
    if carrier_prompt:
        parts.append(f"\n{carrier_prompt}")

    # Layer 3: Domain knowledge
    if domain_knowledge:
        dk_text = _format_domain_knowledge(domain_knowledge)
        if dk_text:
            parts.append(f"\nCARRIER DOMAIN KNOWLEDGE:\n{dk_text}")

    # Layer 4: Extraction rules + schema
    parts.append(EXTRACTION_RULES)
    parts.append(f"\nOUTPUT FORMAT (JSON array):\n{EXTRACTION_SCHEMA}")

    # Layer 5: Few-shot examples
    if few_shot_examples:
        examples_text = json.dumps(few_shot_examples[:3], indent=2, default=str)
        parts.append(f"\nEXAMPLES of correct extraction:\n{examples_text}")

    # Layer 6: Known corrections from past reviews
    # These are field-level corrections that reviewers have consistently made,
    # injected so the LLM avoids repeating the same mistakes.
    if correction_hints:
        corrections_lines = ["KNOWN CORRECTIONS (from past reviews — avoid these mistakes):"]
        for hint in correction_hints[:10]:  # Cap to avoid prompt bloat
            corrections_lines.append(
                f"  - Field '{hint.field_name}': do NOT extract as '{hint.wrong_value}' "
                f"→ correct value is '{hint.correct_value}' "
                f"(corrected {hint.occurrence_count} times)"
            )
        parts.append("\n" + "\n".join(corrections_lines))

    # Layer 7: Spatial address blocks (ground truth from PDF coordinates)
    # These override any addresses in the text that may be column-interleaved.
    if spatial_addresses:
        addr_lines = ["VERIFIED SERVICE LOCATIONS (from PDF spatial layout — use these for address fields):"]
        for acct, addr_text in spatial_addresses.items():
            addr_lines.append(f"  Account {acct}:")
            for line in addr_text.split("\n"):
                stripped = line.strip()
                # Skip short location/branch codes (e.g., "SB", "CARFBR") — not addresses
                if stripped and len(stripped) <= 6 and stripped.isalpha():
                    continue
                if stripped:
                    addr_lines.append(f"    {stripped}")
        addr_lines.append("NOTE: Text above may have spaces stripped. Reconstruct natural spacing "
                         "(e.g., 'GRANDJUNCTION' → 'GRAND JUNCTION', '520S9THST' → '520 S 9TH ST'). "
                         "Format: first line = company name, second = street address, last = city, state zip. "
                         "If two lines look like company names, use the LONGER one (the shorter may be an abbreviation).")
        parts.append("\n" + "\n".join(addr_lines))

    # Layer 7: Global context + section text
    if section.global_context:
        parts.append(f"\nDOCUMENT CONTEXT:\n{section.global_context}")

    parts.append(f"\nDOCUMENT TEXT TO EXTRACT FROM:\n---\n{section.text}\n---")

    return "\n".join(parts)


def _format_domain_knowledge(dk: DomainKnowledge) -> str:
    """Format domain knowledge for prompt injection."""
    parts = []
    if dk.usoc_codes:
        lines = [f"  {code}: {name}" for code, name in list(dk.usoc_codes.items())[:50]]
        parts.append("USOC Code Mappings:\n" + "\n".join(lines))
    if dk.field_codes:
        lines = [f"  {code}: {name}" for code, name in list(dk.field_codes.items())[:30]]
        parts.append("Field Codes:\n" + "\n".join(lines))
    if dk.service_types:
        lines = [f"  {code}: {name}" for code, name in dk.service_types.items()]
        parts.append("Service Types:\n" + "\n".join(lines))
    if dk.line_types:
        lines = [f"  {code}: {name}" for code, name in dk.line_types.items()]
        parts.append("Line Types:\n" + "\n".join(lines))
    return "\n\n".join(parts)


# ============================================
# Pre-extraction: Regex for structured fields
# ============================================

def regex_extract_fields(text: str, carrier: str) -> dict:
    """Pre-extract structured fields deterministically. Higher confidence than LLM."""
    fields = {}
    store = get_config_store()
    carrier_config = store.get_carrier(carrier)

    # Phone numbers (universal pattern)
    phones = re.findall(r'\b(\d{3}[-.]?\d{3}[-.]?\d{4})\b', text)
    if phones:
        fields["_phone_candidates"] = list(set(phones))

    # Dollar amounts
    amounts = re.findall(r'\$?([\d,]+\.\d{2})\b', text)
    if amounts:
        fields["_amount_candidates"] = [a.replace(",", "") for a in amounts]

    # Dates (various formats)
    dates = re.findall(
        r'(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\w+ \d{1,2},?\s*\d{4})',
        text
    )
    if dates:
        fields["_date_candidates"] = dates

    # Account numbers per carrier pattern
    if carrier_config:
        for pattern in carrier_config.account_number_patterns:
            matches = re.findall(pattern.pattern, text)
            if matches:
                match = matches[0]
                if isinstance(match, tuple):
                    match = "".join(match)
                fields["carrier_account_number"] = match
                break

    # Circuit IDs (common patterns)
    circuits = re.findall(r'\b(\d{2}/\w{4}/\d{6}/\d{3}/\w+(?:\s*/\w+)?)\b', text)
    if circuits:
        fields["carrier_circuit_number"] = circuits[0]

    return fields


# ============================================
# Core Extraction
# ============================================

async def extract_section(
    section: ParsedSection,
    carrier: str,
    document_type: str,
    format_config: FormatConfig | None = None,
    few_shot_examples: list[dict] | None = None,
    source_file_path: str | None = None,
    spatial_addresses: dict[str, str] | None = None,
    correction_hints: list | None = None,
) -> tuple[list[ExtractedRow], LLMResponse | None]:
    """Extract 60-field rows from a single parsed section.

    Returns (rows, llm_response) for cost tracking.
    """
    store = get_config_store()
    dk = store.get_knowledge(carrier)
    carrier_prompt = store.get_prompt(carrier, f"{document_type}_extraction")

    # Fallback to generic prompt when no carrier-specific prompt exists.
    # This is what makes the pipeline work for any carrier (known or unknown)
    # and any supported doc type — if configs/processing/{doc_type}_extraction.md
    # exists, we use it. Covers invoice, csr, contract, email, report.
    if not carrier_prompt:
        generic_path = Path(settings.configs_dir) / "processing" / f"{document_type}_extraction.md"
        if generic_path.exists():
            carrier_prompt = generic_path.read_text()

    # Pre-extract structured fields
    regex_fields = regex_extract_fields(section.text, carrier)

    # Build prompt
    prompt = build_extraction_prompt(
        section=section,
        carrier=carrier,
        document_type=document_type,
        domain_knowledge=dk,
        carrier_prompt=carrier_prompt,
        few_shot_examples=few_shot_examples,
        spatial_addresses=spatial_addresses,
        correction_hints=correction_hints,
    )

    # Choose model based on format knowledge
    model = settings.gemini_extraction_model  # Flash for known formats
    if format_config is None:
        model = settings.gemini_complex_model  # Pro for unknown

    # Call Gemini — multimodal for scanned PDFs, text for everything else
    gemini = get_gemini()
    try:
        if section.section_type == "scanned" and source_file_path:
            # Extract page range from section text (e.g., "pages 4-6")
            import re as _re
            page_match = _re.search(r'pages?\s+(\d+)-(\d+)', section.text)
            single_page_match = _re.search(r'pages?\s+(\d+)(?!\s*-)', section.text)

            pdf_to_upload = source_file_path
            page_label = "all pages"

            # Split PDF to the specific page range for reliable extraction.
            # Uploading only the relevant pages avoids Gemini getting confused
            # by unrelated content and prevents output token exhaustion.
            if page_match or single_page_match:
                try:
                    from pypdf import PdfReader, PdfWriter
                    import tempfile

                    if page_match:
                        start_pg = int(page_match.group(1))
                        end_pg = int(page_match.group(2))
                    else:
                        start_pg = end_pg = int(single_page_match.group(1))

                    reader = PdfReader(source_file_path)
                    writer = PdfWriter()
                    for pg_num in range(start_pg - 1, min(end_pg, len(reader.pages))):
                        writer.add_page(reader.pages[pg_num])

                    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                    writer.write(tmp)
                    tmp.close()
                    pdf_to_upload = tmp.name
                    page_label = f"pages {start_pg}-{end_pg}"
                except Exception as e:
                    logger.warning(f"PDF split failed, uploading full file: {e}")

            logger.info(f"Multimodal extraction: {Path(source_file_path).name} ({page_label})")
            prompt = prompt.replace(
                section.text,
                f"See the attached PDF document. "
                f"Extract ALL data visible in the scanned pages. "
                f"Extract EVERY line item — do not truncate or summarize."
            )
            response = await gemini.extract_multimodal(
                prompt=prompt,
                pdf_path=pdf_to_upload,
                model=settings.gemini_complex_model,
            )

            # Clean up temp file
            if pdf_to_upload != source_file_path:
                try:
                    import os as _os
                    _os.unlink(pdf_to_upload)
                except OSError:
                    pass
        else:
            response = await gemini.extract(prompt, model=model)
        raw_rows = _parse_json_response(response.content)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"JSON parse failed: {e}. Response: {response.content[:500]}")
        return [], response
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return [], None

    if not isinstance(raw_rows, list):
        raw_rows = [raw_rows]

    # Parse into ExtractedRow objects and merge regex fields
    rows = []
    for raw in raw_rows:
        try:
            row = _parse_raw_row(raw, carrier, regex_fields)
            rows.append(row)
        except Exception as e:
            logger.warning(f"Row parse error: {e}. Raw: {raw}")
            continue

    # Quality filter for scanned PDF extractions.
    # Multimodal OCR on cover/summary pages can produce junk rows (e.g., "MISC CHARGES"
    # from a billing summary, or section headers misread as line items). These have
    # vague component names and lack the specificity of real service line items.
    # Filter: a real line item should have at least one identifying field beyond
    # carrier_account_number and billing_name (which are account-level, not line-level).
    if section.section_type == "scanned" and rows:
        pre_filter = len(rows)
        line_item_fields = {
            "phone_number", "btn", "sub_account_number_1", "usoc",
            "service_type", "carrier_circuit_number", "quantity",
        }
        filtered = []
        for row in rows:
            row_dict = row.model_dump(exclude_none=True)
            has_line_specificity = any(
                row_dict.get(f) for f in line_item_fields
            )
            # Also accept rows with detailed component names (>25 chars suggests
            # a real service description, not a summary label like "MISC CHARGES")
            comp = str(row.component_or_feature_name or "")
            has_detailed_name = len(comp) > 25

            if has_line_specificity or has_detailed_name:
                filtered.append(row)

        if len(filtered) < pre_filter:
            logger.info(f"Quality filter: {pre_filter - len(filtered)} low-specificity rows "
                         f"removed from scanned extraction ({pre_filter} → {len(filtered)})")
        rows = filtered

    logger.info(f"Extracted {len(rows)} rows from section (carrier={carrier}, type={document_type})")
    return rows, response


def _parse_json_response(text: str) -> list[dict]:
    """Robust JSON parser — handles markdown code blocks, preamble text, truncated output."""
    text = text.strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    json_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if json_match:
        try:
            result = json.loads(json_match.group(1))
            return result if isinstance(result, list) else [result]
        except json.JSONDecodeError:
            pass

    # Try finding JSON array in the text
    bracket_start = text.find('[')
    if bracket_start >= 0:
        # Find matching closing bracket
        depth = 0
        for i in range(bracket_start, len(text)):
            if text[i] == '[':
                depth += 1
            elif text[i] == ']':
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(text[bracket_start:i+1])
                        return result if isinstance(result, list) else [result]
                    except json.JSONDecodeError:
                        break

    # Try finding JSON object
    brace_start = text.find('{')
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(text[brace_start:i+1])
                        return [result]
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"Could not parse JSON from response ({len(text)} chars)")


def _parse_raw_row(raw: dict, carrier: str, regex_fields: dict) -> ExtractedRow:
    """Convert raw LLM JSON to ExtractedRow, merging regex pre-extractions."""
    # Merge regex fields (higher confidence) with LLM fields
    if "carrier_account_number" in regex_fields and not raw.get("carrier_account_number"):
        raw["carrier_account_number"] = regex_fields["carrier_account_number"]

    if "carrier_circuit_number" in regex_fields and not raw.get("carrier_circuit_number"):
        raw["carrier_circuit_number"] = regex_fields["carrier_circuit_number"]

    # Set carrier name
    # Use display name from carrier config (e.g., "AT&T" not "ATT")
    if not raw.get("carrier_name"):
        store = get_config_store()
        carrier_config = store.get_carrier(carrier)
        raw["carrier_name"] = carrier_config.name if carrier_config else carrier.upper()

    # Clean numeric fields
    for field in ["monthly_recurring_cost", "cost_per_unit", "ld_cost", "rate", "ld_flat_rate", "ld_minutes"]:
        val = raw.get(field)
        if isinstance(val, str):
            val = val.replace("$", "").replace(",", "").strip()
            try:
                raw[field] = float(val) if val else None
            except ValueError:
                raw[field] = None

    for field in ["quantity", "num_calls", "contract_term_months"]:
        val = raw.get(field)
        if isinstance(val, str):
            try:
                raw[field] = int(val.strip()) if val.strip() else None
            except ValueError:
                raw[field] = None

    # Map service_or_component to row_type
    soc = raw.get("service_or_component") or raw.get("row_type")
    if soc in ("S", "C"):
        raw["row_type"] = soc
        raw["service_or_component"] = soc

    # Remove unknown keys before Pydantic
    known_fields = set(ExtractedRow.model_fields.keys())
    clean = {k: v for k, v in raw.items() if k in known_fields}

    return ExtractedRow(**clean)


def _backfill_single_line_phones(rows: list[ExtractedRow], carrier: str, document_type: str) -> list[ExtractedRow]:
    """For AT&T single-line invoices: if all rows lack phone_number, derive it from account number.

    AT&T account format: XXX XXX-XXXX XXX X (e.g., '614 408-3082 408 3').
    The first 10 digits ARE the phone number for single-line accounts.
    Only applies when NO rows in the batch have a phone_number (i.e., the invoice
    had no 'Billedfor'/'Chargesfor' phone sections).
    """
    if carrier != "att" or document_type != "invoice":
        return rows
    if not rows:
        return rows

    # Check if ANY row has a phone — if so, it's a multi-line invoice, skip
    has_phone = any(r.phone_number for r in rows)
    if has_phone:
        return rows

    # All rows lack phone — single-line account. Derive phone from account number.
    for row in rows:
        if row.carrier_account_number and not row.phone_number:
            digits = re.sub(r'[^0-9]', '', row.carrier_account_number)
            if len(digits) >= 10:
                phone_digits = digits[:10]
                # Format as XXX-XXX-XXXX
                row.phone_number = f"{phone_digits[:3]}-{phone_digits[3:6]}-{phone_digits[6:10]}"
                if not row.btn:
                    row.btn = row.phone_number

    backfilled = sum(1 for r in rows if r.phone_number)
    if backfilled:
        logger.info(f"Single-line invoice: derived phone from account for {backfilled} rows")

    return rows


# ============================================
# Document-Level Extraction
# ============================================

def _format_extraction_error(section, err) -> str:
    """Render a one-line, user-facing reason a section failed to extract.
    Strips Python tracebacks down to the meaningful clause."""
    msg = str(err)
    if "INVALID_ARGUMENT" in msg and "token count" in msg:
        # The original Gemini error is a long JSON blob — boil it down.
        m = re.search(r"input token count \((\d+)\) exceeds the maximum.*?\((\d+)\)", msg)
        if m:
            return (
                f"section '{section.section_type}' too large for the model "
                f"({int(m.group(1)):,} input tokens > {int(m.group(2)):,} max). "
                f"Pre-parser cap should have prevented this — check carrier config."
            )
    if "TimeoutError" in msg or "timed out" in msg:
        return f"section '{section.section_type}' (sub_account={section.sub_account}) timed out"
    # Generic short summary
    short = msg.splitlines()[0][:200]
    return f"section '{section.section_type}': {short}"


async def extract_document(
    parsed_doc: ParsedDocument,
    few_shot_examples: list[dict] | None = None,
    correction_hints: list | None = None,
    errors_out: list[str] | None = None,
) -> tuple[list[ExtractedRow], list[LLMResponse]]:
    """Extract all sections in parallel batches. Semaphore in GeminiClient handles rate limits.

    errors_out: optional mutable list. When provided, each section that fails
    (LLM error, timeout, JSON parse error) appends a human-readable string so
    the caller can surface "Verizon file: too large for extraction" on the
    upload card instead of a silent 0-rows result.
    """
    store = get_config_store()

    # Pre-extract spatial address blocks from PDF (if available).
    # These are injected into the LLM prompt so it can map addresses to accounts
    # using ground-truth PDF coordinates, immune to column interleaving.
    spatial_addresses: dict[str, str] = {}
    if parsed_doc.file_path.lower().endswith(".pdf"):
        from backend.pipeline.parser import _extract_address_blocks_spatial
        spatial_addresses = _extract_address_blocks_spatial(parsed_doc.file_path)

    # Find format config
    fmt_config = None
    for fmt_name, fmt in store.get_formats(parsed_doc.carrier).items():
        if fmt.name == parsed_doc.format_variant:
            fmt_config = fmt
            break

    # Skip sections that don't contain extractable per-TN data.
    # For section-marker CSRs: INDEX is a page map, REVENUE AMOUNTS is validation totals,
    # DIRECTORY DELIVERY is just a delivery flag.
    SKIP_SECTION_TYPES = {"index", "revenue amounts", "directory delivery"}
    extractable_sections = [
        s for s in parsed_doc.sections
        if s.section_type not in SKIP_SECTION_TYPES
    ]
    skipped = len(parsed_doc.sections) - len(extractable_sections)
    if skipped:
        logger.info(f"Skipping {skipped} non-extractable sections: {SKIP_SECTION_TYPES}")

    all_rows = []
    all_responses = []
    total = len(extractable_sections)
    batch_size = settings.extraction_batch_size
    section_timeout = settings.section_timeout  # Safety net only — streaming handles slow-but-active calls

    async def _extract_with_timeout(section):
        """Wrap extract_section with a safety-net timeout.

        With streaming enabled in the LLM client, Gemini thinking time no longer
        causes timeouts — tokens stream continuously. This timeout only catches
        genuinely stuck calls (network hang, API deadlock).
        503/rate-limit retries are handled by the LLM client (llm.py).
        """
        try:
            return await asyncio.wait_for(
                extract_section(
                    section=section,
                    carrier=parsed_doc.carrier,
                    document_type=parsed_doc.document_type,
                    format_config=fmt_config,
                    few_shot_examples=few_shot_examples,
                    source_file_path=parsed_doc.file_path,
                    spatial_addresses=spatial_addresses,
                    correction_hints=correction_hints,
                ),
                timeout=section_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Section timed out after {section_timeout}s: sub_account={section.sub_account}")
            return TimeoutError(f"Section {section.sub_account} timed out")

    # Process in batches. Semaphore gates API calls within each batch.
    # Streaming API ensures slow-but-active calls complete; timeout catches dead calls.
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = extractable_sections[batch_start:batch_end]
        logger.info(f"Extracting batch {batch_start+1}-{batch_end}/{total} ({len(batch)} sections)")

        tasks = [_extract_with_timeout(section) for section in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        for section, result in zip(batch, results):
            if isinstance(result, (Exception, TimeoutError)):
                logger.error(f"Section extraction failed: {result}")
                if errors_out is not None:
                    errors_out.append(_format_extraction_error(section, result))
                continue

            rows, response = result
            # Even when extract_section catches its own exception (e.g. 400
            # INVALID_ARGUMENT from oversized prompt), it returns ([], None).
            # Surface that as an error too so the user sees why a section
            # produced 0 rows.
            if not rows and response is None and errors_out is not None:
                errors_out.append(_format_extraction_error(
                    section, "LLM call failed (see logs); check model + section size"
                ))
            for row in rows:
                if section.sub_account:
                    row.sub_account_number_1 = section.sub_account
                    # Set carrier_account_number to master if available,
                    # keeping sub_account in sub_account_number_1 (matches golden convention)
                    if row.master_account:
                        row.carrier_account_number = row.master_account
                    elif not row.carrier_account_number:
                        row.carrier_account_number = section.sub_account
                if not row.invoice_file_name:
                    row.invoice_file_name = Path(parsed_doc.file_path).name

            all_rows.extend(rows)
            if response:
                all_responses.append(response)

    # Post-processing: correct addresses using spatial extraction from the PDF.
    # The LLM may misattribute addresses in multi-column layouts. Spatial extraction
    # uses PDF word coordinates to find the exact address block above each AccountNumber:
    # line, which is immune to column-interleaving issues.
    if parsed_doc.file_path.lower().endswith(".pdf") and all_rows:
        _apply_spatial_address_corrections(parsed_doc.file_path, all_rows)

    # Enforce not_available fields: null out any fields the format config says
    # this document type cannot provide. Safety net for LLM hallucination —
    # e.g., box-format CSRs have no CNTS field, so contract dates must be null
    # even if Gemini fabricates them from CD/TACC fields.
    # Skip for contracts: contracts ARE the source of contract fields — a format config's
    # not_available list (designed for invoices/CSRs) would incorrectly null out valid data.
    if fmt_config and all_rows and parsed_doc.document_type != "contract":
        _enforce_not_available(fmt_config, all_rows)

    # Post-processing: single-line invoice phone derivation.
    # AT&T single-line invoices have no "Billedfor" phone sections — the phone IS
    # the first 10 digits of the account number. This backfills phone_number on
    # rows where the LLM left it null because no phone was in the text.
    all_rows = _backfill_single_line_phones(all_rows, parsed_doc.carrier, parsed_doc.document_type)

    # Exact-dup pass: drop rows that are identical on the fingerprint fields.
    # Happens when the LLM emits the same tax/surcharge summary (phone=None,
    # charge_type=Tax|Surcharge) once per chunk across a multi-page invoice.
    before = len(all_rows)
    all_rows = _drop_exact_duplicates(all_rows)
    if before != len(all_rows):
        logger.info(f"Deduplicated {before - len(all_rows)} exact-duplicate rows (kept {len(all_rows)}/{before})")

    logger.info(f"Document extraction complete: {len(all_rows)} total rows from {total} sections")
    return all_rows, all_responses


def _drop_exact_duplicates(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """Remove rows that share the same fingerprint on the stable identity fields.

    Fingerprint fields are those that uniquely identify a billable line item:
    carrier account, phone, component name, cost, charge type, and row type.
    Confidence scores and section metadata are intentionally ignored — two rows
    with identical business content but different per-field confidence are still
    duplicates.
    """
    seen: set[tuple] = set()
    kept: list[ExtractedRow] = []
    for row in rows:
        fp = (
            row.carrier_name,
            row.carrier_account_number,
            row.sub_account_number_1,
            row.phone_number,
            row.row_type,
            row.component_or_feature_name,
            row.usoc,
            row.charge_type,
            str(row.monthly_recurring_cost) if row.monthly_recurring_cost is not None else None,
        )
        if fp in seen:
            continue
        seen.add(fp)
        kept.append(row)
    return kept


def _apply_spatial_address_corrections(file_path: str, rows: list[ExtractedRow]):
    """Fill in missing address fields using spatially-extracted address blocks.

    The LLM receives spatial addresses in the prompt and handles spacing reconstruction.
    This post-processing only fills fields the LLM left empty — it does NOT override
    LLM output, since the LLM's world knowledge produces better-formatted addresses
    than raw PDF text (which has stripped spaces).
    """
    from backend.pipeline.parser import _extract_address_blocks_spatial

    spatial_addresses = _extract_address_blocks_spatial(file_path)
    if not spatial_addresses:
        return

    for row in rows:
        acct = row.carrier_account_number
        if not acct or acct not in spatial_addresses:
            continue

        addr_text = spatial_addresses[acct]
        parsed_addr = _parse_spatial_address(addr_text)
        if not parsed_addr:
            continue

        # Only fill in missing fields — trust LLM output when present,
        # since it reconstructs spacing better than raw spatial text.
        if not row.billing_name and parsed_addr.get("billing_name"):
            row.billing_name = parsed_addr["billing_name"]
        if not row.service_address_1 and parsed_addr.get("service_address_1"):
            row.service_address_1 = parsed_addr["service_address_1"]
        if not row.city and parsed_addr.get("city"):
            row.city = parsed_addr["city"]
        if not row.state and parsed_addr.get("state"):
            row.state = parsed_addr["state"]
        if not row.zip and parsed_addr.get("zip"):
            row.zip = parsed_addr["zip"]


def _parse_spatial_address(addr_text: str) -> dict:
    """Parse a spatial address block into billing_name, service_address, city, state, zip.

    Address blocks from PDF spatial extraction contain a mix of:
    - Short location/branch codes (e.g., "SB", "CARFBR") — skip these
    - Company name (longest all-alpha line)
    - Street address (has digits, e.g., "520S9THST")
    - City, state, zip (last line, e.g., "CITYNAME,CA 90210")
    """
    lines = [l.strip() for l in addr_text.split("\n") if l.strip()]
    if not lines:
        return {}

    result = {}

    # Last line typically has city, state, zip
    last_line = lines[-1]
    city_state_match = re.match(r'(.+?),?\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?$', last_line)
    if city_state_match:
        raw_city = city_state_match.group(1).strip().rstrip(',')
        result["city"] = _reconstruct_spacing(raw_city)
        result["state"] = city_state_match.group(2)
        if city_state_match.group(3):
            result["zip"] = city_state_match.group(3)
        lines = lines[:-1]  # remove city/state/zip line

    # Classify remaining lines into: name (all-alpha), address (has digits), or code (short junk)
    name_candidates = []
    address_candidates = []
    for line in lines:
        # Short all-alpha lines are location/branch codes — skip
        if len(line) <= 6 and line.isalpha():
            continue
        # Lines with digits are street addresses
        if re.search(r'\d', line):
            address_candidates.append(line)
        else:
            name_candidates.append(line)

    # Billing name: pick the longest all-alpha candidate (most complete company name)
    if name_candidates:
        best_name = max(name_candidates, key=len)
        result["billing_name"] = _reconstruct_spacing(best_name)

    # Street address: first line with digits
    if address_candidates:
        result["service_address_1"] = _reconstruct_spacing(address_candidates[0])

    return result


def _reconstruct_spacing(text: str) -> str:
    """Basic spacing reconstruction for space-stripped PDF text.

    Only handles unambiguous structural splits (digit↔letter boundaries,
    ordinal suffixes, street type suffixes). Does NOT attempt to split
    concatenated ALL-CAPS words — that's the LLM's job, since it has
    world knowledge to resolve ambiguous word boundaries.

    This is a safety-net for post-processing, not the primary spacing strategy.
    """
    if ' ' in text and not text.replace(' ', '').isalpha():
        return text  # already has spaces and mixed content

    s = text.strip()

    # Street type suffixes — standard USPS abbreviations
    _STREET_TYPES = r'ST|AVE|RD|DR|BLVD|WAY|CT|LN|PL|CIR|PKWY|HWY|TRL|LOOP'

    # Step 1: Handle ordinals + street type combos
    # 9THST → 9TH ST, 23RDAVE → 23RD AVE
    s = re.sub(rf'(\d(?:ST|ND|RD|TH))({_STREET_TYPES})\b',
               r'\1 \2', s, flags=re.IGNORECASE)

    # Step 2: Split at digit↔letter boundaries
    s = re.sub(r'(\d)([A-Z])', r'\1 \2', s)    # 2323R → 2323 R
    s = re.sub(r'([A-Z])(\d)', r'\1 \2', s)    # S9 → S 9

    # Step 3: Re-join ordinals that got split: 9 TH → 9TH, 23 RD → 23RD
    s = re.sub(r'(\d)\s+(ST|ND|RD|TH)\b', r'\1\2', s, flags=re.IGNORECASE)

    # Clean up multiple spaces
    s = re.sub(r'\s+', ' ', s).strip()

    return s


def _enforce_not_available(fmt_config: FormatConfig, rows: list[ExtractedRow]):
    """Null out fields the format config declares as not_available.

    Safety net against LLM hallucination. For example, box-format CSRs lack
    CNTS fields, so contract_begin_date/contract_expiration_date/contract_term_months
    must always be null — even if Gemini fabricates values from CD or TACC fields.
    """
    not_available = fmt_config.extractable_fields.get("not_available", [])
    if not not_available:
        return

    nulled = 0
    for row in rows:
        for field in not_available:
            if hasattr(row, field) and getattr(row, field) is not None:
                setattr(row, field, None)
                nulled += 1

    if nulled:
        logger.info(f"Enforced not_available: nulled {nulled} fields across {len(rows)} rows "
                     f"(fields: {not_available})")


# ============================================
# Confidence Scoring
# ============================================

def score_confidence(rows: list[ExtractedRow], regex_fields: dict | None = None) -> list[dict[str, ConfidenceLevel]]:
    """Assign per-field confidence for each row."""
    all_scores = []

    for row in rows:
        scores = {}
        row_dict = row.model_dump()

        for field_name, value in row_dict.items():
            if field_name in ("field_confidence", "field_sources", "review_status"):
                continue

            category = FIELD_CATEGORIES.get(field_name)
            if category is None:
                continue

            if value is None:
                scores[field_name] = ConfidenceLevel.MISSING
                continue

            # Structured fields that can be regex-validated
            if category == FieldCategory.STRUCTURED:
                if _validate_structured_field(field_name, value):
                    scores[field_name] = ConfidenceLevel.HIGH
                else:
                    scores[field_name] = ConfidenceLevel.MEDIUM
            elif category == FieldCategory.SEMI_STRUCTURED:
                scores[field_name] = ConfidenceLevel.HIGH if value else ConfidenceLevel.MEDIUM
            elif category == FieldCategory.FUZZY:
                scores[field_name] = ConfidenceLevel.MEDIUM
            elif category == FieldCategory.CONTRACT:
                scores[field_name] = ConfidenceLevel.MEDIUM

        all_scores.append(scores)

    return all_scores


def _validate_structured_field(field_name: str, value) -> bool:
    """Validate structured fields with regex. Returns True if format looks correct."""
    str_val = str(value)

    if field_name in ("btn", "phone_number", "point_to_number"):
        return bool(re.match(r'^[\d\s\-().+]{7,20}$', str_val))

    if field_name in ("monthly_recurring_cost", "cost_per_unit", "ld_cost", "rate", "ld_flat_rate", "mrc_per_currency"):
        try:
            float(str_val)
            return True
        except (ValueError, TypeError):
            return False

    if field_name in ("quantity", "num_calls", "contract_term_months"):
        try:
            int(str_val)
            return True
        except (ValueError, TypeError):
            return False

    if field_name in ("zip", "z_zip"):
        return bool(re.match(r'^\d{5}(-\d{4})?$', str_val))

    if field_name in ("carrier_account_number", "sub_account_number_1", "sub_account_number_2", "master_account"):
        return len(str_val) >= 3  # At least some content

    return True
