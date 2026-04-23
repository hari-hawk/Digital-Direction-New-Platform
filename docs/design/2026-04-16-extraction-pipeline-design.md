# Digital Direction — Telecom Document Extraction Pipeline

**Version**: 1.1 (post eng-review)
**Date**: 2026-04-16
**Author**: Rajat (TechJays) + Claude
**Status**: Design Complete — Eng Review Passed

---

## 1. Problem Statement

Telecom carriers (AT&T, Windstream, Spectrum, Peerless Network, and others) generate invoices, customer service records (CSRs), contracts, reports, and correspondence in varying formats — text PDFs, scanned PDFs, Excel files, CSVs, and emails. No single document contains all the information needed for a complete service inventory.

**Digital Direction** is a production pipeline that:
1. Ingests carrier documents in any combination
2. Classifies them by carrier, document type, and format variant
3. Extracts data into 60 standardized output fields
4. Cross-references multiple documents to fill gaps
5. Presents results for human review with confidence scoring
6. Learns from human corrections to improve over time

### POC Scope

4 accounts across 2 clients:
- **City of Dublin**: AT&T (POTS + Centrex), Spectrum (Internet + TV)
- **NSS / Golub / Tops Markets**: Windstream (Enterprise), Peerless Network (SIP trunking)

### Output Schema

60 fields organized into 10 areas:

| Area | Fields | Key Fields |
|------|--------|------------|
| DD2 Information | 3 | Status, Notes, Contract Info Received |
| File Information | 3 | Invoice File Name, Files Used, Billing Name |
| Location | 6 | Service Address, City, State, Zip, Country |
| Carrier Information | 6 | Carrier, Master Account, Account Number, Sub-Account, BTN |
| Service | 5 | Phone Number, Circuit Number, Service Type |
| Component | 9 | USOC, S/C Designation, Component Name, MRC, Quantity, Unit Cost |
| Additional Component | 7 | Charge Type, Calls, LD Minutes, LD Cost, Rate |
| Circuit Speed | 3 | Port Speed, Access Speed, Upload Speed |
| Z Location | 7 | Z Location Name, Address, City, State, Zip |
| Contract | 11 | Term, Begin/Expiration Date, Month-to-Month, Auto Renew |

Each output row represents either a **Service (S)** summary or a **Component (C)** line item. One account typically produces multiple rows.

---

## 2. Architecture Overview

```
                    ┌─────────────┐
                    │   UPLOAD    │
                    │  (batch of  │
                    │   files)    │
                    └──────┬──────┘
                           │
                ┌──────────▼──────────┐
                │  STAGE 0: CLASSIFY  │
                │  Carrier + Doc Type │
                │  + Format Variant   │
                │  + Account Number   │
                └──────────┬──────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
    │  DOCLING   │   │ RAW TEXT  │   │  PANDAS   │
    │ (visual    │   │ (mainframe│   │ (CSV/XLSX │
    │  docs)     │   │  CSRs)    │   │  files)   │
    └─────┬─────┘   └─────┬─────┘   └─────┬─────┘
          │                │                │
          └────────────────┼────────────────┘
                           │
                ┌──────────▼──────────┐
                │  STAGE 2: EXTRACT   │
                │  Per-section LLM    │
                │  (Gemini 2.5)       │
                │  Parallelized       │
                └──────────┬──────────┘
                           │
                ┌──────────▼──────────┐
                │  STAGE 3: MERGE     │
                │  Cross-reference    │
                │  (Claude)           │
                │  Source attribution │
                └──────────┬──────────┘
                           │
                ┌──────────▼──────────┐
                │  STAGE 4: VALIDATE  │
                │  Confidence scoring │
                │  Regex + cross-field│
                └──────────┬──────────┘
                           │
              ┌────────────┼────────────┐
              │                         │
        ┌─────▼─────┐           ┌──────▼──────┐
        │ AUTO-ACCEPT│           │ HUMAN REVIEW │
        │ (HIGH conf)│           │ (MED/LOW)    │
        └─────┬─────┘           └──────┬──────┘
              │                         │
              └────────────┬────────────┘
                           │
                ┌──────────▼──────────┐
                │  CORRECTIONS +      │
                │  SELF-HEALING       │
                │  FEEDBACK LOOP      │
                └─────────────────────┘
```

---

## 3. Stage 0: Classification

### Multi-Signal Approach (3 stages, escalating confidence)

