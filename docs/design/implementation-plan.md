# Digital Direction — Implementation Plan

**Created**: 2026-04-16
**Design Doc**: 2026-04-16-extraction-pipeline-design.md (v1.1, eng-reviewed)
**Principle**: Extraction quality is the #1 priority. Get documents through the pipeline early, validate against real data, iterate.

---

## Sequencing Strategy

```
Phase 1: Foundation          ━━━━━━━━ (Day 1-2)
Phase 2: Classify + Parse    ━━━━━━━━━━━━ (Day 2-4)
Phase 3: Extract             ━━━━━━━━━━━━━━━━━━━━ (Day 4-8)
Phase 4: Merge + Validate    ━━━━━━━━━━━━ (Day 8-10)
Phase 5: Review UI + API     ━━━━━━━━━━━━━━━━ (Day 10-14)
Phase 6: Eval + Self-Heal    ━━━━━━━━━━━━ (Day 14-17)
```

Extraction quality feedback by **Day 5** (Spectrum end-to-end).
All 4 carriers extracting by **Day 8**.
Full pipeline with human review by **Day 14**.
Production-ready with eval by **Day 17**.

---

## Phase 1: Foundation (Day 1-2)

Everything else depends on this. No parallelism here — sequential setup.

### Task 1.1: Project Scaffolding
**Files**: Project root structure
**What**: Initialize git repo, create directory structure, pyproject.toml, .gitignore, CLAUDE.md
```
digital_direction/
├── backend/
│   ├── api/
│   ├── pipeline/
│   ├── models/
│   ├── services/
│   ├── config_loader.py
│   └── main.py
├── frontend/        (empty for now, Phase 5)
├── configs/
│   ├── carriers/
│   ├── schemas/
│   └── processing/
├── db/
│   └── init.sql
├── evals/
├── storage/
├── data/            (existing, gitignored)
├── docs/            (existing)
├── docker-compose.yml
├── pyproject.toml
├── .env
├── .gitignore
└── CLAUDE.md
```
**Depends on**: Nothing
**Validation**: `git init`, directory exists, Python imports work

### Task 1.2: Docker Compose + PostgreSQL + pgvector
**Files**: `docker-compose.yml`, `db/init.sql`
**What**: PostgreSQL 16 with pgvector extension. Full schema from design doc Section 14: uploads, documents, extraction_runs, extracted_rows, corrections, format_flags, format_signatures, golden_data, eval_runs.
**Depends on**: 1.1
**Validation**: `docker compose up`, `psql` connects, tables exist, `CREATE EXTENSION vector` works

### Task 1.3: Python Dependencies + Backend Skeleton
**Files**: `pyproject.toml`, `backend/main.py`
**What**: Install dependencies:
- FastAPI + uvicorn (API)
- SQLAlchemy + asyncpg (DB)
- pdfplumber (PDF text extraction)
- docling (visual document parsing, will download models)
- google-generativeai (Gemini SDK)
- anthropic (Claude SDK)
- openpyxl + xlrd (Excel)
- pandas (CSV/XLSX processing)
- python-multipart (file uploads)

FastAPI app with health check endpoint.
**Depends on**: 1.1
**Validation**: `uvicorn backend.main:app`, health check returns 200

### Task 1.4: Storage Abstraction
**Files**: `backend/services/storage.py`
**What**: `StorageBackend` base class with `LocalStorage` implementation.
- `upload(local_path, remote_path) -> str`
- `download(remote_path, local_path) -> str`
- `get_url(remote_path) -> str`
- `exists(remote_path) -> bool`

Path structure: `storage/uploads/{upload_id}/{carrier}/{account}/{doc_type}/{filename}`
**Depends on**: 1.1
**Validation**: Upload a test file, verify it lands in correct path

### Task 1.5: LLM Client Wrappers
**Files**: `backend/services/llm.py`
**What**: Unified interface for Gemini and Claude calls:
- `GeminiClient`: Flash and Pro models, with retry (exponential backoff + jitter), rate limit handling (429 → wait), asyncio.Semaphore for concurrency control (configurable max_concurrent)
- `ClaudeClient`: Sonnet and Opus models, same retry logic
- `EmbeddingClient`: Gemini text-embedding-004 for pgvector
- All calls return structured responses with token usage for cost tracking

