-- =============================================================================
-- Migration 078 -- an extra obedience task outside free time, once (owner, 2026-11-15)
-- =============================================================================
-- The owner allowed a one-time exception to quiet hours for an extra task (`python -m cli obedience-extra ... --anytime`). Additive column on
-- obedience_tasks (obedience/service.py): anytime (1 = this row may start and run outside free time; 0 for every existing row).
-- =============================================================================

ALTER TABLE obedience_tasks ADD COLUMN anytime INTEGER NOT NULL DEFAULT 0;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (78, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'an extra obedience task outside free time, once');
