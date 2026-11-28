"""Weekend quiet ends (owner, 2026-11-28)."""


def test_weekend_mornings_are_quiet_until_his_wake_time(tmp_path) -> None:
    """Owner, 2026-11-28: "On weekends I wake up usually at 10 am" -> quiet until 10:00 on Saturday and Sunday."""
    from datetime import date, datetime, time, timezone
    from schedule_settings.service import ScheduleSettings
    from schedule_windows.windows import WindowConfig, WindowKind, classify, discretionary_interval
    from tests.lines.test_lines import core

    db = core(tmp_path)
    s = ScheduleSettings(db, db_path=db.db_path, user_id="u", default_end=time(17, 0), valid=lambda t: None)
    now = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)
    assert s.seed_quiet({5: time(10, 0), 6: time(10, 0)}, now) == [5, 6]
    assert s.seed_quiet({5: time(9, 0)}, now) == []                                   # his own setting is never overwritten
    assert "quiet until Saturday 10:00 / Sunday 10:00" in s.listing()
    cfg = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="06:00", work_start="08:00", work_end="17:00",
                             quiet_ends=s.quiet)
    sat, mon = date(2026, 11, 28), date(2026, 11, 30)
    assert classify(cfg, cfg.to_utc(sat, time(9, 30))) is WindowKind.QUIET
    assert classify(cfg, cfg.to_utc(sat, time(10, 0))) is WindowKind.DISCRETIONARY
    assert classify(cfg, cfg.to_utc(mon, time(6, 30))) is WindowKind.WORK              # weekdays unchanged
    assert discretionary_interval(cfg, sat)[0] == cfg.to_utc(sat, time(10, 0))
