-- =============================================================================
-- Migration 088 -- the wearer's own name for a piece of gear (owner, 2026-11-16; multi-wearer leak audit)
-- =============================================================================
-- obedience_gear.label (written only by obedience/service.py): `gear add vibrating plug as black silicone plug`; NULL = the catalogue name.
-- =============================================================================

ALTER TABLE obedience_gear ADD COLUMN label TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (88, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'gear labels');