**Stage A — Filename Pattern Matching** (instant, free):
- Regex patterns per carrier defined in `configs/carriers/<name>/carrier.yaml`
- Extracts carrier hint, document type hint, account number hint
- Examples: `"ATT - 6143361586496.pdf"` → AT&T invoice, account 6143361586496

**Stage B — First-Page Text Analysis** (fast, free):
- Extract text from first 1-2 pages via pdfplumber
- Match carrier names/aliases in text
- Match document type keywords (BILL, CSR, CUSTOMER SERVICE RECORD, AGREEMENT)
- Match account number patterns per carrier regex
- Match format variant signatures (e.g., `!----` vs `---LISTINGS---` for AT&T CSRs)

**Stage C — Agreement Check + LLM Fallback**:
- If A and B agree → HIGH confidence, proceed
- If A and B disagree → LLM classification (Gemini Flash, 1 cheap call)
- If still uncertain → queue for human classification

### Format Variant Detection

Each format variant has a `signature` in its YAML config containing required patterns and any-of patterns. When a document matches carrier + doc type but no known format signature:
- Flag as `UNKNOWN_FORMAT_VARIANT`
- Auto-attempt extraction with closest format (all fields LOW confidence)
- Queue for developer review to create new format config

### File-to-Account Linking

Account numbers extracted from filename and first-page text. For multi-account documents (e.g., 472-page Windstream bill), the master account is the link; sub-accounts discovered during extraction.

### Upload Hierarchy

```
Upload → Carrier → Account → Document Type → Files
```

Stored in PostgreSQL, mirrors storage path structure.

---

## 4. Stage 1: Structural Parsing

### Three Processing Paths

| Document Type | Path | Tool | Why |
|--------------|------|------|-----|
| Visual docs (invoices, contracts, scanned PDFs) | Docling | Layout analysis, TableFormer, OCR | Handles two-column layouts, complex tables, scanned images |
| Mainframe text (AT&T CSRs, Windstream CSRs) | Raw text | pdfplumber | Already structured; Docling would mangle fixed-width formatting |
| Structured data (CSV, XLSX, XLS) | pandas | openpyxl/xlrd | Direct schema mapping, minimal LLM needed |

### Chunking Strategies (from real data analysis)

**Windstream Enterprise Invoice (472 pages, ~90 sub-accounts):**
- Global context: page header on every page (master account, invoice date)
- LOCATION SUMMARY table (pages 4-13) extracted first as validation index
- Chunk boundary: `ACTIVITY FOR ACCOUNT - XXXXXX` pattern
- Each chunk: 1-3 pages (one sub-account section)
- Call detail pages: separate chunk type
- Validation: sum of extracted MRC per sub-account must match Location Summary totals

**AT&T Invoice (14 pages, two-column layout):**
- Docling separates two-column layout into correct reading order
- Chunk boundary: `Chargesfor` / `Billedfor` + phone/account number
- Last pages (contract notices, terms): separate LLM call for contract fields

**AT&T CSR — Box Format (46 pages):**
- Chunk boundary: phone number `SUBTOTAL` markers
- SERV & EQUIP ACCOUNT SUMMARY section: USOC code lookup table for the account

**AT&T CSR — Section-Marker Format (5-6 pages):**
- Section boundaries: `---LISTINGS---`, `---BILL---`, `---RMKS---`, `---EQUIPMENT---`, `---REVENUE AMOUNTS---`, `---INDEX---`
- Per-TN boundaries: phone number entries (e.g., `567 3328 B1W`)
- Contract data in TACC/CNTS/RMKR fields

**Spectrum (2-4 pages):**
- Small enough for single LLM call
- Two variants detected by account number format (9-digit enterprise vs 16-digit consolidated)

**Peerless (CSV/XLSX):**
- pandas ingestion, direct column mapping, minimal LLM

### Global Context Injection

Every chunk sent to the LLM includes a global context prefix:
```
Master Account: {account_number}
Invoice/Document: {doc_reference}
Period: {billing_period}
Customer: {billing_name}
Carrier: {carrier_name}
```

This ensures each chunk has the account-level context even though it only contains a section of the document.

---

## 5. Stage 2: Per-Section LLM Extraction

### Model Selection

