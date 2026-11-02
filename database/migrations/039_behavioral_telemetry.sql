-- =============================================================================
-- Migration 039 -- v0.39 Phase G1: local derived Behavioral Telemetry (per database)
-- =============================================================================
-- docs/architecture/phase_g_proposal.md (sections 3, 4). Purely additive.
--
-- behavioral_telemetry   derived, numeric facts about the user's behaviour (response_delay, token_burn, inflation_resilience, daily_summary). `metrics` is a
--                        JSON object of numbers, booleans and short enum strings, never free text. `source_key` (UNIQUE) names the source row, so the
--                        derivation is idempotent. `synced_at` is reserved for the opt-in export (G4) and stays NULL until then. The event_type list is
--                        validated in code (behavioral_telemetry/store.py), not with a CHECK, so later steps can add types without rebuilding the table.
--                        Rows older than 180 days are deleted by the collector.
-- telemetry_watermarks   per source: the last (timestamp, id) position the collector has read.
-- =============================================================================

CREATE TABLE behavioral_telemetry (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    metrics     TEXT NOT NULL,
    source_key  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    synced_at   TEXT
);

CREATE INDEX idx_behavioral_telemetry_user ON behavioral_telemetry(user_id, occurred_at);
CREATE INDEX idx_behavioral_telemetry_occurred ON behavioral_telemetry(occurred_at);

CREATE TABLE telemetry_watermarks (
    source      TEXT PRIMARY KEY,
    position_at TEXT NOT NULL,
    position_id TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (39, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase G1: behavioral_telemetry, telemetry_watermarks');
