-- =============================================================================
-- Migration 026 -- Phase 3: Chaster execution plane (foundations)
-- =============================================================================
-- docs/architecture/chaster_execution_phase3_proposal.md (approved).
--
-- 1. chaster_provider_observations gains three NULLABLE provider-reported
--    columns so a LockSnapshot can be hydrated from persisted data:
--      is_frozen     -- Chaster `isFrozen` (0/1), NULL when absent or not a bool
--      has_keyholder -- 1 when `keyholder` is a non-null object, 0 when null,
--                       NULL when the field is absent
--      end_date      -- Chaster `endDate` as stored ISO text, NULL when absent/null
--    Rows written before this migration keep NULLs, which the engine treats as
--    "unknown" and fails closed. `canBeUnlocked` is deprecated by Chaster and is
--    deliberately NOT stored. The table stays append-only.
--
-- 2. lock_decision_executions: append-only audit log, one row per status
--    transition of a lock decision (written only by the execution service).
--    UNIQUE(decision_id, to_status) makes every transition happen at most once
--    per decision: in particular a decision can be claimed ('executing') only
--    once, which is the idempotency guarantee (there is no Chaster idempotency
--    key). No token, header or request body is ever stored here.
--
-- 3. execution_control: single-row runtime halt switch (local DB kill switch),
--    settable only from a local script, never from Discord or a model. The
--    seed row is HALTED: until the owner clears it, nothing executes even with
--    ENABLE_CHASTER_EXECUTION=true. A missing row is also treated as halted.
--
-- lock_decisions.status (no CHECK constraint, migration 025) now takes the
-- values dry_run | pending | executing | executed | failed | blocked | expired
-- | needs_review, advanced only by the execution service.
-- =============================================================================

ALTER TABLE chaster_provider_observations ADD COLUMN is_frozen INTEGER;
ALTER TABLE chaster_provider_observations ADD COLUMN has_keyholder INTEGER;
ALTER TABLE chaster_provider_observations ADD COLUMN end_date TEXT;

CREATE TABLE IF NOT EXISTS lock_decision_executions (
    id               TEXT PRIMARY KEY,
    decision_id       TEXT NOT NULL REFERENCES lock_decisions(id),
    from_status        TEXT NOT NULL,
    to_status           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    detail                TEXT NOT NULL,          -- short, sanitised reason or error class
    http_status            INTEGER,
    end_date_before         TEXT,
    UNIQUE(decision_id, to_status)
);

CREATE INDEX IF NOT EXISTS idx_lock_decision_executions_decision ON lock_decision_executions(decision_id);

CREATE TABLE IF NOT EXISTS execution_control (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    halted         INTEGER NOT NULL CHECK (halted IN (0, 1)),
    reason          TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

INSERT OR IGNORE INTO execution_control (id, halted, reason, updated_at)
VALUES (1, 1, 'initial state: halted until the owner clears it', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (26, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'Phase 3: observation frozen/keyholder/end_date columns, lock_decision_executions, execution_control');
