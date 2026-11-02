-- =============================================================================
-- Migration 035 -- v0.39 Phase E4: managed-lock observations (per tenant database)
-- =============================================================================
-- docs/architecture/dual_role_and_messaging_proposal.md (section 14). Purely additive; unused by the single-wearer system.
--
-- A managed wearer does not have to give this bot a token: the lock is observed through the KEYHOLDER search
-- (POST /keyholder/locks/search) and verified against the tenant's pairing (lock id, the wearer's Chaster account, the bot as keyholder).
-- `outcome`: observed (verified, usable), not_found (the paired lock is not among the keyholder's LOCKED locks: unlocked, archived or gone),
-- mismatch (found, but the wearer or keyholder is not the paired one). Only an `observed` row is ever handed to execution; the others are an
-- audit of why nothing may happen. Source `wearer_connection` = the wearer's own connected account was used because the keyholder search failed.
-- Ids, states and timestamps only: never the lock combination or any token.
-- =============================================================================

CREATE TABLE managed_lock_observations (
    id                   TEXT PRIMARY KEY,
    lock_id              TEXT NOT NULL,
    outcome              TEXT NOT NULL CHECK (outcome IN ('observed', 'not_found', 'mismatch')),
    source               TEXT NOT NULL CHECK (source IN ('keyholder_search', 'wearer_connection')),
    status               TEXT CHECK (status IS NULL OR status IN ('locked', 'unlocked', 'deserted')),
    is_frozen            INTEGER CHECK (is_frozen IS NULL OR is_frozen IN (0, 1)),
    frozen_at            TEXT,
    end_date             TEXT,
    wearer_chaster_id    TEXT,
    keyholder_chaster_id TEXT,
    fetched_at           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    CHECK (outcome != 'observed' OR status IS NOT NULL)
);

CREATE INDEX idx_managed_lock_observations_lock ON managed_lock_observations(lock_id, fetched_at);

INSERT INTO schema_version (version, applied_at, description)
VALUES (35, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase E4: managed_lock_observations');