| Situation | Model | Why |
|-----------|-------|-----|
| Known format, known carrier | Gemini 2.5 Flash | Fast, cheap, sufficient for known patterns |
| Known carrier, new format variant | Gemini 2.5 Pro | More capable, handles ambiguity better |
| Unknown carrier (zero-shot) | Gemini 2.5 Pro | Needs maximum reasoning for unseen layouts |
| Scanned PDFs (OCR) | Gemini 2.5 Pro (multimodal) | Native PDF image understanding |

### Extraction Prompt Structure

```
[System: Carrier domain knowledge from YAML configs]
[Few-shot examples from golden data + past corrections]
[60-field Pydantic schema with descriptions and types]
[Instructions: extract only what's explicit, confidence per field, no hallucination]
---
[Global context prefix]
[Extracted section text/tables]
```

### Field-Specific Extraction Strategies

| Field Category | Strategy |
|---------------|----------|
| Account numbers, phone numbers, zip codes | Regex extraction + LLM verification |
| Dollar amounts (MRC, Unit Cost) | Table extraction (Docling) + LLM row assignment |
| Service type classification | LLM (requires domain understanding) |
| S/C row designation | LLM + rules (USOC present → C, summary line → S) |
| Contract dates | Regex for dates + LLM for begin/end/renewal classification |
| USOC code → component name | Domain knowledge lookup + LLM fallback |

### Parallelization

Sections from the same document are independent and processed concurrently:
- 472-page Windstream bill → ~90 parallel Gemini calls
- Each call: ~2 pages input, ~20-80 output rows
- Total latency: ~10-15 seconds (parallel) vs ~15 minutes (sequential)

---

## 6. Stage 3: Cross-Reference Merge

### Purpose

No single document has all 60 fields. The merge stage combines extractions from multiple documents:

| Field | Typical Source |
|-------|---------------|
| Billing Name, Address | Invoice (primary), CSR (secondary) |
| Phone Numbers, BTN | CSR (primary), Invoice (secondary) |
| MRC, Quantity, Unit Cost | Invoice (primary) |
| USOC, Component Name | CSR (primary), Service Guide (reference) |
| Contract Term, Dates | Contract (primary), CSR TACC fields, Email |
| Circuit ID, Speed | CSR or Report |
| Service Type | CSR (primary), Invoice category headers |

### Merge Logic (Claude)

```
Input: All per-document extractions for one account
       + carrier domain knowledge
       + merge priority rules

Claude's task:
1. Group rows by phone number / sub-account / service line
2. For each group, merge fields from all documents
3. Resolve conflicts using priority rules (invoice MRC > CSR MRC)
4. Fill gaps where possible (CSR phone numbers fill invoice gaps)
5. Apply S/C row logic
6. Add source attribution per field
7. Flag unresolvable conflicts for human review
```

### Source Attribution

Every field in the output carries its source:
```json
{
  "billing_name": {
    "value": "CITY OF DUBLIN",
    "source": "doc_abc123",
    "page": 1,
    "confidence": "high"
  },
  "contract_term_months": {
    "value": 12,
    "source": "doc_xyz789",
    "field": "TACC",
    "page": 1,
    "confidence": "medium"
  }
}
```

---

## 7. Stage 4: Confidence Scoring & Validation

### Per-Field Confidence

```
confidence = weighted_average(
    extraction_method_confidence,   # regex=0.95, LLM_known=0.85, LLM_unknown=0.50
    cross_validation_score,         # qty × unit_cost == MRC? phone format valid?
    source_clarity_score,           # clean table vs messy free text
    historical_accuracy,            # correction rate for this field+carrier+format
)

HIGH   (>0.85): auto-accept
MEDIUM (0.60-0.85): shown to human, pre-filled
LOW    (<0.60): shown to human, flagged as uncertain
MISSING: field not found in any source document
```

### Validation Rules

- **Regex**: account numbers, phone numbers, zip codes must match expected patterns
- **Cross-field**: quantity × cost_per_unit should equal monthly_recurring_cost
- **Summary matching**: sum of extracted sub-account MRCs must match Location Summary totals (Windstream)
- **Date logic**: contract_begin_date must be before contract_expiration_date
- **Currency**: all USD unless explicitly stated otherwise

---

## 8. Carrier Configuration System

### Directory Structure

