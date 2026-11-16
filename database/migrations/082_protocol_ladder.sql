-- =============================================================================
-- Migration 082 -- the protocol ladder (owner, 2026-11-15/16; roadmap 6c + P2, autonomous queue A3)
-- =============================================================================
-- protocol_ladder (protocol/service.py): the current rung and since when, the next rung's start once announced, his title for the bot
-- (rung 4), the last scan for verified tasks (rung 3) and whether a Penalty Window was active at the last look (how he is addressed).
-- =============================================================================

CREATE TABLE IF NOT EXISTS protocol_ladder (
    user_id      TEXT PRIMARY KEY,
    rung         INTEGER NOT NULL CHECK (rung BETWEEN 1 AND 6),
    rung_since   TEXT NOT NULL,
    next_at      TEXT,
    title        TEXT,
    thanks_scan  TEXT,
    window_seen  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (82, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'protocol ladder');
