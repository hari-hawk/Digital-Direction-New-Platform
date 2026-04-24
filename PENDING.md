# Pending Items — Current

**Generated:** April 24, 2026
**Supersedes:** The "Known Issues & Pending Work" section of `DIGITAL_DIRECTION_HANDOFF.md`
**Companion to:** `TODOS.md` (historical, don't edit)

This file captures items that are still open **as of today**, grouped by priority.
Items closed since the Apr 22 handover are listed at the bottom so the delta
is visible.

---

## 1. Critical — blocks production or correctness

### 1.1 Spectrum contract enrichment (0% → ≥95%)
- Contract data exists in cache but is not enriched into invoice rows.
- AT&T has equivalent enrichment working; the Spectrum hookup needs to be
  mirrored (carrier-specific wiring in the merge phase).
- Carried over from handover item #5.

### 1.2 Spectrum row-structure gap (72% match)
- Golden expects **3 rows per location** (S bare + C main + C addon).
- We produce **2** (S with component + C addon). ~128 golden rows affected.
- Fix: adjust `configs/carriers/spectrum/prompts/invoice_extraction.md` to
  split the S row.
- Carried over from handover item #6.

### 1.3 Windstream structured accuracy (87.4% vs 95% target)
- 7.6% gap. Per-field audit needed — most likely account/phone format
  normalization edge cases.
- Carried over from handover item #7.

### 1.4 Production authentication
- Current login is a client-side passphrase (`dd2026`) — no real auth.
- Needed for any deployment outside a single dev machine.
- Carried over from handover item #13.

---

## 2. Important — should fix before broader testing

### 2.1 Per-client master-data store
**New item requested Apr 24. Scope confirmed Apr 24: per-client only (option B).**

Today every project extracts in isolation. When the same client has a new
monthly invoice, we re-extract from scratch — we don't carry forward the
contract terms, expected service addresses, or known circuits from prior
months.

**Decision (confirmed):** build a **per-client persistent knowledge store**.
The unit of organization is the client (not the carrier, not a global library
across clients). Every piece of reference data is scoped to one client.

**What it holds (per client):**
- **Contracts** — signed contract documents + extracted terms (rate, term
  months, begin/end dates, auto-renew, service schedules) indexed by account
  number.
- **Known-good service addresses** — analyst-confirmed per-location map
  (fixes the AT&T 1586 golden gap where 16 addresses map to one CSR).
- **Account alias map** — equivalences the analyst has confirmed (e.g.,
  master account ↔ sub-accounts ↔ carrier format variants).
- **Circuit register** — known SDWAN / MPLS circuit IDs from portals that
  aren't on bills (fixes the Windstream SDWAN golden gap).

**Extraction-time behavior when a client has a master store:**
1. Pipeline detects client from upload metadata → loads that client's store.
2. Pre-fills known contract fields into merged rows (rate, term, dates).
3. Validates uploaded doc against stored data:
   - Invoice MRC == contract rate? If not → flag `Rate mismatch`.
   - Service address still matching known address? If not → flag.
   - Circuit ID still listed? New circuit not in register → flag.
4. Where store has ground truth, use it; where it doesn't, use extraction.

**Architecture sketch:**

```sql
CREATE TABLE clients (
    id UUID PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE client_reference_data (
    id UUID PRIMARY KEY,
    client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
    kind VARCHAR(50) NOT NULL,    -- 'contract' | 'address' | 'account_alias' | 'circuit'
    account_number VARCHAR(100),   -- scope to account when applicable
    data JSONB NOT NULL,           -- kind-specific payload
    source VARCHAR(50),            -- 'analyst_excel' | 'uploaded_contract' | 'portal_export'
    confirmed_by VARCHAR(255),
    confirmed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_client_ref_client_kind ON client_reference_data (client_id, kind);
CREATE INDEX idx_client_ref_account ON client_reference_data (account_number);
```

**Loading mechanisms (ordered by likely priority):**
1. Excel import — analyst drops a master-data spreadsheet per client.
2. Contract PDF upload — a dedicated "Contract" doc type that goes into the
   store instead of as a line item.
3. Auto-learn from confirmed corrections — when analyst approves a row, the
   confirmed values become part of the store.

**Effort estimate:** ~1 week for schema + import UI + merge integration.
Validation UX (flagging mismatches) is another ~2-3 days.

### 2.2 Gemini cost attribution ($270 anomaly)
- Pipeline math estimates ~$4-5 for 2 days of testing. GCP reports $270.
- Need to drill into GCP Console → Billing → cost breakdown by API.
- May be: multimodal image tokens, 503 retry overhead, or unrelated services.
- Carried over from handover item #4.

### 2.3 Sidecar merge bug — Phase B of cross-granularity merge
- The billing-address sidecar columns (`billing_address_1`, etc.) populate
  correctly in `_merge_group` but NOT in `_enrich_from_account_sources`
  (Phase B of `cross_granularity_merge`).
- End-to-end test earlier showed 0 rows populated despite a mixed
  CSR+invoice project with known address divergence.
- Need to replicate the sidecar-capture logic in the Phase B enrichment loop.

### 2.4 AT&T chunk-1 investigation (follow-up)
- Sparse schema + `thinking_budget=0` + `max_output_tokens=65536`
  unblocked chunks 2–5 (+8.7× rows on the test file).
- Chunk 1 (pages 1-3) still returns **0 rows** end-to-end, even though a
  single-chunk probe extracts 50+ rows.
- Root cause unclear — possibly content-shape specific, not the prompt.
- Narrower investigation: compare probe response vs end-to-end path for
  the same chunk 1 input to find where rows are being dropped.

---

## 3. Nice-to-have — reliability & UX polish

### 3.1 Request staggering
- Fire chunks 2–3 at a time vs. all at once, to reduce 503 risk.
- Less urgent now that Vertex has largely eliminated 503s, but
  good hygiene.
- Carried over from handover item #8.

### 3.2 Model fallback chain
- Flash → Pro → Flash-Lite on repeated 503.
- Pro doesn't accept `thinking_budget=0` so the fallback needs to branch on
  model capability.
- Carried over from handover item #9.

### 3.3 Circuit breaker pattern
- Pause all requests after N consecutive 5xx errors to avoid burning cost
  during an outage.
- Carried over from handover item #10.

### 3.4 PDF export
- Button exists in UI, implementation TODO.
- Carried over from handover item #12.

### 3.5 Frontend bulk actions
- "Select all" across multiple rows in Results, bulk re-extract, bulk move.
- Currently have Re-extract per-project; no multi-select.

### 3.6 Review-inline surface (already scoped)
- Results is now the single data-extraction surface (Review removed from
  sidebar nav). Click-to-review pane still lives at the `review` route.
  Consider folding it fully inline as a drawer/modal in Results.

---

## 4. Future exploration — not blocking

### 4.1 LangFuse LLM observability — **paused**
- Infrastructure is fully wired (container, SDK, trace hooks, settings,
  `.env` keys). Currently dormant — `LANGFUSE_ENABLED=false` + container
  stopped to conserve memory.
- **To wake up:** `docker-compose start langfuse` → sign up at
  `http://localhost:3100` → generate API keys → paste into `.env` with
  `LANGFUSE_ENABLED=true` → restart backend.
- Worth turning on when: prompt iteration becomes heavy, extraction failures
  need prompt-level introspection, or we add more engineers who need shared
  visibility.

### 4.2 LangSmith migration (post-LangFuse learnings)
- Cloud-based observability with pattern detection + regression alerts +
  programmatic API for self-healing feedback loops.
- Dependency: LangFuse POC learnings + Phase 7 architecture planning.
- Carried over from old `TODOS.md`.

### 4.3 Graphify codebase graph
- Hooks exist in `.claude/settings.json`, gated on presence of
  `graphify-out/graph.json`. Currently dormant (no graph generated).
- Waiting on install instructions from Rajat. When available, run
  `graphify .` once and the hooks auto-wake.

### 4.4 Auto-register unknown carriers
- Decision made today: we do NOT auto-register. Unknown carriers stay as
  `Validate carrier` status, analyst confirms + registers manually.
- Revisit if analyst workload becomes the bottleneck.

---

## 5. Golden-data gaps — analyzed Apr 24

Out of the 5 gaps carried from the handover, **3 are now closed** via
judge-only fixes (extraction pipeline unchanged). 2 remain and are tied to
the per-client master-data store from §2.1.

### Closed — judge-only normalization (eval accuracy only, pipeline unchanged)

✅ **Gap 4: AT&T `auto_renew` + `auto_renewal_notes` (577 rows)**
Field genuinely doesn't exist in any AT&T document. Analyst writes Yes/No
from contract memory. Added both to `analyst_judgment` list in
`configs/processing/eval_config.yaml` — judge now SKIPs them instead of
scoring WRONG on every extraction.

✅ **Gap 5: "Not mentioned" / "NA" literal strings (512 fields)**
Analyst convention for "document doesn't mention this field." Our pipeline
emits null. Added `_is_empty_placeholder()` helper in `evals/judge.py` that
treats `not mentioned`, `na`, `n/a`, `none`, `null`, `nil`, `-`, `--`, `---`
as equivalent to empty — only in the eval comparison step.

✅ **Gap 2: AT&T component naming (120+ rows)**
CSR descriptive names ("Business Local Calling Unlimited B") vs invoice
mashed names ("BusLocalCallingUnlimitedB") vs raw USOC codes ("1MB") all
now compare correctly. Added `_normalize_component_name()` in `judge.py`
that:
1. Looks up USOC codes against `configs/carriers/att/domain_knowledge/usoc_codes.yaml` → canonical name
2. Splits camelCase / PascalCase boundaries
3. Expands common telecom abbreviations (bus → business, ctx → centrex, etc.)
4. Compares stripped lowercase tokens

Convention: **CSR naming wins** when names differ — per the merge priority
already established in the pipeline (CSR is the source of truth for service
identity and location).

### Still open — require per-client master-data store (§2.1)

⏸ **Gap 1: AT&T 1586 address mapping (332 rows, 35% of AT&T eval)**
Golden has 16 distinct service addresses mapped from a single CSR that
groups them under one billing address. Analyst uses an external master
location DB. Resolved when the per-client store (2.1) holds the
analyst-confirmed location map.

⏸ **Gap 3: Windstream SDWAN circuit IDs (762 rows)**
Golden values like `2389874-SDWAN-1` come from a vendor portal, not from
any billing document. Resolved when the per-client store (2.1) holds the
circuit register (`kind="circuit"` entries).

### Projected eval impact (Gaps 2, 4, 5 closed)

Roughly 1,200 previously-failing rows now SKIP or match correctly.
Back-of-envelope:
- AT&T match rate: 76% → ~85-88%
- Overall structured / semi-structured accuracy: modest bump (+2-4%)
- Fuzzy / component-name accuracy: meaningful bump on AT&T

Exact numbers require re-running evals on the current golden data.

---

## 5.5. Classifier accuracy on bulk uploads (Apr 24)

### What happened
NSS Project upload surfaced real-world classification problems:
- 76 of 213 files (36%) unclassified
- 16 false "ACC Business" matches (no ACC Business in upload)
- 9 false AT&T matches
- Carrier names fragmented: `Sinchmessagemedia`/`Messagemedia`, `Statetelephoneco`/`Statetelephonecompany`, `Bcn`/`Bcntelecom`

### What was fixed (Phase 1 — judge-free, extraction-path unchanged)

- **Alias scanning added to filename + content classification** — 63 registry
  carriers previously invisible to stage-B content scan now match via
  word-boundary alias regex.
- **Short-alias guardrail** — aliases < 4 chars dropped from alias-scan paths
  (eliminates `ACC` → "access charge" substring false positives).
- **Stage C LLM canonicalization** — `match_carrier_name()` now runs on the
  LLM's raw output, collapsing `sinchmessagemedia`/`messagemedia` → canonical
  `Message Media`.
- **AT&T first_page_signals tightened** — `SBC` removed as a standalone
  signal (Windstream CSRs reference SBC historical codes, caused false
  positives). Kept as an alias for secondary alias matching.
- **16 new carrier configs** — Allstar Systems, BCN Telecom, Champlain
  Technology, Crown Castle, Delhi Telephone Company, Directv, FirstLight,
  Granite Telecommunications, Message Media, Mid Hudson Communications,
  Spectrotel, State Telephone Company, TDS Telecom, T-Mobile, Verizon
  Wireless, Charter Communications.

### Results

| Metric | Before | After | Δ |
|--------|-------|-------|---|
| Total carriers in registry | 67 | **83** | +16 |
| Unclassified files (NSS) | 76/213 (36%) | **34/213 (16%)** | **−55%** |
| ACC Business false positives | 16 | **0** | −100% |
| AT&T false positives | 9 | **5** | −44% |
| Carrier-name fragmentation | 6+ variants for 3 carriers | **0** (all canonical) | — |

### Remaining classifier work

⏸ **34 still-unclassified files** — customer-named filenames
(`Price Chopper …`), scanned PDFs (`Scan_20250407.pdf`), Excel inventories.
None have the carrier name visible in filename or first-page text. Need:
- OCR (Docling path) for scanned PDFs — **already implemented**, need to
  verify it's wired in the UI classify flow
- Stage C LLM fallback — **already exists** in `classify_by_llm`, verify it's
  reached in the UI upload path (may be gated on agreement-check)
- Per-client master-data (§2.1) — when the client "Golub Corporation" is
  known, files under that client default to the carriers they use

⏸ **5 residual AT&T false positives** — likely documents that genuinely
mention "AT&T" (e.g., shared network references in other carriers' contracts).
Needs content inspection + possibly requiring 2+ signals instead of 1.

---

## 6. Closed since the Apr 22 handover

For completeness, these items were on the handover's pending list and are
now **done** as of commit `704bbd5`:

| Handover item | Resolved by | Commit |
|--------------|------------|--------|
| Gemini Flash 503 outages | Switched to Vertex AI; sparse JSON schema; `thinking_budget=0` | multiple |
| AT&T chunk-1 prompt fix | Validated (chunks 2–5 unblocked). Chunk 1 still has deeper issue — see 2.4 | `bab6316` |
| `compliance_flags` missing from `init.sql` | Schema now includes it + `00_create_langfuse_db.sh` | `fd98db3` |
| LangFuse API keys needed | Infrastructure kept; deliberately paused, not blocking | — |
| Review page absorbed into Results | Review removed from sidebar nav | `bab6316` |
| Generic pipeline for any carrier | Generic prompts + 67-carrier registry + canonicalization | `4dd8ad5`, `704bbd5` |
| Unknown-carrier handling | `Validate carrier` status + `rows_needing_carrier_validation` surfaced | `704bbd5` |
| Spend cap ($100) with sidebar meter | Spend ledger + `/api/spend` + UI meter | `fd98db3` |
| Bin (soft-delete + restore + purge) | All endpoints + UI page + Empty-bin button | `fd98db3`, `bab6316` |
| Re-extract + Download ZIP | Both available on every project card | `fd98db3` |
| Validation auto-run every extraction | `validate_rows` + compliance flags on every upload | `bab6316` |
| Multi-upload reading-order | Prompt enforces PDF order + `extraction_order` field | — |
| Data grid compact + 63-col toggle + horizontal scroll | Results page | `bab6316` |
| Theme toggle + neumorphic UI | Google-style sliding switch + `.neu` utility classes | `bab6316` |

---

## Suggested next-session pickup order

When you're ready to tackle the next round, I'd recommend:

1. **Sidecar Phase B bug (2.3)** — smallest scope, closes an open half-built
   feature. ~2 hours.
2. **Per-client master-data store (2.1)** — scope confirmed (option B). First
   increment: the `clients` + `client_reference_data` schema, Excel import,
   and a merge-time lookup hook. ~1 week.
3. **Spectrum contract enrichment (1.1)** — unlocks contract accuracy for the
   third-largest carrier in the eval set.
4. **Cost anomaly investigation (2.2)** — low effort, high signal, closes a
   mystery.

Flag anything you want reprioritized.
