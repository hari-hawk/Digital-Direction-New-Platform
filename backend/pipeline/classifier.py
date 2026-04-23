"""Document classifier — 3-stage: filename → first-page text → LLM fallback.

Identifies carrier, document type, format variant, and account number.
Client-specific identifiers learned dynamically via known_accounts DB table.
"""

import logging
import re
from pathlib import Path

import pdfplumber

from backend.config_loader import get_config_store, CarrierConfig, FormatConfig
from backend.models.schemas import ClassificationResult, ConfidenceLevel

logger = logging.getLogger(__name__)


# ============================================
# Stage A: Filename Pattern Matching
# ============================================

def classify_by_filename(filename: str) -> ClassificationResult:
    """Match filename against carrier patterns. Fast, free, no file I/O."""
    store = get_config_store()
    best_match = ClassificationResult(method="filename")
    best_score = 0.0

    for carrier_name, carrier_config in store.get_all_carriers().items():
        for doc_type, patterns in carrier_config.filename_patterns.items():
            for pattern in patterns:
                if pattern.matches(filename) and pattern.confidence > best_score:
                    best_score = pattern.confidence
                    best_match = ClassificationResult(
                        carrier=carrier_name,
                        document_type=doc_type,
                        confidence=ConfidenceLevel.HIGH if pattern.confidence > 0.85 else ConfidenceLevel.MEDIUM,
                        method="filename",
                    )

    # Extract account number from filename if carrier found
    if best_match.carrier:
        carrier_config = store.get_carrier(best_match.carrier)
        if carrier_config:
            for acct_pattern in carrier_config.account_number_patterns:
                matches = re.findall(acct_pattern.pattern, filename)
                if matches:
                    # Flatten tuple matches from grouped regex
                    match = matches[0]
                    if isinstance(match, tuple):
                        match = "".join(match)
                    best_match.account_number = match
                    break

    return best_match


# ============================================
# Stage B: First-Page Text Analysis
# ============================================

