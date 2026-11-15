-- =============================================================================
-- Migration 075 -- settings changes are visible (owner, 2026-11-15; gap fix G4)
-- =============================================================================
-- The bot remembers the watched settings (time zone, quiet hours, workday, holiday country, weekend release start, runtime halt). A change
-- is recorded, asked about in the DM (free answer, never a consequence by itself) and listed in the weekly review.
--
-- settings_seen    (written only by settings_watch/service.py): key (PK), value, seen_at
-- settings_changes (written only by settings_watch/service.py): id, user_id, key, old, new, changed_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS settings_seen (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL,
    seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_changes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    old         TEXT NOT NULL,
    new         TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (75, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'settings watch');
