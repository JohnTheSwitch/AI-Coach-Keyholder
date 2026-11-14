-- =============================================================================
-- Migration 070 -- obedience tasks: "Not alone right now" (owner, 2026-11-14)
-- =============================================================================
-- The owner does not live alone. A task can be postponed ONCE by up to POSTPONE (2 hours); after that it is done or it fails.
-- Additive column on obedience_tasks (obedience/service.py): postponed_at
-- =============================================================================

ALTER TABLE obedience_tasks ADD COLUMN postponed_at TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (70, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'obedience postpone');
