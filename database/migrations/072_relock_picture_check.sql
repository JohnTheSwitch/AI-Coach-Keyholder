-- =============================================================================
-- Migration 072 -- relock picture check (owner, 2026-11-15)
-- =============================================================================
-- After a planned hygiene opening, while the relock picture is required (hygiene Trust below 0.70), Chaster's verification pictures are
-- read: none within 30 minutes of the relock = the wearer is asked to explain (HYGIENE_PICTURE_MISSING, warning only). A health opening is
-- never checked. Additive columns on wear_exemptions (exemptions/service.py): picture_checked_at, picture_outcome
--   picture_outcome: sent | missing | not_required | unknown
-- =============================================================================

ALTER TABLE wear_exemptions ADD COLUMN picture_checked_at TEXT;
ALTER TABLE wear_exemptions ADD COLUMN picture_outcome TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (72, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'relock picture check');
