-- =============================================================================
-- Migration 062 -- commitment time windows (owner, 2026-11-13; honesty and proof, slice 1)
-- =============================================================================
-- "To prove something, it has to be reported right after it." A commitment gets a part of its day when it is accepted (morning, afternoon,
-- evening or any time). The bot asks "Done?" when that part ends, and a Kept counts only inside the report window: not before the commitment
-- could have been done, and not later than REPORT_GRACE after its window. A Kept outside it is recorded and earns nothing. Older rows have no
-- window (NULL): the whole waking day of their date applies to them.
--
-- daily_commitments (written only by coach/desk.py), new nullable columns:
--   window_part    morning | afternoon | evening | any
--   window_start   UTC, when the commitment's part of the day begins
--   window_end     UTC, when it ends (fixed; the report window ends REPORT_GRACE later)
--   ask_at         UTC, when the bot asks "Done?" (window_end, moved once by "Still doing it"); NULL: never asked
--   asked_at       when that question was queued
--   extended_at    when the wearer answered "Still doing it" (once)
--   report_timing  timely | too_soon | late, for a Kept report
-- =============================================================================

ALTER TABLE daily_commitments ADD COLUMN window_part TEXT;
ALTER TABLE daily_commitments ADD COLUMN window_start TEXT;
ALTER TABLE daily_commitments ADD COLUMN window_end TEXT;
ALTER TABLE daily_commitments ADD COLUMN ask_at TEXT;
ALTER TABLE daily_commitments ADD COLUMN asked_at TEXT;
ALTER TABLE daily_commitments ADD COLUMN extended_at TEXT;
ALTER TABLE daily_commitments ADD COLUMN report_timing TEXT;

CREATE INDEX IF NOT EXISTS idx_daily_commitments_ask ON daily_commitments (status, ask_at);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (62, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'commitment time windows');
