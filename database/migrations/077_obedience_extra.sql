-- =============================================================================
-- Migration 077 -- an extra obedience task on the same day (owner, 2026-11-15)
-- =============================================================================
-- The owner wanted to test an erotic task on a training day. `python -m cli obedience-extra` (owner only) files ONE extra task for today
-- (slot 1) beside the day's own task (slot 0); it runs the usual way ("Are you alone right now?", Chaster task, photo, community review).
--
-- obedience_tasks is rebuilt only to add `slot` and widen the unique key to (user_id, local_date, slot) (SQLite cannot alter a constraint);
-- every row and column is copied unchanged and existing rows get slot 0.
-- =============================================================================

CREATE TABLE obedience_tasks_new (
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
    postponed_at   TEXT,
    details        TEXT,
    mode           TEXT,
    asked_at       TEXT,
    slot           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, local_date, slot)
);

INSERT INTO obedience_tasks_new (id, user_id, local_date, catalog_key, text, planned_at, status, created_at, lock_id, task_id, deadline, submitted_at,
                                 review_status, decided_at, outcome, postponed_at, details, mode, asked_at, slot)
SELECT id, user_id, local_date, catalog_key, text, planned_at, status, created_at, lock_id, task_id, deadline, submitted_at,
       review_status, decided_at, outcome, postponed_at, details, mode, asked_at, 0
FROM obedience_tasks;

DROP TABLE obedience_tasks;
ALTER TABLE obedience_tasks_new RENAME TO obedience_tasks;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (77, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'an extra obedience task on the same day');
