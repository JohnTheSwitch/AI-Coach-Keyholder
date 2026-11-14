-- =============================================================================
-- Migration 068 -- lines on paper (owner, 2026-11-14; brat taming B2)
-- =============================================================================
-- Lines are now written by hand and shown as a Chaster task (photo of the sheet with Chaster's code, community review). Typing them in the
-- DM counts only when Chaster's one task slot stayed taken for FALLBACK_AFTER. Additive columns on line_assignments (lines/service.py):
--   lock_id, task_id (the open Chaster task, NULL when none), task_assigned_at, review_status
-- =============================================================================

ALTER TABLE line_assignments ADD COLUMN lock_id TEXT;
ALTER TABLE line_assignments ADD COLUMN task_id TEXT;
ALTER TABLE line_assignments ADD COLUMN task_assigned_at TEXT;
ALTER TABLE line_assignments ADD COLUMN review_status TEXT;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (68, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'lines on paper');
