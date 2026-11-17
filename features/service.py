"""
features/service.py

Per-wearer feature switches (owner, 2026-11-16): "we should make a switch for working out and for all the other things that different users
could prefer to be off", and "a switch ... that wouldn't punish for not relocking (or not relocking in some time)" - "I meant the relock after token spending, all
other should be mandatory".

Every feature is ON unless the wearer switched it off (no row = on, so nothing changes for the owner). The wearer sends `features` for the
list, `<feature> off` / `<feature> on` to change one. While locked a switch-off waits 24 hours and is asked about (opt_outs, gap fix G3);
switching on is always immediate. The core of the system (check-ins, every detector but the release window, Trust, Penalty Windows, tokens,
health, emergency) has no switch.

Table `feature_switches` (migration 086), written only here. Event: `features.switched`. Other modules never import this module's writer;
the wiring hands them `get_feature_on` (or the in-transaction variant for outbox consumers).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso

logger = logging.getLogger("ai_coach_keyholder.features")

__all__ = ["FEATURES", "Features", "get_feature_on", "get_feature_on_in_transaction"]

# key -> (the words he types, what it covers)
FEATURES: dict[str, tuple[str, str]] = {
    "workouts": ("workouts", "the training plan and workout tasks"),
    "erotic_tasks": ("erotic tasks", "the daily erotic / BDSM obedience task"),
    "protocol": ("protocol", "the protocol ladder (morning report rung, asking properly, title, thank-yous, kneeling)"),
    "morning_report": ("morning report", "the morning report"),
    "lines": ("lines", "lines for confirmed incidents and release lines"),
    "temptations": ("temptations", "temptation offers"),
    # Owner, 2026-11-16: only the relock after a release earned with a token; hygiene, exemption and relock-picture checks stay mandatory.
    "release_relock": ("release relock", "the 2-hour window to relock after a token release (off: no consequence for relocking later)"),
}
_BY_WORDS = {words: key for key, (words, _about) in FEATURES.items()}


def get_feature_on_in_transaction(tx: Transaction, user_id: str, feature: str) -> bool:
    """Read-only, in the caller's transaction. On unless switched off; an unknown feature is on."""
    row = tx.fetch_one("SELECT enabled FROM feature_switches WHERE user_id = ? AND feature = ?", (user_id, feature))
    return row is None or bool(row["enabled"])


def get_feature_on(core: CoreDatabase, user_id: str, feature: str) -> bool:
    """Read-only. Fails open to ON (the system as the owner built it), never to a silently missing rule."""
    try:
        with core.transaction() as tx:
            return get_feature_on_in_transaction(tx, user_id, feature)
    except Exception:
        logger.exception("A feature switch could not be read; it counts as on.")
        return True


