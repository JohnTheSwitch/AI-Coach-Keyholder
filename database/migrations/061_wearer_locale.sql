-- =============================================================================
-- Migration 061 -- the wearer's own time zone and public holidays (owner, 2026-11-13)
-- =============================================================================
-- Multi-wearer parity, slice 2: an additional wearer sets their own IANA time zone and public-holiday country with the DM commands
-- `timezone <IANA>` and `holidays <country code|none>`. Quiet hours, the workday, check-in slots, the day boundary of the daily evaluation and
-- the release windows then follow the wearer's clock, not the owner's. The owner's own database never uses this table (APP_TIMEZONE and
-- HOLIDAY_COUNTRY in the configuration stay the owner's settings).
--
-- wearer_locale (one row, id = 1, written only by system.wearer_locale):
--   timezone (NULL: not set), holiday_country (NULL: not set; '' : no public holidays), updated_at
-- =============================================================================

CREATE TABLE IF NOT EXISTS wearer_locale (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    timezone         TEXT,
    holiday_country  TEXT,
    updated_at       TEXT NOT NULL
);

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (61, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'the wearer''s own time zone and public holidays');
