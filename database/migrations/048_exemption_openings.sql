-- =============================================================================
-- Migration 048 -- v0.39 backlog: an exemption opens the lock (owner, 2026-11-04)
-- =============================================================================
-- docs/architecture/backlog_completion.md, section 4. Purely additive for wear_exemptions; infraction_signals is rebuilt only to widen its CHECK (SQLite
-- cannot alter one), and every row is copied.
--
-- `opening_saved_json`: the lock's own openingTime and regularity, put back when the exemption ends. `opening_claimed_at` is written BEFORE the call to Chaster, so an
-- ambiguous opening is never sent twice. `relocked_at` is Chaster's own log time, the proof the wearer's "I'm back" is checked against.
-- =============================================================================

ALTER TABLE wear_exemptions ADD COLUMN opening_lock_id TEXT;
ALTER TABLE wear_exemptions ADD COLUMN opening_saved_json TEXT;
ALTER TABLE wear_exemptions ADD COLUMN opening_claimed_at TEXT;
ALTER TABLE wear_exemptions ADD COLUMN opened_at TEXT;
ALTER TABLE wear_exemptions ADD COLUMN opening_outcome TEXT;
ALTER TABLE wear_exemptions ADD COLUMN relocked_at TEXT;
ALTER TABLE wear_exemptions ADD COLUMN overdue_notified_at TEXT;

CREATE TABLE infraction_signals_new (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('hygiene_duration', 'hygiene_frequency', 'lock_claim', 'exemption_overrun')),
    subject_id              TEXT NOT NULL,
    trust_domain            TEXT NOT NULL,
    detail                  TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('waiting', 'cleared', 'open', 'confirmed', 'dismissed')),
    review_after            TEXT NOT NULL,
    incident_id             TEXT NOT NULL,
    incident_registered_at  TEXT,
    cleared_reason          TEXT,
    created_at              TEXT NOT NULL,
    decided_at              TEXT,
    UNIQUE (kind, subject_id)
);

INSERT INTO infraction_signals_new SELECT id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, incident_registered_at, cleared_reason, created_at, decided_at
FROM infraction_signals;

DROP TABLE infraction_signals;
ALTER TABLE infraction_signals_new RENAME TO infraction_signals;
CREATE INDEX IF NOT EXISTS idx_infraction_signals_status ON infraction_signals(status, review_after);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (48, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 backlog: exemption openings, exemption_overrun signals');
