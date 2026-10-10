-- =============================================================================
-- Migration 028 -- v0.39 Proactive Supervision: supervision_states
-- =============================================================================
-- docs/architecture/v039_adaptive_release_and_supervision_proposal.md (final).
--
-- One row per Trust domain. Supervision activates when the domain's Trust score
-- is below the entry threshold with sufficient confidence, and deactivates only
-- after the score has stayed at or above the exit threshold for two consecutive
-- evaluations (hysteresis, `recovery_streak`). The state is DERIVED from Trust and
-- is the only mutable bookkeeping here; every transition emits an outbox event.
--
-- The check-in and hygiene tables are intentionally NOT created yet: their
-- delivery/evaluation mechanisms are later phases (see the proposal, section 9).
-- =============================================================================

CREATE TABLE IF NOT EXISTS supervision_states (
    domain_id            TEXT PRIMARY KEY REFERENCES trust_domains(domain_id),
    active                INTEGER NOT NULL CHECK (active IN (0, 1)),
    entered_at             TEXT,
    entry_score             REAL,
    recovery_streak          INTEGER NOT NULL DEFAULT 0,
    exited_at                 TEXT,
    last_evaluated_at          TEXT NOT NULL
);


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (28, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Proactive Supervision: supervision_states');
