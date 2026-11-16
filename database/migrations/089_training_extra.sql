-- 089: an extra training session on demand (owner, 2026-11-16: "I've already done the 3 this week but I have time today, so I can do
-- the fourth"). At most one per ISO week; NULL for weeks without one (old rows have none).
ALTER TABLE training_weeks ADD COLUMN extra_date TEXT;
ALTER TABLE training_weeks ADD COLUMN extra_kind TEXT;

INSERT INTO schema_version (version, applied_at, description)
VALUES (89, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'training extra session');
