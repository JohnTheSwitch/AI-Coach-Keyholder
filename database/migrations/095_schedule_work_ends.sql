-- 095: per-day end of the school/work day (owner, 2026-11-25: "Per day end times, round it to 3 and 5 pm"). Written only by
-- schedule_settings/service.py. A weekday (0 = Monday .. 4 = Friday) with no row uses WORKDAY_END.
CREATE TABLE IF NOT EXISTS schedule_work_ends (
    weekday    INTEGER PRIMARY KEY CHECK (weekday BETWEEN 0 AND 4),
    work_end   TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

INSERT INTO schema_version (version, applied_at, description)
VALUES (95, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'per-day school/work day ends');
