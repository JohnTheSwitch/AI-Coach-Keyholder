-- =============================================================================
-- Migration 071 -- his training plan in the bot (owner, 2026-11-15; work queue Q4c)
-- =============================================================================
-- The plan's session is that day's obedience task (proof: a Chaster photo of the training log). Progression from his `log ...` messages.
--
-- training_sessions (written only by training/service.py): user_id, local_date (PK with user_id), session_key, created_at
-- training_weights  (written only by training/service.py): user_id, exercise (PK with user_id), weight, updated_at
-- training_logs     (written only by training/service.py): id, user_id, logged_at, exercise, weight, reps (comma list), all_top, raised_to
-- training_weeks    (written only by training/service.py): id, user_id, iso_week (UNIQUE with user_id), short_day (mon | thu | NULL), asked_at
-- obedience_tasks.details (obedience/service.py): the session plan shown in the DM when the task is assigned
-- =============================================================================

CREATE TABLE IF NOT EXISTS training_sessions (
    user_id      TEXT NOT NULL,
    local_date   TEXT NOT NULL,
    session_key  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, local_date)
);

CREATE TABLE IF NOT EXISTS training_weights (
    user_id     TEXT NOT NULL,
    exercise    TEXT NOT NULL,
    weight      REAL NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, exercise)
);

CREATE TABLE IF NOT EXISTS training_logs (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    logged_at  TEXT NOT NULL,
    exercise   TEXT NOT NULL,
    weight     REAL,
    reps       TEXT NOT NULL,
    all_top    INTEGER NOT NULL,
    raised_to  REAL
);

CREATE TABLE IF NOT EXISTS training_weeks (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    iso_week   TEXT NOT NULL,
    short_day  TEXT,
    asked_at   TEXT,
    UNIQUE (user_id, iso_week)
);

ALTER TABLE obedience_tasks ADD COLUMN details TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (71, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'training plan');
