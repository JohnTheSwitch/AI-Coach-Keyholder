-- =============================================================================
-- Migration 053 -- photo check-ins (owner, 2026-11-13)
-- =============================================================================
-- Some check-ins ask for a verification picture in Chaster instead of a text answer. The picture stays on Chaster and its community
-- review; this database only learns that one was submitted in time.
--
-- 1. check_ins.kind (NULLABLE): NULL = a text check-in (every existing row), 'photo' = a photo check-in.
-- 2. photo_check_in_consent: the wearer's own opt-in, revocable at any time. `last_drawn_date` is the local date whose photo
--    draw has been made, so a restart never draws the same day twice.
-- =============================================================================

ALTER TABLE check_ins ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'photo');

CREATE TABLE IF NOT EXISTS photo_check_in_consent (
    user_id          TEXT PRIMARY KEY,
    consented_at     TEXT,
    revoked_at       TEXT,
    last_drawn_date  TEXT,
    updated_at       TEXT NOT NULL
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (53, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'photo check-ins');
