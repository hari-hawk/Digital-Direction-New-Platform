# Digital Direction — deploy package (2026-04-28)

**For:** Dhanapal (GCP deploy)
**Repo:** `https://github.com/hari-hawk/Digital-Direction-New-Platform`
**Branch:** `main`
**Target HEAD:** `4f3dc98`
**Previous prod state:** `afcc5a0` ("added missing libraries")
**Working tree:** clean

---

## TL;DR

Six commits since prod's last deploy, addressing five distinct production issues. The user-facing symptoms were:

1. Project names + file lists silently disappearing after a day or after Cloud Run restarts.
2. Large PDFs producing **0 extracted rows** with no error.
3. New carriers (anything outside the 4 fully-tuned ones) showing "Validate carrier" friction.
4. CSR/contract files extracting under the wrong prompt because `doc_type` defaulted to "invoice".
5. (Latent) files on Cloud Run's ephemeral disk being lost on every redeploy.

After this deploy:

- Upload metadata lives in **Postgres** (already-running). Survives Redis flushes, container restarts, redeploys.
- Large PDFs (e.g., a 9.8 MB Verizon bill) now extract — verified locally producing **460 rows** where it previously produced 0.
- Newly-discovered carriers **auto-register** the first time they appear; no manual `carrier.yaml` PRs.
- `doc_type` is inferred from filename keywords (BILL→invoice, CSR→csr, contract→contract, etc.).
- Per-file extraction failures are **surfaced** on the upload card with a real reason instead of silent zeros.
- (Optional, off by default) `STORAGE_BACKEND=gcs` makes file storage durable across Cloud Run redeploys.

The deploy is **strictly additive** — no rollback risk for existing data. The only required prod-side action besides the code deploy is one schema migration (`IF NOT EXISTS`-safe) and a one-time data migration script.

---

## 1. Commits in this deploy

