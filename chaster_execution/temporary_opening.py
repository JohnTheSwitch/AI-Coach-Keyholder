"""
chaster_execution/temporary_opening.py

Opens the lock for an approved Temporary Wear Exemption, through Chaster's hygiene opening, and reads whether it was closed again (owner's design, 2026-11-04). This is what makes an
exemption an actual break from the device: lifting a freeze alone leaves the lock locked.

Verified live on the keyholder test lock: a keyholder can trigger an opening, it works on a frozen lock, the lock stays `locked`, and Chaster logs `temporary_opening_opened` and
`temporary_opening_locked` with exact times. `nextActionDate` is the last opening plus `regularity`, so a short `regularity` clears the cooldown; `openingTime` takes effect at once.

Sequence: the runtime halt is checked; the lock is read; `openingTime` becomes the approved length and `regularity` a moment, the late-relock penalty staying 0; a fresh read proves it; the
opening is sent; `regularity` goes back. `openingTime` goes back when the exemption ends.

- **Never an unlock.** The only path here is `temporary-opening/{id}/open`. The lock session continues, and no duration is touched.
- **The combination is never read.** Chaster shows it to the wearer. It does not pass through this bot.
- **At most once.** The caller claims the attempt before `open_for`. An ambiguous result is reported and never retried; only a provable refusal can be tried again.
- **Never past the halt.** `HALTED` means nothing was sent; the caller tries again later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable

from chaster.extension_config import LockConfiguration
from chaster.extension_plan import ReviewPlan, plan_opening_settings
from chaster.lock_client import CHASTER_USER_AGENT
from chaster_execution.models import TransportOutcome
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso

logger = logging.getLogger("ai_coach_keyholder.chaster_execution.temporary_opening")

__all__ = ["OpeningOutcome", "OpeningResult", "OpeningStatus", "ExemptionOpening", "OPENED_EVENT", "LOCKED_EVENT", "CLEAR_COOLDOWN_REGULARITY"]

OPENED_EVENT = "temporary_opening_opened"
LOCKED_EVENT = "temporary_opening_locked"
CLEAR_COOLDOWN_REGULARITY = 1        # seconds: next opening = last opening + regularity, so this makes one available at once


class OpeningOutcome(StrEnum):
    OPENED = "opened"
    HALTED = "halted"                # nothing was sent; try again
    UNAVAILABLE = "unavailable"      # provably nothing opened: not locked, no extension, not ours to edit, or Chaster refused
    AMBIGUOUS = "ambiguous"          # it may have opened: never retried


@dataclass(frozen=True, kw_only=True)
class OpeningResult:
    outcome: OpeningOutcome
    saved: dict | None = None        # the lock's own values, to put back
    detail: str | None = None


@dataclass(frozen=True, kw_only=True)
class OpeningStatus:
    is_open: bool
    relocked_at: datetime | None


class ExemptionOpening:
    def __init__(self, *, lock_id: str, account_user_id: str, control, reader, transport, token_manager, lock_client, send, clock) -> None:
        self.lock_id = lock_id
        self._account = account_user_id
        self._control = control
        self._reader = reader
        self._transport = transport
        self._tokens = token_manager
        self._client = lock_client
        self._send = send
        self._clock = clock

    def open_for(self, hours: float) -> OpeningResult:
        if self._control.is_halted():
            return OpeningResult(outcome=OpeningOutcome.HALTED)
        config = self._read_config()
        if config is None:
            return OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail="read_failed")
        extension = config.extension("temporary-opening")
        if config.status != "locked" or extension is None or not config.can_edit_extensions:
            return OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail="not_openable")
        saved = {"openingTime": extension.config.get("openingTime"), "regularity": extension.regularity}
        plan = plan_opening_settings(config, opening_seconds=round(hours * 3600), regularity=CLEAR_COOLDOWN_REGULARITY)
        if plan.refusal is not None:
            return OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail=plan.refusal)
        if not self._write(plan):
            return OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail="settings_not_applied")
        if self._control.is_halted():                       # again, immediately before the call that opens
            self.restore(saved)
            return OpeningResult(outcome=OpeningOutcome.HALTED)
        try:
            response = self._tokens.call_with_token(self._account, now=self._clock.now(), operation=self._post_open)
        except Exception:
            logger.exception("The opening call raised; its outcome is ambiguous.")
            return OpeningResult(outcome=OpeningOutcome.AMBIGUOUS, saved=saved, detail="raised")
        if response.status_code in (200, 201, 204):
            self._restore_regularity(saved)
            return OpeningResult(outcome=OpeningOutcome.OPENED, saved=saved)
        logger.warning("Chaster refused the opening with HTTP %s.", response.status_code)
        self.restore(saved)
        if 400 <= response.status_code < 500:
            return OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail=f"http_{response.status_code}")
        return OpeningResult(outcome=OpeningOutcome.AMBIGUOUS, saved=saved, detail=f"http_{response.status_code}")

    def status(self, *, opened_at: datetime) -> OpeningStatus | None:
        """What Chaster shows now, or None when it cannot be read: an unread state never counts as relocked."""
        try:
            body = self._tokens.call_with_token(self._account, now=self._clock.now(), operation=lambda t: self._client.get_temporary_opening(access_token=t, lock_id=self.lock_id))
            history = self._tokens.call_with_token(self._account, now=self._clock.now(), operation=lambda t: self._client.get_lock_history(access_token=t, lock_id=self.lock_id))
        except Exception:
            logger.warning("Could not read the opening state.", exc_info=True)
            return None
        if not isinstance(body, dict) or "openedAt" not in body:
            return None
        if body["openedAt"] is not None:
            return OpeningStatus(is_open=True, relocked_at=None)
        times = sorted(t for t in (_event_time(e) for e in history if e.get("type") == LOCKED_EVENT) if t is not None and t >= opened_at)
        return OpeningStatus(is_open=False, relocked_at=times[0] if times else None)

    def restore(self, saved: dict | None) -> bool:
        """The lock's own opening length and regularity. Idempotent; False means try again later."""
        if not saved or self._control.is_halted():
            return False
        config = self._read_config()
        if config is None:
            return False
        plan = plan_opening_settings(config, opening_seconds=saved.get("openingTime"), regularity=saved.get("regularity"))
        return plan.refusal is None and (not plan.has_changes or self._write(plan))

    # ------------------------------------------------------------------ internals

    def _restore_regularity(self, saved: dict) -> None:
        config = self._read_config()
        if config is None:
            logger.warning("Could not read the lock to put its regularity back; the exemption's end restores it.")
            return
        extension = config.extension("temporary-opening")
        plan = plan_opening_settings(config, opening_seconds=extension.config.get("openingTime") if extension else None, regularity=saved.get("regularity"))
        if plan.refusal is None and plan.has_changes:
            self._write(plan)

    def _read_config(self) -> LockConfiguration | None:
        report = self._reader.read(user_id=self._account, lock_ids=[self.lock_id], now=self._clock.now())
        return report.configurations[0] if report.configurations else None

    def _write(self, plan: ReviewPlan) -> bool:
        """Only a fresh read counts: Chaster answers 201 to edits it ignores."""
        sent = self._transport.set_extensions(lock_id=self.lock_id, body=plan.body)
        if sent.outcome is not TransportOutcome.OK:
            return False
        config = self._read_config()
        extension = config.extension("temporary-opening") if config is not None else None
        wanted = next(e for e in plan.body["extensions"] if e["slug"] == "temporary-opening")
        return extension is not None and extension.config.get("openingTime") == wanted["config"].get("openingTime") and extension.regularity == wanted["regularity"]

    def _post_open(self, token: str):
        return self._send("POST", f"https://api.chaster.app/extensions/temporary-opening/{self.lock_id}/open", json={}, timeout=(5, 25),
                          headers={"Authorization": f"Bearer {token}", "User-Agent": CHASTER_USER_AGENT, "Content-Type": "application/json"})


def _event_time(entry: dict) -> datetime | None:
    try:
        return parse_iso(entry["createdAt"])
    except (KeyError, TypeError, ValueError):
        return None
