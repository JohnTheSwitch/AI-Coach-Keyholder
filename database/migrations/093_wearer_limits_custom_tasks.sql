-- 093: another wearer's own erotic tasks (owner, 2026-11-17): the wearer gives their limits in the DM, the owner (with Claude) writes tasks
-- from them, and the wearer approves each one before it can be given. Written only by custom_tasks/service.py.
CREATE TABLE IF NOT EXISTS wearer_limits (
    user_id    TEXT PRIMARY KEY,
    hard       TEXT,
    soft       TEXT,
    likes      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_tasks (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    text        TEXT NOT NULL,
    needs       TEXT NOT NULL DEFAULT '',
    modes       TEXT NOT NULL CHECK (modes IN ('alone', 'discreet', 'both')),
    status      TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
    proposed_at TEXT NOT NULL,
    decided_at  TEXT,
    UNIQUE (user_id, number)
);
CREATE INDEX IF NOT EXISTS idx_custom_tasks_user ON custom_tasks (user_id, status);

INSERT INTO schema_version (version, applied_at, description)
VALUES (93, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'wearer limits and custom tasks');
