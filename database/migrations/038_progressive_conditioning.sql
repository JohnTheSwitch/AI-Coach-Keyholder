-- =============================================================================
-- Migration 038 -- v0.39 Phase F4: Progressive Conditioning (per database)
-- =============================================================================
-- docs/architecture/phase_f_proposal.md (sections 5.2, 9). Purely additive.
--
-- training_settings      the OWNER's per-database switch (`python -m cli tenants slave-training <id> --enable|--disable`). No row = off.
-- training_state         the hidden factor per user (decimal text, 1.000 .. 1.500). No row = 1.000. Never shown to the model or the wearer.
-- training_increments    append-only audit: one row per confirmed release that was considered (UNIQUE authorization id, so a release can never raise the
--                        factor twice), with the outcome: increased | capped | not_token | wearer_off | owner_off.
-- The token pool pays a token at base(mode) * factor (base 1.0 Standard, 2.0 Advanced) while ENABLE_SLAVE_TRAINING is on.
-- =============================================================================

CREATE TABLE training_settings (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    owner_enabled INTEGER NOT NULL CHECK (owner_enabled IN (0, 1)),
    updated_at    TEXT NOT NULL
);

CREATE TABLE training_state (
    user_id    TEXT PRIMARY KEY,
    factor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE training_increments (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    outcome          TEXT NOT NULL CHECK (outcome IN ('increased', 'capped', 'not_token', 'wearer_off', 'owner_off')),
    factor_before    TEXT NOT NULL,
    factor_after     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (38, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase F4: Progressive Conditioning (training_settings, training_state, training_increments)');