def extract_first_pages_text(file_path: str, max_pages: int = 2) -> str:
    """Extract text from first N pages of PDF, or first lines of text files."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            with pdfplumber.open(file_path) as pdf:
                texts = []
                for i, page in enumerate(pdf.pages[:max_pages]):
                    text = page.extract_text()
                    if text:
                        texts.append(text)
                result = "\n".join(texts)
                if not result.strip():
                    logger.info(f"PDF has no extractable text (likely scanned): {file_path}")
                return result
        except Exception as e:
            logger.warning(f"PDF text extraction failed for {file_path}: {e}")
            return ""

    elif suffix in (".csv", ".tsv", ".txt"):
        try:
            with open(file_path, "r", errors="ignore") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= 100:
                        break
                    lines.append(line)
                return "".join(lines)
        except Exception as e:
            logger.warning(f"Text file read failed for {file_path}: {e}")
            return ""

    elif suffix in (".xlsx", ".xls"):
        try:
            import pandas as pd
            if suffix == ".xlsx":
                df = pd.read_excel(file_path, nrows=20, engine="openpyxl")
            else:
                df = pd.read_excel(file_path, nrows=20, engine="xlrd")
            return df.to_string()
        except Exception as e:
            logger.warning(f"Excel read failed for {file_path}: {e}")
            return ""

    elif suffix in (".msg", ".eml"):
        return _extract_email_text(file_path)

    elif suffix == ".docx":
        try:
            import docx
            doc = docx.Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs[:50])
        except Exception as e:
            logger.warning(f"DOCX read failed for {file_path}: {e}")
            return ""

    return ""


def _extract_email_text(file_path: str) -> str:
    """Extract text from .msg or .eml files."""
    path = Path(file_path)

    if path.suffix.lower() == ".msg":
        try:
            import extract_msg
            msg = extract_msg.Message(file_path)
            body = msg.body or ""
            # Fallback to HTML body if plain text is empty
            if not body.strip() and msg.htmlBody:
                html = msg.htmlBody
                if isinstance(html, bytes):
                    html = html.decode("utf-8", errors="ignore")
                # Strip HTML tags to get plain text
                body = re.sub(r'<[^>]+>', ' ', html)
                body = re.sub(r'&nbsp;', ' ', body)
                body = re.sub(r'&[a-z]+;', '', body)
                body = re.sub(r'\s+', ' ', body).strip()
            return f"Subject: {msg.subject}\n\n{body}"
        except Exception as e:
            logger.warning(f"MSG parse failed: {e}")
            return ""

    elif path.suffix.lower() == ".eml":
        try:
            import email
            with open(file_path, "r", errors="ignore") as f:
                msg = email.message_from_file(f)
            subject = msg.get("Subject", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            return f"Subject: {subject}\n\n{body}"
        except Exception as e:
            logger.warning(f"EML parse failed: {e}")
            return ""

    return ""


def _classify_structured_deep_scan(file_path: str) -> ClassificationResult:
    """Deep scan structured files (XLSX/CSV) for carrier signals in data values.

    When standard text signals fail (e.g., no carrier name in headers or first rows),
    this scans cell values for:
    1. Carrier name/alias strings in any cell
    2. Account numbers matching carrier-specific patterns (by pattern specificity)

    Returns ClassificationResult with carrier if a clear winner is found.
    """
    store = get_config_store()
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix not in (".xlsx", ".xls", ".csv"):
        return ClassificationResult(method="structured_deep_scan")

    # Read up to 100 rows of data
    try:
        import pandas as pd
        if suffix == ".csv":
            df = pd.read_csv(file_path, nrows=100)
        else:
            engine = "openpyxl" if suffix == ".xlsx" else "xlrd"
            df = pd.read_excel(file_path, nrows=100, engine=engine)
    except Exception as e:
        logger.debug(f"Structured deep scan failed to read {file_path}: {e}")
        return ClassificationResult(method="structured_deep_scan")

    if df.empty:
        return ClassificationResult(method="structured_deep_scan")

    # Collect all string cell values (headers + data)
    all_values = []
    for col in df.columns:
        all_values.append(str(col))
    for _, row in df.iterrows():
        for val in row:
            if pd.notna(val):
                all_values.append(str(val))

    combined_text = " ".join(all_values).lower()

    # Score each carrier by alias matches in cell values
    carrier_scores: dict[str, int] = {}
    for carrier_name, carrier_config in store.get_all_carriers().items():
        score = 0

        # Check aliases in cell values (weighted: each distinct alias match = 5 points)
        for alias in carrier_config.aliases:
            if len(alias) >= 3 and alias.lower() in combined_text:
                score += 5

        # Check carrier display name
        if carrier_config.name.lower() in combined_text:
            score += 10

        # Check account number patterns against cell values
        # Only use patterns with specificity > 7 digits (avoid false positives)
        for acct_pattern in (carrier_config.account_number_patterns or []):
            pattern_str = acct_pattern.pattern
            # Skip very generic patterns (e.g., bare \b(\d{7})\b)
            if re.fullmatch(r'\\b\(\\d\{\d+\}\)\\b', pattern_str):
                continue
            try:
                matches = re.findall(pattern_str, combined_text)
                if matches:
                    score += len(matches) * 2
            except re.error:
                pass

        if score > 0:
            carrier_scores[carrier_name] = score

    if not carrier_scores:
        return ClassificationResult(method="structured_deep_scan")

    # Pick winner if clear (best score >= 5, and at least 2x the runner-up)
    sorted_carriers = sorted(carrier_scores.items(), key=lambda x: -x[1])
    best_carrier, best_score = sorted_carriers[0]
    runner_up_score = sorted_carriers[1][1] if len(sorted_carriers) > 1 else 0

    if best_score >= 5 and (runner_up_score == 0 or best_score >= runner_up_score * 2):
        carrier_config = store.get_carrier(best_carrier)
        result = ClassificationResult(
            carrier=best_carrier,
            confidence=ConfidenceLevel.MEDIUM,
            method="structured_deep_scan",
        )

        # Try to detect doc_type from column headers
        headers_lower = " ".join(str(c).lower() for c in df.columns)
        if carrier_config and carrier_config.first_page_signals and carrier_config.first_page_signals.doc_type_markers:
            for doc_type, markers in carrier_config.first_page_signals.doc_type_markers.items():
                if any(m.lower() in headers_lower for m in markers):
                    result.document_type = doc_type
                    break
        # Default to "report" for structured files without clear doc_type
        if not result.document_type:
            result.document_type = "report"

        # Extract account number
        if carrier_config:
            for acct_pattern in (carrier_config.account_number_patterns or []):
                matches = re.findall(acct_pattern.pattern, combined_text)
                if matches:
                    match = matches[0]
                    if isinstance(match, tuple):
                        match = "".join(match)
                    result.account_number = match
                    break

        logger.info(f"Structured deep scan: {best_carrier} (score={best_score}, "
                    f"runner_up={runner_up_score}) for {path.name}")
        return result

    logger.debug(f"Structured deep scan inconclusive for {path.name}: {carrier_scores}")
    return ClassificationResult(method="structured_deep_scan")


def classify_by_content(file_path: str) -> ClassificationResult:
    """Analyze first-page text for carrier signals, doc type, format variant."""
    store = get_config_store()
    text = extract_first_pages_text(file_path)

    if not text:
        return ClassificationResult(method="first_page")

    result = ClassificationResult(method="first_page")

    # Find carrier by text signals
    for carrier_name, carrier_config in store.get_all_carriers().items():
        signals = carrier_config.first_page_signals
        if not signals:
            continue

        # Check required_any — at least one alias must appear
        if signals.required_any:
            found = any(sig.lower() in text.lower() for sig in signals.required_any)
            if not found:
                continue

        result.carrier = carrier_name

        # Detect document type from markers
        if signals.doc_type_markers:
            for doc_type, markers in signals.doc_type_markers.items():
                if any(marker.lower() in text.lower() for marker in markers):
                    result.document_type = doc_type
                    break

        # Detect format variant
        fmt = store.match_format_variant(carrier_name, text)
        if fmt:
            result.format_variant = fmt.name

        # Extract account number
        for acct_pattern in carrier_config.account_number_patterns:
            matches = re.findall(acct_pattern.pattern, text)
            if matches:
                match = matches[0]
                if isinstance(match, tuple):
                    match = "".join(match)
                result.account_number = match
                break

        result.confidence = ConfidenceLevel.HIGH
        break  # Found carrier, stop searching

    # For structured files: if standard text signals didn't find a carrier,
    # do a deeper scan of data values for carrier-specific patterns
    if not result.carrier and Path(file_path).suffix.lower() in (".xlsx", ".xls", ".csv"):
        deep_result = _classify_structured_deep_scan(file_path)
        if deep_result.carrier:
            result = deep_result

    return result


# ============================================
# Stage C: Agreement Check + LLM Fallback
# ============================================

async def classify_by_llm(file_path: str, text: str) -> ClassificationResult:
    """Open-ended LLM classification — returns whatever carrier the document declares.

    Unlike stages A and B which require pre-configured patterns, this accepts ANY
    carrier name (Frontier, Lumen, Verizon, etc.). Downstream the carrier_key is
    normalized (lowercased / slug); if no carrier-specific config exists, the
    extractor falls back to the generic prompts in configs/processing/.

    If the document genuinely has no carrier mark (blank scan, handwritten note),
    carrier is returned as null.
    """
    from backend.services.llm import get_gemini
    import json as _json
    import re as _re

    store = get_config_store()
    known_keys = list(store.get_all_carriers().keys())

    prompt = f"""You are classifying a telecom document. Read the text and return a JSON object with these fields.

