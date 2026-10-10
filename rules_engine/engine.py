"""
rules_engine/engine.py

The pure, deterministic core: `evaluate(inputs, ruleset) -> DecisionOutcome`.
No database, no clock (`now` is an input), no network, no model. Same inputs
and ruleset always give the same outcome.

Order: (1) absolute safety gates, (2) first matching rule, (3) the rule's
action must be valid for the lock's current state, (4) hard limits.
Every outcome, including NO_ACTION, carries a non-empty reason (DEC-2).
"""

from __future__ import annotations

from rules_engine.limits import LOCK_DATA_MAX_AGE, apply_limits
from rules_engine.models import (
    ActionType,
    DecisionOutcome,
    EntitlementClass,
    Rule,
    RuleInputs,
    Ruleset,
)

__all__ = ["evaluate"]


def _no_action(ruleset: Ruleset, cls: EntitlementClass, reason: str, rule_id: str | None = None) -> DecisionOutcome:
    return DecisionOutcome(
        action_type=ActionType.NO_ACTION, hours=0, entitlement_class=cls, rule_id=rule_id,
        ruleset_version=ruleset.version, reason=reason,
    )


def _safety_gate(inputs: RuleInputs, ruleset: Ruleset) -> DecisionOutcome | None:
    if inputs.trust_score is None or inputs.trust_confidence is None:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: trust data is unavailable.")
    lock = inputs.lock
    if lock is None:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: no lock data is available.")
    age = inputs.now - lock.observed_at
    if age.total_seconds() < 0:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock data timestamp is invalid.")
    if age > LOCK_DATA_MAX_AGE:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock data is too old (over 15 minutes).")
    if lock.status != "locked":
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock is not in a locked state.")
    if lock.is_keyholder_lock is not True:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: this is not confirmed to be a keyholder lock.")
    if lock.is_frozen is None:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock's frozen state is unknown.")
    return None


def _matches(rule: Rule, i: RuleInputs) -> bool:
    if rule.direction is not None and i.direction != rule.direction:
        return False
    if rule.min_setback_streak is not None and i.setback_streak < rule.min_setback_streak:
        return False
    if rule.min_progress_streak is not None and i.progress_streak < rule.min_progress_streak:
        return False
    score, confidence = i.trust_score, i.trust_confidence
    if confidence < rule.min_confidence:
        return False
    if rule.trust_below is not None and not score < rule.trust_below:
        return False
    if rule.trust_at_least is not None and not score >= rule.trust_at_least:
        return False
    prev = i.previous_trust_score
    if rule.crossed_below is not None and not (prev is not None and prev >= rule.crossed_below > score):
        return False
    if rule.crossed_above is not None and not (prev is not None and prev < rule.crossed_above <= score):
        return False
    return True


def evaluate(inputs: RuleInputs, ruleset: Ruleset) -> DecisionOutcome:
    gate = _safety_gate(inputs, ruleset)
    if gate is not None:
        return gate

    rule = next((r for r in ruleset.rules if _matches(r, inputs)), None)
    if rule is None:
        return _no_action(ruleset, EntitlementClass.GUARANTEED, "No action: no rule applies to this situation.")

    frozen = inputs.lock.is_frozen
    if rule.action in (ActionType.ADD_TIME, ActionType.REMOVE_TIME) and frozen:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock is frozen.", rule.id)
    if rule.action == ActionType.FREEZE and frozen:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock is already frozen.", rule.id)
    if rule.action == ActionType.UNFREEZE and not frozen:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, "No action: the lock is not frozen.", rule.id)

    hours, refusal = apply_limits(rule.action, rule.hours, inputs.recent_actions, inputs.now)
    if refusal is not None:
        return _no_action(ruleset, EntitlementClass.ABSOLUTE, f"No action: {refusal}.", rule.id)

    return DecisionOutcome(
        action_type=rule.action, hours=hours, entitlement_class=EntitlementClass.GUARANTEED,
        rule_id=rule.id, ruleset_version=ruleset.version,
        reason=f"{rule.description}: {rule.action.value.replace('_', ' ')}"
               + (f" by {hours} hour(s)" if hours else "") + " (dry run, not executed).",
    )
