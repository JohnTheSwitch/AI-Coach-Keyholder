-- =============================================================================
-- Migration 050 -- the wearer is asked first, and the system judges (owner, 2026-11-10)
-- =============================================================================
-- There is no second person to press a keyholder button: the Keyholder is the system's own role. So a detection is now a question to the wearer, the
-- wearer's own explanation is judged (code first for health and emergencies, then the language model), and the system itself confirms what stays
-- unexplained. See infractions/README.md.
--
-- infraction_signals is rebuilt only to widen its two CHECKs (SQLite cannot alter one); every row is copied and every old column keeps its meaning.
-- New kinds: photo_rejected (a community review), checkin_noncompliance (the check-in escalation's Incident). New statuses: judging (an answer waits for the
-- model), decided (unexplained; confirmed when the appeal time is over). `open` and `dismissed` stay for rows written before this migration.
--
-- New columns: facts (what the judge sees; never shown to the wearer, it may hold limits derived from Trust), window_start / window_end (the time the
-- finding covers, for the excuse check), explanation / follow_up / appeal and their times (the wearer's own words, erased by `forget all`), awaiting /
-- awaiting_since (which answer the next DM is taken as), verdict / verdict_reason / judged_at, judge_pending_since (an answer the model could not judge
-- yet), confirm_after (the end of the appeal time), rule_group_id (chosen when it is decided: silence is warning-only).
-- =============================================================================

CREATE TABLE infraction_signals_new (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('hygiene_duration', 'hygiene_frequency', 'lock_claim', 'exemption_overrun', 'photo_rejected',
                                                          'checkin_noncompliance')),
    subject_id              TEXT NOT NULL,
    trust_domain            TEXT NOT NULL,
    detail                  TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('waiting', 'judging', 'decided', 'cleared', 'open', 'confirmed', 'dismissed')),
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
    verdict                 TEXT CHECK (verdict IS NULL OR verdict IN ('explained', 'unclear', 'not_explained', 'silence')),
    verdict_reason          TEXT,
    judged_at               TEXT,
    judge_pending_since     TEXT,
    confirm_after           TEXT,
    rule_group_id           TEXT,
    UNIQUE (kind, subject_id)
);

INSERT INTO infraction_signals_new (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, incident_registered_at, cleared_reason,
                                    created_at, decided_at)
SELECT id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, incident_registered_at, cleared_reason, created_at, decided_at
FROM infraction_signals;

DROP TABLE infraction_signals;
ALTER TABLE infraction_signals_new RENAME TO infraction_signals;
CREATE INDEX IF NOT EXISTS idx_infraction_signals_status ON infraction_signals(status, review_after);
CREATE INDEX IF NOT EXISTS idx_infraction_signals_incident ON infraction_signals(incident_id);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (50, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'the wearer is asked first, and the system judges');
