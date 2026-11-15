-- =============================================================================
-- Migration 073 -- obedience tasks: his gear list and "Are you alone right now?" (owner, 2026-11-15)
-- =============================================================================
-- Every task names the gear it needs; only tasks whose gear he owns are given. He keeps the list in the DM (`gear`, `gear add blindfold`,
-- `gear remove blindfold`); a gear piece with no row here keeps its default (obedience/service.py DEFAULT_OWNED).
-- Before an erotic task the bot asks whether he is alone: alone = the full version, not alone = a discreet version; both are mandatory.
-- No answer in 15 minutes = the discreet version.
--
-- obedience_gear (written only by obedience/service.py): user_id, gear, owned (0/1), changed_at; PRIMARY KEY (user_id, gear)
-- obedience_tasks, additive columns: mode (alone | discreet), asked_at
--   status: planned -> asking -> assigned -> submitted -> passed | failed | cancelled | skipped
-- =============================================================================

CREATE TABLE IF NOT EXISTS obedience_gear (
    user_id     TEXT NOT NULL,
    gear        TEXT NOT NULL,
    owned       INTEGER NOT NULL,
    changed_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, gear)
);

ALTER TABLE obedience_tasks ADD COLUMN mode TEXT;
ALTER TABLE obedience_tasks ADD COLUMN asked_at TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (73, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'obedience gear list and alone question');
