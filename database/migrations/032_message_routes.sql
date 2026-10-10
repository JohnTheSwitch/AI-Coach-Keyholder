-- =============================================================================
-- Migration 032 -- v0.39 dual-role, Phase B: messaging channel preference and routing metadata
-- =============================================================================
-- docs/architecture/dual_role_and_messaging_proposal.md (section 4.2). Purely additive: one new table, nothing altered.
--
-- message_routes: which channel a user's outgoing messages use. One DEFAULT route per user (lock_key = '') and optionally
-- one route per Chaster lock (lock_key = the lock id). No row means Discord. A 'chaster' route must carry the Chaster
-- conversation id the message is sent to (POST /conversations/{conversationId}); the lock id is routing metadata only.
-- Ids only: no message text and no tokens are stored here.
-- =============================================================================

CREATE TABLE message_routes (
    user_id                 TEXT NOT NULL,
    lock_key                TEXT NOT NULL DEFAULT '',
    channel                 TEXT NOT NULL CHECK (channel IN ('discord', 'chaster')),
    chaster_lock_id         TEXT,
    chaster_conversation_id TEXT,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (user_id, lock_key),
    CHECK (channel = 'discord' OR chaster_conversation_id IS NOT NULL),
    CHECK (lock_key = COALESCE(chaster_lock_id, ''))
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (32, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 dual-role Phase B: message_routes (channel preference and routing metadata)');
