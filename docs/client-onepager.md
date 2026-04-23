# Digital Direction — Telecom Inventory Extraction

**What it does.** Takes a folder of mixed carrier documents (invoices, CSRs, contracts, service reports, emails) and produces a clean, standardized 60-field service inventory — with every value traceable back to the source document, page, and section it came from.

---

## POC Scope (what we ran against your data)

| Client | Carriers | Accounts | Source Files |
|---|---|---|---|
| City of Dublin | AT&T, Spectrum | 2 | 23 files (invoices, CSRs, service guide, contracts, email correspondence) |
| NSS / Golub / Tops Markets | Windstream, Peerless | 2 | 62 files (multi-year contracts, amendments, CSRs, SD-WAN orders, reports) |

Formats handled: text PDFs, scanned PDFs (OCR), Excel (.xls/.xlsx), CSVs, `.eml`/`.msg` email files, and 472-page enterprise bills with ~90 sub-accounts.

---

## The Pipeline (5 stages, fully automated)

```
  Upload  →  1. Classify  →  2. Parse  →  3. Extract  →  4. Merge  →  5. Validate  →  Review
                                                                                          ↓
                                                                           Corrections feed back in
```

| # | Stage | What happens | Why it matters |
|---|---|---|---|
| 1 | **Classify** | Detects carrier, document type (invoice / CSR / contract / report), and format variant using filename patterns, first-page text, and an LLM fallback. Extracts account numbers up front. | Same carrier ships multiple formats (e.g., AT&T has box-format and section-marker CSRs). Routing decisions happen here so the right parser and prompt are used. |
| 2 | **Parse** | Three paths chosen per document: **Docling** for visual layouts (invoices, contracts, scanned PDFs), **pdfplumber** for mainframe-style CSRs, **pandas** for CSV/XLSX. Large bills are chunked at sub-account boundaries. | A 472-page Windstream bill becomes ~90 focused chunks instead of one unreadable blob. Two-column AT&T layouts are read in the correct order. |
| 3 | **Extract** | Per-chunk LLM extraction (Gemini 2.5) into the 60-field schema, with carrier-specific domain knowledge (USOC codes, field codes, line types) injected from YAML configs. Runs in parallel — 472-page bill takes ~10–15 seconds, not 15 minutes. | Extracts only what's explicitly in the document. No hallucinated values. Every field comes with a confidence score. |
| 4 | **Merge** | Cross-references every document for the same account: invoice for MRC/quantity, CSR for phone numbers and USOCs, contract for term/dates. Rule-based joins first, LLM (Claude) only for conflicts and gaps. | No single document has all 60 fields. This stage fills the gaps and resolves conflicts using priority rules you can audit. |
| 5 | **Validate** | Regex checks (account #, phone, zip), cross-field math (qty × unit cost = MRC), summary-total reconciliation (sub-account MRCs must sum to the Location Summary), and per-field confidence scoring. | Catches extraction errors before they reach your reviewers. |

---

## Output

Each row in the 60-field output is tagged as either a **Service (S)** summary or **Component (C)** line item, and every field carries:

- **Value** — extracted from the document
- **Source** — document ID, page, and section
- **Confidence** — HIGH (auto-accept), MEDIUM (pre-filled for review), LOW (flagged), MISSING (not present in any source)

Fields span: DD2 info, location, carrier/account/BTN, service & component detail (USOC, MRC, quantity), circuit speeds, Z-location, and contract terms (begin/expiration, auto-renew, month-to-month).

---

## Human-in-the-Loop + Self-Healing

- **Review UI** shows source document beside extracted fields; reviewer corrects LOW/MEDIUM items; HIGH-confidence rows bulk-approve.
- **Corrections are stored with full context** and feed three improvement channels: (1) few-shot examples for future extractions of the same carrier/format, (2) YAML domain-knowledge updates (new USOCs, new field mappings), (3) dynamic confidence thresholds.
- **Unknown formats are flagged, not silently failed** — a new carrier or format variant is resolved by a YAML config, not a code change.

---

## Accuracy Targets (POC)

| Field category | Target |
|---|---|
| Structured (account #, phone #, amounts, dates) | **>98%** |
| Semi-structured (address, billing name) | **>90%** |
| Fuzzy (service type, component name) | **>80%** |
| Contract fields (term, dates, renewal) | **>75%** *(often absent from source documents)* |
| Document classification | **>95%** |
| New carrier onboarding | **<1 day** to first extraction, <1 week to 80%+ accuracy |

---

## Why this approach holds up

- **Carrier logic lives in YAML, not code** — adding a new carrier or format is a config change, not an engineering project.
- **Every field is auditable** — source attribution means any number can be traced back to the exact document and page.
- **Graceful degradation** — partial document sets still produce useful output; missing fields are marked MISSING (not guessed) and filled in when new documents arrive.
- **Cost-aware** — rule-based merge before LLM merge reduces cross-reference cost 60–80%.
