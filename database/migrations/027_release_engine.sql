-- =============================================================================
-- Migration 027 -- v0.39 Release Engine: token_ledger, release_plans, release_authorizations
-- =============================================================================
-- docs/architecture/v039_adaptive_release_and_supervision_proposal.md (final) and
-- activity_authorization_technical_design.md Section 5 (the ledger shape).
--
-- token_ledger: append-only (no UPDATE/DELETE path exists at the access layer).
--   balance = SUM(delta); there is no other definition (LEDGER2). A mistaken entry
--   is undone only by a compensating REVERSAL row. LEDGER3: at most one SPEND per
--   authorization (partial unique index), so a retry can never charge twice.
--
-- release_plans: the adaptive discretionary schedule. `target_at` and `seed` are
--   SECRET: they are never put in an event payload, log line, model context,
--   Discord message or decision reason. The seed is stored so the plan survives a
--   restart without being redrawn and can be audited. At most one 'planned' plan
--   per user.
--
-- release_authorizations: what was authorised and the provider-side progress:
--   authorized -> provider_requested -> provider_confirmed | provider_failed |
--   physical_state_unknown. kind='backstop' (maximum-hold safety release) spends
--   no token. UNIQUE(plan_id): one authorization per plan.
-- =============================================================================

CREATE TABLE IF NOT EXISTS token_ledger (
    id                          TEXT PRIMARY KEY,
    user_id                      TEXT NOT NULL,
    delta                         INTEGER NOT NULL CHECK (delta != 0),
    entry_type                     TEXT NOT NULL CHECK (entry_type IN ('earn', 'spend', 'adjustment', 'reversal')),
    reason_code                     TEXT NOT NULL,
    authorization_decision_id        TEXT,
    source_event_id                   TEXT,
    reverses_entry_id                  TEXT REFERENCES token_ledger(id),
    created_at                          TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_token_ledger_spend_per_authorization
    ON token_ledger(authorization_decision_id) WHERE entry_type = 'spend';
CREATE UNIQUE INDEX IF NOT EXISTS uq_token_ledger_single_reversal
    ON token_ledger(reverses_entry_id) WHERE reverses_entry_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_token_ledger_earn_per_source
    ON token_ledger(source_event_id) WHERE entry_type = 'earn' AND source_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_token_ledger_user ON token_ledger(user_id, created_at);

CREATE TABLE IF NOT EXISTS release_plans (
    id                    TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('planned', 'authorized', 'cancelled')),
    requirement_met_at       TEXT NOT NULL,
    target_at                 TEXT NOT NULL,       -- SECRET
    seed                       BLOB NOT NULL,       -- SECRET
    cancel_reason               TEXT,
    created_at                   TEXT NOT NULL,
    updated_at                    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_release_plans_one_planned_per_user
    ON release_plans(user_id) WHERE status = 'planned';

CREATE TABLE IF NOT EXISTS release_authorizations (
    id               TEXT PRIMARY KEY,
    plan_id           TEXT NOT NULL UNIQUE REFERENCES release_plans(id),
    user_id            TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('token', 'backstop')),
    state                TEXT NOT NULL CHECK (state IN (
                            'authorized', 'provider_requested', 'provider_confirmed',
                            'provider_failed', 'physical_state_unknown')),
    spend_entry_id        TEXT REFERENCES token_ledger(id),
    detail                 TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (27, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Release Engine: token_ledger, release_plans, release_authorizations');
