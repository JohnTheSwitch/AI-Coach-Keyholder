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


def ra(minutes_ago: int, action=A.ADD_TIME, hours=1) -> RecentAction:
    return RecentAction(created_at=NOW - timedelta(minutes=minutes_ago), action_type=action, hours=hours)


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
        out = evaluate(inputs(**kw), DEFAULT_RULESET)
        assert out.action_type == A.NO_ACTION and out.entitlement_class == E.ABSOLUTE
        assert text in out.reason

    def test_data_exactly_15_minutes_old_is_still_fresh(self) -> None:
        out = evaluate(inputs(lock=lock(observed_at=NOW - timedelta(minutes=15))), DEFAULT_RULESET)
        assert out.action_type == A.ADD_TIME


class TestRules:
    def test_missed_goal_adds_time(self) -> None:
        out = evaluate(inputs(), DEFAULT_RULESET)
        assert (out.action_type, out.hours, out.rule_id, out.entitlement_class) == (A.ADD_TIME, 1, "setback", E.GUARANTEED)

    def test_streak_outranks_plain_setback(self) -> None:
        out = evaluate(inputs(setback_streak=2), DEFAULT_RULESET)
        assert out.rule_id == "setback_streak" and out.hours == 2

    def test_low_trust_setback(self) -> None:
        out = evaluate(inputs(trust_score=0.2), DEFAULT_RULESET)
        assert out.rule_id == "setback_low_trust"

    def test_low_confidence_blocks_confidence_gated_rules(self) -> None:
        out = evaluate(inputs(trust_score=0.2, trust_confidence=0.1), DEFAULT_RULESET)
        assert out.rule_id == "setback"

    def test_progress_with_high_trust_removes_time(self) -> None:
        out = evaluate(inputs(direction=D.PROGRESS, trust_score=0.8), DEFAULT_RULESET)
        assert (out.action_type, out.hours, out.rule_id) == (A.REMOVE_TIME, 1, "progress_high_trust")

    def test_progress_with_ordinary_trust_does_nothing(self) -> None:
        out = evaluate(inputs(direction=D.PROGRESS, trust_score=0.5), DEFAULT_RULESET)
        assert out.action_type == A.NO_ACTION and out.entitlement_class == E.GUARANTEED

    def test_neutral_does_nothing(self) -> None:
        assert evaluate(inputs(direction=D.NEUTRAL), DEFAULT_RULESET).action_type == A.NO_ACTION

    def test_threshold_crossing_down_and_up(self) -> None:
        down = evaluate(inputs(direction=D.NEUTRAL, trust_score=0.25, previous_trust_score=0.35), DEFAULT_RULESET)
        assert down.rule_id == "trust_fell_below_floor" and down.action_type == A.ADD_TIME
        up = evaluate(inputs(direction=D.NEUTRAL, trust_score=0.72, previous_trust_score=0.68), DEFAULT_RULESET)
        assert up.rule_id == "trust_rose_above_ceiling" and up.action_type == A.REMOVE_TIME

    def test_crossing_needs_a_known_previous_score(self) -> None:
        out = evaluate(inputs(direction=D.NEUTRAL, trust_score=0.25, previous_trust_score=None), DEFAULT_RULESET)
        assert out.action_type == A.NO_ACTION

    def test_every_outcome_has_a_reason(self) -> None:
        for kw in (dict(), dict(lock=None), dict(direction=D.NEUTRAL), dict(setback_streak=3)):
            assert evaluate(inputs(**kw), DEFAULT_RULESET).reason.strip()

    def test_deterministic(self) -> None:
        assert evaluate(inputs(), DEFAULT_RULESET) == evaluate(inputs(), DEFAULT_RULESET)


class TestFreezeDormantButValid:
    RULES = Ruleset(version="t", rules=(
        Rule(id="f", description="freeze", action=A.FREEZE, direction=D.SETBACK),
        Rule(id="u", description="unfreeze", action=A.UNFREEZE, direction=D.PROGRESS),
    ))

    def test_default_ruleset_has_no_freeze_rules(self) -> None:
        assert not {r.action for r in DEFAULT_RULESET.rules} & {A.FREEZE, A.UNFREEZE}

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

    def test_time_change_on_frozen_lock_is_refused(self) -> None:
        out = evaluate(inputs(lock=lock(is_frozen=True)), DEFAULT_RULESET)
        assert out.action_type == A.NO_ACTION and "frozen" in out.reason


