-- =============================================================================
-- Migration 045 -- v0.39 Phase J2: the Communication-First release ledger
-- =============================================================================
-- docs/architecture/phase_j_proposal.md (section 4.5). Purely additive.
--
-- One row per wearer and local day on which a release became possible. It makes each message go out once, however often the periodic cycle
-- runs: `eligible_at` the eligibility notice or the combined notice-and-prompt; `prompted_at` the check-in prompt part of it; `ready_at` the
-- "releasing you now" message; `moved_at` the note that the day's window closed first. Timestamps of OUR OWN messages only: a release
-- plan's secret target is never stored here.
-- =============================================================================

CREATE TABLE IF NOT EXISTS release_notices (
    user_id      TEXT NOT NULL,
    local_date   TEXT NOT NULL,
    eligible_at  TEXT,
    prompted_at  TEXT,
    ready_at     TEXT,
    moved_at     TEXT,
    PRIMARY KEY (user_id, local_date)
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (45, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase J2: release_notices (Communication-First release)');