Load API keys from .env.
**Depends on**: 1.1, .env exists with keys
**Validation**: Test call to Gemini Flash with simple prompt, verify response

### Task 1.6: Config Loader
**Files**: `backend/config_loader.py`
**What**: Loads carrier YAML configs at startup:
- `load_carrier_config(carrier_name) -> CarrierConfig`
- `load_format_config(carrier_name, format_name) -> FormatConfig`
- `load_domain_knowledge(carrier_name) -> DomainKnowledge`
- `get_all_carriers() -> list[CarrierConfig]`

Pydantic models for type-safe config objects.
**Depends on**: 1.1
**Validation**: Load a test YAML, verify Pydantic model populates correctly

### Task 1.7: Carrier Configs (all 4 POC carriers)
**Files**: `configs/carriers/{att,windstream,spectrum,peerless}/`
**What**: Create YAML configs for all 4 carriers based on design doc Section 8:
- `carrier.yaml`: name, aliases, filename_patterns, account_number_patterns, first_page_signals
- `domain_knowledge/`: USOC codes (AT&T), field codes, service types
- `formats/`: signature patterns, processing path, chunking strategy, extractable_fields
- `prompts/`: extraction prompt templates (markdown files)

This is based on the real data analysis we did earlier in this session.
**Depends on**: 1.6
**Validation**: Config loader successfully loads all 4 carriers, all format signatures defined

### Task 1.8: 60-Field Output Schema
**Files**: `configs/schemas/output_60_fields.yaml`, `backend/models/schemas.py`
**What**: Pydantic model for the 60-field output row. Each field has: name, type, description, validation_regex (optional), required_level (required/conditional/optional), category (structured/semi-structured/fuzzy/contract).
**Depends on**: 1.1
**Validation**: Pydantic model instantiates with sample data from the SampleOutput Excel

---

## Phase 2: Classify + Parse (Day 2-4)

Classification and the 3 parsing paths can be built in parallel.

### Task 2.1: Document Classifier — Stage A (Filename Patterns)
**Files**: `backend/pipeline/classifier.py`
**What**: `classify_by_filename(filename: str) -> ClassificationHint`
- Iterate all carrier configs, match filename against each carrier's filename_patterns
- Return carrier_hint, doc_type_hint, account_hint, confidence
- Test against all 91 files in our data directory
**Depends on**: 1.6, 1.7
**Validation**: Run against all POC files, verify >80% get a carrier hint from filename alone

### Task 2.2: Document Classifier — Stage B (First-Page Text)
**Files**: `backend/pipeline/classifier.py` (extend)
**What**: `classify_by_content(file_path: str) -> ClassificationResult`
- Extract first 2 pages of text (pdfplumber for PDF, read first 100 lines for CSV)
- Match against carrier first_page_signals (aliases, doc_type_markers)
- Match account number patterns per carrier
- Match format variant signatures
- Return carrier, doc_type, format_variant, account_number, confidence
**Depends on**: 2.1, 1.3 (pdfplumber)
**Validation**: Run against all POC files, verify >95% correctly classified

### Task 2.3: Document Classifier — Stage C (Agreement + LLM Fallback)
**Files**: `backend/pipeline/classifier.py` (extend)
**What**: `classify_document(file_path: str) -> FinalClassification`
- Run Stage A and Stage B
- If agree → HIGH confidence, done
- If disagree → call Gemini Flash with first 2 pages: "What carrier and document type is this?"
- If still uncertain → mark as unclassified for human review
- Format variant flagging: if carrier + doc type known but no format signature matches → create format_flag record
**Depends on**: 2.1, 2.2, 1.5
**Validation**: Run against all POC files. Zero misclassifications on the 4 known carriers.

