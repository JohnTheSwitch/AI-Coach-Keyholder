-- =============================================================================
-- Migration 030 -- v0.39 Phase 3: check-in escalation ledger
-- =============================================================================
-- docs/architecture/v039_adaptive_release_and_supervision_proposal.md (section 11.7).
--
-- Two consecutive missed check-ins, or three missed within a rolling 48 hours, register ONE low-severity
-- Incident (rule group CHECKIN_REPEATED_NONCOMPLIANCE) with the Trust Manager / Penalty Engine path. This table
-- makes that idempotent and crash-safe:
--   * UNIQUE(trigger_check_in_id): the newest missed check-in of an escalation can trigger it only once;
--   * missed check-ins up to `trigger_due_at` are "covered" and never counted again, so each escalation
--     consumes at least two misses;
--   * state 'registered' = the Incident exists but is not yet confirmed (a crash here is recovered by
--     confirming it on the next pass); 'confirmed' = done.
-- Contains ids and codes only; never any message text.
-- =============================================================================

CREATE TABLE check_in_escalations (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    trigger_check_in_id TEXT NOT NULL UNIQUE REFERENCES check_ins(id),
    trigger_due_at      TEXT NOT NULL,
    reason              TEXT NOT NULL CHECK (reason IN ('consecutive', 'rolling_48h')),
    incident_id         TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('registered', 'confirmed')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX idx_check_in_escalations_user ON check_in_escalations(user_id, trigger_due_at);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (30, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase 3: check-in escalation ledger');
