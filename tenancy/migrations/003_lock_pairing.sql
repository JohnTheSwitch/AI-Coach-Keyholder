-- =============================================================================
-- Platform database, migration 003 -- v0.39 Phase E4: lock pairing confirmation
-- =============================================================================
-- A tenant's managed lock is paired by the owner (`tenants pair`, verified live against the keyholder search) and then CONFIRMED
-- by the wearer (`confirm lock` in their own DM), or confirmed automatically when the wearer's own connected Chaster account is the lock's
-- wearer. Execution for a tenant reads its lock only while the pairing is confirmed. Re-pairing clears the confirmation.
-- =============================================================================

ALTER TABLE tenants ADD COLUMN pairing_confirmed_at TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'platform: tenants.pairing_confirmed_at');
