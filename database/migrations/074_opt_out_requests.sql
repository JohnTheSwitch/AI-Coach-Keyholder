-- =============================================================================
-- Migration 074 -- switch-offs wait while locked (owner, 2026-11-15; gap fix G3)
-- =============================================================================
-- While locked, switching off photo check-ins, Progressive Conditioning or the weekly review waits 24 hours; then the keyholder asks
-- "Do you still want it off?" and only a yes applies it. The halt, leaving, health, the emergency option and research sharing stay immediate.
--
-- opt_out_requests (written only by opt_outs/service.py):
--   id, user_id, switch (photo_check_ins | conditioning | weekly_review), status, requested_at, ask_at, asked_at, decided_at
--   status: waiting -> asked -> applied | kept_on | lapsed; waiting/asked -> withdrawn (switched back on)
-- =============================================================================

CREATE TABLE IF NOT EXISTS opt_out_requests (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    switch        TEXT NOT NULL,
    status        TEXT NOT NULL,
    requested_at  TEXT NOT NULL,
    ask_at        TEXT NOT NULL,
    asked_at      TEXT,
    decided_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_opt_out ON opt_out_requests(user_id, switch) WHERE status IN ('waiting', 'asked');

INSERT INTO schema_version (version, applied_at, description)
VALUES (74, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'opt-out requests wait while locked');
