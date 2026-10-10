-- =============================================================================
-- Migration 025 -- Rules Engine: lock_decision_triggers, lock_decisions
-- =============================================================================
-- docs/architecture/rules_engine_phase2_proposal.md (approved) and
-- relationship_decision_engine_technical_design.md (Decision, Entitlement).
--
-- Phase 2 is STRICT DRY-RUN: a Decision is a recorded recommendation and is
-- never executed against Chaster. `status` is therefore always 'dry_run'
-- until Phase 3 introduces execution (validated in code, not a CHECK, so
-- Phase 3 does not need a table rebuild).
--
-- lock_decision_triggers: one row per consumed
--   goal_accountability_assessment.recorded event (id = the event id, which
--   makes recording idempotent). Written by the outbox handler, which may
--   not call other modules; evaluation happens afterwards, outside that
--   transaction. processed_at is the only mutable column (bookkeeping).
--
-- lock_decisions: append-only (no UPDATE/DELETE path exists at the access
--   layer). UNIQUE(trigger_id): at most one Decision per trigger, so a
--   redelivery or a re-run never produces a second one. inputs_json is the
--   exact input snapshot used, for audit and replay.
-- =============================================================================

CREATE TABLE IF NOT EXISTS lock_decision_triggers (
    id                  TEXT PRIMARY KEY,
    trust_domain         TEXT NOT NULL,
    assessment_id         TEXT NOT NULL,
    direction              TEXT NOT NULL,      -- 'progress' | 'setback' | 'neutral' (neutral if not relevant to Trust)
    created_at              TEXT NOT NULL,
    processed_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_lock_decision_triggers_pending
    ON lock_decision_triggers(created_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lock_decision_triggers_domain
    ON lock_decision_triggers(trust_domain, created_at);

CREATE TABLE IF NOT EXISTS lock_decisions (
    id                  TEXT PRIMARY KEY,
    trigger_id           TEXT NOT NULL UNIQUE REFERENCES lock_decision_triggers(id),
    trust_domain          TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    action_type              TEXT NOT NULL,     -- ActionType
    hours                     INTEGER NOT NULL,  -- 0 for NO_ACTION / FREEZE / UNFREEZE
    entitlement_class          TEXT NOT NULL,     -- 'absolute' | 'guaranteed' | 'discretionary'
    rule_id                     TEXT,              -- NULL when no rule matched / a safety gate decided
    ruleset_version              TEXT NOT NULL,
    reason                        TEXT NOT NULL,     -- DEC-2: always non-empty
    inputs_json                    TEXT NOT NULL,
    status                          TEXT NOT NULL      -- 'dry_run' in Phase 2
);

CREATE INDEX IF NOT EXISTS idx_lock_decisions_created ON lock_decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_lock_decisions_domain ON lock_decisions(trust_domain, created_at);


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (25, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'Rules Engine (Phase 2, dry-run): lock_decision_triggers, lock_decisions');
