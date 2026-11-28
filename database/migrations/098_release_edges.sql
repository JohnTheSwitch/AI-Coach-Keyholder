-- 098: edging during a release session earns token progress (owner, 2026-11-28: "Edging while released I think should add tokens instead
-- of costing them, because it's something the wearer has to do that delays cumming" -> "+0.05, bot runs the toy"). Written only by
-- temptations/service.py. One offer per confirmed release; the reward counts at the day's evaluation of `decided_at`.
CREATE TABLE IF NOT EXISTS release_edges (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    offered_at       TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('offered', 'ran', 'earned', 'declined', 'expired')),
    reward           TEXT NOT NULL DEFAULT '0',
    played_seconds   REAL,
    decided_at       TEXT
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (98, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'release edging earns token progress');
