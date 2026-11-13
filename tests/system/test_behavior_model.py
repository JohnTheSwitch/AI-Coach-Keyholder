"""v0.39 section 11.8: pure behavioural math (token pool, clean-day streak, penalty duration, violations catalog)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from penalty_engine import violations_catalog as cat
from penalty_engine.duration import (
    HARD_CAP_HOURS, INITIAL_PW_MAX_CAP_HOURS, SEVERITY_RANGES_DAYS, Mode, ViolationSeverity as S, initial_window_hours,
)
from release_engine.token_pool import apply_day, daily_progress
from trust_manager.clean_streak import advance, is_clean_day, recovery_for_streak_day

D = Decimal


class TestTokenPool:
    def test_perfect_week_yields_two_tokens(self) -> None:
        pool, earned = D(0), 0
        for _ in range(7):
            r = apply_day(pool, 4, 4, 1.0)
            pool, earned = r.pool_after, earned + r.tokens_earned
        assert earned == 2 and pool == D("0.002")

    def test_weighting_is_75_25(self) -> None:
        assert daily_progress(0, 4, 1.0) == D("0.0715")
        assert daily_progress(4, 4, 0.0) == D("0.2145")
        assert daily_progress(2, 4, 0.5) == ((D("0.375") + D("0.125")) * D("0.286")).quantize(D("0.000001"))

    def test_a_token_is_paid_when_the_pool_reaches_one_and_one_is_subtracted(self) -> None:
        r = apply_day(D("0.9"), 4, 4, 1.0)
        assert r.tokens_earned == 1 and r.pool_after == D("0.186")
        assert apply_day(D("0.714"), 4, 4, 1.0).tokens_earned == 1          # exactly 1.000 pays out
        assert apply_day(D("0.713"), 4, 4, 1.0).tokens_earned == 0

    def test_no_float_drift(self) -> None:
        pool = D(0)
        for _ in range(1000):
            pool = apply_day(pool, 1, 1, 1.0).pool_after
        assert pool == D("0.000") + (D("0.286") * 1000 % 1)

    def test_a_goalless_day_counts_as_a_full_goal_ratio(self) -> None:
        assert daily_progress(0, 0, 1.0) == D("0.286")
        assert daily_progress(0, 0, 0.0) == D("0.2145")
        assert daily_progress(0, 0, 0.5) == daily_progress(4, 4, 0.5)

    @pytest.mark.parametrize("args", [(1, 0, 0.5), (5, 4, 0.5), (-1, 4, 0.5), (1, 4, 1.5), (1, 4, -0.1)])
    def test_invalid_inputs_are_refused(self, args) -> None:
        with pytest.raises(ValueError):
            daily_progress(*args)

    def test_a_pool_is_never_negative_and_the_threshold_positive(self) -> None:
        # Phase F4: the threshold can move (base(mode) * factor), so a stored pool at or above a lowered threshold simply pays out.
        with pytest.raises(ValueError):
            apply_day(-0.1, 1, 1, 1.0)
        with pytest.raises(ValueError):
            apply_day(0, 1, 1, 1.0, threshold=0)
        assert apply_day(1, 1, 1, 1.0).tokens_earned == 1


class TestCleanStreak:
    @pytest.mark.parametrize("day,rate", [(1, "0.008"), (3, "0.008"), (4, "0.015"), (7, "0.015"), (8, "0.025"), (30, "0.025")])
    def test_recovery_rates(self, day, rate) -> None:
        assert recovery_for_streak_day(day) == D(rate)

    def test_clean_day_needs_all_three_conditions(self) -> None:
        ok = dict(violations=0, goals_assigned=3, goals_met=3, check_ins_due=2, check_ins_ok=2)
        assert is_clean_day(**ok)
        assert not is_clean_day(**{**ok, "violations": 1})
        assert not is_clean_day(**{**ok, "goals_met": 2})
        assert not is_clean_day(**{**ok, "check_ins_ok": 1})

    def test_streak_grows_and_recovery_accelerates(self) -> None:
        streak, trust, gains = 0, D("0.50"), []
        for _ in range(9):
            r = advance(streak, trust, clean=True)
            streak, trust = r.streak, r.trust
            gains.append(r.delta)
        assert gains == [D("0.008")] * 3 + [D("0.015")] * 4 + [D("0.025")] * 2 and streak == 9

    def test_a_dirty_day_resets_the_streak(self) -> None:
        r = advance(10, D("0.60"), clean=False)
        assert r.streak == 0 and r.trust == D("0.60") and r.delta == 0

    def test_trust_never_exceeds_one(self) -> None:
        r = advance(20, D("0.99"), clean=True)
        assert r.trust == D("1.00") and r.delta == D("0.01")
        assert advance(20, D("1.00"), clean=True).delta == 0


class TestPenaltyDuration:
    def test_formula_endpoints_and_midpoint(self) -> None:
        assert initial_window_hours(S.MODERATE, Mode.STANDARD, 0.0) == 5 * 24
        assert initial_window_hours(S.MODERATE, Mode.STANDARD, 1.0) == 3 * 24
        assert initial_window_hours(S.MODERATE, Mode.STANDARD, 0.5) == 4 * 24
        assert initial_window_hours(S.MODERATE, Mode.ADVANCED, 0.5) == int(7.5 * 24)

    def test_higher_trust_means_a_shorter_window(self) -> None:
        values = [initial_window_hours(S.SEVERE, Mode.ADVANCED, t / 10) for t in range(11)]
        assert values == sorted(values, reverse=True) and values[0] == 360 and values[-1] == 192

    def test_first_minor_offense_is_a_warning_only(self) -> None:
        assert initial_window_hours(S.MINOR, Mode.STANDARD, 0.0) == 0
        assert initial_window_hours(S.MINOR, Mode.STANDARD, 0.0, prior_offenses=1) == 72
        assert initial_window_hours(S.MINOR, Mode.ADVANCED, 1.0, prior_offenses=2) == 48

    def test_a_new_window_never_exceeds_half_the_mode_limit(self) -> None:
        assert INITIAL_PW_MAX_CAP_HOURS == {Mode.STANDARD: 168, Mode.ADVANCED: 360}
        assert HARD_CAP_HOURS == {Mode.STANDARD: 336, Mode.ADVANCED: 720}
        for (mode, sev), (lo, hi) in SEVERITY_RANGES_DAYS.items():
            assert hi * 24 <= INITIAL_PW_MAX_CAP_HOURS[mode]
            for t in (0.0, 0.3, 1.0):
                assert initial_window_hours(sev, mode, t, prior_offenses=1) <= INITIAL_PW_MAX_CAP_HOURS[mode]

    def test_rounding_to_whole_hours(self) -> None:
        h = initial_window_hours(S.MODERATE, Mode.STANDARD, 0.337)
        assert isinstance(h, int) and h == round((5 - 0.337 * 2) * 24)

    @pytest.mark.parametrize("trust,prior", [(-0.1, 0), (1.1, 0), (0.5, -1), (0.5, True)])
    def test_invalid_inputs(self, trust, prior) -> None:
        with pytest.raises(ValueError):
            initial_window_hours(S.MODERATE, Mode.STANDARD, trust, prior_offenses=prior)


class TestCatalog:
    def test_codes_are_unique_and_domains_known(self) -> None:
        codes = [v.code for v in cat.CATALOG]
        assert len(codes) == len(set(codes)) and {v.domain for v in cat.CATALOG} == {"hygiene", "supervision", "integrity"}

    def test_owner_specified_drops_and_severities(self) -> None:
        assert (cat.get("CHECKIN_MISSED_FIRST").trust_drop, cat.get("CHECKIN_INSUFFICIENT").trust_drop) == (D("0.02"), D("0.01"))
        assert cat.get("CHECKIN_REPEATED_NONCOMPLIANCE").severity is S.MINOR
        assert cat.get("CHECKIN_REPEATED_NONCOMPLIANCE").trust_drop == D("0.06")
        assert cat.get("EPISTEMIC_MISREPRESENTATION").severity is S.SEVERE

    def test_unspecified_drops_use_the_severity_defaults(self) -> None:
        assert cat.DEFAULT_TRUST_DROP == {S.MINOR: D("0.02"), S.MODERATE: D("0.05"), S.SEVERE: D("0.10")}
        assert cat.get("CHASTER_SETTINGS_TAMPERED").trust_drop == D("0.05")
        assert cat.get("EPISTEMIC_MISREPRESENTATION").trust_drop == D("0.10")
        assert cat.get("HYGIENE_MISUSE").trust_drop == D("0.10")

    def test_the_only_hygiene_infraction_is_discretionary_misuse(self) -> None:
        assert [v.code for v in cat.by_domain("hygiene")] == ["HYGIENE_MISUSE"]
        assert "iscretionary" in cat.get("HYGIENE_MISUSE").description

    def test_emergency_and_mandatory_access_can_never_be_an_infraction(self) -> None:
        for v in cat.CATALOG:
            text = f"{v.code} {v.description}".lower()
            assert "emergency" not in text and "mandatory" not in text and "health" not in text

    def test_goal_failures_are_not_violations(self) -> None:
        assert not any("GOAL" in v.code for v in cat.CATALOG)

    def test_only_existing_modules_are_marked_detectable(self) -> None:
        assert {v.code for v in cat.CATALOG if v.detectable} == {
            "CHECKIN_MISSED_FIRST", "CHECKIN_INSUFFICIENT", "CHECKIN_REPEATED_NONCOMPLIANCE", "HYGIENE_MISUSE", "EPISTEMIC_MISREPRESENTATION", "DETECTION_UNANSWERED",
            "EXEMPTION_OVERRIDE"}
