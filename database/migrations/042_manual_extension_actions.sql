-- =============================================================================
-- Migration 042 -- v0.39 Phase I2: audit trail of manual extension-review updates
-- =============================================================================
-- docs/architecture/phase_i_proposal.md (section 5). Purely additive.
--
-- One row per invocation of `python -m cli extension-review ... --apply`, refusals included (a dry run writes nothing). A real attempt is
-- written FIRST with outcome 'attempting' (before the Chaster call) and updated afterwards, so a crash cannot hide a live change; a row
-- still 'attempting' means the outcome is unknown. `changes` holds the planned field changes as enum text only
-- ("tasks peerVerification.visibility keyholder -> all_members"); no task text, no token and no provider text is stored.
-- =============================================================================

CREATE TABLE manual_extension_actions (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    lock_id      TEXT NOT NULL,
    plan_id      TEXT NOT NULL,
    changes      TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    http_status  INTEGER,
    updated_at   TEXT NOT NULL
);

CREATE INDEX idx_manual_extension_actions_created ON manual_extension_actions(created_at);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (42, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase I2: manual_extension_actions audit trail');
