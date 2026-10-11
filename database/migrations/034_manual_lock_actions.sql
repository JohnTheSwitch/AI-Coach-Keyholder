-- =============================================================================
-- Migration 034 -- v0.39 dual-role, Phase D: audit trail of manual freeze / unfreeze invocations
-- =============================================================================
-- docs/architecture/dual_role_and_messaging_proposal.md (section 9). Purely additive.
--
-- One row per invocation of `python -m cli freeze-lock`, refusals included. A real attempt is written FIRST with outcome 'attempting'
-- (before the Chaster call) and updated afterwards, so a crash cannot hide a physical action; a row still 'attempting' means the
-- outcome is unknown. `actor` is the identity string given on the command line (truncated); no tokens or provider text are stored.
-- =============================================================================

CREATE TABLE manual_lock_actions (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    actor            TEXT NOT NULL,
    lock_id          TEXT NOT NULL,
    requested_frozen INTEGER NOT NULL CHECK (requested_frozen IN (0, 1)),
    outcome          TEXT NOT NULL,
    http_status      INTEGER,
    updated_at       TEXT NOT NULL
);

CREATE INDEX idx_manual_lock_actions_created ON manual_lock_actions(created_at);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (34, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 dual-role Phase D: manual_lock_actions audit trail');
