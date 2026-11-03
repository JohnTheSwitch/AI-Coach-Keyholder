-- =============================================================================
-- Migration 043 -- v0.39 Phase I4: community review (peer verification) ledger
-- =============================================================================
-- docs/architecture/phase_i_proposal.md (section 7). Purely additive.
--
-- peer_review_locks        one row per lock once its existing review history has been recorded as a BASELINE (reviews that had
--                          already ended before the watch started never create an Incident).
-- peer_reviews             the latest known state of each community review, keyed by (lock, peer verification id). `incident_id` is
--                          claimed in the same transaction that first records a `rejected` status, so one review can never create more
--                          than one Incident; `incident_registered_at` is set once the UNCONFIRMED Incident exists (the id is chosen in
--                          advance, so a retried registration cannot duplicate it). `owner_decision` is the owner's later manual decision
--                          (`confirmed` | `dismissed`).
-- peer_review_transitions  append-only: every status change seen by the poll (previous NULL = first sighting).
--
-- Stored: ids, enum statuses, vote counts, timestamps. NEVER stored: task texts, verification codes, image keys, voter names.
-- =============================================================================

CREATE TABLE IF NOT EXISTS peer_review_locks (
    lock_id        TEXT PRIMARY KEY,
    baselined_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peer_reviews (
    lock_id                 TEXT NOT NULL,
    review_id               TEXT NOT NULL,
    source                  TEXT NOT NULL,          -- tasks | verification-picture
    status                  TEXT NOT NULL,          -- ongoing | verified | rejected (latest seen)
    verified_votes          INTEGER,
    rejected_votes          INTEGER,
    submitted_at            TEXT,
    baseline                INTEGER NOT NULL DEFAULT 0,
    first_seen_at           TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    incident_id             TEXT,
    incident_registered_at  TEXT,
    notified_at             TEXT,
    owner_decision          TEXT,
    decided_at              TEXT,
    PRIMARY KEY (lock_id, review_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_peer_reviews_incident ON peer_reviews(incident_id) WHERE incident_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS peer_review_transitions (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    lock_id          TEXT NOT NULL,
    review_id        TEXT NOT NULL,
    source           TEXT NOT NULL,
    previous_status  TEXT,
    new_status       TEXT NOT NULL,
    verified_votes   INTEGER,
    rejected_votes   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_peer_review_transitions_review ON peer_review_transitions(lock_id, review_id, created_at);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (43, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase I4: peer_reviews, peer_review_transitions, peer_review_locks');
