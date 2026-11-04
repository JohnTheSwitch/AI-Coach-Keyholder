-- =============================================================================
-- Migration 049 -- v0.39: asking the bot for a hygiene opening (owner, 2026-11-04)
-- =============================================================================
-- docs/architecture/backlog_completion.md, section 4. Purely additive.
--
-- A hygiene request reuses the exemption's whole path (opening, "I'm back" checked against Chaster, late relock, restoring the lock's settings), so it is a row in
-- wear_exemptions with its own `kind`. `exemption`: a Temporary Wear Exemption. `hygiene`: a Discretionary Hygiene Break, counted against the weekly allowance. `hygiene_health`:
-- Mandatory Access (philosophy 3.9), never limited, counted or judged. `minutes` replaces `hours` for the two hygiene kinds.
-- =============================================================================

ALTER TABLE wear_exemptions ADD COLUMN kind TEXT NOT NULL DEFAULT 'exemption' CHECK (kind IN ('exemption', 'hygiene', 'hygiene_health'));
ALTER TABLE wear_exemptions ADD COLUMN minutes INTEGER;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (49, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'v0.39: hygiene requests through the exemption path');
