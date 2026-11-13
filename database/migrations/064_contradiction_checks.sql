-- =============================================================================
-- Migration 064 -- contradictions (owner, 2026-11-13; honesty and proof, slice 4)
-- =============================================================================
-- A fact that contradicts a report becomes a question to the wearer (infractions path), never a verdict. First kind: a bedtime commitment that
-- names a time ("Sleep by 23:00") reported as kept, while the wearer wrote to the bot later that night. Each kept bedtime commitment is
-- looked at once, after its night is over.
--
-- contradiction_checks (written only by proof_checks/contradictions.py):
--   commitment_id (PK), user_id, checked_at, outcome (no_time | none | found), message_at (the first message after the stated time, if found)
-- =============================================================================

CREATE TABLE IF NOT EXISTS contradiction_checks (
    commitment_id  TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    checked_at     TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    message_at     TEXT
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (64, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'contradiction checks');
