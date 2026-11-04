"""
rules_engine/ruleset.py

Rulesets are system-configured data, validated fail-fast at load. There is
no runtime mutation path and no model-driven change (owner decision);
any future runtime change requires explicit consent plus an audit log.

The thresholds and step sizes in DEFAULT_RULESET are bootstrap defaults
(tagged below) pending owner tuning. They are inert in practice while Phase 2
is dry-run only.
"""

from __future__ import annotations

from rules_engine.models import ActionType, GoalDirection, Rule, Ruleset

__all__ = ["RulesetValidationError", "validate_ruleset", "DEFAULT_RULESET"]


class RulesetValidationError(ValueError):
    pass


def _unit_interval(rule: Rule, name: str, value: float | None) -> None:
    if value is not None and not (0.0 <= value <= 1.0):
        raise RulesetValidationError(f"rule {rule.id!r}: {name} must be within [0, 1]")


def validate_ruleset(ruleset: Ruleset) -> Ruleset:
    """Fail-fast validation; returns the ruleset unchanged when valid."""
    if not ruleset.version or not ruleset.version.strip():
        raise RulesetValidationError("ruleset version must be non-empty")
    seen: set[str] = set()
    for rule in ruleset.rules:
        if not rule.id or not rule.id.strip():
            raise RulesetValidationError("rule id must be non-empty")
        if rule.id in seen:
            raise RulesetValidationError(f"duplicate rule id {rule.id!r}")
        seen.add(rule.id)
        if not rule.description.strip():
            raise RulesetValidationError(f"rule {rule.id!r}: description must be non-empty")
        if not isinstance(rule.action, ActionType) or rule.action == ActionType.NO_ACTION:
            raise RulesetValidationError(f"rule {rule.id!r}: action must be an actionable ActionType")
        if rule.hours != 0:
            raise RulesetValidationError(f"rule {rule.id!r}: {rule.action.value} takes no hours")
        if rule.direction is not None and not isinstance(rule.direction, GoalDirection):
            raise RulesetValidationError(f"rule {rule.id!r}: direction must be a GoalDirection")
        for name in ("min_setback_streak", "min_progress_streak"):
            v = getattr(rule, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
                raise RulesetValidationError(f"rule {rule.id!r}: {name} must be an int >= 1")
        for name in ("trust_below", "trust_at_least", "crossed_below", "crossed_above", "min_confidence"):
            _unit_interval(rule, name, getattr(rule, name))
        has_condition = any(
            getattr(rule, n) is not None
            for n in ("direction", "min_setback_streak", "min_progress_streak", "trust_below",
                      "trust_at_least", "crossed_below", "crossed_above")
        )
        if not has_condition:
            raise RulesetValidationError(f"rule {rule.id!r}: an unconditional rule is not allowed")
    return ruleset


# Owner decision (Zero-Time-Modification): the shipped ruleset is EMPTY. A goal or Trust change never touches a lock; progression is governed
# purely by the frozen state and the Release Engine. An action type for changing time no longer exists, so no ruleset can add one.
DEFAULT_RULESET = validate_ruleset(Ruleset(version="2", rules=()))
