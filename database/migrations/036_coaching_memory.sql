-- =============================================================================
-- Migration 036 -- v0.39 Phase F2: Coaching Memory and the conditioning preference
-- =============================================================================
-- docs/architecture/phase_f_proposal.md (sections 5.1, 5.4). Purely additive; per database (each tenant has its own).
--
-- wearer_dossier ("Coaching Memory"): short notes the model may save about the user's OWN behaviour, stated goals and quotes. Written only through the
-- validated structured-output path (never from an untrusted channel), filtered in code against health, medical, trauma, self-harm and distress content,
-- capped in length, rate-limited and de-duplicated. The user lists entries with `memory` and erases them with `forget <id>` / `forget all`: erasure is
-- a real DELETE (it is the user's own data).
--
-- conditioning_preferences: the wearer's own switch for Progressive Conditioning (`conditioning on|off`). No row = on (accepted with the invitation).
-- Phase F4 reads it; while off, the hidden factor never increases.
-- =============================================================================

CREATE TABLE wearer_dossier (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    category     TEXT NOT NULL CHECK (category IN ('behavior_pattern', 'stated_goal', 'quote')),
    content      TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 300),
    content_key  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (user_id, content_key)
);

CREATE INDEX idx_wearer_dossier_user ON wearer_dossier(user_id, created_at);

CREATE TABLE conditioning_preferences (
    user_id    TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (36, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase F2: wearer_dossier (Coaching Memory), conditioning_preferences');
