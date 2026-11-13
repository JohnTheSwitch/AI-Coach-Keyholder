-- =============================================================================
-- Migration 056 -- leaving supervision by a run of clean days (owner, 2026-11-13)
-- =============================================================================
-- Live, routine Trust sat at 0.00 and leaving Supervision Mode needed a score of 0.50: about 24 clean days in a row, and one miss started it over.
-- Supervision now also ends after PROBATION_CLEAN_DAYS (7) clean days in a row on which at least one check-in was answered.
--
-- probation_days (NULLABLE): the current run of such days while supervised. NULL for rows from before (read as 0).
--
-- Hygiene (owner, 2026-11-13): it ends after PROBATION_QUIET_DAYS (14) days without a confirmed incident in that domain, with at least one
-- planned opening relocked on time in that time.
-- probation_since (NULLABLE): start of the current run without an incident. NULL: no run yet (rows from before).
-- probation_proofs (NULLABLE): openings relocked on time in that run (read as 0).
-- =============================================================================

ALTER TABLE supervision_states ADD COLUMN probation_days INTEGER;
ALTER TABLE supervision_states ADD COLUMN probation_since TEXT;
ALTER TABLE supervision_states ADD COLUMN probation_proofs INTEGER;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (56, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'leaving supervision by a run of clean days');
