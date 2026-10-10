-- =============================================================================
-- Migration 031 -- v0.39 behavioural model: mode-dependent penalty caps, warnings, daily evaluation
-- =============================================================================
-- docs/architecture/v039_adaptive_release_and_supervision_proposal.md (section 11.9).
--
-- 1. penalty_windows.max_target_hours: the hard cap that applies to THIS window, fixed when it starts
--    (Standard 336 h, Advanced 720 h). Existing rows keep 336.
-- 2. incident_warnings: a confirmed Incident that is a FIRST minor offense is consumed as a warning only
--    (Trust drop through the incident evidence, no Penalty Window). Write-once per incident, like
--    incident_consumption; both tables count as "prior offenses" of a rule group.
-- 3. token_pool_state / clean_streak_state / daily_evaluations: the Token Progress Pool and the Clean Day
--    streak, evaluated once per local day (UNIQUE per user and date, so a re-run changes nothing).
--    The pool is stored as a decimal string (exact arithmetic); no goal text or message text is stored.
-- =============================================================================

ALTER TABLE penalty_windows ADD COLUMN max_target_hours REAL NOT NULL DEFAULT 336;

CREATE TABLE incident_warnings (
    incident_id   TEXT PRIMARY KEY,
    trust_domain  TEXT NOT NULL,
    rule_group_id TEXT NOT NULL,
    consumed_at   TEXT NOT NULL
);

CREATE TABLE token_pool_state (
    user_id    TEXT PRIMARY KEY,
    pool       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE clean_streak_state (
    user_id    TEXT PRIMARY KEY,
    streak     INTEGER NOT NULL CHECK (streak >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE daily_evaluations (
    user_id         TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    goals_assigned  INTEGER NOT NULL,
    goals_met       INTEGER NOT NULL,
    check_ins_due   INTEGER NOT NULL,
    check_ins_ok    INTEGER NOT NULL,
    violations      INTEGER NOT NULL,
    trust_used      TEXT NOT NULL,
    progress        TEXT NOT NULL,
    pool_after      TEXT NOT NULL,
    tokens_earned   INTEGER NOT NULL,
    clean           INTEGER NOT NULL,
    streak_after    INTEGER NOT NULL,
    bonus           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, local_date)
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (31, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39: penalty mode caps, incident warnings, token pool, clean streak, daily evaluations');