```
configs/
├── carriers/
│   ├── att/
│   │   ├── carrier.yaml                    # Name, aliases, filename patterns, account patterns
│   │   ├── domain_knowledge/
│   │   │   ├── usoc_codes.yaml             # USOC → human name mapping
│   │   │   ├── field_codes.yaml            # BN1, TACC, CNTS → meaning
│   │   │   ├── line_types.yaml             # B1W → "Business 1-party Wire"
│   │   │   └── service_types.yaml          # BLC → "Business Local Calling"
│   │   ├── formats/
│   │   │   ├── csr_box.yaml               # Signature, chunking, field rules
│   │   │   ├── csr_section.yaml
│   │   │   ├── invoice_standard.yaml
│   │   │   └── contract_scanned.yaml
│   │   └── prompts/
│   │       ├── csr_extraction.md
│   │       ├── invoice_extraction.md
│   │       └── cross_reference.md
│   ├── windstream/
│   │   ├── carrier.yaml
│   │   ├── domain_knowledge/
│   │   ├── formats/
│   │   │   ├── invoice_kinetic.yaml
│   │   │   ├── invoice_enterprise.yaml
│   │   │   ├── csr_summary.yaml
│   │   │   └── csr_detailed.yaml
│   │   └── prompts/
│   ├── spectrum/
│   │   └── ...
│   └── peerless/
│       └── ...
├── schemas/
│   ├── output_60_fields.yaml
│   └── confidence_thresholds.yaml
└── processing/
    ├── routing_rules.yaml
    └── chunking_defaults.yaml
```

### Format Config Structure

Each format YAML contains:
- **signature**: required and any-of patterns for detection
- **processing**: which path (docling, raw_text, pandas)
- **chunking**: boundary markers, global context source, validation section
- **extractable_fields**: which of the 60 fields this format can provide, with confidence level
- **field_rules**: carrier-specific extraction rules (regex patterns, code mappings)
- **examples**: references to golden data / few-shot examples

### Maintenance

- All configs are version controlled (git)
- Changes tracked via git blame
- Carrier config is in YAML; chunking strategies are Python functions referenced by name from YAML (algorithmic logic cannot be purely declarative)
- Three roles interact with configs:
  - **Reviewer**: never touches configs (only corrects extracted data)
  - **Developer/Admin**: edits YAML configs, creates new formats
  - **Engineer**: modifies pipeline code (rare)

---

## 9. Self-Healing & Feedback Loop

### Correction Storage

Every human correction stored with full context: extracted value, corrected value, correction type (wrong_value, missing, spurious, formatting), source text snippet, carrier, format variant.

### Three Feedback Channels

**Channel 1 — Few-Shot Example Accumulation**:
Corrections become examples injected into future extraction prompts for the same carrier/format/field combination. Retrieved via pgvector similarity when relevant.

**Channel 2 — Domain Knowledge Enrichment**:
Periodic analysis of corrections identifies patterns (e.g., USOC code always corrected the same way). Generates suggestions to update carrier domain knowledge YAML files. Developer approves.

**Channel 3 — Confidence Threshold Adaptation**:
Historical correction rate per field per carrier adjusts confidence thresholds. High correction rate → lower confidence → more human review. Low correction rate → higher confidence → more auto-acceptance.

### Self-Healing Escalation Ladder

| Level | Action | Who | Frequency |
|-------|--------|-----|-----------|
| 1. Auto-correct | Known pattern from past corrections | System | Per extraction |
| 2. Human correction | Reviewer fixes data in UI | Reviewer | Per review session |
| 3. Config update | Developer edits YAML (new USOC, adjusted rules) | Developer | Weekly/monthly |
| 4. New format creation | Developer creates new format YAML | Developer | When flagged |
| 5. Code change | Engineer modifies pipeline | Engineer | Rare |

### Format Variant Flagging

Unknown format variants are flagged with: closest known format, similarity score, missing/unexpected patterns, first-page text. Developer creates new format config (YAML only, no code change) to resolve.

---

## 10. Eval Framework

### Three Modes

**Mode 1 — Golden Data Comparison**:
Claude as LLM judge compares extracted rows against known-correct golden data. Per-field scoring: CORRECT, PARTIAL, WRONG, MISSING, EXTRA. Root cause analysis: OCR_ERROR, PARSING_ERROR, CROSS_REF_ERROR, HALLUCINATION, DOMAIN_ERROR, FORMAT_ERROR.

**Mode 2 — Correction-Based Eval** (ongoing):
Tracks correction rate per field per carrier over time. Alerts on spikes (regression detection). Measures self-healing effectiveness (is correction rate decreasing?).

**Mode 3 — Self-Consistency Check** (no golden data needed):
Extract same document with different prompts/models. Flag disagreements on structured fields (account numbers, dollar amounts). Useful for validating prompt changes before deployment.

