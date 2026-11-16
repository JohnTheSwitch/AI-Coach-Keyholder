-- =============================================================================
-- Migration 083 -- swats instead of lines, the written request, the release window (owner, 2026-11-15/16; autonomous queue B1-B3)
-- =============================================================================
-- line_assignments (lines/service.py): mode (NULL = lines, 'swats', 'request'), when it was chosen, and the written request's two answers.
-- release_windows (release_earning/window.py): each confirmed release's 2-hour window to relock and send a verification picture.
-- infraction_signals is rebuilt only to widen the kind CHECK (new kind release_window_overrun); every row and column is copied unchanged.
-- =============================================================================

ALTER TABLE line_assignments ADD COLUMN mode TEXT CHECK (mode IS NULL OR mode IN ('swats', 'request'));
ALTER TABLE line_assignments ADD COLUMN mode_chosen_at TEXT;
ALTER TABLE line_assignments ADD COLUMN request_why TEXT;
ALTER TABLE line_assignments ADD COLUMN request_better TEXT;

CREATE TABLE IF NOT EXISTS release_windows (
    authorization_id  TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    confirmed_at      TEXT NOT NULL,
    due_at            TEXT NOT NULL,
    checked_at        TEXT,
    outcome           TEXT CHECK (outcome IS NULL OR outcome IN ('picture', 'asked', 'unknown')),
    created_at        TEXT NOT NULL
);

CREATE TABLE infraction_signals_new (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('hygiene_duration', 'hygiene_frequency', 'lock_claim', 'exemption_overrun', 'photo_rejected',
                                                          'checkin_noncompliance', 'self_report_hygiene',
                                                          'exemption_override', 'proof_failed', 'contradiction', 'relock_picture_missing',
                                                          'obedience_missed', 'health_claim', 'release_window_overrun')),
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


-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (83, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'swats, written request, release window');
