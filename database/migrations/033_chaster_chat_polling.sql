-- =============================================================================
-- Migration 033 -- v0.39 dual-role, Phase C: Chaster chat polling state
-- =============================================================================
-- docs/architecture/dual_role_and_messaging_proposal.md (section 8). Purely additive.
--
-- Chaster has no webhooks, so inbound chat is polled. Two small tables keep that safe:
--   chaster_chat_state : one row per polled conversation. `armed_at` is when polling started; messages that already existed
--                        are marked seen at that moment and never processed (no replay of history).
--   chaster_chat_seen  : message ids already handled or deliberately ignored. A message id is recorded BEFORE it is handed to
--                        the conversation (at-most-once: a crash loses one message rather than answering it twice).
-- Ids and timestamps only: no message text is stored here.
-- =============================================================================

CREATE TABLE chaster_chat_state (
    conversation_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    armed_at        TEXT NOT NULL,
    last_polled_at  TEXT
);

CREATE TABLE chaster_chat_seen (
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    seen_at         TEXT NOT NULL,
    PRIMARY KEY (conversation_id, message_id)
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (33, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 dual-role Phase C: Chaster chat polling state');