class TestLimits:
    def test_no_history_allows_the_full_amount(self) -> None:
        assert apply_limits(A.ADD_TIME, 2, (), NOW) == (2, None)

    def test_max_two_actions_per_24h(self) -> None:
        recent = (ra(600), ra(300))
        hours, why = apply_limits(A.ADD_TIME, 1, recent, NOW)
        assert hours == 0 and "action limit" in why

    def test_actions_older_than_24h_do_not_count(self) -> None:
        recent = (ra(24 * 60 + 1), ra(24 * 60 + 2))
        assert apply_limits(A.ADD_TIME, 1, recent, NOW) == (1, None)

    def test_cooldown_of_4_hours(self) -> None:
        hours, why = apply_limits(A.ADD_TIME, 1, (ra(239),), NOW)
        assert hours == 0 and "cooldown" in why
        assert apply_limits(A.ADD_TIME, 1, (ra(240),), NOW) == (1, None)

    def test_added_hours_cap_clamps_then_refuses(self) -> None:
        # 11h already added in the window (one action), so only 1h of the 2h requested remains.
        assert apply_limits(A.ADD_TIME, 2, (ra(300, hours=11),), NOW) == (1, None)
        hours, why = apply_limits(A.ADD_TIME, 1, (ra(300, hours=12),), NOW)
        assert hours == 0 and "cap" in why

    def test_removed_hours_cap(self) -> None:
        assert apply_limits(A.REMOVE_TIME, 3, (ra(300, A.REMOVE_TIME, 5),), NOW) == (1, None)
        hours, why = apply_limits(A.REMOVE_TIME, 1, (ra(300, A.REMOVE_TIME, 6),), NOW)
        assert hours == 0 and "cap" in why

    def test_add_and_remove_caps_are_independent(self) -> None:
        assert apply_limits(A.REMOVE_TIME, 1, (ra(300, A.ADD_TIME, 12),), NOW) == (1, None)

    def test_single_action_never_exceeds_6_hours(self) -> None:
        assert apply_limits(A.ADD_TIME, 99, (), NOW) == (6, None)

    def test_freeze_counts_against_frequency_and_cooldown(self) -> None:
        assert apply_limits(A.FREEZE, 0, (ra(100, A.UNFREEZE, 0),), NOW)[1] is not None
        assert apply_limits(A.FREEZE, 0, (), NOW) == (0, None)

    def test_property_no_sequence_exceeds_the_caps(self) -> None:
        """Replay many triggers over a day: totals never exceed the caps."""
        recent: list[RecentAction] = []
        t = NOW - timedelta(hours=23)
        for step in range(200):
            now = t + timedelta(minutes=7 * step)
            if now > NOW:
                break
            action = A.ADD_TIME if step % 3 else A.REMOVE_TIME
            hours, why = apply_limits(action, 6, tuple(recent), now)
            if why is None:
                recent.append(RecentAction(created_at=now, action_type=action, hours=hours))
        assert len(recent) <= 2
        assert sum(a.hours for a in recent if a.action_type == A.ADD_TIME) <= 12
        assert sum(a.hours for a in recent if a.action_type == A.REMOVE_TIME) <= 6


class TestRulesetValidation:
    def _rs(self, *rules, version="1") -> Ruleset:
        return Ruleset(version=version, rules=tuple(rules))

    def test_default_is_valid(self) -> None:
        validate_ruleset(DEFAULT_RULESET)

    @pytest.mark.parametrize("rule,version", [
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=7, direction=D.SETBACK), "1"),      # above single limit
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=0, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=1.5, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.FREEZE, hours=1, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.NO_ACTION, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=1), "1"),                           # unconditional
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=1, trust_below=1.5), "1"),
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=1, direction=D.SETBACK, min_confidence=2.0), "1"),
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=1, min_setback_streak=0), "1"),
        (Rule(id="a", description=" ", action=A.ADD_TIME, hours=1, direction=D.SETBACK), "1"),
        (Rule(id="", description="x", action=A.ADD_TIME, hours=1, direction=D.SETBACK), "1"),
        (Rule(id="a", description="x", action=A.ADD_TIME, hours=1, direction=D.SETBACK), " "),
    ])
    def test_invalid_rules_are_rejected(self, rule, version) -> None:
        with pytest.raises(RulesetValidationError):
            validate_ruleset(self._rs(rule, version=version))

    def test_duplicate_ids_rejected(self) -> None:
        r = Rule(id="a", description="x", action=A.ADD_TIME, hours=1, direction=D.SETBACK)
        with pytest.raises(RulesetValidationError):
            validate_ruleset(self._rs(r, r))

    def test_no_code_reveal_action_exists(self) -> None:
        assert {a.value for a in A} == {"no_action", "add_time", "remove_time", "freeze", "unfreeze"}
