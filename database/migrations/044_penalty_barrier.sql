-- =============================================================================
-- Migration 044 -- v0.39 Phase J2: penalty holds, the barrier intent queue, check-in warnings
-- =============================================================================
-- docs/architecture/phase_j_proposal.md (section 4). Purely additive.
--
-- penalty_holds            one row per Penalty Window, written by the Rules Engine's state recorder from the window's own events. While
--                          `released_at` is NULL token redemption is blocked globally (earning is untouched). `raised_freeze` = 1 only when
--                          this window's own start actually froze the lock, so its end never releases a barrier it did not raise.
-- penalty_barrier_intents  the durable queue of intended Chaster freeze / unfreeze calls (id = the domain event id, so a redelivered event
--                          cannot queue twice). An intent NEVER expires: while the runtime is halted or the feature is off it stays
--                          `pending`, logged once (`logged_at`). status: pending -> executing -> executed | failed | needs_review, or skipped.
-- check_in_escalations     gains the Warning and the keyholder's decision: an escalation stays state 'registered' (Incident UNCONFIRMED)
--                          until the keyholder confirms it; `keyholder_decision` = confirmed | dismissed.
--
-- Ids, enum codes and timestamps only: no message text, no token, no provider response.
-- =============================================================================

CREATE TABLE IF NOT EXISTS penalty_holds (
    penalty_window_id  TEXT PRIMARY KEY,
    started_at         TEXT NOT NULL,
    released_at        TEXT,
    raised_freeze      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_penalty_holds_open ON penalty_holds(started_at) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS penalty_barrier_intents (
    id                 TEXT PRIMARY KEY,
    penalty_window_id  TEXT NOT NULL,
    action             TEXT NOT NULL CHECK (action IN ('freeze', 'unfreeze')),
    reason             TEXT NOT NULL CHECK (reason IN ('window_started', 'exemption_started', 'exemption_ended', 'window_completed')),
    status             TEXT NOT NULL CHECK (status IN ('pending', 'executing', 'executed', 'skipped', 'failed', 'needs_review')),
    detail             TEXT,
    http_status        INTEGER,
    lock_id            TEXT,
    logged_at          TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_penalty_barrier_intents_pending ON penalty_barrier_intents(created_at) WHERE status IN ('pending', 'executing');

ALTER TABLE check_in_escalations ADD COLUMN warned_at TEXT;
ALTER TABLE check_in_escalations ADD COLUMN warning_notified_at TEXT;
ALTER TABLE check_in_escalations ADD COLUMN keyholder_decision TEXT;
ALTER TABLE check_in_escalations ADD COLUMN decided_at TEXT;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (44, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase J2: penalty_holds, penalty_barrier_intents, check-in warnings');
