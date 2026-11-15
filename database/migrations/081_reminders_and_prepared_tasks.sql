-- =============================================================================
-- Migration 081 -- fewer repeated reminders; tasks chosen at the heads-up (owner, 2026-11-16; autonomous queue A0, A0b)
-- =============================================================================
-- reminder_log (reminders/service.py): when a standing reminder (safety, discretion, the health option, the skin check) was last shown
-- in full; it is shown again only after a week.
-- obedience_tasks.prepared (obedience/service.py): the alone and the discreet version of a task, chosen at the clean-up heads-up so the
-- heads-up goes out only when the task uses a plug (JSON; NULL when chosen later, as before).
-- =============================================================================

CREATE TABLE IF NOT EXISTS reminder_log (
    user_id   TEXT NOT NULL,
    key       TEXT NOT NULL,
    shown_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

ALTER TABLE obedience_tasks ADD COLUMN prepared TEXT;

-- =============================================================================
-- Seed: record this migration in schema_version
-- =============================================================================
INSERT INTO schema_version (version, applied_at, description)
VALUES (81, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'reminders and prepared tasks');
