-- =============================================================================
-- Platform database, migration 001 -- v0.39 Phase E1: tenant registry, global halt, lifecycle audit
-- =============================================================================
-- docs/architecture/dual_role_and_messaging_proposal.md (section 10). This is a SEPARATE small SQLite file from every tenant database; it
-- holds only who the tenants are, whether each is allowed to be managed, and a global kill switch. No goals, Trust, lock data, tokens
-- or message text live here.
--
--   tenants       one row per person the keyholder manages. `tenant_0` is the existing system (the owner's own database), registered
--                 unchanged. Status: invited -> active (the wearer accepted) <-> suspended (keyholder pause); removed (terminal) by the
--                 keyholder or by the wearer leaving. Only non-removed tenants count against the cap and own a Discord id / lock id.
--   platform_halt a single row: one switch that stops execution for EVERY tenant (each tenant also keeps its own halt).
--   tenant_events append-only lifecycle audit (invite, accept, suspend, resume, remove, leave, pair, halt) with the actor.
-- =============================================================================

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE tenants (
    id                 TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL,
    discord_id         TEXT,
    chaster_account_id TEXT,
    lock_id            TEXT,
    db_file            TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('invited', 'active', 'suspended', 'removed')),
    is_tenant_zero     INTEGER NOT NULL DEFAULT 0 CHECK (is_tenant_zero IN (0, 1)),
    invited_at         TEXT,
    accepted_at        TEXT,
    removed_at         TEXT,
    removed_by         TEXT CHECK (removed_by IS NULL OR removed_by IN ('keyholder', 'wearer')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK (status != 'removed' OR (removed_at IS NOT NULL AND removed_by IS NOT NULL)),
    CHECK (is_tenant_zero = 0 OR status IN ('active', 'suspended'))
);

CREATE UNIQUE INDEX ux_tenants_db_file ON tenants(db_file);
CREATE UNIQUE INDEX ux_tenants_tenant_zero ON tenants(is_tenant_zero) WHERE is_tenant_zero = 1;
CREATE UNIQUE INDEX ux_tenants_discord ON tenants(discord_id) WHERE discord_id IS NOT NULL AND status != 'removed';
CREATE UNIQUE INDEX ux_tenants_lock ON tenants(lock_id) WHERE lock_id IS NOT NULL AND status != 'removed';
CREATE UNIQUE INDEX ux_tenants_chaster ON tenants(chaster_account_id) WHERE chaster_account_id IS NOT NULL AND status != 'removed';

CREATE TABLE platform_halt (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    halted     INTEGER NOT NULL CHECK (halted IN (0, 1)),
    reason     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO platform_halt (id, halted, reason, updated_at) VALUES (1, 0, 'initial state', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE tenant_events (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT,
    event      TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_tenant_events_tenant ON tenant_events(tenant_id, created_at);

INSERT INTO schema_version (version, applied_at, description)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'platform: tenants, platform_halt, tenant_events');
