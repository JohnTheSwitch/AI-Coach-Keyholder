-- =============================================================================
-- Migration 060 -- the first sighting of a lock (owner, 2026-11-13)
-- =============================================================================
-- "I want the bot to freeze the lock automatically when it sees it for the first time." The release tick freezes a newly recognised,
-- locked keyholder lock that is not frozen ONCE, at its first sighting. This table remembers which locks were already seen, so the bot never
-- refreezes a lock that a release unfroze (that is the release session's own refreeze rule).
--
-- release_lock_first_seen (written only by release_engine):
--   lock_id (PK), first_seen_at, was_frozen (1/0: frozen when first seen), freeze_recorded (1/0: a first freeze was recorded)
-- =============================================================================

CREATE TABLE IF NOT EXISTS release_lock_first_seen (
    lock_id          TEXT PRIMARY KEY,
    first_seen_at    TEXT NOT NULL,
    was_frozen       INTEGER NOT NULL,
    freeze_recorded  INTEGER NOT NULL
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (60, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'the first sighting of a lock');
