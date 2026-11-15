-- =============================================================================
-- Migration 076 -- health and emergency claims reviewed (G5, owner 2026-11-15); signal kinds widened
-- =============================================================================
-- G5 (philosophy v1.17): a health opening, a health exemption or a Chaster emergency unlock is granted at once and never delayed. Afterwards it
-- is reviewed when it lasted longer than 3 hours, or when a declined request came shortly before it: the wearer is asked, the judge decides with
-- one appeal, and a claim confirmed false is a full Incident in the Honesty domain (FALSE_HEALTH_CLAIM).
--
-- infraction_signals is rebuilt only to widen the kind CHECK (SQLite cannot alter one); every row and every column is copied unchanged.
-- Bug fix: the kinds proof_failed, contradiction, relock_picture_missing and obedience_missed were added in code (migrations 064-072 era) but never
-- to this CHECK, and INSERT OR IGNORE silently skipped them. New kind: health_claim.
--
-- New table `emergency_unlocks` (infractions/health_claims.py is its only writer): Chaster emergency unlocks seen on the wearer's locks.
-- =============================================================================

CREATE TABLE infraction_signals_new (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('hygiene_duration', 'hygiene_frequency', 'lock_claim', 'exemption_overrun', 'photo_rejected',
                                                          'checkin_noncompliance', 'self_report_hygiene',
                                                          'exemption_override', 'proof_failed', 'contradiction', 'relock_picture_missing',
                                                          'obedience_missed', 'health_claim')),
    subject_id              TEXT NOT NULL,
    trust_domain            TEXT NOT NULL,
    detail                  TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('waiting', 'judging', 'decided', 'cleared', 'open', 'confirmed', 'dismissed', 'offered')),
    review_after            TEXT NOT NULL,
    incident_id             TEXT NOT NULL,
    incident_registered_at  TEXT,
    cleared_reason          TEXT,
    created_at              TEXT NOT NULL,
    decided_at              TEXT,
    facts                   TEXT,
    window_start            TEXT,
    window_end              TEXT,
    explanation             TEXT,
    explanation_at          TEXT,
    follow_up               TEXT,
    follow_up_asked_at      TEXT,
    follow_up_at            TEXT,
    appeal                  TEXT,
    appealed_at             TEXT,
    awaiting                TEXT CHECK (awaiting IS NULL OR awaiting IN ('explanation', 'follow_up', 'appeal')),
    awaiting_since          TEXT,
    verdict                 TEXT CHECK (verdict IS NULL OR verdict IN ('explained', 'unclear', 'not_explained', 'silence', 'self_reported')),
    verdict_reason          TEXT,
    judged_at               TEXT,
    judge_pending_since     TEXT,
    confirm_after           TEXT,
    rule_group_id           TEXT,
    admitted                INTEGER CHECK (admitted IS NULL OR admitted IN (0, 1)),
    UNIQUE (kind, subject_id)
);

INSERT INTO infraction_signals_new (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, incident_registered_at, cleared_reason,
                                    created_at, decided_at, facts, window_start, window_end, explanation, explanation_at, follow_up, follow_up_asked_at,
                                    follow_up_at, appeal, appealed_at, awaiting, awaiting_since, verdict, verdict_reason, judged_at, judge_pending_since,
                                    confirm_after, rule_group_id, admitted)
SELECT id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, incident_registered_at, cleared_reason,
       created_at, decided_at, facts, window_start, window_end, explanation, explanation_at, follow_up, follow_up_asked_at,
       follow_up_at, appeal, appealed_at, awaiting, awaiting_since, verdict, verdict_reason, judged_at, judge_pending_since,
       confirm_after, rule_group_id, admitted
FROM infraction_signals;

DROP TABLE infraction_signals;
ALTER TABLE infraction_signals_new RENAME TO infraction_signals;
CREATE INDEX IF NOT EXISTS idx_infraction_signals_status ON infraction_signals(status, review_after);
CREATE INDEX IF NOT EXISTS idx_infraction_signals_incident ON infraction_signals(incident_id);

CREATE TABLE IF NOT EXISTS emergency_unlocks (
    lock_id            TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    unlocked_at        TEXT NOT NULL,
    seen_at            TEXT NOT NULL,
    relocked_seen_at   TEXT,
    reviewed_at        TEXT,
    outcome            TEXT
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (76, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'health and emergency claims reviewed (G5); signal kinds widened');
