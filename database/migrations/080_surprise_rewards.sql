-- =============================================================================
-- Migration 080 -- surprise rewards (owner, 2026-11-15; research item a)
-- =============================================================================
-- About 1 in 5 verified obedience tasks adds a surprise +0.05 to the token pool, unannounced until it happens. Additive column on
-- obedience_tasks (obedience/service.py): surprise (the reward as text, NULL when none).
-- =============================================================================

ALTER TABLE obedience_tasks ADD COLUMN surprise TEXT;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (80, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'surprise rewards');
