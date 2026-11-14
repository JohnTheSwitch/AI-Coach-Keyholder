-- =============================================================================
-- Migration 067 -- verified recovery tasks (owner, 2026-11-14; brat taming B3)
-- =============================================================================
-- During a Penalty Window the bot offers a chore as a Chaster task (photo + Chaster's code, community review). A passed task completes its
-- Recovery Plan task, and the Penalty Engine credits 2 hours against the window (philosophy 3.4, capped at half the window).
--
-- recovery_chaster_tasks (written only by recovery_tasks/service.py):
--   id, user_id, penalty_window_id, recovery_task_id (recovery_plan), chore, status, created_at, offer_expires_at,
--   lock_id, task_id (Chaster), deadline, submitted_at, review_status, decided_at, outcome
--   status: offered -> assigned -> submitted -> passed | failed | declined | expired | cancelled
-- =============================================================================

CREATE TABLE IF NOT EXISTS recovery_chaster_tasks (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    penalty_window_id  TEXT NOT NULL,
    recovery_task_id   TEXT NOT NULL,
    chore              TEXT NOT NULL,
    status             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    offer_expires_at   TEXT NOT NULL,
    lock_id            TEXT,
    task_id            TEXT,
    deadline           TEXT,
    submitted_at       TEXT,
    review_status      TEXT,
    decided_at         TEXT,
    outcome            TEXT
);

CREATE INDEX IF NOT EXISTS idx_recovery_chaster_tasks_status ON recovery_chaster_tasks (user_id, status);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (67, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'verified recovery tasks');