### Quality Dashboard

Tracks: overall accuracy by carrier, worst-performing fields with root causes, correction volume trends, self-healing actions taken, format flags pending.

---

## 11. Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend API | Python, FastAPI |
| Frontend | Next.js (React) |
| Database | PostgreSQL 16 + pgvector |
| Storage | GCS (prod), local filesystem (dev) |
| Document Parsing | Docling (visual docs), pdfplumber (text docs), pandas (structured) |
| LLM — Extraction | Gemini 2.5 Flash / Pro |
| LLM — Cross-Reference | Claude Sonnet (routine), Claude Opus (complex: >5 docs or >500 rows) |
| LLM — Eval Judge | Claude (Opus) |
| Embeddings | Gemini text-embedding-004 via pgvector |
| Config | YAML, version controlled |
| Local Dev | Docker Compose (PostgreSQL + pgvector) |
| Prod Deployment | GCP (Cloud Run, Cloud SQL, GCS) |

---

## 12. Local Dev Setup

```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:pg16
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: digital_direction
      POSTGRES_USER: dd_user
      POSTGRES_PASSWORD: dd_local_dev
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql
volumes:
  pgdata:
```

Storage abstraction: `LocalStorage` (dev) and `GCSStorage` (prod) implement the same interface. Path structure mirrors GCS:
```
storage/uploads/{upload_id}/{carrier}/{account}/{doc_type}/{filename}
```

---

## 13. Project Structure

```
digital_direction/
├── data/                              # Raw data (gitignored)
├── docs/design/                       # Design documents
├── configs/                           # Carrier configs (YAML, version controlled)
│   ├── carriers/{att,windstream,spectrum,peerless}/
│   ├── schemas/
│   └── processing/
├── backend/                           # Python FastAPI
│   ├── api/                           # REST endpoints
│   ├── pipeline/                      # classifier, parser, extractor, merger, validator
│   ├── models/                        # SQLAlchemy models
│   ├── services/                      # LLM clients, storage, embeddings
│   ├── config_loader.py
│   └── main.py
├── frontend/                          # Next.js
│   └── app/                           # uploads, review, dashboard, admin
├── db/init.sql                        # Schema + pgvector
├── evals/                             # Golden data, judge, reports
├── storage/                           # Local file storage (gitignored)
├── docker-compose.yml
├── .env
└── CLAUDE.md
```

---

## 14. Data Model

### Core Tables

- **uploads**: Batch upload tracking (id, client, status, file count)
- **documents**: Individual files (storage path, classification, processing status, parsed text/sections)
- **extraction_runs**: Processing run metadata (stats, config version)
- **extracted_rows**: The 60-field output (all fields + confidence JSONB + source attribution JSONB + review status)

### Self-Healing Tables

- **corrections**: Human corrections with full context + pgvector embedding for retrieval
- **format_flags**: Unknown format variant flags for developer review
- **format_signatures**: Document format embeddings for similarity-based detection

### Eval Tables

- **golden_data**: Known-correct output rows for comparison
- **eval_runs**: Evaluation results (accuracy scores, per-field breakdown, error analysis)

---

## 15. Human Review Workflow

### Web UI (Next.js)

**Review Screen**: Source document viewer (left) + extracted fields with confidence indicators (right). Inline editing for corrections. Bulk "Approve All HIGH" action.

**Excel Export/Import**: Download extracted rows as Excel with confidence color coding. Upload corrected Excel → system diffs and stores corrections.

### User Roles

- **Reviewer**: Corrects extracted data (Levels 1-2)
- **Developer/Admin**: Manages carrier configs, resolves format flags (Levels 3-4)
- **Engineer**: Pipeline code changes (Level 5, rare)

---

## 16. Partial Upload Handling (Graceful Degradation)

When only some document types are available for an account (e.g., invoice uploaded but no CSR or contract):

- The system extracts maximum fields from whatever documents are present
- Fields that require a missing document type are marked as **MISSING** (not LOW confidence — genuinely not available)
- MISSING is distinct from LOW: MISSING means "no source document could provide this", LOW means "we tried but aren't sure"
- When additional documents are uploaded later for the same account, the system re-runs the merge stage to fill previously MISSING fields
- The review UI clearly distinguishes MISSING (grey, no source) from LOW (yellow, needs verification)

---

