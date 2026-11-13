"""
release_engine/conditioning.py

The wearer's own switch for Progressive Conditioning (v0.39 Phase F2; table `conditioning_preferences`, migration 036).

Accepting the invitation is consent to Progressive Conditioning (docs/architecture/phase_f_proposal.md 5.1), so a user with no row is ON. `conditioning off`
stores OFF: from then on the hidden factor never increases; it stays at its current value (it is not reset).

Phase F4 (`ProgressiveConditioning`, below): the factor itself, the payout threshold of the Token Progress Pool and the owner's per-database switch.
The factor is never put into an event, a log line, a prompt or a reply to the wearer; only the owner CLI shows it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import release_decision
from infrastructure.time_format import iso as _iso

__all__ = ["ConditioningPreferences", "ProgressiveConditioning", "IncrementOutcome", "FACTOR_START", "FACTOR_STEP", "FACTOR_CAP", "BASE_THRESHOLD"]


class ConditioningPreferences:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)

    def is_enabled(self, user_id: str) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT enabled FROM conditioning_preferences WHERE user_id = ?", (user_id,))
        return True if row is None else bool(row["enabled"])

    def set_enabled(self, user_id: str, enabled: bool, *, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO conditioning_preferences (user_id, enabled, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                       (user_id, int(bool(enabled)), _iso(now)))

        apply_transition(self._core, write=write)


# =====================================================================================================================================
# Phase F4: the factor itself
# =====================================================================================================================================

FACTOR_START = Decimal("1.000")
FACTOR_STEP = Decimal("0.025")
FACTOR_CAP = Decimal("1.500")
BASE_THRESHOLD = {"standard": Decimal("1.0"), "advanced": Decimal("2.0")}   # about 3.5 days / 7.0 days per token at full performance


class IncrementOutcome(StrEnum):
    INCREASED = "increased"
    CAPPED = "capped"
    NOT_TOKEN = "not_token"            # the 72-hour backstop (kind 'backstop') never counts
    WEARER_OFF = "wearer_off"          # the wearer ran `conditioning off`: the factor stays frozen
    OWNER_OFF = "owner_off"            # the owner's switch for this database is off
    PAUSED = "paused"                  # Phase G6: a vacation was in force (audited in conditioning_paused_releases, not training_increments)


_LOGGED = {
    IncrementOutcome.INCREASED: "Progressive Conditioning: this token release counted; the next one costs a little more.",
    IncrementOutcome.CAPPED: "Progressive Conditioning: this token release counted, but the factor is already at its cap.",
    IncrementOutcome.NOT_TOKEN: "Progressive Conditioning: not a token release (the backstop), so it does not count.",
    IncrementOutcome.OWNER_OFF: "Progressive Conditioning: the owner switch was off, so this release did not count.",
    IncrementOutcome.WEARER_OFF: "Progressive Conditioning: the wearer has `conditioning off`, so this release did not count.",
}


class ProgressiveConditioning:
    """The hidden factor of Progressive Conditioning (philosophy 4.2 / 4.5; owner decisions in phase_f_proposal 5.2).

    It is built only while ENABLE_SLAVE_TRAINING is on. The payout threshold of the Token Progress Pool is `base(mode) * factor`. The factor rises by
    FACTOR_STEP, up to FACTOR_CAP, ONLY through `on_release_confirmed_in_transaction`, which the outbox consumer of `release.provider_confirmed` calls:
    a release that Chaster confirmed. Anything that is not a token-funded release (kind `backstop`), any emergency unlock, failed or ambiguous release or
    manual freeze never produces that event for a token authorization, so it can never raise the factor. Each authorization is considered once
    (UNIQUE audit row). The factor never appears in an event, a log line, a prompt or a wearer-facing reply."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None,
                 preferences: ConditioningPreferences | None = None, vacation_in=None) -> None:
        """`vacation_in(tx, user_id, at)` (Phase G6, iron_contract.vacation.VacationService.running_id_in) returns the id of a vacation in force, which
        freezes the factor."""
        self.db_path = Path(db_path)
        self._core = core if core is not None else CoreDatabase(self.db_path)
        self._prefs = preferences or ConditioningPreferences(self.db_path, core=self._core)
        self._vacation_in = vacation_in

    # ------------------------------------------------------------------ owner switch (per database)

    def owner_enabled(self) -> bool:
        with self._core.transaction() as tx:
            return self._owner_enabled_in(tx)

    def set_owner_enabled(self, enabled: bool, *, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO training_settings (id, owner_enabled, updated_at) VALUES (1, ?, ?) "
                       "ON CONFLICT(id) DO UPDATE SET owner_enabled = excluded.owner_enabled, updated_at = excluded.updated_at",
                       (int(bool(enabled)), _iso(now)))

        apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ reads (owner CLI and the daily evaluation only)

    def factor(self, user_id: str) -> Decimal:
        with self._core.transaction() as tx:
            return self.factor_in(tx, user_id)

    def threshold_in(self, tx: Transaction, user_id: str) -> Decimal:
        """The pool's payout threshold for this user right now: base(operating mode) * factor."""
        row = tx.fetch_one("SELECT current_mode FROM operating_mode_state WHERE id = 1")
        mode = row["current_mode"] if row is not None and row["current_mode"] in BASE_THRESHOLD else "standard"
        return BASE_THRESHOLD[mode] * self.factor_in(tx, user_id)

    @staticmethod
    def factor_in(tx: Transaction, user_id: str) -> Decimal:
        row = tx.fetch_one("SELECT factor FROM training_state WHERE user_id = ?", (user_id,))
        return FACTOR_START if row is None else Decimal(row["factor"])

    # ------------------------------------------------------------------ the only way the factor rises

    def on_release_confirmed_in_transaction(self, tx: Transaction, *, authorization_id: str, now: datetime) -> IncrementOutcome | None:
        """Consumer side of `release.provider_confirmed`. Returns None when this authorization was already considered or is unknown."""
        if tx.fetch_one("SELECT 1 FROM training_increments WHERE authorization_id = ?", (authorization_id,)):
            return None
        if tx.fetch_one("SELECT 1 FROM conditioning_paused_releases WHERE authorization_id = ?", (authorization_id,)):
            return None
        auth = tx.fetch_one("SELECT user_id, kind, state FROM release_authorizations WHERE id = ?", (authorization_id,))
        if auth is None or auth["state"] != "provider_confirmed":
            return None
        user_id = auth["user_id"]
        vacation_id = self._vacation_in(tx, user_id, now) if self._vacation_in is not None and auth["kind"] == "token" else None
        if vacation_id is not None:
            tx.execute("INSERT INTO conditioning_paused_releases (authorization_id, user_id, vacation_id, created_at) VALUES (?, ?, ?, ?)",
                       (authorization_id, user_id, vacation_id, _iso(now)))
            release_decision("Progressive Conditioning: a vacation was in force, so this release did not count.", authorization_id=authorization_id,
                             outcome=IncrementOutcome.PAUSED.value)
            return IncrementOutcome.PAUSED
        before = self.factor_in(tx, user_id)
        if auth["kind"] != "token":
            outcome = IncrementOutcome.NOT_TOKEN
        elif not self._owner_enabled_in(tx):
            outcome = IncrementOutcome.OWNER_OFF
        elif not self._wearer_enabled_in(tx, user_id):
            outcome = IncrementOutcome.WEARER_OFF
        elif before >= FACTOR_CAP:
            outcome = IncrementOutcome.CAPPED
        else:
            outcome = IncrementOutcome.INCREASED
        after = min(FACTOR_CAP, before + FACTOR_STEP) if outcome is IncrementOutcome.INCREASED else before
        if after != before:
            tx.execute("INSERT INTO training_state (user_id, factor, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET factor = excluded.factor, updated_at = excluded.updated_at",
                       (user_id, str(after), _iso(now)))
        tx.execute("INSERT INTO training_increments (id, user_id, authorization_id, outcome, factor_before, factor_after, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), user_id, authorization_id, outcome.value, str(before), str(after), _iso(now)))
        # Owner, 2026-11-13: "it should be somewhere in the logs too". Whether the release counted, and why not; never the factor itself.
        release_decision(_LOGGED.get(outcome, f"Progressive Conditioning: {outcome.value}."), authorization_id=authorization_id, outcome=outcome.value)
        return outcome

    @staticmethod
    def _owner_enabled_in(tx: Transaction) -> bool:
        row = tx.fetch_one("SELECT owner_enabled FROM training_settings WHERE id = 1")
        return row is not None and bool(row["owner_enabled"])

    @staticmethod
    def _wearer_enabled_in(tx: Transaction, user_id: str) -> bool:
        row = tx.fetch_one("SELECT enabled FROM conditioning_preferences WHERE user_id = ?", (user_id,))
        return True if row is None else bool(row["enabled"])
