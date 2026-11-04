-- =============================================================================
-- Migration 047 -- v0.39 backlog: notification queue, infraction detectors, exemption workflow
-- =============================================================================
-- docs/architecture/backlog_completion.md. Purely additive.
--
-- `notifications`: the durable outbound queue. A row is written in the same transaction as the fact it reports, so a notice can never be lost to a
-- crash or a closed DM. `dedupe_key` makes every enqueue idempotent. The text never holds a Trust value.
-- `hygiene_openings`: Chaster's `temporary-opening` state as this system saw it. `classification` is the wearer's own word and is never second-guessed:
-- `health` is Mandatory Access (philosophy 3.9) and counts for nothing; `unclassified` also counts for nothing, because silence is not evidence (2.12).
-- `infraction_signals`: what a detector found. A signal becomes an UNCONFIRMED Incident only after the wearer had the chance to say it was a health need;
-- only the keyholder can confirm it. One subject can never raise the same kind of signal twice (UNIQUE).
-- `lock_claim_checks`: which `lock_reports` rows were compared with Chaster, and what came of it. Audit only.
-- `wear_exemptions`: Temporary Wear Exemption requests (philosophy 3.6). The decision never reads the Penalty Window (invariant I7).
-- =============================================================================

CREATE TABLE IF NOT EXISTS notifications (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    kind              TEXT NOT NULL,
    dedupe_key        TEXT NOT NULL UNIQUE,
    text              TEXT NOT NULL,
    embed_json        TEXT,
    actions_json      TEXT,
    immediate         INTEGER NOT NULL DEFAULT 0 CHECK (immediate IN (0, 1)),
    status            TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'delivered', 'failed')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   TEXT NOT NULL,
    claimed_at        TEXT,
    created_at        TEXT NOT NULL,
    delivered_at      TEXT,
    last_outcome      TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_due ON notifications(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS hygiene_openings (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    lock_id            TEXT NOT NULL,
    opened_at          TEXT NOT NULL,
    last_seen_open_at  TEXT NOT NULL,
    closed_at          TEXT,
    duration_known     INTEGER NOT NULL DEFAULT 1 CHECK (duration_known IN (0, 1)),
    classification     TEXT NOT NULL DEFAULT 'unclassified' CHECK (classification IN ('unclassified', 'planned', 'health')),
    classified_at      TEXT,
    evaluated_at       TEXT,
    UNIQUE (lock_id, opened_at)
);

CREATE INDEX IF NOT EXISTS idx_hygiene_openings_user ON hygiene_openings(user_id, opened_at);

CREATE TABLE IF NOT EXISTS infraction_signals (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('hygiene_duration', 'hygiene_frequency', 'lock_claim')),
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

CREATE INDEX IF NOT EXISTS idx_infraction_signals_status ON infraction_signals(status, review_after);

CREATE TABLE IF NOT EXISTS lock_claim_checks (
    report_id   TEXT PRIMARY KEY REFERENCES lock_reports(id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('baseline', 'consistent', 'stale', 'excused', 'contradicted', 'unlocked_while_locked')),
    checked_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wear_exemptions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    category      TEXT CHECK (category IN ('family', 'social', 'work_school', 'health', 'travel', 'other')),
    hours         INTEGER,
    status        TEXT NOT NULL CHECK (status IN ('draft', 'requested', 'approved', 'declined', 'ended')),
    created_at    TEXT NOT NULL,
    requested_at  TEXT,
    decided_at    TEXT,
    decided_by    TEXT,
    expires_at    TEXT,
    ended_at      TEXT,
    end_reason    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_exemption ON wear_exemptions(user_id) WHERE status IN ('draft', 'requested', 'approved');

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (47, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 backlog: notifications, hygiene_openings, infraction_signals, lock_claim_checks, wear_exemptions');