### Task 2.4: Parser — Docling Path (Visual Documents)
**Files**: `backend/pipeline/parser.py`
**What**: `parse_visual_document(file_path: str, format_config: FormatConfig) -> ParsedDocument`
- Initialize Docling with TableFormer model
- Parse PDF → DoclingDocument
- Extract tables with structure preserved
- Split into sections based on format_config chunking boundaries
- For each section: extract text + tables + global context
- If Docling fails or quality is low → flag for Gemini multimodal fallback
- Output: list of ParsedSection objects (text, tables, page_numbers, global_context)
**Depends on**: 1.3 (docling), 1.6
**Validation**: Parse AT&T 14-page invoice → verify two-column layout separated correctly. Parse Spectrum 2-page invoice → verify charge table extracted.

### Task 2.5: Parser — Raw Text Path (Mainframe CSRs)
**Files**: `backend/pipeline/parser.py` (extend)
**What**: `parse_mainframe_text(file_path: str, format_config: FormatConfig) -> ParsedDocument`
- Extract full text with pdfplumber
- Split into sections based on format_config section_markers or per_tn_boundary patterns
- Inject global context (account header) into each section
- Handle page continuations (section spans across page break → merge)
- Output: list of ParsedSection objects
**Depends on**: 1.3 (pdfplumber), 1.6
**Validation**: Parse AT&T CSR box format (46 pages) → correct phone number sections. Parse AT&T CSR section format (5 pages) → correct TN boundaries.

### Task 2.6: Parser — Structured Data Path (CSV/XLSX)
**Files**: `backend/pipeline/parser.py` (extend)
**What**: `parse_structured_data(file_path: str) -> ParsedDocument`
- pandas read_csv or read_excel
- Detect column headers, map to known schemas
- Handle XLS (xlrd) and XLSX (openpyxl) formats
- Output: ParsedSection with dataframe + column mapping
**Depends on**: 1.3 (pandas, openpyxl)
**Validation**: Parse Peerless DID CSV → correct DID/destination/provider columns. Parse Windstream Report XLS → read successfully.

### Task 2.7: Parser — Windstream Enterprise Chunking
**Files**: `backend/pipeline/parser.py` (extend, Windstream-specific chunking function)
**What**: `chunk_windstream_enterprise(parsed_text: str) -> list[ParsedSection]`
- Extract LOCATION SUMMARY table (pages 4-13) as validation index
- Detect `ACTIVITY FOR ACCOUNT` boundaries
- Each chunk = one sub-account section (1-3 pages)
- Separate CALL DETAIL pages into their own chunks
- Inject global context (master account, invoice number, period) into each chunk
**Depends on**: 2.5
**Validation**: Parse 472-page Windstream bill → ~90 sub-account chunks. Sum of Location Summary totals matches known total ($116,258.78).

### Task 2.8: Deduplication
**Files**: `backend/services/dedup.py`
**What**: `check_duplicate(file_path: str, upload_id: str) -> DedupResult`
- SHA-256 hash the file
- Check against documents table
- Same hash → return existing document reference (skip processing)
- Same filename, different hash → mark as superseding version, process as new
**Depends on**: 1.2 (DB)
**Validation**: Upload same file twice → second upload skipped. Upload modified file with same name → processed as new version.

---

## Phase 3: Extract (Day 4-8)

This is where extraction quality is proven. Start with simplest carrier, build up.

### Task 3.1: Extraction Engine Core
**Files**: `backend/pipeline/extractor.py`
**What**: `extract_section(section: ParsedSection, carrier_config: CarrierConfig, format_config: FormatConfig) -> list[ExtractedRow]`
- Build extraction prompt from: carrier prompt template + domain knowledge + few-shot examples + 60-field schema + section text
- Call Gemini (Flash for known formats, Pro for unknown)
- Parse structured JSON response into ExtractedRow objects
- Per-field confidence scoring (regex-validated → HIGH, LLM-only → per format default)
- Handle extraction failures gracefully (log error, skip section, flag for review)
**Depends on**: 1.5, 1.6, 1.7, 1.8
**Validation**: Extract a single Spectrum section → valid 60-field rows

