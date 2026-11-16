-- =============================================================================
-- Migration 085 -- `proof checks off` for an additional wearer (roadmap slice 8, work queue Q9)
-- =============================================================================
-- proof_check_switch (proof_checks/service.py): the wearer switched spot checks off (1) or back on (0). No row = on (as disclosed in the
-- invitation they accepted).
-- =============================================================================

CREATE TABLE IF NOT EXISTS proof_check_switch (
    user_id     TEXT PRIMARY KEY,
    off         INTEGER NOT NULL CHECK (off IN (0, 1)),
    changed_at  TEXT NOT NULL
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (85, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'proof check switch');
