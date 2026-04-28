-- Surface silent extraction failures (large PDF rejected by Gemini, parser
-- returned no sections, malformed file, etc.) so users see WHY a file
-- produced 0 rows instead of guessing.
--
-- Each array entry is {filename, carrier, reason}. _run_extraction in
-- backend/api/uploads.py appends here; the frontend upload card renders
-- a banner listing them.

ALTER TABLE uploads
    ADD COLUMN IF NOT EXISTS extraction_errors JSONB DEFAULT '[]'::jsonb;
