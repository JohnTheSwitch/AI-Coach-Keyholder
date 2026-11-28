-- Live, 2026-11-28: the owner ended his lock in Chaster on 2026-11-25 evening and the PC stayed off; the last reading said "locked", so the
-- days after it counted as locked days and earned token progress. A reading of a lock that has ended now keeps Chaster's own `unlockedAt`
-- (provider-reported), so a day after the lock ended is known to be unlocked even when nothing was read during it.
ALTER TABLE managed_lock_observations ADD COLUMN ended_at TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (96, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'managed_lock_observations.ended_at (Chaster unlockedAt)');
