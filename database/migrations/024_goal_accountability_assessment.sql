-- =============================================================================
-- Migration 024 -- Goal Management: goal_accountability_assessments
-- =============================================================================
-- docs/architecture/goal_technical_design.md Section 6 (the Keyholder's
-- accountability judgment about ONE GoalEvaluation) and
-- trust_manager_technical_design.md Section 15 (the only bridge from
-- Goals to Trust).
--
-- Append-only: the access layer provides no UPDATE/DELETE. GOAL-13: at
-- most ONE assessment per GoalEvaluation (UNIQUE evaluation_id) -- a
-- changed judgment requires a new GoalEvaluation.
--
-- trust_domain is denormalised from the Goal at write time so the event
-- payload (and this audit row) is self-contained: a consumer running
-- inside consume_event()'s transaction cannot call back into Goal
-- Management. review_outcome / negotiation_reason (GoalNegotiation,
-- Section 7) are deferred and intentionally absent here.
-- =============================================================================

CREATE TABLE IF NOT EXISTS goal_accountability_assessments (
    id                 TEXT PRIMARY KEY,
    evaluation_id       TEXT NOT NULL UNIQUE REFERENCES goal_evaluations(id),
    goal_group_id        TEXT NOT NULL REFERENCES goals(goal_group_id),
    trust_domain          TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    relevant_to_trust        INTEGER NOT NULL CHECK (relevant_to_trust IN (0, 1)),
    direction                 TEXT NOT NULL CHECK (direction IN ('progress', 'setback', 'neutral')),
    rationale                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_goal_accountability_group ON goal_accountability_assessments(goal_group_id);


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (24, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'Goal Management: goal_accountability_assessments (Phase 1: Goal -> Trust bridge)');
