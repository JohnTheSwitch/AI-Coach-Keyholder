-- =============================================================================
-- Migration 054 -- exemption requests decided by the system (owner, 2026-11-13)
-- =============================================================================
-- The wearer was also the account that got the keyholder's Approve button and approved their own exemption. Requests are now decided by
-- exemptions/judge.py on the wearer's own reason. All columns NULLABLE: earlier requests had no reason and no ruling.
--
-- reason            the wearer's own words: what it is for (and the answer to the one follow-up question)
-- reason_asked_at   when the bot asked for the reason; a draft with this set is waiting for it
-- follow_up         the one follow-up question, when the first answer was unclear
-- verdict_reason    the ruling's one sentence, as the wearer was told
-- =============================================================================

ALTER TABLE wear_exemptions ADD COLUMN reason TEXT;
ALTER TABLE wear_exemptions ADD COLUMN reason_asked_at TEXT;
ALTER TABLE wear_exemptions ADD COLUMN follow_up TEXT;
ALTER TABLE wear_exemptions ADD COLUMN verdict_reason TEXT;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (54, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'exemption requests decided by the system');
