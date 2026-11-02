-- =============================================================================
-- Platform database, migration 004 -- v0.39 Phase E6: archival of removed tenants
-- =============================================================================
-- When a tenant is removed (by the keyholder, or the wearer left or declined), its database file is moved out of the live tenant
-- directory into `tenants/archive/` (read-only) after a short grace period, so it can never be processed again but stays available
-- for audit. `archive_file` is the archived file name (NULL with `archived_at` set = there was no database to archive).
-- =============================================================================

ALTER TABLE tenants ADD COLUMN archived_at TEXT;
ALTER TABLE tenants ADD COLUMN archive_file TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'platform: tenants.archived_at, tenants.archive_file');
