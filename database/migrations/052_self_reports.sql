-- =============================================================================
-- Migration 052 -- what the wearer owns up to by themselves (owner, 2026-11-12, option A)
-- =============================================================================
-- Live, the wearer told the bot they had edged during a hygiene opening, and nothing happened: no rule saw it. A confession now becomes a finding marked
-- as self-disclosed. The bot asks once whether it read the message right ('offered'); only the wearer's own "Yes" decides it (verdict 'self_reported',
-- admitted = 1), and after the appeal time it is confirmed like any other finding, weighing less because the wearer told on themselves. Unanswered or
-- "No": nothing is recorded.
--
-- infraction_signals is rebuilt only to widen three CHECKs (SQLite cannot alter one); every row and every column is copied unchanged.
-- New kind: self_report_hygiene. New status: offered. New verdict: self_reported.
-- =============================================================================

CREATE TABLE infraction_signals_new (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('hygiene_duration', 'hygiene_frequency', 'lock_claim', 'exemption_overrun', 'photo_rejected',
                                                          'checkin_noncompliance', 'self_report_hygiene')),
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
VALUES (52, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'what the wearer owns up to by themselves');
