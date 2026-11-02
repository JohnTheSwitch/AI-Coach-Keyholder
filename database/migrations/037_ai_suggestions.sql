-- =============================================================================
-- Migration 037 -- v0.39 Phase F3: the idea box (per database)
-- =============================================================================
-- docs/architecture/phase_f_proposal.md (sections 5.5, 8). Purely additive; each tenant has its own table in its own database.
--
-- ai_suggestions: private ideas the model may submit for the KEYHOLDER through the validated structured-output path (never from an untrusted channel).
-- WRITE-ONLY for the model: nothing that builds model context ever reads this table (guarded by tests); only the owner CLI reads it, across tenants.
-- An accepted suggestion changes nothing by itself: adopting it is a separate decision that needs the affected wearer's consent.
-- Text that touches health, trauma, self-harm or distress is never stored here.
-- =============================================================================

CREATE TABLE ai_suggestions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    suggestion_type TEXT NOT NULL CHECK (suggestion_type IN ('rule_tweak', 'new_punishment', 'system_flaw')),
    content         TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 500),
    content_key     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'accepted', 'rejected', 'archived')),
    owner_note      TEXT,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT,
    UNIQUE (user_id, content_key)
);

CREATE INDEX idx_ai_suggestions_status ON ai_suggestions(status, created_at);

INSERT INTO schema_version (version, applied_at, description)
VALUES (37, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39 Phase F3: ai_suggestions (idea box)');
