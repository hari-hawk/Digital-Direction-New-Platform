-- Inventory versioning (Apr-29-2026, after Matt's email)
--
-- Customer's mental model: download Excel → edit → re-upload = a new
-- "version" of the inventory. They want to switch between versions on
-- the Results page (default = latest, can filter back to v0 / v1 / etc.).
--
-- Granularity (Option 1 confirmed):
--   * v0 = the original extraction snapshot, taken when extraction
--          completes. Inline edits made BEFORE any download happen
--          against the live data; the v0 snapshot stays as-extracted
--          for audit, while the live data drifts.
--   * v1, v2, … = a snapshot taken at every successful corrections
--          import. Each import bumps the version number by one. The
--          live data also matches the latest snapshot at the moment
--          of import, then drifts again with subsequent inline edits.
--
-- Storage:
--   * `rows_snapshot` JSONB holds the full row list at snapshot time
--     (mirroring `uploads.results`'s shape). One JSONB blob per version.
--   * `source` distinguishes 'extraction' (v0) from 'import' (v1+).
--   * `file_blob` optional — saved on import so the analyst can re-download
--     the exact Excel that was uploaded. Skipped for the v0 extraction
--     snapshot (no Excel file existed at that point).
--   * `file_hash` for content-dedup on imports — re-uploading the same file
--     twice without edits won't pile up identical versions.

CREATE TABLE IF NOT EXISTS inventory_versions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    upload_id       UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    version_number  INT NOT NULL,
    source          VARCHAR(20) NOT NULL,               -- 'extraction' | 'import'
    rows_snapshot   JSONB NOT NULL,
    file_blob       BYTEA,                              -- optional Excel bytes
    file_hash       VARCHAR(64),                        -- sha256 of file_blob (dedup)
    rows_count      INT NOT NULL DEFAULT 0,
    note            TEXT,                               -- e.g. "Initial extraction" / "Re-uploaded by Matt"
    created_at      TIMESTAMP DEFAULT NOW(),
    created_by      VARCHAR(255),
    UNIQUE (upload_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_inv_versions_upload ON inventory_versions(upload_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_inv_versions_hash ON inventory_versions(upload_id, file_hash);
