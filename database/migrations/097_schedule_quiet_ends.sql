-- 097: weekend mornings quiet until his wake time (owner, 2026-11-28: "On weekends I wake up usually at 10 am", "Yes, quiet until 10:00").
-- Written only by schedule_settings/service.py. Saturday (5) or Sunday (6) with no row ends quiet hours at QUIET_HOURS_END.
CREATE TABLE IF NOT EXISTS schedule_quiet_ends (
    weekday    INTEGER PRIMARY KEY CHECK (weekday BETWEEN 5 AND 6),
    quiet_end  TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (97, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'weekend quiet ends');