| Commit | Title | What it does |
|---|---|---|
| `28f2a75` | Fix upload state expiring after 24h — remove Redis TTL on uploads | Removes the 86 400-sec TTL on every `r.set()` of upload state. Stops project names from disappearing after a day. |
| `1c112f0` | Phase B: durable upload state — move metadata from Redis to Postgres | Moves `project_name`, `client_name`, `classified`, `file_assignments`, `results`, etc. from Redis JSON blobs into the existing `uploads` Postgres table. Redis keeps only the live `files_processed` counter. |
| `92a483c` | Phase C: GCS-backed file storage — files survive Cloud Run redeploys | Adds a `GCSStorage` backend behind the existing `get_storage()` abstraction. **Off by default** — keep `STORAGE_BACKEND=local` unless you're explicitly migrating to GCS. |
| `7f47347` | Extraction reliability: section-size cap + visible per-file errors | Caps any single LLM-bound section at 600K chars (~150K tokens, well under Gemini Flash's 1M limit). Adds `extraction_errors` JSONB column. Surfaces failures on the upload card. |
| `4f3dc98` | Auto-config: register new carriers on the fly + filename doc_type inference | When the LLM extracts a carrier name not in the registry, writes a minimal `carrier.yaml` and hot-reloads. Adds `_infer_doc_type_from_filename` for invoice/csr/contract/email/report/did_list/subscription. |

Older relevant commits (already on prod):

| Commit | Title |
|---|---|
| `7678f97` | Hybrid Gemini routing — Vertex + AI Studio with auto-failover |
| `afcc5a0` | added missing libraries |
| `311a643` | §2.1 admin UI: Clients page for viewing per-client master-data |
| `46a2c62` | §2.1 integration: classify-flow client linkage + merge-time master-data overrides + correction writeback |
| `1131ba4` | §2.1 schema slice: Client + ClientReferenceData tables + /api/clients |

---

## 2. Database schema delta

All changes are **additive ALTER TABLE statements** on the existing `uploads` table. No new tables. No drops. No data rewrites. All `IF NOT EXISTS`-safe — re-running is harmless.

### Run this on the prod Postgres

```sql
-- =============================================================
-- Phase B: durable upload state (commit 1c112f0)
-- =============================================================

ALTER TABLE uploads
    ADD COLUMN IF NOT EXISTS short_id          VARCHAR(8),
    ADD COLUMN IF NOT EXISTS description       TEXT,
    ADD COLUMN IF NOT EXISTS classified        JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS file_assignments  JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS files             JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS results           JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS raw_results       JSONB,
    ADD COLUMN IF NOT EXISTS has_raw_results   BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS computed_carriers JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS rows_with_issues  INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rows_error_level  INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unique_accounts   INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rows_needing_carrier_validation INT DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS uploads_short_id_key ON uploads(short_id);

-- =============================================================
-- Extraction reliability: visible per-file errors (commit 7f47347)
-- =============================================================

ALTER TABLE uploads
    ADD COLUMN IF NOT EXISTS extraction_errors JSONB DEFAULT '[]'::jsonb;
```

The same SQL also lives in the repo at:
- `db/migrations/2026-04-28_uploads_durable_state.sql`
- `db/migrations/2026-04-28_uploads_extraction_errors.sql`

### New columns added to `uploads`

| Column | Type | Default | Purpose |
|---|---|---|---|
| `short_id` | VARCHAR(8) UNIQUE | (null) | 8-char hex id used in URLs |
| `description` | TEXT | (null) | Optional project description |
| `classified` | JSONB | `[]` | List of classified files: `[{filename, carrier, doc_type, format_variant, file_size}]` |
| `file_assignments` | JSONB | `[]` | User's carrier assignments: `[{filename, carrier}]` |
| `files` | JSONB | `{}` | Filename → storage_path map |
| `results` | JSONB | `[]` | Merged extracted rows |
| `raw_results` | JSONB | (null) | Pre-merge results (for "raw" toggle) |
| `has_raw_results` | BOOLEAN | `false` | True after merge runs |
| `computed_carriers` | JSONB | `[]` | Canonical carrier names from rows |
| `rows_with_issues` | INT | `0` | Validation issue count |
| `rows_error_level` | INT | `0` | Error-severity issue count |
| `unique_accounts` | INT | `0` | Distinct `carrier_account_number` count |
| `rows_needing_carrier_validation` | INT | `0` | Now always 0 (auto-register closes the loop) |
| `extraction_errors` | JSONB | `[]` | Per-file failures: `[{filename, carrier, reason}]` |

**Nothing else changes.** The other 11 tables (`clients`, `client_reference_data`, `documents`, `extraction_runs`, `extracted_rows`, `corrections`, `known_accounts`, `format_signatures`, `format_flags`, `golden_data`, `eval_runs`) are untouched.

---

## 3. Environment variables

**No new required env vars.** The new code defaults to the same behavior as today.

```bash
# Already on prod — keep as-is:
DATABASE_URL=...
DATABASE_URL_SYNC=...
REDIS_URL=...
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
GCP_PROJECT_ID=...
LLM_BACKEND=auto

# DEFAULT — do NOT add unless ready for GCS file storage:
# STORAGE_BACKEND=local
# GCS_BUCKET_NAME=
```

`STORAGE_BACKEND=gcs` is a separate, optional opt-in (requires creating a GCS bucket and granting the Cloud Run service account `roles/storage.objectAdmin`). The code is ready for it but does not switch automatically.

---

## 4. Deploy steps

### Step 1 — pull and deploy the code

Standard Cloud Run deploy from `main` at HEAD `4f3dc98`. No build changes, no new system dependencies. The new Python dep (`google-cloud-storage>=2.18.0`) is in `pyproject.toml` and will be picked up by the build.

### Step 2 — apply schema migration

From any pod or local with prod-DB access:

```bash
docker exec -i <prod-postgres> psql -U dd_user -d digital_direction \
  < db/migrations/2026-04-28_uploads_durable_state.sql

docker exec -i <prod-postgres> psql -U dd_user -d digital_direction \
  < db/migrations/2026-04-28_uploads_extraction_errors.sql
```

(or paste the combined SQL block from §2 directly).

### Step 3 — one-time Redis → Postgres data migration

This copies any existing prod uploads from Redis into the new Postgres columns so they don't disappear from the UI:

```bash
# From a backend pod (or any environment with prod's REDIS_URL + DATABASE_URL):
python scripts/migrate_redis_uploads_to_pg.py --dry-run    # preview only
python scripts/migrate_redis_uploads_to_pg.py              # commit
```

Idempotent (uses upsert on `short_id`). On dry-run it prints the planned actions; on the real run it logs each upsert.

**Optional later:** after a day of stability, you can free the old Redis keys by re-running with `--flush-after`. **Do NOT do this on the first deploy.**

---

## 5. Smoke-test checklist

After deploy + schema + migration:

- [ ] `https://digital-direction-v1.techjays.com/` loads
- [ ] Previous Uploads list shows existing projects with names intact (no "(unnamed)")
- [ ] `GET /api/uploads` returns the same set of uploads as before
- [ ] `GET /api/uploads/orphans` returns `{"orphans": []}` (or any list — just not 500)
- [ ] Upload a small PDF → progress moves → extraction completes
- [ ] Upload a large PDF (>5 MB Verizon-style) → does NOT silently produce 0 rows
- [ ] If a file fails, the upload card shows an amber banner with a reason
- [ ] Bin → Restore round-trip works
- [ ] Bin → Purge removes the upload + its on-disk files
- [ ] Restart the backend → uploads still visible (durability check)

---

## 6. Rollback plan

If anything misbehaves, revert in reverse order:

```bash
git revert 4f3dc98 7f47347 92a483c 1c112f0 28f2a75
git push
# redeploy
```

The schema additions can stay — every new column has a default and is unread by older code, so reverting the code does not require dropping the columns. (If you want to drop them anyway, the columns are listed in §2.)

---

## 7. POC validation summary

Verified on the two project folders the user attached (`/Users/harivershan/Downloads/NSS POC Inputs` + `/Users/harivershan/Downloads/City of Dublin POC Input`):

- **246 source files** across 30 unique carriers + multiple file types (PDF, XLSX, CSV, MSG, EML, DOCX).
- **29 of 30 carriers** are already in the registry (22 by exact slug, 7 by alias).
- **1 new carrier (FirstNet)** will auto-register the first time a FirstNet file is uploaded (no manual setup).
- **`doc_type` filename inference**: 100% on NSS Invoices (80/80), 100% on all CSRs (18/18), correctly tags `.msg/.eml` as `email`, `_BILL.pdf` and `_BILL1.pdf` as `invoice`, "Service Agreement" as `contract`, etc.

---

## 8. What changed in the code (high-level)

### New files

| Path | Purpose |
|---|---|
| `backend/services/upload_store.py` | Postgres-backed CRUD for upload metadata. Same dict shape as the legacy Redis JSON, so the API layer didn't change. |
| `backend/services/auto_carrier_registry.py` | `register_discovered_carrier(name)` — sanity-checks + writes a minimal `carrier.yaml` + hot-reloads the config store. |
| `db/migrations/2026-04-28_uploads_durable_state.sql` | Phase B schema |
| `db/migrations/2026-04-28_uploads_extraction_errors.sql` | extraction_errors column |
| `scripts/migrate_redis_uploads_to_pg.py` | One-time data migration |

### Modified files

| Path | Summary |
|---|---|
| `backend/api/uploads.py` | All 7 helper functions are now thin async wrappers over `upload_store`. New `/orphans` endpoint. Auto-register hook in `_run_extraction`. Storage abstraction wrapped around classify + extract reads. |
| `backend/api/dashboard.py` | `/live` endpoint reads from `upload_store.list_uploads()` instead of Redis. |
| `backend/api/review.py` | Master-data writeback uses `Upload.client_id` (UUID FK) with legacy `config_version` short_id fallback. |
| `backend/main.py` | `await detect_stuck_extractions()` (now async). |
| `backend/models/orm.py` | `Upload` model gains the 14 new columns. |
| `backend/config_loader.py` | Adds `reset_config_store()` for hot-reload after auto-register writes. |
| `backend/pipeline/parser.py` | `MAX_SECTION_CHARS = 600_000` cap with `_enforce_max_section_size()` helper. Applied at the end of `parse_raw_text` and `parse_with_docling`. |
| `backend/pipeline/extractor.py` | `extract_document(errors_out=...)` opt-in error collector. `_format_extraction_error()` produces user-readable reasons. |
| `backend/pipeline/classifier.py` | `_infer_doc_type_from_filename()` with custom word-boundary regex (handles `_BILL.pdf`, `BILL1`, etc.). |
| `backend/services/storage.py` | Adds `GCSStorage`, new context-manager API (`save`, `open_local`, `delete_prefix`, `list_prefix`, `public_url`). Old `upload`/`download` kept for backward compat. |
| `backend/services/upload_store.py` | (new — see above) |
| `db/init.sql` | Updated to include the new columns for fresh installs. |
| `pyproject.toml` | Adds `google-cloud-storage>=2.18.0`. |
| `frontend/src/lib/api.ts` | `UploadSummary.extraction_errors` field added. |
| `frontend/src/lib/store.ts` | `Upload.extractionErrors` field; persisted across reloads. |
| `frontend/src/components/pages/upload.tsx` | Amber banner on the upload card listing per-file failures with reasons. |

The frontend API surface stayed identical — same URLs, same JSON shape. Only the **internals** changed.

---

## 9. Reference: full database schema (for context)

Run on prod to verify everything is in place:

```sql
SELECT table_name,
       (SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = t.table_name) AS column_count
FROM information_schema.tables t
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

Expected output:

| Table | Columns | Layer |
|---|---|---|
| `clients` | 5 | Project |
| `uploads` | **27** *(was 13 before this deploy)* | Project |
| `documents` | 21 | Project |
| `client_reference_data` | 11 | Reference (master-data) |
| `extraction_runs` | 19 | Process |
| `extracted_rows` | 83 | Process |
| `corrections` | 21 | Process |
| `known_accounts` | 7 | Reference |
| `format_signatures` | 9 | Reference |
| `format_flags` | 15 | Reference |
| `golden_data` | 7 | Eval |
| `eval_runs` | 13 | Eval |

The 4-layer architecture:

```
PROJECT LAYER
  clients ──┐
            ├─< uploads ──< documents
            └─< client_reference_data

PROCESS LAYER
  uploads ──< extraction_runs ──< extracted_rows ──< corrections
                                                       │
                                  documents ──────────┘ (source linkage)

REFERENCE / LEARNING LAYER
  documents ──< known_accounts        (analyst-confirmed account ↔ client)
  documents ──< format_flags          (format detection signals)
  format_signatures                   (pgvector-indexed format prototypes)

EVAL LAYER
  golden_data ──< eval_runs >── extraction_runs
```

---

## 10. Operational notes

### What's safe to flush on prod

- **Redis** (`platform-redis-1` or equivalent): safe to flush at any time after this deploy. The only Redis state that matters now is the `dd:upload:{short_id}:files_processed` counter, which is rebuilt automatically on the next extraction.
- **`storage/temp/`** on Cloud Run: ephemeral by default. Files lost here CAN'T be recovered without the user re-uploading. To make this durable, switch to GCS (see Phase C activation below).

### What's NOT safe to drop

- The `uploads` table — now the source of truth for project state.
- Any of the new columns — they back the entire upload UX.

### Activating Phase C (GCS file storage) — separate, later

When you're ready to make file contents durable across Cloud Run redeploys:

1. Create a GCS bucket (e.g., `dd-uploads-prod` in `us-central1`).
2. Grant the Cloud Run service account `roles/storage.objectAdmin` on the bucket.
3. Set Cloud Run env: `STORAGE_BACKEND=gcs`, `GCS_BUCKET_NAME=dd-uploads-prod`.
4. Restart the backend. New uploads land in GCS; existing files in `storage/temp/` are ignored (re-upload to migrate, or write a one-off copy script).

This is OPTIONAL and can be deferred. The default `STORAGE_BACKEND=local` keeps current behavior.

---

## 11. Location / address handling — strategic detail

The address fields use a **layered priority strategy** that mirrors how analysts think about telecom records: CSR is the system of record for *where the service is*, contract is the secondary source, invoice is the tertiary source (because invoices typically carry the billing/HQ address, not the service address). Master-data overrides them all.

### Layer priorities (top wins)

| Layer | Priority | Why this rank |
|---|---|---|
| **Layer 0 — Per-client master-data** | **15** | Analyst-confirmed authoritative facts (`client_reference_data` table). Set by manual confirmation or `analyst_correction` writeback. Trumps everything because a human said so. |
| **Layer 1 — CSR (Customer Service Record)** | **10** | The carrier's own per-service-location record. CSRs list the actual address where each service is provisioned. Most reliable for service location. |
| **Layer 2 — Contract** | **8** | Usually includes the schedule of locations. Reliable but rarely line-by-line. |
| **Layer 3 — Subscription / portal data** | **5** | Useful when contract / CSR are missing; less granular. |
| **Layer 4 — Invoice** | **6** | Often shows the *billing/mailing* address (corporate HQ), NOT the service address. Lowest priority for service-location fields. |

These priorities apply to: `service_address_1`, `service_address_2`, `city`, `state`, `zip`, `country`, `billing_name`. Defined in `backend/pipeline/merger.py` `FIELD_PRIORITIES`. Per-carrier overrides are supported via `merge_rules.yaml` in each carrier config.

### Why invoice still has *some* priority on service-address fields

If a project has an invoice but no CSR or contract, the invoice's address is the only signal we have. Priority 6 lets it fill the field — but loses to any CSR or contract that comes in later.

### Sidecar columns — preserve the invoice address even when it loses

When the invoice address differs from the CSR/contract service address, we keep BOTH so an analyst can see the divergence in the grid. The sidecar columns hold the invoice-sourced values:

| Primary field (winner) | Sidecar column (always invoice-sourced) |
|---|---|
| `service_address_1` | `billing_address_1` |
| `city` | `billing_city` |
| `state` | `billing_state` |
| `zip` | `billing_zip` |
| `billing_name` | `billing_name_from_invoice` |

The sidecar is populated unconditionally from any invoice row in the merge group — it doesn't compete with the primary field's priority logic. So you always have the invoice's billing address visible, regardless of which layer won the primary field.

### Divergence flag

After merge, if the primary `service_address_*` differs from the corresponding `billing_*` sidecar (case-insensitive, trimmed compare), the row gets `status = "Needs Review"` (unless a stronger status was already set, e.g., `Active` / `Inactive`). Logic in `merger.py` lines 769–783.

This means analysts see a clear list of rows where "the carrier sends bills to one address but services another" — a common audit finding that's worth flagging.

### Z-location (circuit far-end)

For circuit-based services (point-to-point Ethernet, MPLS, etc.) there's also a separate "Z" location — the far-end of the circuit:

| Column | Purpose |
|---|---|
| `z_location_name` | Site name at the far end |
| `z_address_1`, `z_address_2` | Far-end street address |
| `z_city`, `z_state`, `z_zip`, `z_country` | Far-end geo |

Populated from CSR / circuit reports when present. Not subject to the sidecar logic — Z is its own field set.

### Schema status — no changes needed for this deploy

All address-layer columns are **already on prod** (added pre-`afcc5a0` as part of the §2.1 master-data work). For reference, here's the complete address column inventory on `extracted_rows`:

```
SERVICE-ADDRESS LAYER (winner of CSR/contract/invoice/master-data priority):
  service_address_1, service_address_2, city, state, zip, country, billing_name

INVOICE SIDECAR LAYER (always invoice-sourced):
  billing_address_1, billing_city, billing_state, billing_zip, billing_name_from_invoice

Z-LOCATION LAYER (circuit far-end):
  z_location_name, z_address_1, z_address_2, z_city, z_state, z_zip, z_country
```

Master-data overrides come from the separate `client_reference_data` table (per-client authoritative facts), not from columns on `extracted_rows`. The merger consults that table at priority 15 before falling through to the per-row priority logic above.

**Verify on prod with**:

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'extracted_rows'
  AND column_name IN (
    -- Service-address layer (7)
    'service_address_1', 'service_address_2', 'city', 'state', 'zip', 'country', 'billing_name',
    -- Invoice sidecar layer (5)
    'billing_address_1', 'billing_city', 'billing_state', 'billing_zip', 'billing_name_from_invoice',
    -- Z-location layer (7)
    'z_location_name', 'z_address_1', 'z_address_2', 'z_city', 'z_state', 'z_zip', 'z_country'
  )
ORDER BY column_name;
```

Should return **19 columns**. If any are missing, prod hasn't applied the §2.1 schema — re-run `db/init.sql` (everything is `CREATE TABLE IF NOT EXISTS`-safe).

---

## Contact

If anything blocks the deploy or smoke tests fail, check the backend log first — the new `extraction_errors` system means most failures will surface as user-readable reasons in the upload card itself. For deeper issues, the relevant files are:

- Upload state: `backend/api/uploads.py`, `backend/services/upload_store.py`
- File storage: `backend/services/storage.py`
- Extraction: `backend/pipeline/parser.py`, `backend/pipeline/extractor.py`
- Auto-config: `backend/services/auto_carrier_registry.py`, `backend/pipeline/classifier.py`
- **Address-layer merge**: `backend/pipeline/merger.py` (priorities + sidecar + divergence flag)
- **Master-data overrides**: `backend/services/master_data.py`, `backend/api/clients.py`
