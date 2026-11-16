-- 090: a short session moved to another day (owner, 2026-11-16: "I won't do the leg day today and will do it tomorrow instead"; "get rid
-- of the catch"): its own slot, so moving never uses up the week's extra session. NULL for weeks without a move.
ALTER TABLE training_weeks ADD COLUMN moved_date TEXT;
ALTER TABLE training_weeks ADD COLUMN moved_kind TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (90, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'training moved session');
