-- =============================================================================
-- Migration 059 -- the keyholder account's inbox watch (owner, 2026-11-13)
-- =============================================================================
-- The keyholder account is not read by a person. messaging/keyholder_inbox.py forwards each new Chaster message from anyone but the owner
-- to the owner's Discord DM, once. This table remembers, per conversation, the last message handled. No message text is stored here.
--
-- keyholder_inbox_seen (written only by messaging):
--   conversation_id (PK), last_message_id, seen_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS keyholder_inbox_seen (
    conversation_id   TEXT PRIMARY KEY,
    last_message_id   TEXT NOT NULL,
    seen_at           TEXT NOT NULL
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (59, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'the keyholder account''s inbox watch');
