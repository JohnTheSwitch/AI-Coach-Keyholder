-- =============================================================================
-- Migration 066 -- lines (owner, 2026-11-14; brat taming B2)
-- =============================================================================
-- Every confirmed incident gets lines: 20 for a Warning-level incident, 40 for one that opens or extends a Penalty Window. The sentence
-- matches the rule broken. They are typed in the bot DM and counted exactly by code (no model). While lines are open, the next planned
-- hygiene break waits (health access is never blocked) and, as release debt (B4), a token cannot be spent on a release.
--
-- line_assignments (written only by lines/service.py):
--   id, user_id, incident_id (UNIQUE: one assignment per incident), rule_group_id, sentence, required, done, status (open | done),
--   created_at, completed_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS line_assignments (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    incident_id    TEXT NOT NULL UNIQUE,
    rule_group_id  TEXT,
    sentence       TEXT NOT NULL,
    required       INTEGER NOT NULL CHECK (required > 0),
    done           INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('open', 'done')),
    created_at     TEXT NOT NULL,
    completed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_line_assignments_open ON line_assignments (user_id, status);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (66, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'lines');
