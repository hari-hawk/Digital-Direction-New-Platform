-- Phase B (durable upload state):
-- Move upload metadata from Redis (ephemeral) to Postgres (durable).
-- Adds the columns the API was previously stashing in Redis JSON blobs.
--
-- short_id is the user-facing 8-char hex identifier used in URLs and the
-- frontend; the UUID id stays as the relational primary key.
--
-- All JSONB columns default to empty containers so existing rows (created
-- by the legacy pipeline) don't violate NOT-NULL constraints — though we
-- leave them nullable since "no data yet" is a valid state during upload.

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

-- Unique short_id so the API lookup-by-short-id is safe.
-- Use UNIQUE INDEX (not constraint) so the IF NOT EXISTS works on re-run.
CREATE UNIQUE INDEX IF NOT EXISTS uploads_short_id_key ON uploads(short_id);
