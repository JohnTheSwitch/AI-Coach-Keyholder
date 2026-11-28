"""Owner, 2026-11-25: per-day end of the school day (`schedule`)."""

from datetime import date, datetime, time, timezone
from pathlib import Path

from infrastructure.database import Database
from schedule_settings.service import ScheduleSettings, work_ends_for
from schedule_windows.windows import WindowConfig, WindowKind, classify, discretionary_interval

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)


def _core(tmp_path):
    core = Database(tmp_path / "d.db")
    with core.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.commit()
    return core


def _cfg(ends=None):
    return WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00", work_start="07:00", work_end="16:30",
                              work_ends=ends)


def test_he_sets_his_days_and_the_windows_follow_at_once(tmp_path) -> None:
    core = _core(tmp_path)
    s = ScheduleSettings(core, db_path=tmp_path / "d.db", user_id="u", default_end=time(16, 30), valid=lambda end: None)
    assert s.command("other", "schedule", NOW) is None and s.command("u", "hello", NOW) is None
    assert "16:30 (default)" in s.command("u", "schedule", NOW)
    assert s.command("u", "schedule mon thu fri 15:00", NOW).startswith("Done.")
    assert s.command("u", "schedule tue wed 5pm", NOW).startswith("Done.")
    cfg = _cfg(work_ends_for(tmp_path / "d.db"))
    monday, tuesday = date(2026, 11, 23), date(2026, 11, 24)
    assert cfg.end_of_work(monday) == time(15, 0) and cfg.end_of_work(tuesday) == time(17, 0)
    assert classify(cfg, datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)) is WindowKind.DISCRETIONARY      # Mon 15:30 Prague
    assert classify(cfg, datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc)) is WindowKind.WORK               # Tue 16:30 Prague
    assert discretionary_interval(cfg, tuesday)[0] == datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)
    assert s.command("u", "schedule tue default", NOW).startswith("Done.") and cfg.end_of_work(tuesday) == time(16, 30)
    with core.transaction() as tx:
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM schedule_work_ends")["n"] == 4
        assert tx.fetch_one("SELECT COUNT(*) AS n FROM domain_events WHERE event_type = 'schedule_settings.work_end_changed'")["n"] == 6


def test_bad_input_and_a_refused_time_change_nothing(tmp_path) -> None:
    core = _core(tmp_path)
    s = ScheduleSettings(core, db_path=tmp_path / "d.db", user_id="u", default_end=time(16, 30),
                         valid=lambda end: "too late" if end >= time(20, 0) else None)
    assert "don't know the day" in s.command("u", "schedule sat 15:00", NOW)
    assert "couldn't read the time" in s.command("u", "schedule mon soon", NOW)
    assert s.command("u", "schedule mon 21:00", NOW) == "too late"
    assert work_ends_for(tmp_path / "d.db").all() == {}


def test_without_per_day_ends_nothing_changes() -> None:
    cfg = _cfg()
    assert cfg.end_of_work(date(2026, 11, 23)) == time(16, 30)


def test_the_seed_fills_only_days_he_has_not_set(tmp_path) -> None:
    core = _core(tmp_path)
    s = ScheduleSettings(core, db_path=tmp_path / "d.db", user_id="u", default_end=time(16, 30), valid=lambda end: None)
    s.command("u", "schedule mon 16:00", NOW)
    done = s.seed({0: time(15, 0), 3: time(15, 0), 4: time(15, 0), 1: time(17, 0), 2: time(17, 0)}, NOW)
    assert sorted(done) == [1, 2, 3, 4]
    assert s.ends.all() == {0: time(16, 0), 1: time(17, 0), 2: time(17, 0), 3: time(15, 0), 4: time(15, 0)}
    assert s.seed({0: time(15, 0)}, NOW) == []


def test_while_locked_an_earlier_end_waits_24_hours_and_a_later_one_is_at_once(tmp_path) -> None:
    """Owner, 2026-11-25: "Yes, make it wait 24 hours" (more free time = an earlier possible release)."""
    from datetime import timedelta
    from opt_outs.service import OptOuts
    core = _core(tmp_path)
    s = ScheduleSettings(core, db_path=tmp_path / "d.db", user_id="u", default_end=time(16, 30), valid=lambda end: None)
    s.seed({0: time(15, 0), 1: time(17, 0)}, NOW)
    opt = OptOuts(core, locked=lambda user: True, local_clock=lambda t: t.strftime("%H:%M"))
    opt.register_schedule(s.apply_change, label=s.describe)
    s.request_change = opt.request_off
    reply = s.command("u", "schedule mon tue 14:00", NOW)
    assert "isn't done at once" in reply and "Monday and Tuesday's school day to end at 14:00" in reply
    assert s.ends.get(0) == time(15, 0) and s.ends.get(1) == time(17, 0)          # nothing yet
    assert s.command("u", "schedule mon 16:00", NOW).startswith("Done.") and s.ends.get(0) == time(16, 0)   # later: at once
    opt.tick(NOW + timedelta(hours=24, minutes=1))
    with core.transaction() as tx:
        row = tx.fetch_one("SELECT id, status FROM opt_out_requests WHERE switch LIKE 'schedule:%'")
    assert row["status"] == "asked"
    assert opt.press(user_id="u", row_id=row["id"], action="yes", now=NOW + timedelta(hours=25)).startswith("Done.")
    assert s.ends.get(0) == time(14, 0) and s.ends.get(1) == time(14, 0)


def test_unlocked_an_earlier_end_is_at_once(tmp_path) -> None:
    from opt_outs.service import OptOuts
    core = _core(tmp_path)
    s = ScheduleSettings(core, db_path=tmp_path / "d.db", user_id="u", default_end=time(16, 30), valid=lambda end: None)
    opt = OptOuts(core, locked=lambda user: False, local_clock=lambda t: t.strftime("%H:%M"))
    opt.register_schedule(s.apply_change, label=s.describe)
    s.request_change = opt.request_off
    assert s.command("u", "schedule fri 14:00", NOW).startswith("Done.") and s.ends.get(4) == time(14, 0)
