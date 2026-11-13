-- =============================================================================
-- Migration 058 -- which stored replies the model wrote (2026-11-13)
-- =============================================================================
-- After a restart the working memory is refilled from conversation_messages (system/memory_seed.py). It restored EVERY exchange, also command
-- replies and status cards ("Active Locks (2)", "Erased."), which the model then read as its own earlier words. Live operation only ever keeps
-- model exchanges, so the restore now does the same.
--
-- from_model (NULLABLE): 1 for a reply the conversation model wrote, 0 for any other reply. NULL for user messages and for rows from before.
-- =============================================================================

ALTER TABLE conversation_messages ADD COLUMN from_model INTEGER;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (58, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'which stored replies the model wrote');
