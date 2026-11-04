-- =============================================================================
-- Migration 046 -- v0.39 Phase J3: the coach's proposals and daily commitments
-- =============================================================================
-- docs/architecture/phase_j_proposal.md (section 5). Purely additive.
--
-- `coach_proposals`: what the language model asked for AFTER validation and the deterministic gates. Nothing here has any effect by itself: a
-- proposal is shown to the user on a card and only their button applies it, exactly as recorded (`payload_json` is never rewritten, philosophy
-- 2.5 and GOAL-6). At most one is pending per user. `fingerprint` is the folded word set, so a declined proposal is not offered again.
-- `coach_refusals`: reason codes of what the gates turned down, never the text. Audit only, read by `python -m cli coach-status`.
-- `daily_commitments`: what the user accepted. Their outcome is the user's own button press. Not kept is a minor Trust reduction through an event and
-- nothing else: it is not goal evidence, it takes no tokens, and it never reaches the lock. Unanswered commitments lapse at no cost.
-- =============================================================================

CREATE TABLE IF NOT EXISTS coach_proposals (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    local_date    TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('propose_goal', 'adapt_goal', 'propose_commitment')),
    payload_json  TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'accepted', 'declined', 'superseded', 'expired')),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    resolved_at   TEXT,
    result_id     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_coach_proposal ON coach_proposals(user_id) WHERE status IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS idx_coach_proposals_user_day ON coach_proposals(user_id, local_date);

CREATE TABLE IF NOT EXISTS coach_refusals (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    action      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_commitments (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    for_date            TEXT NOT NULL,
    text                TEXT NOT NULL,
    goal_group_id       TEXT REFERENCES goals(goal_group_id),
    task_assignment_id  TEXT REFERENCES task_assignments(id),
    proposal_id         TEXT NOT NULL UNIQUE REFERENCES coach_proposals(id),
    status              TEXT NOT NULL CHECK (status IN ('open', 'kept', 'partly', 'not_kept', 'lapsed')),
    created_at          TEXT NOT NULL,
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_commitments_user_date ON daily_commitments(user_id, for_date);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (46, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase J3: coach_proposals, coach_refusals, daily_commitments');