### Task 3.2: Extract — Spectrum (simplest carrier, end-to-end validation)
**Files**: Spectrum prompt templates, test script
**What**: Full pipeline for Spectrum:
- Classify 4 Spectrum files → correct carrier + type + format
- Parse: pdfplumber (simple enough, Docling optional) → sections
- Extract: Gemini Flash with Spectrum prompt → 60-field rows
- Output to Excel for manual comparison with expected output
- **This is the first real extraction quality checkpoint.**
**Depends on**: 2.3, 2.4 or 2.5, 3.1
**Validation**: Extract all 4 Spectrum invoices. Manually compare against expected field values. Structured fields (account#, amounts) should be >98% correct.

### Task 3.3: Extract — AT&T Invoices
**Files**: AT&T invoice prompt template
**What**:
- Docling on 14-page two-column invoice → verify column separation
- If Docling fails → Gemini multimodal fallback (send page images)
- Chunk at "Chargesfor" / "Billedfor" boundaries
- Extract per sub-line charges
- Extract contract terms from last pages (free text)
**Depends on**: 2.4, 3.1
**Validation**: Extract AT&T invoice 6143361586496 (14 pages, ~35 sub-lines). All BLC $105.00 charges captured. Federal Access Charges captured. Contract term from page 11-12 captured.

### Task 3.4: Extract — AT&T CSRs (both formats)
**Files**: AT&T CSR prompt templates (box + section), USOC knowledge base
**What**:
- Detect format variant (box vs section-marker) from classifier
- Box format: chunk by SUBTOTAL markers, parse fixed-width columns
- Section format: chunk by TN entries, extract TACC/CNTS/RMKR for contract fields
- Map USOC codes to human names using domain_knowledge/usoc_codes.yaml
- Extract per-phone-number: line type, features, charges, descriptions
**Depends on**: 2.5, 3.1, 1.7 (AT&T domain knowledge)
**Validation**: Extract Dublin CSR (46 pages) → all phone numbers captured, USOC codes mapped to names. Extract Choctaw CSR (5 pages) → contract info from TACC extracted (1YR term, BLC plan).

### Task 3.5: Extract — Windstream Enterprise
**Files**: Windstream Enterprise prompt template
**What**:
- Use chunked sub-account sections from Task 2.7
- Extract per sub-account: service products (Dynamic IP, SD-WAN, UCaaS), line items with qty/unit/amount, circuit IDs, surcharges, taxes
- Parallel extraction with semaphore (max 30 concurrent Gemini calls)
- Validate: sum MRC per sub-account against Location Summary
**Depends on**: 2.7, 3.1
**Validation**: Extract Windstream 2389882 bill (472 pages, ~90 sub-accounts). Total MRC sums match Location Summary ($116,258.78). Circuit IDs captured. All product categories (Dynamic IP, SD-WAN VMware, UCaaS Standard, LAN Services) extracted.

### Task 3.6: Extract — Windstream Kinetic + CSRs
**Files**: Windstream Kinetic prompt template, CSR prompt template
**What**:
- Kinetic invoices (small, 3-4 pages): single LLM call per document
- Windstream CSR summary format: extract account name, TNs, product counts
- Windstream CSR detailed format (021942648): extract per-TN features with charges
**Depends on**: 2.5, 3.1
**Validation**: Extract Kinetic invoice 021942648 → CENTREX, BUSINESS INTERNET charges captured. CSR 021942648 → all TNs and feature charges match invoice.

### Task 3.7: Extract — Peerless Network
**Files**: Peerless prompt template
**What**:
- CSV/XLSX: pandas ingestion for DIDs and subscriptions (mostly schema mapping)
- Bill PDF: Gemini extraction for SIP trunk charges
- Signed quotes: extract contract terms, line items, quantities, MRC
**Depends on**: 2.6, 3.1
**Validation**: DID CSV → all 1500+ DIDs mapped. Subscriptions → channel fees, DID types, MRC captured. Signed quote → 36 month term, $9,953.99/mo total captured.

### Task 3.8: Extract — Email/MSG Files
**Files**: `backend/pipeline/email_parser.py`
**What**:
- Parse .eml (Python email module) and .msg (extract-msg library)
- Extract email body text
- Send to Gemini for extraction of: contract terms, rates, renewal dates, account context
- These are supplementary — fill gaps that invoices/CSRs don't cover
**Depends on**: 3.1
**Validation**: Parse AT&T email → extract $105/line BLC rate, 12M term, $57.50 LD plan.

---

## Phase 4: Merge + Validate (Day 8-10)

### Task 4.1: Rule-Based Merge (First Pass)
**Files**: `backend/pipeline/merger.py`
**What**: `rule_based_merge(extractions: dict[str, list[ExtractedRow]]) -> list[MergedRow]`
- Group all extracted rows by account_number + phone_number (or sub_account)
- For each group, merge fields using priority matrix:
  - MRC, charges → invoice (primary)
  - Phone numbers, BTN, USOC → CSR (primary)
  - Contract term, dates → contract (primary), CSR TACC (secondary), email (tertiary)
  - Address → invoice (primary), CSR (secondary)
  - Circuit ID → CSR (primary), invoice (secondary)
- Track source attribution per field
- Identify CONFLICTS (same field, different values from different docs) and GAPS (field available in no doc)
**Depends on**: Phase 3 complete
**Validation**: Merge AT&T invoice + CSR for account 6143361586496 → phone numbers from CSR fill invoice gaps. MRC from invoice takes priority.

### Task 4.2: LLM Merge for Conflicts (Second Pass)
**Files**: `backend/pipeline/merger.py` (extend)
**What**: `llm_resolve_conflicts(conflicts: list[ConflictRecord], context: MergeContext) -> list[Resolution]`
- Only called for CONFLICTS and GAPS that rule-based merge couldn't resolve
- Batch conflicts in groups of 15 sub-accounts
- Claude Sonnet for routine merges, Opus for complex (>5 docs or >500 rows)
- Claude receives: conflicting values, source documents, carrier context
- Returns: resolved value + reasoning + confidence
**Depends on**: 4.1, 1.5 (Claude client)
**Validation**: Simulate a conflict (invoice says address A, CSR says address B) → Claude resolves with reasoning.

### Task 4.3: S/C Row Logic
**Files**: `backend/pipeline/merger.py` (extend)
**What**: Apply Service (S) vs Component (C) row designation:
- Summary charge line → S row (service-level)
- Individual feature/USOC line → C row (component-level)
- Rules: if USOC is present and maps to a feature → C. If it's a package/bundle total → S.
- Carrier-specific rules in config (e.g., AT&T BLC package = S, individual features within = C)
**Depends on**: 4.1
**Validation**: AT&T account → S row for "Bus Local Calling Unlimited B $105", C rows for "Line Charge", "CO Termination", etc.

### Task 4.4: Confidence Scoring + Validation
**Files**: `backend/pipeline/validator.py`
**What**: `validate_and_score(rows: list[MergedRow]) -> list[ScoredRow]`
- Per-field confidence: weighted average of extraction_method, cross_validation, source_clarity, historical_accuracy
- Regex validation: phone numbers, account numbers, zip codes
- Cross-field: quantity * cost_per_unit == monthly_recurring_cost
- Summary matching: Windstream sub-account MRC sums vs Location Summary
- Date logic: begin_date < expiration_date
- Mark fields: HIGH (>0.85), MEDIUM (0.60-0.85), LOW (<0.60), MISSING
**Depends on**: 4.1 or 4.2
**Validation**: Run on all extracted data. Structured fields should be mostly HIGH. Contract fields should be MEDIUM/LOW. Missing fields correctly identified.

### Task 4.5: Pipeline Orchestrator
**Files**: `backend/pipeline/orchestrator.py`
**What**: `process_upload(upload_id: str) -> ExtractionRun`
- Orchestrate the full pipeline: classify → parse → extract → merge → validate
- Track progress in extraction_runs table
- Handle errors gracefully (one failed document doesn't kill the batch)
- Store results in extracted_rows table
- Return extraction run stats (docs processed, rows extracted, confidence breakdown)
**Depends on**: 2.3, 2.4-2.7, 3.1-3.8, 4.1-4.4
**Validation**: Upload all City of Dublin files → full pipeline runs → extracted_rows table populated.

### Task 4.6: CLI Extraction Runner (for testing without UI)
**Files**: `backend/cli.py`
**What**: Command-line interface to run extraction without the web UI:
```bash
python -m backend.cli extract --input-dir data/Consolidated\ -\ City\ of\ Dublin\ Project\ Files/ --client "City of Dublin"
python -m backend.cli extract --input-dir data/Consolidated\ -\ NSS\ Project\ Files/ --client "NSS"
python -m backend.cli export --run-id <id> --output results.xlsx
```
Essential for rapid iteration on extraction quality before UI exists.
**Depends on**: 4.5
**Validation**: Run CLI on all POC data, export to Excel, manual review.

---

## Phase 5: Review UI + API (Day 10-14)

### Task 5.1: FastAPI Endpoints
**Files**: `backend/api/uploads.py`, `backend/api/review.py`, `backend/api/corrections.py`, `backend/api/exports.py`
**What**:
- `POST /api/uploads` — create upload, accept files, trigger background processing
- `GET /api/uploads` — list uploads with status
- `GET /api/uploads/{id}/status` — polling endpoint for processing progress
- `GET /api/review/{upload_id}/rows` — get extracted rows with confidence, filterable
- `GET /api/review/{upload_id}/rows/{row_id}/sources` — get source document snippets for a row
- `PATCH /api/review/rows/{row_id}` — submit correction for a field
- `POST /api/review/{upload_id}/bulk-approve` — approve all HIGH confidence rows
- `GET /api/exports/{upload_id}/excel` — download as Excel
- `POST /api/imports/corrections` — upload corrected Excel, diff and store corrections
- Background task processing: FastAPI BackgroundTasks for extraction pipeline
**Depends on**: 4.5
**Validation**: API tests for each endpoint. Upload file via API → extraction runs → rows returned.

### Task 5.2: Next.js Project Setup
**Files**: `frontend/` directory
**What**: Initialize Next.js with:
- App Router
- Tailwind CSS
- shadcn/ui components
- API client (fetch wrapper pointing to FastAPI backend)
**Depends on**: Nothing (can start in parallel with backend)
**Validation**: `bun dev` serves the app, basic page renders

### Task 5.3: Upload Dashboard Screen
**Files**: `frontend/app/uploads/page.tsx`
**What**:
- List of uploads with status badges (classifying, extracting, reviewing, complete)
- Upload button with drag-and-drop file area
- Per-upload: file count, carrier breakdown, progress bar during extraction
- Polling for status updates while processing
**Depends on**: 5.1, 5.2
**Validation**: Upload files → see progress → status changes to "reviewing" when done

### Task 5.4: Review Screen (Core Workflow)
**Files**: `frontend/app/review/[uploadId]/page.tsx`
**What**:
- Left panel: source document viewer (PDF.js for PDFs, highlighted source text snippets)
- Right panel: extracted fields table with confidence indicators (green/yellow/red/grey)
- Inline editing: click a field → edit → save correction
- Row navigation: previous/next row, filter by confidence level
- Bulk approve: "Approve All HIGH" button
- Field source attribution: hover on a field → see which document + page it came from
**Depends on**: 5.1, 5.2
**Validation**: Open review for an upload → see rows with confidence → edit a field → correction saved in DB

### Task 5.5: Excel Export/Import
**Files**: `backend/api/exports.py`, `frontend/app/review/` (export/import buttons)
**What**:
- Export: Generate Excel with 60-field columns. Phone numbers as TEXT (not numeric). Dates as ISO strings. Confidence color coding (green/yellow/red). Header row matches sample output format.
- Import: Upload corrected Excel. Diff against current extraction. Store each changed field as a correction with context. Validate: normalize Excel mutations (scientific notation → phone number, locale dates → ISO).
**Depends on**: 5.1
**Validation**: Export → open in Excel → modify some fields → re-import → corrections stored correctly. Phone numbers survive round-trip.

### Task 5.6: Admin Screen (Carrier Config Viewer)
**Files**: `frontend/app/admin/page.tsx`
**What**:
- List all carriers with their format configs
- View format flags (unknown variants pending review)
- View correction statistics per carrier per field
- Simple read-only view for POC (config editing stays in YAML files)
**Depends on**: 5.1, 5.2
**Validation**: See all 4 carriers listed. See format flag count (should be 0 for known formats).

---

## Phase 6: Eval + Self-Heal (Day 14-17)

### Task 6.1: Golden Data Ingestion
**Files**: `evals/golden/`, `backend/services/golden.py`
**What**:
- Accept golden data (correct 60-field output) for comparison
- Store in golden_data table linked to carrier + account
- Format: Excel or JSON matching the 60-field schema
- When golden data is provided later, it drops into this framework
**Depends on**: 1.2
**Validation**: Import golden data for one account → stored in DB → retrievable

### Task 6.2: Deterministic Eval (Structured Fields)
**Files**: `evals/judge.py`
**What**: `eval_structured(extracted: list[Row], golden: list[Row]) -> EvalResult`
- Exact match for: account numbers, phone numbers, BTN, zip codes, dollar amounts, dates, quantities
- Normalized comparison: strip whitespace, normalize phone formats, round dollars to 2 decimal
- Per-field scoring: CORRECT, WRONG, MISSING, EXTRA
- Overall accuracy by field category (structured, semi-structured, fuzzy, contract)
**Depends on**: 6.1
**Validation**: Run eval with known-good extraction → 100% correct. Run with intentionally wrong data → errors detected.

### Task 6.3: LLM Judge (Fuzzy Fields)
**Files**: `evals/judge.py` (extend)
**What**: `eval_fuzzy(extracted: list[Row], golden: list[Row]) -> EvalResult`
- Claude Opus judges: service_type, component_name, service_type_2, charge_type, auto_renewal_notes
- Scores: CORRECT, PARTIAL (close but not exact, e.g., "BLC" vs "Business Local Calling"), WRONG
- Root cause analysis per error: OCR_ERROR, PARSING_ERROR, CROSS_REF_ERROR, HALLUCINATION, DOMAIN_ERROR
**Depends on**: 6.1, 1.5 (Claude client)
**Validation**: Run with sample data → judge returns reasonable scores and root causes.

### Task 6.4: Eval Runner + Report
**Files**: `evals/runner.py`, `evals/reports/`
**What**: `run_eval(extraction_run_id: str, golden_data_id: str) -> EvalReport`
- Combine deterministic + LLM judge results
- Generate report: overall accuracy, per-carrier accuracy, per-field-category accuracy, worst fields, error root causes
- Save report to evals/reports/ and eval_runs table
- Compare against success criteria (Section 18): structured >98%, semi-structured >90%, fuzzy >80%, contract >75%
**Depends on**: 6.2, 6.3
**Validation**: Run full eval on POC data → report generated → meets or identifies gaps vs targets.

### Task 6.5: Correction Feedback — Few-Shot Injection
**Files**: `backend/services/feedback.py`
**What**: `get_relevant_corrections(carrier: str, format: str, field: str, context: str) -> list[Correction]`
- When extracting a field, retrieve top-5 most relevant past corrections for same carrier + format + field
- Use pgvector similarity on correction_context embeddings
- Inject as few-shot examples in the extraction prompt
- Guardrail: correction only becomes a few-shot example after 2+ agreeing corrections for the same pattern
**Depends on**: 1.5 (embedding client), corrections table populated from human review
**Validation**: Create 3 corrections for same pattern → next extraction includes them as few-shot. Single correction → not included (guardrail).

### Task 6.6: Domain Knowledge Enrichment
**Files**: `backend/services/feedback.py` (extend)
**What**: `analyze_corrections(carrier: str) -> list[KnowledgeSuggestion]`
- Periodic analysis: group corrections by carrier + field
- If same USOC code corrected >3 times to same value → suggest adding to usoc_codes.yaml
- If same field systematically wrong for a format → suggest prompt adjustment
- Output: suggestions list for developer review (not auto-applied)
**Depends on**: corrections table populated
**Validation**: Create 5 corrections mapping USOC "PGO9T" → "BLC" → system suggests adding to AT&T domain knowledge.

### Task 6.7: Quality Dashboard
**Files**: `frontend/app/dashboard/page.tsx`
**What**:
- Overall accuracy trend (if golden data exists) or correction rate trend
- By carrier: accuracy / correction rate
- Worst-performing fields with root causes
- Correction volume (last 7/30 days)
- Self-healing activity (few-shot examples added, knowledge suggestions)
- Format flags pending review
**Depends on**: 5.1, 6.4
**Validation**: Dashboard shows real data from extraction runs and corrections.

### Task 6.8: Cost Tracking
**Files**: `backend/services/cost_tracker.py`
**What**: Track LLM token usage and estimated cost per extraction run:
- Per-call: input tokens, output tokens, model used
- Per-run: total tokens, total cost, cost per account, cost per document
- Stored in extraction_runs table
- Displayed in dashboard
**Depends on**: 1.5 (LLM clients return token counts)
**Validation**: Run extraction → cost tracked → visible in dashboard.

---

## Phase Dependencies (Parallelization Opportunities)

```
LANE A (Backend Core):
  1.1 → 1.2 → 1.3 → 1.5 → 1.6 → 1.7 → 2.1 → 2.2 → 2.3
                   → 1.4 (parallel with 1.5)
                   → 1.8 (parallel with 1.5)

LANE B (Parsers — parallel after 1.3):
  2.4 (Docling)
  2.5 (Raw text)     } can run in parallel
  2.6 (Structured)
  2.7 (Windstream chunking, depends on 2.5)

LANE C (Extraction — sequential by carrier):
  3.1 → 3.2 (Spectrum) → 3.3 (AT&T inv) → 3.4 (AT&T CSR)
       → 3.5 (Windstream, parallel with AT&T)
       → 3.7 (Peerless, parallel with Windstream)
       → 3.6 (WS Kinetic, parallel)
       → 3.8 (Email, parallel)

LANE D (Frontend — independent until API ready):
  5.2 → 5.3 (Upload)
     → 5.4 (Review)    } parallel after 5.2
     → 5.6 (Admin)

LANE E (Eval — after extraction works):
  6.1 → 6.2 → 6.3 → 6.4
  6.5 + 6.6 (after corrections exist)
  6.7 + 6.8 (parallel with eval)
```

**Maximum parallelism**: Lanes A+B can run simultaneously in Phase 1-2. Within Phase 3, Spectrum is first (quality validation), then AT&T/Windstream/Peerless in parallel. Frontend (Lane D) starts in Phase 5 but scaffolding can begin earlier.

---

## NOT in Scope (Deferred)

| Item | Why Deferred | When to Add |
|------|-------------|-------------|
| GCS storage (production) | Local filesystem sufficient for POC | Before production deployment |
| Cloud Run / Cloud SQL deployment | Docker Compose sufficient for POC | Before production deployment |
| Celery / Cloud Tasks | FastAPI BackgroundTasks sufficient for POC scale | When processing >50 uploads/day |
| Multi-user auth | Single user for POC | Before multi-tenant production |
| Tenant isolation (OV-11) | Single client per POC test | Before serving multiple clients |
| Auto-correct (self-healing Level 1) | Needs correction data to accumulate first | After 1+ months of corrections |
| Confidence threshold adaptation | Needs historical correction data | After 1+ months of corrections |
| Format signature embeddings (pgvector) | Rule-based classification sufficient for 4 carriers | When carrier count > 10 |

---

## Risk Checkpoints

| Day | Checkpoint | Go/No-Go Criteria |
|-----|-----------|-------------------|
| Day 5 | Spectrum end-to-end | Can we extract all charge line items from Spectrum invoices correctly? |
| Day 6 | AT&T CSR extraction | Can we parse both CSR formats and map USOC codes to names? |
| Day 7 | Windstream chunking | Does the 472-page bill chunk correctly into ~90 sub-accounts? |
| Day 8 | Cross-doc merge | Does invoice + CSR merge produce complete rows? |
| Day 10 | Full pipeline | All 4 carriers extract and merge. Manual Excel review looks reasonable. |
| Day 14 | Review UI | Can a reviewer correct fields and approve rows in the web UI? |
| Day 17 | Eval against golden | Per-category accuracy meets targets (when golden data provided)? |