## 17. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| LLM hallucination on dollar amounts | Cross-field validation (qty × unit = MRC), summary total matching |
| Unknown format breaks extraction | Signature detection + flagging + auto-attempt at LOW confidence |
| Two-column PDF layout mangles text | Docling layout analysis separates columns |
| 472-page bill overwhelms LLM | Chunk at sub-account boundaries, ~2 pages per LLM call, parallelized |
| CSR format variants within same carrier | Format variant detection per carrier, LLM handles both with domain knowledge |
| Scanned PDFs (AT&T contracts) | Docling OCR + Gemini multimodal as fallback |
| New carrier with no examples | Zero-shot extraction + all LOW confidence + human bootstrapping |
| Correction quality varies by reviewer | Eval framework detects reviewer disagreements, golden data as ground truth |

---

## 18. Success Criteria (POC)

| Metric | Target |
|--------|--------|
| Structured field accuracy (account#, phone#, amounts, dates) | >98% |
| Semi-structured field accuracy (address, billing name) | >90% |
| Fuzzy field accuracy (service type, component name) | >80% |
| Contract field accuracy (term, dates, renewal) | >75% (often missing from docs) |
| Classification accuracy | >95% (near-zero misclassification) |
| New carrier onboarding | <1 day to first extraction, <1 week to 80%+ accuracy |
| Human review throughput | Reviewer processes 100+ rows/hour with bulk approve |
| Self-healing | Correction rate decreases month-over-month |
| Format variant detection | 100% of unknown variants flagged (no silent failures) |

---

## 19. Eng Review Amendments (2026-04-16)

Changes made during /plan-eng-review:

### Architecture (from review)
- **ARCH-1**: LLM calls use exponential backoff + jitter. Claude as fallback if Gemini fails after 3 retries.
- **ARCH-2**: Stage 3 merge in batches of 10-15 sub-accounts, not single call.
- **ARCH-3**: Parsed text/sections stored in filesystem, DB stores path reference only.
- **ARCH-4**: Docling primary for visual docs. Gemini multimodal as fallback if Docling output quality is low.
- **ARCH-5**: SHA-256 hash dedup. Same hash = skip. Same filename + different hash = process as superseding version with audit trail.

### Performance
- **PERF-1**: asyncio.Semaphore with configurable max concurrent LLM calls (e.g., max_gemini_concurrent=30). Auto-adjusts on 429 responses.

### From Outside Voice (accepted)
- **OV-1**: Split eval: deterministic exact-match for structured fields (account#, amounts, dates), Claude judge only for fuzzy fields (service type, component name).
- **OV-4**: Stage 3 merge = rule-based first pass (join on account# + phone#, priority matrix) + LLM only for conflicts/gaps. Reduces cost 60-80%.
- **OV-9**: Async processing via FastAPI BackgroundTasks + status polling. Upload returns immediately with run_id. Upgrade to Cloud Tasks in prod.
- **OV-10**: Per-category accuracy targets (see Section 18 above).

### From Outside Voice (noted for implementation)
- **OV-2**: Add cost tracking per extraction run (LLM tokens used, estimated cost).
- **OV-3**: Chunking strategies are Python functions referenced by name from YAML (not pure YAML).
- **OV-5**: Field-level locking after human approval. Late-arriving documents don't overwrite approved fields without reviewer confirmation.
- **OV-6**: Correction guardrails: minimum 2 agreeing corrections before a pattern becomes an auto-applied few-shot example. Correction rollback mechanism.
- **OV-7**: Separate pgvector indices/queries for corrections vs format signatures (different similarity semantics).
- **OV-8**: Classification flow explicitly handles unknown carriers: if LLM classification confidence < 0.5 for all known carriers, route to "new carrier" path.
- **OV-11**: Client/tenant scoping on all queries. Reviewer sees only their assigned client's data.
- **OV-12**: Excel export: format phone numbers as text (not numeric), dates as ISO strings. Import: validate and normalize before diffing.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | -- | -- |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | -- | -- |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 6 issues (5 arch + 1 perf), all resolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | -- | -- |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | -- | -- |

- **OUTSIDE VOICE:** Claude subagent found 12 additional issues. 4 accepted as architecture changes, 8 noted for implementation.
- **CROSS-MODEL:** No tension. Review and outside voice agreed on all major points.
- **UNRESOLVED:** 0 decisions pending.
- **VERDICT:** ENG REVIEW CLEARED. Ready to implement.
