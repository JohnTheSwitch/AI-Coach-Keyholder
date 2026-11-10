-- =============================================================================
-- Migration 051 -- an admission is not an explanation, but it counts in the wearer's favour (owner, 2026-11-10)
-- =============================================================================
-- Live, the judge accepted an apology as a reason. An apology or admission now leaves the finding standing, and `admitted` = 1 records that the wearer
-- owned up: the confirmed Incident then carries CooperationAssessment.self_disclosed, which softens its Trust effect (trust_manager/severity.py).
-- Purely additive.
-- =============================================================================

ALTER TABLE infraction_signals ADD COLUMN admitted INTEGER CHECK (admitted IS NULL OR admitted IN (0, 1));

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (51, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'an admission is not an explanation');
