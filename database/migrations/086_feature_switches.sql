-- =============================================================================
-- Migration 086 -- per-wearer feature switches (owner, 2026-11-16)
-- =============================================================================
-- feature_switches (written only by features/service.py): one row per switched feature; no row = on.
-- =============================================================================

CREATE TABLE IF NOT EXISTS feature_switches (
    user_id     TEXT NOT NULL,
    feature     TEXT NOT NULL,
    enabled     INTEGER NOT NULL,
    changed_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, feature)
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (86, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'per-wearer feature switches');
