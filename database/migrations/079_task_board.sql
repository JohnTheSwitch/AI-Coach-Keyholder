-- =============================================================================
-- Migration 079 -- the task board (owner, 2026-11-15; story idea P1)
-- =============================================================================
-- Every Monday a few tasks with a deadline and a reward in token progress; he takes one at a time; a taken task is a promise.
-- `board_tasks` is written only by task_board/service.py. The Chaster part is an obedience task (slot 2 of its day).
-- =============================================================================

CREATE TABLE IF NOT EXISTS board_tasks (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    week               TEXT NOT NULL,
    task_key           TEXT NOT NULL,
    kind               TEXT NOT NULL,
    text               TEXT NOT NULL,
    reward             TEXT NOT NULL,
    deadline           TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('offered', 'taken', 'started', 'passed', 'failed', 'missed', 'expired', 'cancelled')),
    created_at         TEXT NOT NULL,
    taken_at           TEXT,
    started_at         TEXT,
    obedience_task_id  TEXT,
    decided_at         TEXT,
    outcome            TEXT
);

CREATE INDEX IF NOT EXISTS idx_board_tasks_open ON board_tasks (user_id, status);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (79, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'the task board');
