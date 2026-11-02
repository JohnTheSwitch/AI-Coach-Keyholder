-- =============================================================================
-- Migration 040 -- v0.39 Phase G4: opt-in research export (per database)
-- =============================================================================
-- docs/architecture/phase_g_proposal.md (section 6, G4). Purely additive.
--
-- research_preferences      the wearer's own `research on|off`. No row = off. `enabled_at` is the moment of the latest `research on`: only records that
--                           occurred after it are exported (consent is not retroactive).
-- research_erase_requests   queued by `research off` and by `forget all`; sent to the receiver as an erase of the user's pseudonym, then `sent_at` is set.
-- behavioral_telemetry.synced_at (migration 039) is set when a record was accepted by the receiver (HTTP 200).
-- =============================================================================

CREATE TABLE research_preferences (
    user_id    TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    enabled_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE research_erase_requests (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE INDEX idx_research_erase_pending ON research_erase_requests(sent_at, created_at);
CREATE INDEX idx_behavioral_telemetry_unsynced ON behavioral_telemetry(synced_at, occurred_at);

INSERT INTO schema_version (version, applied_at, description)
VALUES (40, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase G4: research_preferences, research_erase_requests');
