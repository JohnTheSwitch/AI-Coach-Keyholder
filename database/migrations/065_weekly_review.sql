-- =============================================================================
-- Migration 065 -- weekly review (owner, 2026-11-13; roadmap slice 5)
-- =============================================================================
-- Sunday at 19:00 local the wearer gets the week in plain facts, written by code (no model, no Trust scores). Skipped during a vacation.
-- `weekly review off` turns it off; no row means on.
--
-- weekly_review_settings (written only by weekly_review/service.py):
--   user_id (PK), enabled (0 | 1), updated_at
-- weekly_reviews (written only by weekly_review/service.py), one row per user and week, so a week is decided once:
--   user_id, week_start (local Monday, ISO date), decided_at, outcome (sent | vacation)
-- =============================================================================

CREATE TABLE IF NOT EXISTS weekly_review_settings (
    user_id     TEXT PRIMARY KEY,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
    user_id     TEXT NOT NULL,
    week_start  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    PRIMARY KEY (user_id, week_start)
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (65, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'weekly review');
