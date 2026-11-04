"""Pure tests for rules_engine.engine / limits / ruleset (Phase 2, dry run)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rules_engine.engine import evaluate
from rules_engine.limits import apply_limits
from rules_engine.models import (
    ActionType as A,
    EntitlementClass as E,
    GoalDirection as D,
    LockSnapshot,
    RecentAction,
    Rule,
    RuleInputs,
    Ruleset,
)
from rules_engine.ruleset import DEFAULT_RULESET, RulesetValidationError, validate_ruleset

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def lock(**kw) -> LockSnapshot:
    base = dict(observed_at=NOW - timedelta(minutes=1), status="locked", is_frozen=False,
                can_be_unlocked=False, is_keyholder_lock=True)
    base.update(kw)
    return LockSnapshot(**base)


def inputs(**kw) -> RuleInputs:
    base = dict(now=NOW, trust_domain="d", trust_score=0.5, trust_confidence=0.8, previous_trust_score=None,
                direction=D.SETBACK, setback_streak=1, progress_streak=0, lock=lock(), recent_actions=())
    base.update(kw)
    return RuleInputs(**base)


def ra(minutes_ago: int, action=A.UNFREEZE, hours=0) -> RecentAction:
    return RecentAction(created_at=NOW - timedelta(minutes=minutes_ago), action_type=action, hours=hours)


# The shipped DEFAULT_RULESET is empty and a time action no longer exists (owner decision, Zero-Time-Modification). The pure engine still
# evaluates any valid ruleset, so its ordering, gates and limits are exercised with this test-only ruleset. It is NOT what runs in production.
ENGINE_TEST_RULESET = validate_ruleset(Ruleset(version="1", rules=(
    Rule(id="setback_streak", description="Repeated missed goals", action=A.FREEZE, direction=D.SETBACK, min_setback_streak=2),
    Rule(id="trust_fell_below_floor", description="Trust fell below the floor", action=A.FREEZE, crossed_below=0.3, min_confidence=0.3),
    Rule(id="setback_low_trust", description="Missed goal while trust is low", action=A.FREEZE, direction=D.SETBACK, trust_below=0.4,
         min_confidence=0.3),
    Rule(id="setback", description="Missed goal", action=A.FREEZE, direction=D.SETBACK),
    Rule(id="trust_rose_above_ceiling", description="Trust rose above the ceiling", action=A.UNFREEZE, crossed_above=0.7, min_confidence=0.3),
    Rule(id="progress_high_trust", description="Goal met while trust is high", action=A.UNFREEZE, direction=D.PROGRESS, trust_at_least=0.7,
         min_confidence=0.3),
)))


class TestZeroTime:
    def test_the_shipped_ruleset_is_empty(self) -> None:
        assert DEFAULT_RULESET.rules == ()

    def test_no_time_action_exists(self) -> None:
        assert {a.value for a in A} == {"no_action", "freeze", "unfreeze"}

    @pytest.mark.parametrize("kw", [dict(), dict(setback_streak=5), dict(trust_score=0.2), dict(previous_trust_score=0.35, trust_score=0.25),
                                    dict(direction=D.PROGRESS, trust_score=0.9), dict(direction=D.NEUTRAL, previous_trust_score=0.6, trust_score=0.8)])
    def test_goals_and_trust_never_touch_the_lock(self, kw) -> None:
        assert evaluate(inputs(**kw), DEFAULT_RULESET).action_type == A.NO_ACTION

    @pytest.mark.parametrize("name", ["MAX_ADDED_HOURS_PER_WINDOW", "MAX_REMOVED_HOURS_PER_WINDOW", "MAX_SINGLE_ACTION_HOURS", "HOURS_UNIT"])
    def test_no_time_limit_is_left_to_tune(self, name) -> None:
        import rules_engine.limits as limits
        assert not hasattr(limits, name)


class TestSafetyGates:
    @pytest.mark.parametrize("kw,text", [
        (dict(lock=None), "no lock data"),
        (dict(lock=lock(observed_at=NOW - timedelta(minutes=16))), "too old"),
        (dict(lock=lock(observed_at=NOW + timedelta(minutes=1))), "invalid"),
        (dict(lock=lock(status="unlocked")), "not in a locked state"),
        (dict(lock=lock(status="deserted")), "not in a locked state"),
        (dict(lock=lock(status=None)), "not in a locked state"),
        (dict(lock=lock(is_keyholder_lock=False)), "keyholder"),
        (dict(lock=lock(is_keyholder_lock=None)), "keyholder"),
        (dict(lock=lock(is_frozen=None)), "frozen state is unknown"),
        (dict(trust_score=None), "trust data"),
    ])
    def test_fail_closed(self, kw, text) -> None:
        out = evaluate(inputs(**kw), ENGINE_TEST_RULESET)
        assert out.action_type == A.NO_ACTION and out.entitlement_class == E.ABSOLUTE
        assert text in out.reason

    def test_data_exactly_15_minutes_old_is_still_fresh(self) -> None:
        out = evaluate(inputs(lock=lock(observed_at=NOW - timedelta(minutes=15))), ENGINE_TEST_RULESET)
        assert out.action_type == A.FREEZE


class TestRules:
    def test_a_matching_rule_decides(self) -> None:
        out = evaluate(inputs(), ENGINE_TEST_RULESET)
        assert (out.action_type, out.hours, out.rule_id, out.entitlement_class) == (A.FREEZE, 0, "setback", E.GUARANTEED)

    def test_streak_outranks_plain_setback(self) -> None:
        out = evaluate(inputs(setback_streak=2), ENGINE_TEST_RULESET)
        assert out.rule_id == "setback_streak" and out.hours == 0

    def test_low_trust_setback(self) -> None:
        out = evaluate(inputs(trust_score=0.2), ENGINE_TEST_RULESET)
        assert out.rule_id == "setback_low_trust"

    def test_low_confidence_blocks_confidence_gated_rules(self) -> None:
        out = evaluate(inputs(trust_score=0.2, trust_confidence=0.1), ENGINE_TEST_RULESET)
        assert out.rule_id == "setback"

    def test_progress_with_high_trust_matches_its_rule(self) -> None:
        out = evaluate(inputs(direction=D.PROGRESS, trust_score=0.8, lock=lock(is_frozen=True)), ENGINE_TEST_RULESET)
        assert (out.action_type, out.hours, out.rule_id) == (A.UNFREEZE, 0, "progress_high_trust")

    def test_progress_with_ordinary_trust_does_nothing(self) -> None:
        out = evaluate(inputs(direction=D.PROGRESS, trust_score=0.5), ENGINE_TEST_RULESET)
        assert out.action_type == A.NO_ACTION and out.entitlement_class == E.GUARANTEED

    def test_neutral_does_nothing(self) -> None:
        assert evaluate(inputs(direction=D.NEUTRAL), ENGINE_TEST_RULESET).action_type == A.NO_ACTION

    def test_threshold_crossing_down_and_up(self) -> None:
        down = evaluate(inputs(direction=D.NEUTRAL, trust_score=0.25, previous_trust_score=0.35), ENGINE_TEST_RULESET)
        assert down.rule_id == "trust_fell_below_floor" and down.action_type == A.FREEZE
        up = evaluate(inputs(direction=D.NEUTRAL, trust_score=0.72, previous_trust_score=0.68, lock=lock(is_frozen=True)), ENGINE_TEST_RULESET)
        assert up.rule_id == "trust_rose_above_ceiling" and up.action_type == A.UNFREEZE

    def test_crossing_needs_a_known_previous_score(self) -> None:
        out = evaluate(inputs(direction=D.NEUTRAL, trust_score=0.25, previous_trust_score=None), ENGINE_TEST_RULESET)
        assert out.action_type == A.NO_ACTION

    def test_every_outcome_has_a_reason(self) -> None:
        for kw in (dict(), dict(lock=None), dict(direction=D.NEUTRAL), dict(setback_streak=3)):
            assert evaluate(inputs(**kw), ENGINE_TEST_RULESET).reason.strip()

    def test_deterministic(self) -> None:
        assert evaluate(inputs(), ENGINE_TEST_RULESET) == evaluate(inputs(), ENGINE_TEST_RULESET)


class TestFreezeDormantButValid:
    RULES = Ruleset(version="t", rules=(
        Rule(id="f", description="freeze", action=A.FREEZE, direction=D.SETBACK),
        Rule(id="u", description="unfreeze", action=A.UNFREEZE, direction=D.PROGRESS),
    ))

    def test_freeze_on_unfrozen_lock(self) -> None:
        out = evaluate(inputs(), self.RULES)
        assert out.action_type == A.FREEZE and out.hours == 0

    def test_freeze_on_frozen_lock_is_refused(self) -> None:
        out = evaluate(inputs(lock=lock(is_frozen=True)), self.RULES)
        assert out.action_type == A.NO_ACTION and "already frozen" in out.reason

    def test_unfreeze_on_frozen_lock(self) -> None:
        out = evaluate(inputs(direction=D.PROGRESS, lock=lock(is_frozen=True)), self.RULES)
        assert out.action_type == A.UNFREEZE

    def test_unfreeze_on_unfrozen_lock_is_refused(self) -> None:
        out = evaluate(inputs(direction=D.PROGRESS), self.RULES)
        assert out.action_type == A.NO_ACTION and "not frozen" in out.reason


class TestLimits:
    def test_no_history_allows_a_release(self) -> None:
        assert apply_limits(A.UNFREEZE, 0, (), NOW) == (0, None)

    def test_max_two_releases_per_24h(self) -> None:
        _, why = apply_limits(A.UNFREEZE, 0, (ra(600), ra(300)), NOW)
        assert "action limit" in why

    def test_releases_older_than_24h_do_not_count(self) -> None:
        assert apply_limits(A.UNFREEZE, 0, (ra(24 * 60 + 1), ra(24 * 60 + 2)), NOW) == (0, None)

    def test_cooldown_of_4_hours(self) -> None:
        assert "cooldown" in apply_limits(A.UNFREEZE, 0, (ra(239),), NOW)[1]
        assert apply_limits(A.UNFREEZE, 0, (ra(240),), NOW) == (0, None)

    def test_freeze_is_never_limited_but_unfreeze_is(self) -> None:
        assert apply_limits(A.FREEZE, 0, (ra(100),), NOW) == (0, None)
        assert apply_limits(A.FREEZE, 0, (), NOW) == (0, None)
        assert apply_limits(A.UNFREEZE, 0, (ra(100),), NOW)[1] is not None

    def test_freezes_do_not_use_the_release_budget(self) -> None:
        assert apply_limits(A.UNFREEZE, 0, (ra(10, A.FREEZE), ra(20, A.FREEZE)), NOW) == (0, None)

    def test_property_no_sequence_exceeds_the_caps(self) -> None:
        """Replay many triggers over a day: releases never exceed the cap."""
        recent: list[RecentAction] = []
        t = NOW - timedelta(hours=23)
        for step in range(200):
            now = t + timedelta(minutes=7 * step)
            if now > NOW:
                break
            if apply_limits(A.UNFREEZE, 0, tuple(recent), now)[1] is None:
                recent.append(RecentAction(created_at=now, action_type=A.UNFREEZE, hours=0))
        assert len(recent) <= 2


class TestRulesetValidation:
    def _rs(self, *rules, version="1") -> Ruleset:
        return Ruleset(version=version, rules=tuple(rules))

    def test_default_is_valid(self) -> None:
        validate_ruleset(DEFAULT_RULESET)

    @pytest.mark.parametrize("rule,version", [
        (Rule(id="a", description="x", action=A.FREEZE, hours=1, direction=D.SETBACK), "1"),        # no action carries hours
        (Rule(id="a", description="x", action=A.FREEZE, hours=1.5, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.NO_ACTION, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.FREEZE), "1"),                                      # unconditional
        (Rule(id="a", description="x", action=A.FREEZE, trust_below=1.5), "1"),
        (Rule(id="a", description="x", action=A.FREEZE, direction=D.SETBACK, min_confidence=2.0), "1"),
        (Rule(id="a", description="x", action=A.FREEZE, min_setback_streak=0), "1"),
        (Rule(id="a", description=" ", action=A.FREEZE, direction=D.SETBACK), "1"),
        (Rule(id="", description="x", action=A.FREEZE, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.FREEZE, direction=D.SETBACK), " "),
    ])
    def test_invalid_rules_are_rejected(self, rule, version) -> None:
        with pytest.raises(RulesetValidationError):
            validate_ruleset(self._rs(rule, version=version))

    def test_duplicate_ids_rejected(self) -> None:
        r = Rule(id="a", description="x", action=A.FREEZE, direction=D.SETBACK)
        with pytest.raises(RulesetValidationError):
            validate_ruleset(self._rs(r, r))

    def test_no_code_reveal_action_exists(self) -> None:
        assert {a.value for a in A} == {"no_action", "freeze", "unfreeze"}
