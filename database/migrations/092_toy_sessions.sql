-- 092: the bot runs his Deep Diver during a task (owner, 2026-11-17: shaped sessions that learn from "Too weak / Right / Too much").
-- One row per session; written only by toy_control/store.py. next_level is the level the next session starts from (NULL: no feedback).
CREATE TABLE IF NOT EXISTS toy_sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    task_ref    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    profile     TEXT NOT NULL,
    base_level  REAL NOT NULL,
    started_at  TEXT NOT NULL,
    feedback    TEXT CHECK (feedback IS NULL OR feedback IN ('too_weak', 'right', 'too_much')),
    feedback_at TEXT,
    next_level  REAL
);
CREATE INDEX IF NOT EXISTS idx_toy_sessions_user ON toy_sessions (user_id, started_at);

INSERT INTO schema_version (version, applied_at, description)
VALUES (92, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'toy sessions');
