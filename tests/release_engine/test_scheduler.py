"""Pure tests for release_engine.scheduler (adaptive discretionary target)."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from release_engine.scheduler import MIN_LEAD, draw_offset_minutes, plan_target, validate_offsets
from schedule_windows.windows import WindowConfig, WindowKind, classify

PRAGUE = ZoneInfo("Europe/Prague")
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
SEED = bytes(range(32))


def local(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=PRAGUE).astimezone(timezone.utc)


class TestOffsets:
    @pytest.mark.parametrize("lo,hi", [(-1, 5), (10, 5), (0, 450), (30, 330), (1.5, 5), (True, 5), ("1", 5)])
    def test_invalid_offsets_fail_fast(self, lo, hi) -> None:
        with pytest.raises(ValueError):
            validate_offsets(CFG, lo, hi)

    def test_defaults_are_valid(self) -> None:
        validate_offsets(CFG, 30, 210)

    def test_draw_is_deterministic_and_in_range(self) -> None:
        d = date(2026, 10, 12)
        assert draw_offset_minutes(SEED, d, 30, 210) == draw_offset_minutes(SEED, d, 30, 210)
        for i in range(400):
            assert 30 <= draw_offset_minutes(os.urandom(32), d + timedelta(days=i), 30, 210) <= 210

    def test_draw_varies_with_date_and_seed(self) -> None:
        by_date = {draw_offset_minutes(SEED, date(2026, 10, 12) + timedelta(days=i), 30, 210) for i in range(60)}
        by_seed = {draw_offset_minutes(os.urandom(32), date(2026, 10, 12), 30, 210) for _ in range(60)}
        assert len(by_date) > 20 and len(by_seed) > 20

    def test_a_degenerate_range_is_exact(self) -> None:
        assert draw_offset_minutes(SEED, date(2026, 10, 12), 45, 45) == 45

    def test_short_seed_rejected(self) -> None:
        with pytest.raises(ValueError):
            draw_offset_minutes(b"short", date(2026, 10, 12), 30, 210)


class TestPlanTarget:
    def test_met_in_the_morning_targets_that_evening(self) -> None:
        now = local(date(2026, 10, 12), 10, 0)
        t = plan_target(now, SEED, CFG, 30, 210)
        lt = t.astimezone(PRAGUE)
        assert lt.date() == date(2026, 10, 12) and 17 <= lt.hour < 20 or (lt.hour == 20 and lt.minute == 0)

    def test_met_after_todays_window_targets_a_later_day(self) -> None:
        now = local(date(2026, 10, 12), 21, 0)
        t = plan_target(now, SEED, CFG, 30, 210)
        assert t.astimezone(PRAGUE).date() > date(2026, 10, 12)

    def test_met_during_quiet_hours(self) -> None:
        now = local(date(2026, 10, 13), 3, 0)
        t = plan_target(now, SEED, CFG, 30, 210)
        assert t > now

    def test_weekend_and_holiday_use_the_same_base(self) -> None:
        for d in (date(2026, 10, 17), date(2026, 10, 28)):
            t = plan_target(local(d, 8, 0), SEED, CFG, 30, 210)
            lt = t.astimezone(PRAGUE)
            assert lt.date() == d and (17, 0) <= (lt.hour, lt.minute) <= (20, 0)

    def test_property_target_is_always_valid(self) -> None:
        start = local(date(2026, 3, 20), 0, 0)         # spans the spring DST change
        for i in range(0, 24 * 60 * 20, 37):
            now = start + timedelta(minutes=i)
            for seed in (SEED, bytes(32 * [7]), bytes(32 * [250])):
                t = plan_target(now, seed, CFG, 30, 210)
                assert t is not None and t > now + MIN_LEAD
                assert classify(CFG, t) is WindowKind.DISCRETIONARY
                lt = t.astimezone(PRAGUE)
                assert (17, 0) <= (lt.hour, lt.minute) <= (20, 0)

    def test_same_seed_same_day_is_reproducible_and_different_seed_differs(self) -> None:
        now = local(date(2026, 10, 12), 9, 0)
        assert plan_target(now, SEED, CFG, 30, 210) == plan_target(now, SEED, CFG, 30, 210)
        targets = {plan_target(now, os.urandom(32), CFG, 30, 210) for _ in range(40)}
        assert len(targets) > 10

    def test_not_in_the_past_and_not_inside_the_lead_time(self) -> None:
        d = date(2026, 10, 12)
        t0 = plan_target(local(d, 10, 0), SEED, CFG, 30, 210)
        just_before = t0 - timedelta(seconds=30)               # inside MIN_LEAD of today's draw
        t1 = plan_target(just_before, SEED, CFG, 30, 210)
        assert t1 > just_before + MIN_LEAD and t1 != t0