Fields:
- carrier_name: the exact carrier/provider name as printed on the document (e.g., "Frontier", "Lumen", "Verizon Business", "AT&T"). If no carrier is visible, return null.
- carrier_key: a lowercase slug derived from carrier_name ("Verizon Business" -> "verizon", "AT&T" -> "att", "Lumen" -> "lumen"). Use a known value from {known_keys} when the carrier matches one; otherwise invent a reasonable slug. Return null if carrier_name is null.
- document_type: one of ["invoice", "csr", "contract", "report", "did_list", "subscription", "email", "service_guide", "other"].
- account_number: the primary account number if visible, otherwise null.
- confidence: "high", "medium", or "low".

Document filename: {Path(file_path).name}

First 3000 characters of document text:
{text[:3000]}

Return ONLY the JSON object, no prose.
"""

    try:
        gemini = get_gemini()
        response = await gemini.extract(prompt)
        data = _json.loads(response.content)

        carrier_key = data.get("carrier_key")
        if isinstance(carrier_key, str):
            # Canonicalize the slug: lowercase, non-alphanumerics -> removed
            carrier_key = _re.sub(r'[^a-z0-9]', '', carrier_key.lower()) or None
        else:
            carrier_key = None

        conf_raw = data.get("confidence", "low")
        conf = ConfidenceLevel(conf_raw) if conf_raw in ("high", "medium", "low") else ConfidenceLevel.LOW

        return ClassificationResult(
            carrier=carrier_key,
            document_type=data.get("document_type"),
            account_number=data.get("account_number"),
            confidence=conf,
            method="llm",
        )
    except Exception as e:
        logger.error(f"LLM classification failed: {e}")
        return ClassificationResult(method="llm")


# ============================================
# Known Accounts DB Lookup
# ============================================

async def lookup_known_account(identifier: str, db_session=None) -> str | None:
    """Check known_accounts table for previously learned carrier mapping."""
    if not db_session:
        return None
    # TODO: implement DB lookup when SQLAlchemy session available
    # SELECT carrier FROM known_accounts WHERE identifier = :identifier
    return None


async def store_known_account(
    identifier: str, identifier_type: str, carrier: str, document_id: str = None, db_session=None
) -> None:
    """Store new carrier mapping learned from classification."""
    if not db_session:
        return
    # TODO: implement DB insert when SQLAlchemy session available
    # INSERT INTO known_accounts (identifier, identifier_type, carrier, learned_from_document_id)
    pass


# ============================================
# Main Classification Pipeline
# ============================================

def validate_carrier_post_extraction(
    classified_carrier: str,
    extracted_rows: list,
    file_path: str,
) -> str | None:
    """Post-extraction validation: check if extracted carrier_name contradicts classification.

    If a majority of extracted rows contain a carrier_name that maps to a different
    carrier than what the classifier chose, return the correct carrier name.
    Returns None if classification looks correct.
    """
    if not extracted_rows or not classified_carrier:
        return None

    store = get_config_store()
    carrier_counts: dict[str, int] = {}

    for row in extracted_rows:
        # Get carrier_name from row (works with both dict and ExtractedRow objects)
        carrier_val = row.get("carrier_name") if isinstance(row, dict) else getattr(row, "carrier_name", None)
        if not carrier_val:
            continue

        carrier_val_lower = str(carrier_val).lower()

        # Check which carrier this value matches
        for cname, cconfig in store.get_all_carriers().items():
            if cconfig.name.lower() in carrier_val_lower or carrier_val_lower in cconfig.name.lower():
                carrier_counts[cname] = carrier_counts.get(cname, 0) + 1
                break
            for alias in cconfig.aliases:
                if alias.lower() in carrier_val_lower or carrier_val_lower in alias.lower():
                    carrier_counts[cname] = carrier_counts.get(cname, 0) + 1
                    break

    if not carrier_counts:
        return None

    # Find the most common carrier in extracted data
    best_carrier = max(carrier_counts, key=carrier_counts.get)
    best_count = carrier_counts[best_carrier]
    total = sum(carrier_counts.values())

    # If >60% of rows point to a different carrier, flag it
    if best_carrier != classified_carrier and best_count > total * 0.6:
        logger.warning(
            f"Post-extraction carrier mismatch for {Path(file_path).name}: "
            f"classified={classified_carrier}, extracted data says={best_carrier} "
            f"({best_count}/{total} rows). Reclassifying."
        )
        return best_carrier

    return None


async def classify_document(file_path: str, db_session=None) -> ClassificationResult:
    """Full 3-stage classification: filename → content → LLM fallback.

    Returns best classification with carrier, doc_type, format_variant, account_number.
    """
    filename = Path(file_path).name

    # Stage A: Filename
    stage_a = classify_by_filename(filename)
    logger.info(f"Stage A ({filename}): carrier={stage_a.carrier}, type={stage_a.document_type}, conf={stage_a.confidence}")

    # Stage B: First-page content
    stage_b = classify_by_content(file_path)
    logger.info(f"Stage B ({filename}): carrier={stage_b.carrier}, type={stage_b.document_type}, variant={stage_b.format_variant}")

    # Check known_accounts for identifier-based lookup
    if stage_a.account_number and not stage_a.carrier:
        known_carrier = await lookup_known_account(stage_a.account_number, db_session)
        if known_carrier:
            stage_a.carrier = known_carrier
            stage_a.confidence = ConfidenceLevel.HIGH
            logger.info(f"Known account match: {stage_a.account_number} → {known_carrier}")

    # Agreement logic
    if stage_a.carrier and stage_b.carrier:
        if stage_a.carrier == stage_b.carrier:
            # Both agree — HIGH confidence
            result = ClassificationResult(
                carrier=stage_a.carrier,
                document_type=stage_b.document_type or stage_a.document_type,
                format_variant=stage_b.format_variant,
                account_number=stage_b.account_number or stage_a.account_number,
                confidence=ConfidenceLevel.HIGH,
                method="filename+first_page",
            )
        else:
            # Disagree — LLM fallback
            logger.warning(f"Classification disagreement: A={stage_a.carrier}, B={stage_b.carrier}. Using LLM.")
            text = extract_first_pages_text(file_path)
            result = await classify_by_llm(file_path, text)

    elif stage_b.carrier:
        # Only content matched — trust it
        result = stage_b

    elif stage_a.carrier:
        # Only filename matched — try to enrich with format variant + account from text
        # Common for mainframe CSRs that don't mention carrier name in text
        store = get_config_store()
        text = extract_first_pages_text(file_path)
        if text:
            fmt = store.match_format_variant(stage_a.carrier, text)
            if fmt:
                stage_a.format_variant = fmt.name
                stage_a.confidence = ConfidenceLevel.HIGH  # filename + signature = strong match
            # Try to extract account number from text
            carrier_config = store.get_carrier(stage_a.carrier)
            if carrier_config and not stage_a.account_number:
                for acct_pattern in carrier_config.account_number_patterns:
                    matches = re.findall(acct_pattern.pattern, text)
                    if matches:
                        match = matches[0]
                        if isinstance(match, tuple):
                            match = "".join(match)
                        stage_a.account_number = match
                        break
        if not stage_a.format_variant:
            stage_a.confidence = ConfidenceLevel.MEDIUM
        result = stage_a

    else:
        # Neither matched — LLM fallback
        text = extract_first_pages_text(file_path)
        if text:
            result = await classify_by_llm(file_path, text)
        else:
            result = ClassificationResult(
                confidence=ConfidenceLevel.LOW,
                method="unclassified",
            )

    # Learn new account mapping if classified successfully
    if result.carrier and result.account_number and result.confidence in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
        await store_known_account(result.account_number, "account_number", result.carrier, db_session=db_session)

    # Detect file type from extension
    suffix = Path(file_path).suffix.lower()
    file_type_map = {
        ".pdf": "pdf", ".xlsx": "xlsx", ".xls": "xls", ".csv": "csv",
        ".docx": "docx", ".msg": "msg", ".eml": "eml",
    }
    result.file_type = file_type_map.get(suffix, "unknown")

    logger.info(f"Final ({filename}): carrier={result.carrier}, type={result.document_type}, "
                f"variant={result.format_variant}, account={result.account_number}, conf={result.confidence}")

    return result
