-- 091: a release window whose lock was never opened (owner, 2026-11-16 21:50: the backstop unfroze the lock, he stayed locked, and was
-- asked to explain an "overrun"). release_windows is rebuilt only to widen the outcome CHECK (new value not_opened); every row is copied.
CREATE TABLE release_windows_new (
    authorization_id  TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    confirmed_at      TEXT NOT NULL,
    due_at            TEXT NOT NULL,
    checked_at        TEXT,
    outcome           TEXT CHECK (outcome IS NULL OR outcome IN ('picture', 'asked', 'unknown', 'not_opened')),
    created_at        TEXT NOT NULL
);
INSERT INTO release_windows_new (authorization_id, user_id, confirmed_at, due_at, checked_at, outcome, created_at)
SELECT authorization_id, user_id, confirmed_at, due_at, checked_at, outcome, created_at FROM release_windows;
DROP TABLE release_windows;
ALTER TABLE release_windows_new RENAME TO release_windows;

INSERT INTO schema_version (version, applied_at, description)
VALUES (91, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'release window not opened');
