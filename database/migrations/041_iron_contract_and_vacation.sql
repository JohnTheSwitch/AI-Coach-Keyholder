-- =============================================================================
-- Migration 041 -- v0.39 Phase G6: scheduled vacations (pause mode)
-- =============================================================================
-- docs/architecture/phase_g_proposal.md (section 7). Purely additive.
--
-- vacations                       a wearer's scheduled pause, set while unlocked: local dates start_date..end_date (inclusive) and the UTC span
--                                 [start_at, end_at). `vacation end` sets end_at to the moment it was ended (ended_early = 1); `vacation cancel`
--                                 (before the start) sets status 'cancelled'. While a vacation is in force no check-in is sent and no deadline turns
--                                 into a miss, a fully paused day is not evaluated, and the Progressive Conditioning factor cannot rise. Penalties,
--                                 the token ledger and the rules are untouched.
-- conditioning_paused_releases    audit of a confirmed release that came during a vacation and therefore did not raise the factor (it is
--                                 considered exactly once, like training_increments).
-- daily_evaluations.paused        1 for a day that lay entirely inside a vacation: pool, streak and tokens unchanged.
-- =============================================================================

CREATE TABLE vacations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
    ended_early INTEGER NOT NULL DEFAULT 0 CHECK (ended_early IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_vacations_user ON vacations(user_id, status, end_at);

CREATE TABLE conditioning_paused_releases (
    authorization_id TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    vacation_id      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

ALTER TABLE daily_evaluations ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;

INSERT INTO schema_version (version, applied_at, description)
VALUES (41, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase G6: vacations, conditioning_paused_releases, daily_evaluations.paused');
