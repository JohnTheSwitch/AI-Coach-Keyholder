# schedule_settings: per-day end of the school/work day

Owner, 2026-11-25: "Per day end times, round it to 3 and 5 pm". His timetable has him home at 15:00 on Mon/Thu/Fri and at 17:00 on Tue/Wed;
one `WORKDAY_END` for every weekday took 1.5 free hours on three days and called him free on his way home on two.

- `schedule`: what is set. `schedule mon thu fri 15:00`: those days end then. `schedule tue wed default`: back to `WORKDAY_END`.
- Table `schedule_work_ends` (migration 095), written only by `service.py`; event `schedule_settings.work_end_changed`.
- `work_ends_for(db_path)` is one shared in-memory map per database, loaded once, updated by the command: a change applies at once, without
  a restart, everywhere a `WindowConfig` (`schedule_windows.windows.WindowConfig.end_of_work`) is used: free time, tasks, the release base.
- A time is refused when it isn't after the work start or leaves the evening too short before quiet hours (the release planner's room).
- Weekends and public holidays have no work time and cannot be set. Quiet hours and the work start stay in `.env`.
- Not built: school breaks (the owner was offered a `school off` command; not asked for yet).