class Features:
    def __init__(self, core: CoreDatabase) -> None:
        self._core = core
        # opt_outs (G3): (user_id, opt-out key, now) -> the waiting reply, or None to apply at once; and the withdraw on switching back on.
        self.request_off: Callable[[str, str, datetime], str | None] | None = None
        self.withdraw: Callable[[str, str, datetime], object] | None = None
        # Leak audit (2026-11-17): features that cannot be switched on for this wearer yet, with the reason they are told. Such a feature
        # counts as off whatever its row says (another wearer must never get the owner's own training plan).
        # A value is the reason itself, or a callable(user_id) -> the reason, or None once it is available (2026-11-17: erotic tasks wait for
        # the wearer's first approved task, custom_tasks/).
        self.unavailable: dict[str, object] = {}

    def why_unavailable(self, user_id: str, feature: str) -> str | None:
        why = self.unavailable.get(feature)
        if callable(why):
            try:
                return why(user_id)
            except Exception:
                logger.exception("Whether a feature is available could not be read; it counts as not available.")
                return "That isn't available right now."
        return why

    def is_on(self, user_id: str, feature: str) -> bool:
        if self.why_unavailable(user_id, feature) is not None:
            return False
        return get_feature_on(self._core, user_id, feature)

    def set(self, user_id: str, feature: str, on: bool, now: datetime) -> bool:
        """True when it changed."""
        assert feature in FEATURES

        def write(tx: Transaction, _s: object) -> bool:
            if get_feature_on_in_transaction(tx, user_id, feature) == on:
                return False
            tx.execute("INSERT INTO feature_switches (user_id, feature, enabled, changed_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (user_id, feature) DO UPDATE SET enabled = excluded.enabled, changed_at = excluded.changed_at",
                       (user_id, feature, 1 if on else 0, _iso(now)))
            write_event(tx, DomainEvent(event_type="features.switched", source_module="features", occurred_at=now,
                                        payload={"user_id": user_id, "feature": feature, "on": on}))
            return True

        changed = bool(apply_transition(self._core, write=write))
        if changed:
            decision("features", f"A feature was switched {'on' if on else 'off'}.", feature=feature)
        return changed

    def seed_off(self, user_id: str, features, now: datetime) -> list[str]:
        """Owner, 2026-11-16 (multi-wearer): a new wearer starts with these off until they switch them on. Only where no row exists, so
        a wearer's own choice is never overwritten. Returns the features seeded now."""
        seeded: list[str] = []

        def write(tx: Transaction, _s: object) -> None:
            for feature in features:
                assert feature in FEATURES
                if tx.execute("INSERT OR IGNORE INTO feature_switches (user_id, feature, enabled, changed_at) VALUES (?, ?, 0, ?)",
                              (user_id, feature, _iso(now))).rowcount:
                    write_event(tx, DomainEvent(event_type="features.switched", source_module="features", occurred_at=now,
                                                payload={"user_id": user_id, "feature": feature, "on": False, "seeded": True}))
                    seeded.append(feature)

        apply_transition(self._core, write=write)
        return seeded

    def apply_off(self, user_id: str, feature: str, now: datetime) -> str:
        self.set(user_id, feature, False, now)
        return f"{FEATURES[feature][0].capitalize()} is off. `{FEATURES[feature][0]} on` starts it again."

    # ------------------------------------------------------------------ the command

    def command(self, user_id: str, text: str, now: datetime) -> str | None:
        """`features`, `<feature> off`, `<feature> on`; None for any other message."""
        said = " ".join((text or "").lower().split())
        if said in ("features", "feature list", "switches"):
            return self.listing(user_id)
        for verb in ("off", "on"):
            if said.endswith(" " + verb) and said[: -len(verb) - 1] in _BY_WORDS:
                key = _BY_WORDS[said[: -len(verb) - 1]]
                return self._switch_on(user_id, key, now) if verb == "on" else self._switch_off(user_id, key, now)
        return None

    def listing(self, user_id: str) -> str:
        rows = [f"- {words}: {'not available yet' if self.why_unavailable(user_id, key) else 'on' if self.is_on(user_id, key) else 'off'} ({about})"
                for key, (words, about) in FEATURES.items()]
        return ("Your features:\n" + "\n".join(rows) + "\nChange one with `<feature> off` or `<feature> on`. While you're locked, switching "
                "off waits 24 hours and I ask again. Check-ins, Trust, penalties, health and the emergency option have no switch.")

    def _switch_on(self, user_id: str, key: str, now: datetime) -> str:
        if (why := self.why_unavailable(user_id, key)) is not None:
            return why
        if self.withdraw is not None:
            try:
                self.withdraw(user_id, f"feature:{key}", now)
            except Exception:
                logger.exception("A waiting switch-off could not be withdrawn; it is switched on anyway.")
        changed = self.set(user_id, key, True, now)
        return f"{FEATURES[key][0].capitalize()} {'is on again' if changed else 'is already on'}."

    def _switch_off(self, user_id: str, key: str, now: datetime) -> str:
        if not self.is_on(user_id, key):
            return f"{FEATURES[key][0].capitalize()} is already off."
        if self.request_off is not None:
            try:
                waiting = self.request_off(user_id, f"feature:{key}", now)
            except Exception:
                logger.exception("Could not read whether the switch-off waits; it waits (never a loophole by accident).")
                return "That can't be changed right now. Try again later."
            if waiting is not None:
                return waiting
        return self.apply_off(user_id, key, now)
