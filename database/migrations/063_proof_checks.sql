-- =============================================================================
-- Migration 063 -- proof spot checks (owner, 2026-11-13; honesty and proof, slice 3)
-- =============================================================================
-- A random share of timely "Done" reports is checked: the bot assigns a Chaster task ("Show: <commitment>") that needs a photo with
-- Chaster's code, and the Chaster community votes on it. Passed: the report counts, plus a small Honesty bonus. Missed or rejected: the
-- report earns nothing and the wearer is asked to explain (infractions path, Honesty domain, Trust and a Warning). "I can't show it now":
-- the report earns nothing, once a week free, after that it counts as Not kept. "I didn't actually do it": Not kept, nothing in Honesty.
--
-- proof_checks (written only by proof_checks/service.py):
--   id, user_id, commitment_id (UNIQUE), commitment_text, local_date, status, created_at,
--   lock_id, task_id (Chaster task id), deadline, submitted_at, review_id, review_status, unknown_since, decided_at, outcome
--   status: selected -> assigned -> submitted -> passed | failed | declined | retracted | cancelled
--   outcome: verified | unknown | missed | late | rejected | free_pass | pass_used_up | retracted | not_started
-- =============================================================================

CREATE TABLE IF NOT EXISTS proof_checks (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    commitment_id    TEXT NOT NULL UNIQUE,
    commitment_text  TEXT NOT NULL,
    local_date       TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    lock_id          TEXT,
    task_id          TEXT,
    deadline         TEXT,
    submitted_at     TEXT,
    review_id        TEXT,
    review_status    TEXT,
    unknown_since    TEXT,
    decided_at       TEXT,
    outcome          TEXT
);

CREATE INDEX IF NOT EXISTS idx_proof_checks_status ON proof_checks (status);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (63, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'proof spot checks');
