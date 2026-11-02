"""
iron_contract/guard.py

The Iron Contract (v0.39 Phase G6; docs/architecture/phase_g_proposal.md section 7). While the wearer is LOCKED:
- commands that lower the difficulty (requesting or confirming a switch back to Standard Mode) are refused;
- the model receives `[STATE: LOCKED]` and its guardrails tell it not to negotiate deadlines, extensions or excuses (health, safety and real emergencies
  always come first, philosophy 2.3);
- a vacation can be scheduled or cancelled only while UNLOCKED (that requirement belongs to the vacation feature itself and applies everywhere).

What it never blocks (philosophy 2.5, consent is revocable at any time; the invitation promises these "at any time"): `halt`, `resume`, `leave`,
`conditioning off`, `research off`, `forget all`, the emergency path, `vacation end`, and reporting the lock state.

Consent: in a tenant database the contract (the difficulty gate and the locked-state stance) applies only when the accepted invitation disclosed it
(IRON_CONTRACT_MARKER in consent_log), like Behavioral Telemetry; tenant zero is the owner's own database.

Lock status: LOCKED when the latest Chaster observation of the observed wearer OR the wearer's own latest report says locked; UNLOCKED when one of them says
unlocked and neither says locked; otherwise UNKNOWN. A guard that needs "unlocked" refuses UNKNOWN too (fail closed), and says how to report the state.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Callable

from infrastructure.database import Database as CoreDatabase

logger = logging.getLogger("ai_coach_keyholder.iron_contract")

__all__ = ["IronContract", "LockStatus", "IRON_CONTRACT_MARKER", "IRON_CONTRACT_DISCLOSURE", "LOCKED_REFUSAL", "UNKNOWN_REFUSAL"]

IRON_CONTRACT_MARKER = "Iron Contract"
IRON_CONTRACT_DISCLOSURE = (
    f"While you are locked, the {IRON_CONTRACT_MARKER} applies: you cannot lower the difficulty (switch back to Standard Mode) or schedule or cancel a "
    "vacation, and the coach will not negotiate deadlines or excuses. Your health, safety and real emergencies always come first, and `halt`, `leave`, "
    "`conditioning off`, `research off` and `forget all` always work.")

LOCKED_REFUSAL = (f"Not while you are locked. The {IRON_CONTRACT_MARKER} holds until you are unlocked. "
                  "If this is about your health or safety, that always comes first: use your emergency option.")
UNKNOWN_REFUSAL = ("Your lock status is not known, so this is only possible once you are known to be unlocked. "
                   "If you are unlocked, report it with `lock report unlocked` first.")


class LockStatus(StrEnum):
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    UNKNOWN = "unknown"


class IronContract:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, consent_target: str | None = None,
                 wearer_user_id: str | None = None, get_observation: Callable[[], object] | None = None) -> None:
        """`get_observation()` returns the latest Chaster observation of `wearer_user_id` (or None); the composition sets it once observations exist."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._consent_target = consent_target
        self._wearer = wearer_user_id
        self.get_observation = get_observation

    def applies(self) -> bool:
        """Whether the difficulty gate and the locked-state stance are in force in this database."""
        if self._consent_target is None:
            return True
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT explanation_shown FROM consent_log WHERE target_id = ? AND action = 'approved'", (self._consent_target,))
        return any(IRON_CONTRACT_MARKER in (r["explanation_shown"] or "") for r in rows)

    def lock_status(self, user_id: str) -> LockStatus:
        try:
            seen = {self._observed(user_id), self._reported(user_id)}
        except Exception:
            logger.exception("Could not read the lock status; treating it as unknown.")
            return LockStatus.UNKNOWN
        if LockStatus.LOCKED in seen:
            return LockStatus.LOCKED
        if LockStatus.UNLOCKED in seen:
            return LockStatus.UNLOCKED
        return LockStatus.UNKNOWN

    def refusal(self, user_id: str, *, always: bool = False) -> str | None:
        """None when the action may go ahead. `always=True` for actions that need "unlocked" whether or not the contract was disclosed (vacations)."""
        if not always and not self.applies():
            return None
        status = self.lock_status(user_id)
        if status is LockStatus.UNLOCKED:
            return None
        return LOCKED_REFUSAL if status is LockStatus.LOCKED else UNKNOWN_REFUSAL

    def locked_for_context(self, user_id: str) -> bool:
        """True when the model must see [STATE: LOCKED]."""
        try:
            return self.applies() and self.lock_status(user_id) is LockStatus.LOCKED
        except Exception:
            logger.exception("Could not determine the Iron Contract state for the prompt.")
            return False

    # ------------------------------------------------------------------ sources

    def _observed(self, user_id: str) -> LockStatus:
        if self.get_observation is None or user_id != self._wearer:
            return LockStatus.UNKNOWN
        observation = self.get_observation()
        status = getattr(getattr(observation, "status", None), "value", getattr(observation, "status", None))
        if status == "locked":
            return LockStatus.LOCKED
        if status == "unlocked":
            return LockStatus.UNLOCKED
        return LockStatus.UNKNOWN

    def _reported(self, user_id: str) -> LockStatus:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT status FROM lock_reports WHERE user_id = ? ORDER BY sequence_number DESC LIMIT 1", (user_id,))
        if row is None:
            return LockStatus.UNKNOWN
        return LockStatus.LOCKED if row["status"] == "locked_user_reported" else LockStatus.UNLOCKED
