-- =============================================================================
-- Migration 057 -- the owner's one-time test release (owner, 2026-11-13)
-- =============================================================================
-- The owner moves to a real (non-test) lock and wants to test spending a token and the relock on it while Supervision Mode would hold the
-- release for about a week. `python -m cli release-test-override` (owner only, --yes) files ONE request here; the running bot's release tick
-- carries it out: it spends exactly one token and releases despite supervision or a penalty window, but only with a confirmed barrier, in
-- the free-time window, with execution enabled and the runtime halt clear. It never touches time. An unused request expires.
--
-- release_owner_requests (written only by release_engine):
--   id, user_id, requested_at, expires_at
--   status: 'pending' | 'done' | 'refused' | 'expired'
--   outcome (NULLABLE): why it ended (e.g. 'authorized', 'insufficient_tokens', 'expired')
--   authorization_id (NULLABLE): the release it produced
--   decided_at (NULLABLE)
-- =============================================================================

CREATE TABLE IF NOT EXISTS release_owner_requests (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    requested_at      TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('pending', 'done', 'refused', 'expired')),
    outcome           TEXT,
    authorization_id  TEXT,
    decided_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_release_owner_requests_pending ON release_owner_requests(user_id, status);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (57, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'the owner''s one-time test release');
