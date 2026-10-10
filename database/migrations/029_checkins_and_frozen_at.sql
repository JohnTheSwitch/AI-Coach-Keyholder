-- =============================================================================
-- Migration 029 -- v0.39 Phase 2: check_ins and provider `frozenAt`
-- =============================================================================
-- docs/architecture/v039_adaptive_release_and_supervision_proposal.md (final).
--
-- 1. chaster_provider_observations.frozen_at (NULLABLE): Chaster's `frozenAt`, when
--    present and well-typed. It lets the maximum-hold backstop measure how long the
--    barrier has really been up instead of guessing from our own observation log.
--    NULL = unknown (the backstop then simply cannot fire from that observation).
--
-- 2. check_ins: proactive supervision check-ins. One row per scheduled prompt.
--    UNIQUE(user_id, local_date, slot_index) makes scheduling idempotent. Transitions
--    are compare-and-set by the check-in service; a prompt is CLAIMED ('sending')
--    before it is sent and a crash leaves 'needs_review' (never resent automatically).
--    An undeliverable or skipped prompt is NEVER a miss. UNCERTAIN / NOT_EVALUATED
--    verdicts never carry a consequence. `response_text` is the user's own message,
--    kept locally only so it can be evaluated; it is never put in an event payload.
--    (Hygiene sessions remain deferred: their Chaster extension integration is unverified.)
-- =============================================================================

ALTER TABLE chaster_provider_observations ADD COLUMN frozen_at TEXT;

CREATE TABLE IF NOT EXISTS check_ins (
    id                  TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL,
    local_date            TEXT NOT NULL,        -- calendar date in APP_TIMEZONE
    slot_index             INTEGER NOT NULL,
    level                   INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    due_at                   TEXT NOT NULL,
    status                    TEXT NOT NULL CHECK (status IN (
                                 'scheduled', 'sending', 'sent', 'responded', 'answered', 'answered_uncertain',
                                 'insufficient_pending', 'insufficient', 'missed', 'undeliverable', 'skipped',
                                 'needs_review')),
    prompt_text                TEXT,
    sent_at                     TEXT,
    deadline_at                  TEXT,
    response_text                 TEXT,
    responded_at                   TEXT,
    verdict                         TEXT CHECK (verdict IS NULL OR verdict IN (
                                       'SUFFICIENT', 'INSUFFICIENT', 'UNCERTAIN', 'NOT_EVALUATED')),
    rubric_version                   TEXT,
    follow_up_until                   TEXT,
    created_at                         TEXT NOT NULL,
    updated_at                          TEXT NOT NULL,
    UNIQUE(user_id, local_date, slot_index)
);

CREATE INDEX IF NOT EXISTS idx_check_ins_user_status ON check_ins(user_id, status);
CREATE INDEX IF NOT EXISTS idx_check_ins_due ON check_ins(due_at) WHERE status = 'scheduled';


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (29, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase 2: check_ins, observation frozen_at');
