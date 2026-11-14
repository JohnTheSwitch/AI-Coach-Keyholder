-- =============================================================================
-- Migration 069 -- daily obedience tasks (owner, 2026-11-14; work queue Q4b)
-- =============================================================================
-- One erotic / BDSM (or, about one in four, workout) task a day at a random moment in free time, from a closed catalogue that uses only the
-- owner's own gear. Shown as a Chaster task (photo with Chaster's code, community review). Not done in time or rejected: the wearer is asked
-- to explain (infractions path); unexplained = a Warning-level incident (OBEDIENCE_TASK_MISSED: Trust and 20 lines). A stated health or safety
-- need cancels a task without consequence.
--
-- obedience_tasks (written only by obedience/service.py), one row per local day:
--   id, user_id, local_date (UNIQUE with user_id), catalog_key, text, planned_at, status, created_at,
--   lock_id, task_id, deadline, submitted_at, review_status, decided_at, outcome
--   status: planned -> assigned -> submitted -> passed | failed | cancelled | skipped
-- =============================================================================

CREATE TABLE IF NOT EXISTS obedience_tasks (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    local_date     TEXT NOT NULL,
    catalog_key    TEXT NOT NULL,
    text           TEXT NOT NULL,
    planned_at     TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    lock_id        TEXT,
    task_id        TEXT,
    deadline       TEXT,
    submitted_at   TEXT,
    review_status  TEXT,
    decided_at     TEXT,
    outcome        TEXT,
    UNIQUE (user_id, local_date)
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (69, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'daily obedience tasks');
