"""Pure tests for schedule_windows.windows (v0.39). All local times are Europe/Prague."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import schedule_windows.windows as W
from schedule_windows.windows import WindowConfig, WindowKind as K, classify, discretionary_interval

PRAGUE = ZoneInfo("Europe/Prague")


def cfg(**kw) -> WindowConfig:
    base = dict(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00", work_start="07:00",
                work_end="16:30", holiday_country="CZ")
    base.update(kw)
    return WindowConfig.build(**base)


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    """A local Prague wall-clock time as an aware UTC instant."""
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=PRAGUE).astimezone(timezone.utc)


MON = date(2026, 10, 12)          # ordinary Monday (CEST)
SAT = date(2026, 10, 17)
HOLIDAY_WED = date(2026, 10, 28)  # Czech statehood day, a Wednesday
EASTER_MON = date(2026, 4, 6)
GOOD_FRIDAY = date(2026, 4, 3)


class TestWorkday:
    @pytest.mark.parametrize("h,m,s,kind", [
        (0, 0, 0, K.QUIET), (6, 59, 59, K.QUIET), (7, 0, 0, K.WORK), (12, 0, 0, K.WORK),
        (16, 29, 59, K.WORK), (16, 30, 0, K.DISCRETIONARY), (19, 0, 0, K.DISCRETIONARY),
        (21, 59, 59, K.DISCRETIONARY), (22, 0, 0, K.QUIET), (23, 59, 59, K.QUIET),
    ])
    def test_boundaries_to_the_second(self, h, m, s, kind) -> None:
        assert classify(cfg(), at(MON, h, m, s)) is kind

    def test_precedence_quiet_beats_work_and_discretionary(self) -> None:
        # a (contrived) layout where work hours would overlap quiet is rejected outright
        with pytest.raises(ValueError):
            cfg(work_end="22:30")


class TestWeekendAndHolidays:
    @pytest.mark.parametrize("d", [SAT, date(2026, 10, 18)])
    @pytest.mark.parametrize("h,m,kind", [
        (6, 59, K.QUIET), (7, 0, K.DISCRETIONARY), (10, 0, K.DISCRETIONARY), (16, 29, K.DISCRETIONARY),
        (21, 59, K.DISCRETIONARY), (22, 0, K.QUIET),
    ])
    def test_weekend_is_discretionary_outside_quiet(self, d, h, m, kind) -> None:
        assert classify(cfg(), at(d, h, m)) is kind

    @pytest.mark.parametrize("d", [HOLIDAY_WED, EASTER_MON, GOOD_FRIDAY, date(2026, 5, 1), date(2026, 5, 8),
                                    date(2026, 12, 24), date(2026, 12, 25), date(2026, 12, 26), date(2026, 1, 1)])
    def test_czech_public_holidays_behave_like_weekends(self, d) -> None:
        assert classify(cfg(), at(d, 10, 0)) is K.DISCRETIONARY
        assert classify(cfg(), at(d, 6, 59)) is K.QUIET
        assert classify(cfg(), at(d, 22, 0)) is K.QUIET

    def test_day_after_a_holiday_is_a_normal_workday(self) -> None:
        assert classify(cfg(), at(date(2026, 10, 29), 10, 0)) is K.WORK

    def test_no_holiday_country_means_no_holidays(self) -> None:
        assert classify(cfg(holiday_country=None), at(HOLIDAY_WED, 10, 0)) is K.WORK

    def test_holiday_calendar_failure_never_frees_a_workday(self, monkeypatch) -> None:
        def boom(country, year):
            raise RuntimeError("calendar unavailable")
        monkeypatch.setattr(W, "_holidays_for", boom)
        assert classify(cfg(), at(HOLIDAY_WED, 10, 0)) is K.WORK           # stricter, not looser
        assert classify(cfg(), at(SAT, 10, 0)) is K.DISCRETIONARY           # weekends are certain


class TestDaylightSaving:
    def test_spring_forward_quiet_hours_are_never_shortened_by_local_clock(self) -> None:
        # 2026-03-29: 02:00 -> 03:00. Sat 28th 21:30Z = 22:30 CET (quiet) ... Sun 29th 05:00Z = 07:00 CEST
        assert classify(cfg(), datetime(2026, 3, 28, 21, 30, tzinfo=timezone.utc)) is K.QUIET
        assert classify(cfg(), datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)) is K.QUIET   # 01:59 CET
        assert classify(cfg(), datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)) is K.QUIET    # 03:00 CEST
        assert classify(cfg(), datetime(2026, 3, 29, 4, 59, tzinfo=timezone.utc)) is K.QUIET   # 06:59 CEST
        assert classify(cfg(), datetime(2026, 3, 29, 5, 0, tzinfo=timezone.utc)) is K.DISCRETIONARY   # 07:00 CEST, Sunday

    def test_fall_back_repeated_hour_is_inside_quiet(self) -> None:
        # 2026-10-25: 03:00 CEST -> 02:00 CET. Both 02:30s are quiet.
        assert classify(cfg(), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)) is K.QUIET   # 02:30 CEST
        assert classify(cfg(), datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)) is K.QUIET   # 02:30 CET
        assert classify(cfg(), datetime(2026, 10, 25, 5, 59, tzinfo=timezone.utc)) is K.QUIET   # 06:59 CET
        assert classify(cfg(), datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)) is K.DISCRETIONARY  # 07:00 CET, Sunday

    def test_interval_boundaries_follow_local_time_across_the_change(self) -> None:
        s, e = discretionary_interval(cfg(), date(2026, 3, 30))       # Monday after spring forward (CEST)
        assert (s, e) == (datetime(2026, 3, 30, 14, 30, tzinfo=timezone.utc), datetime(2026, 3, 30, 20, 0, tzinfo=timezone.utc))
        s, e = discretionary_interval(cfg(), date(2026, 10, 25))      # Sunday of the fall-back (all-day, CET evening)
        assert (s, e) == (datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc), datetime(2026, 10, 25, 21, 0, tzinfo=timezone.utc))


class TestIntervalsAndInputs:
    def test_workday_interval_is_the_evening(self) -> None:
        s, e = discretionary_interval(cfg(), MON)
        assert s == at(MON, 16, 30) and e == at(MON, 22, 0)

    def test_weekend_interval_is_the_whole_non_quiet_day(self) -> None:
        s, e = discretionary_interval(cfg(), SAT)
        assert s == at(SAT, 7, 0) and e == at(SAT, 22, 0)

    def test_naive_instants_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            classify(cfg(), datetime(2026, 10, 12, 10, 0))

    def test_any_aware_zone_is_accepted(self) -> None:
        tokyo = at(MON, 10, 0).astimezone(ZoneInfo("Asia/Tokyo"))
        assert classify(cfg(), tokyo) is K.WORK


class TestConfigValidation:
    @pytest.mark.parametrize("kw", [
        dict(timezone_name="Not/AZone"), dict(quiet_start="25:00"), dict(quiet_end="7"), dict(work_start="abc"),
        dict(quiet_start="06:00", quiet_end="07:00"),          # does not cross midnight
        dict(work_start="06:00"),                              # work starts inside quiet hours
        dict(work_start="17:00", work_end="16:30"),            # end before start
        dict(work_end="22:00"),                                # work reaches quiet start
    ])
    def test_invalid_layouts_fail_fast(self, kw) -> None:
        with pytest.raises(ValueError):
            cfg(**kw)

    def test_a_later_work_start_leaves_the_morning_non_discretionary(self) -> None:
        c = cfg(work_start="08:00")
        assert classify(c, at(MON, 7, 30)) is K.WORK
