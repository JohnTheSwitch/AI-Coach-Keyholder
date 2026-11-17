-- 094: workouts and temptations for another wearer (owner, 2026-11-17: "make everything including workouts and temptations available -
-- of course after setting up"). The owner (with Claude) writes the wearer's training plan and their temptations; the wearer approves each
-- before it can be given. custom_tasks.kind and wearer_training_plans are written only by custom_tasks/service.py; temptation_offers.text
-- only by temptations/service.py (the text offered, NULL for the owner's own fixed offer).
ALTER TABLE custom_tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task' CHECK (kind IN ('task', 'temptation'));

CREATE TABLE IF NOT EXISTS wearer_training_plans (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    plan_json   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'replaced')),
    proposed_at TEXT NOT NULL,
    decided_at  TEXT,
    UNIQUE (user_id, number)
);
CREATE INDEX IF NOT EXISTS idx_wearer_training_plans_user ON wearer_training_plans (user_id, status);

ALTER TABLE temptation_offers ADD COLUMN text TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (94, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'wearer training plans and temptations');
