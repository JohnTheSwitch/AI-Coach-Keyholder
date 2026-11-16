-- =============================================================================
-- Migration 084 -- temptations (owner, 2026-11-15/16; story idea P5, autonomous queue B7)
-- =============================================================================
-- temptation_offers (temptations/service.py): about once a week an offer of 15 minutes of edging with the Deep Diver (no cumming) at a
-- cost to token progress, about 1 in 4 free. cost is text (a Decimal), 0 for a free one.
-- =============================================================================

CREATE TABLE IF NOT EXISTS temptation_offers (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    local_date   TEXT NOT NULL,
    offer_at     TEXT NOT NULL,
    expires_at   TEXT,
    cost         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('planned', 'offered', 'accepted', 'expired', 'skipped')),
    decided_at   TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (user_id, local_date)
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (84, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'temptations');
