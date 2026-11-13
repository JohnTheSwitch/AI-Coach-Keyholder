"""
exemptions/service.py

The Temporary Wear Exemption workflow (philosophy 3.6), from the wearer's first message to the Penalty Engine's existing `freeze()`:

    "I need an exemption" -> reason button -> length button -> the keyholder's card -> Approve -> `freeze(temporary_wear_exemption, expires_at)`

Everything after that already existed and is reused: `penalty_window.frozen` -> `rules_engine/penalty_hold.py` -> an unfreeze intent -> the penalty barrier, under the
runtime halt. When the exemption ends, by time or by the wearer's "I'm back", the window's `resumed` event raises the barrier again. No hour is added or removed anywhere: a pause is a Penalty
Window freeze, and the redemption hold stays on (Zero-Time-Modification).

**It opens the lock (owner, 2026-11-04).** Lifting a freeze leaves a lock locked, so an approved exemption also triggers a hygiene opening through the injected `opening` port
(`chaster_execution/temporary_opening.py`), with or without a Penalty Window. "I'm back" is checked against Chaster, not believed: a lock still open keeps the exemption running, and a relock is
compared with the approved time by Chaster's own log. A late relock goes to `infraction_signals`, where the wearer can still say it was a health need, and a keyholder decides; it never moves time.
Chaster can show that the lock was relocked in the app, not that the device is on. That is the limit of this check.

**Who decides.** The keyholder, by button (3.2, and 2.5: a person decides, a model never does). `decide()` reads the request and nothing else: it cannot see whether a
Penalty Window exists (invariant I7), so a request is treated the same inside and outside one. Applying the decision is a separate step, `tick()`.

**Not a gate on safety.** This is for planned life. A health or safety need never waits for this (3.9, 3.10), and every message here says so.

An exemption approved while no window runs is simply recorded as in force, and `tick()` pauses a window that starts during it.

**Asking for a hygiene opening (owner, 2026-11-04)** uses this same path, as a row with its own `kind`. The phrase only brings up buttons: a question must never open a lock, so a press is what commits.
Within the weekly allowance the press opens at once. Past it, the press asks the keyholder. A health need is the other button: Mandatory Access (philosophy 3.9), never limited, counted or judged, and
its opening is not checked for lateness. A hygiene break does not pause a Penalty Window: that is what an exemption is for.
"""

from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime, timedelta
from enum import StrEnum
import json
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from application.models import ActionSpec, OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.decision_log import decision
from infrastructure.decision_log import decision as _log
from infrastructure.text import plain_text
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from notifications.queue import enqueue_in_transaction
from penalty_engine.models import FreezeReason

logger = logging.getLogger("ai_coach_keyholder.exemptions")

__all__ = ["ExemptionService", "ExemptionDecision", "is_exemption_request", "is_hygiene_request", "HEALTH_MINUTES", "CATEGORIES", "DURATIONS", "SAFETY_LINE"]

CATEGORIES = {"family": "Family event", "social": "Social occasion", "work_school": "Work or school", "health": "Health", "travel": "Travel", "other": "Something else"}
DURATIONS = (3, 6, 12, 24, 72, 168)         # hours; a longer absence is what a vacation is for. 3 and 12: live, 2026-11-13, "till evening" had to be 24
UNJUDGED_LIMIT = timedelta(hours=1)          # a request the judge could not decide this long is closed, never as a decline: "ask again"
APPEAL_WINDOW = timedelta(hours=24)          # a new request this soon after a declined one is judged as an appeal
MAX_APPEALS = 1                              # owner, 2026-11-13: one appeal, and it must bring something new; after it, "Take it anyway"
ANYWAY_VALID = timedelta(hours=2)            # how long after the decline the "Take it anyway" button works
REASON_QUESTION = ("In a sentence or two: what is it, and why does the lock need to come off for it? I decide on that, the way a keyholder "
                   "would.")
SAFETY_LINE = "If this is an urgent health or safety matter, you don't need this: do what keeps you safe, and it will never be held against you."

_REQUEST = re.compile(r"\b(exemption|exempt me|v[yý]jimk[auy]|v[yý]jimku)\b", re.IGNORECASE)
_HYGIENE = re.compile(r"\bhygiene (opening|break)\b|\bhygienick\w* (otevren\w*|prestavk\w*)\b|\botevr\w* .*\bhygien")
HEALTH_MINUTES = 60                         # how long a health opening runs on Chaster's clock; nothing is judged against it
ALLOWANCE_WINDOW = timedelta(days=7)


def is_exemption_request(message: str) -> bool:
    """Deterministic, never a model's reading: the word itself opens the buttons, and a button is what commits to anything."""
    return isinstance(message, str) and len(message) <= 300 and _REQUEST.search(message) is not None


# Live, 2026-11-12: "You're not worried about me misusing the hygiene opening?" opened a new card. Only a request does now: a request word, or the
# phrase almost on its own ("hygiene opening", "hygiene break please").
_ASKING = re.compile(r"\b(can i|could i|may i|i need|i'd like|i would like|i want|give me|let me|please|request|need a|need an|open (it|my lock|me)|"
                     r"chci|potrebuj\w*|muzu|mohl\w* bych|prosim|otevri|dej mi|pust mi)\b")


def is_hygiene_request(message: str) -> bool:
    """Deterministic, and it only offers buttons."""
    if not isinstance(message, str) or len(message) > 300:
        return False
    text = plain_text(message)
    found = _HYGIENE.search(text)
    if found is None:
        return False
    rest = re.sub(r"[^a-z ]+", " ", (text[:found.start()] + " " + text[found.end():])).split()
    return _ASKING.search(text) is not None or len(rest) <= 2


class ExemptionDecision(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"
    NOT_OWNER = "not_owner"
    NOT_PENDING = "not_pending"


class Opening(Protocol):
    lock_id: str
    def open_for(self, hours: float): ...
    def status(self, *, opened_at: datetime): ...
    def restore(self, saved: dict | None) -> bool: ...


class Penalties(Protocol):
    def get_active_or_frozen_penalty_window(self): ...
    def freeze(self, penalty_window_id: str, reason: FreezeReason, *, now: datetime, exemption_id: str | None = None, expires_at: datetime | None = None): ...
    def resume(self, penalty_window_id: str, reason: FreezeReason, *, now: datetime) -> None: ...


def _hours(value: int) -> str:
    return f"{value // 24} day{'s' if value >= 48 else ''}" if value >= 24 else f"{value} hours"


def _length(row) -> str:
    return f"{row['minutes']} minutes" if row["minutes"] else _hours(row["hours"])


def _span(row) -> timedelta:
    return timedelta(minutes=row["minutes"]) if row["minutes"] else timedelta(hours=row["hours"])


class ExemptionService:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, owner_user_id: str | None, penalties: Penalties, opening: Opening | None = None,
                 raise_overrun: Callable[[Transaction, dict, datetime, datetime], bool] | None = None, hygiene_allowance: Callable[[], tuple[int, int]] | None = None) -> None:
        """`opening`: None means no lock is configured, and an exemption pauses a window without opening anything. `raise_overrun(tx, row, relocked_at, now)` files a late relock as a
        signal, and returns False when it cannot; the keyholder then gets a plain note."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None
        self._penalties = penalties
        self._opening = opening
        self._raise_overrun = raise_overrun
        self._allowance = hygiene_allowance         # () -> (planned breaks a rolling week, minutes each); None means hygiene requests are not offered
        self._judge = None                          # exemptions.judge.ExemptionJudge: when set, requests are decided by it and never by a keyholder's button
        self._record_override = None                # (tx, row, now) -> bool: records "taken anyway" as a self-disclosed incident; None: not offered

    def set_override_signal(self, record_override: Callable[[Transaction, dict, datetime], bool]) -> None:
        """Owner, 2026-11-13: after a declined appeal the wearer may take it anyway, as an incident they disclose themselves. Built after this service."""
        self._record_override = record_override

    def set_judge(self, judge) -> None:
        """Owner, 2026-11-13: the wearer is also the account that got the keyholder's button. The judge is built after this service."""
        self._judge = judge

    def set_overrun_signal(self, raise_overrun: Callable[[Transaction, dict, datetime, datetime], bool]) -> None:
        """The detectors are built after this service."""
        self._raise_overrun = raise_overrun

    # ------------------------------------------------------------------ the wearer

    def start(self, user_id: str, now: datetime) -> OutgoingMessage:
        def write(tx: Transaction, _s: object) -> tuple[str, str | None]:
            row = tx.fetch_one("SELECT id, status FROM wear_exemptions WHERE user_id = ? AND status IN ('requested', 'approved')", (user_id,))
            if row is not None:
                return row["status"], row["id"]
            tx.execute("DELETE FROM wear_exemptions WHERE user_id = ? AND status = 'draft'", (user_id,))     # an abandoned draft holds nothing
            new_id = str(uuid4())
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at) VALUES (?, ?, 'draft', ?)", (new_id, user_id, _iso(now)))
            return "draft", new_id

        status, ident = apply_transition(self._core, write=write)
        if status == "requested":
            return OutgoingMessage(text=f"Your exemption request is {'still being decided' if self._judge is not None else 'with your keyholder'}. {SAFETY_LINE}")
        if status == "approved":
            return self._running_card(ident)
        return OutgoingMessage(text=f"Of course. What is it for? {SAFETY_LINE}",
                               actions=tuple(ActionSpec(action_id=f"exemption:cat_{code}:{ident}", label=label) for code, label in CATEGORIES.items()))

    def start_hygiene(self, user_id: str, now: datetime) -> OutgoingMessage | None:
        """Asking for a hygiene opening. Only offers buttons: nothing opens until one is pressed."""
        if self._allowance is None or self._opening is None:
            return None
        breaks, minutes = self._allowance()
        within = self._planned_this_week(user_id, now) < breaks

        def write(tx: Transaction, _s: object) -> tuple[str, str | None]:
            row = tx.fetch_one("SELECT id, status FROM wear_exemptions WHERE user_id = ? AND status IN ('requested', 'approved')", (user_id,))
            if row is not None:
                return row["status"], row["id"]
            tx.execute("DELETE FROM wear_exemptions WHERE user_id = ? AND status = 'draft'", (user_id,))
            new_id = str(uuid4())
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, kind, minutes) VALUES (?, ?, 'draft', ?, 'hygiene', ?)", (new_id, user_id, _iso(now), minutes))
            return "draft", new_id

        status, ident = apply_transition(self._core, write=write)
        if status == "draft":
            decision("exemptions", "Hygiene opening asked for: " + ("offered within this week's allowance." if within else "this week's allowance is used up."),
                     exemption_id=ident, minutes=minutes, planned_breaks_allowed=breaks)
        if status == "requested":
            return OutgoingMessage(text=f"Your earlier request is still with your keyholder. {SAFETY_LINE}")
        if status == "approved":
            return self._running_card(ident)
        health = ActionSpec(action_id=f"exemption:hhealth:{ident}", label="It's a health need", style="success")
        cancel = ActionSpec(action_id=f"exemption:hcancel:{ident}", label="Not now")
        if within:
            return OutgoingMessage(text=f"You can have a {minutes}-minute hygiene opening now. The combination appears in Chaster, not here. A health need never counts against anything.",
                                   actions=(ActionSpec(action_id=f"exemption:hopen:{ident}", label="Open it", style="primary"), health, cancel))
        if self._judge is not None:
            return OutgoingMessage(text="You've used this week's planned breaks, so another one needs a good reason. A health need is different: it never counts against anything.",
                                   actions=(ActionSpec(action_id=f"exemption:hask:{ident}", label="Ask for an extra one", style="primary"), health, cancel))
        return OutgoingMessage(text="You've used this week's planned breaks, so another one needs your keyholder. A health need is different: it never counts against anything.",
                               actions=(ActionSpec(action_id=f"exemption:hask:{ident}", label="Ask my keyholder", style="primary"), health, cancel))

    def press_hygiene(self, user_id: str, exemption_id: str, action: str, now: datetime) -> OutgoingMessage:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND user_id = ? AND status = 'draft' AND kind = 'hygiene'", (exemption_id, user_id))
        if row is None:
            return OutgoingMessage(text="That request is no longer open. Just ask again if you still need it.")
        if action == "hcancel":
            def drop(tx: Transaction, _s: object) -> None:
                tx.execute("DELETE FROM wear_exemptions WHERE id = ? AND status = 'draft'", (exemption_id,))

            apply_transition(self._core, write=drop)
            decision("exemptions", "Hygiene opening declined by the wearer; nothing was opened.", exemption_id=exemption_id)
            return OutgoingMessage(text="Okay, nothing was opened.")
        if action == "hask" or (action == "hopen" and self._planned_this_week(user_id, now) >= self._allowance()[0]):
            if self._judge is not None:
                def ask(tx: Transaction, _s: object) -> None:
                    tx.execute("UPDATE wear_exemptions SET reason_asked_at = ? WHERE id = ? AND status = 'draft'", (_iso(now), exemption_id))

                apply_transition(self._core, write=ask)
                decision("exemptions", "Extra hygiene opening asked for: waiting for the wearer's reason.", exemption_id=exemption_id)
                return OutgoingMessage(text="You've had this week's planned breaks. Why do you need another one now? I decide on that.")
            return self._ask_keyholder(row, now)
        health = action == "hhealth"
        minutes = HEALTH_MINUTES if health else row["minutes"]

        def approve(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE wear_exemptions SET status = 'approved', kind = ?, minutes = ?, requested_at = ?, decided_at = ?, decided_by = ?, expires_at = ? WHERE id = ?",
                       ("hygiene_health" if health else "hygiene", minutes, _iso(now), _iso(now), "wearer_health" if health else "allowance", _iso(now + timedelta(minutes=minutes)), exemption_id))

        apply_transition(self._core, write=approve)
        decision("exemptions", "Health opening approved (a health need never counts against anything)." if health else "Hygiene opening approved by the allowance.",
                 exemption_id=exemption_id, minutes=minutes, until=_iso(now + timedelta(minutes=minutes)))
        self.tick(now)
        with self._core.transaction() as tx:
            current = tx.fetch_one("SELECT opened_at, opening_outcome FROM wear_exemptions WHERE id = ?", (exemption_id,))
        if current["opened_at"] is None:
            return OutgoingMessage(text="I couldn't open it just now. You'll get a message as soon as I know more." + (f" {SAFETY_LINE}" if health else ""))
        text = "Opening your lock now: the combination appears in Chaster. Take the time you need, then relock there and press the button."
        return OutgoingMessage(text=text if health else f"Opening your lock now for {minutes} minutes: the combination appears in Chaster. When you're done, relock there and press the button.",
                               actions=self._back_button(exemption_id))

    def choose_category(self, user_id: str, exemption_id: str, code: str, now: datetime) -> OutgoingMessage:
        if code not in CATEGORIES:
            return OutgoingMessage(text="That isn't one of the options.")

        def write(tx: Transaction, _s: object) -> bool:
            return tx.execute("UPDATE wear_exemptions SET category = ? WHERE id = ? AND user_id = ? AND status = 'draft'", (code, exemption_id, user_id)).rowcount == 1

        if not apply_transition(self._core, write=write):
            return OutgoingMessage(text="That request is no longer open. Just ask again if you still need one.")
        return OutgoingMessage(text="How long do you need?",
                               actions=tuple(ActionSpec(action_id=f"exemption:h{hours}:{exemption_id}", label=_hours(hours)) for hours in DURATIONS))

    def choose_hours(self, user_id: str, exemption_id: str, hours: int, now: datetime) -> OutgoingMessage:
        if hours not in DURATIONS:
            return OutgoingMessage(text="That isn't one of the options.")

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT category FROM wear_exemptions WHERE id = ? AND user_id = ? AND status = 'draft' AND category IS NOT NULL", (exemption_id, user_id))
            if row is None:
                return False
            if self._judge is not None:
                tx.execute("UPDATE wear_exemptions SET hours = ?, reason_asked_at = ? WHERE id = ?", (hours, _iso(now), exemption_id))
                return True
            tx.execute("UPDATE wear_exemptions SET hours = ?, status = 'requested', requested_at = ? WHERE id = ?", (hours, _iso(now), exemption_id))
            if self._owner:
                enqueue_in_transaction(
                    tx, user_id=self._owner, kind="exemption_request", dedupe_key=f"exemption:request:{exemption_id}", now=now, immediate=True,
                    text=f"Exemption request: {CATEGORIES[row['category']]}, {_hours(hours)}. Decide on what it's for, and nothing else.",
                    actions=(ActionSpec(action_id=f"exemption:approve:{exemption_id}", label="Approve", style="success"),
                             ActionSpec(action_id=f"exemption:decline:{exemption_id}", label="Decline", style="secondary")))
            return True

        if not apply_transition(self._core, write=write):
            return OutgoingMessage(text="That request is no longer open. Just ask again if you still need one.")
        if self._judge is not None:
            decision("exemptions", "Exemption asked for: waiting for the wearer's reason.", exemption_id=exemption_id, hours=hours)
            return OutgoingMessage(text=REASON_QUESTION)
        return OutgoingMessage(text=f"Sent to your keyholder. I'll tell you as soon as they answer. {SAFETY_LINE}")

    # ------------------------------------------------------------------ the judge (owner, 2026-11-13)

    def awaiting_reason(self, user_id: str) -> bool:
        if self._judge is None:
            return False
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM wear_exemptions WHERE user_id = ? AND status = 'draft' AND reason_asked_at IS NOT NULL",
                                (user_id,)) is not None

    def give_reason(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """The wearer's next message after the reason question (or the follow-up question) is the reason. Then the judge decides."""
        if self._judge is None or not isinstance(text, str) or not text.strip():
            return None
        words = text.strip()[:1500]

        def write(tx: Transaction, _s: object) -> str | None:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE user_id = ? AND status = 'draft' AND reason_asked_at IS NOT NULL", (user_id,))
            if row is None:
                return None
            reason = words if row["follow_up"] is None else f"{row['reason'] or ''}\n---\n{words}"
            tx.execute("UPDATE wear_exemptions SET reason = ?, status = 'requested', requested_at = ? WHERE id = ?", (reason, _iso(now), row["id"]))
            return row["id"]

        ident = apply_transition(self._core, write=write)
        if ident is None:
            return None
        text_out, actions = self._rule(ident, now)
        return OutgoingMessage(text=text_out, actions=actions)

    def _declined_lately(self, row, now: datetime) -> list[str]:
        """The reasons of this wearer's requests the judge declined in the last day, oldest first: each new request is an appeal against them."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT reason FROM wear_exemptions WHERE user_id = ? AND id != ? AND status = 'declined' AND decided_by = 'judge' "
                                "AND decided_at > ? AND reason IS NOT NULL ORDER BY decided_at", (row["user_id"], row["id"], _iso(now - APPEAL_WINDOW)))
        return [r["reason"] for r in rows]

    def _rule(self, exemption_id: str, now: datetime) -> tuple[str, tuple[ActionSpec, ...]]:
        """Judges a 'requested' request that has a reason, and applies the ruling. Returns what to tell the wearer."""
        from exemptions.judge import Ruling

        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND status = 'requested'", (exemption_id,))
        if row is None:
            return "That request was already decided.", ()
        hygiene = row["kind"] == "hygiene"
        what = "an extra hygiene opening past this week's planned breaks" if hygiene else f"an exemption ({CATEGORIES.get(row['category'], 'something else')})"
        follow_up = row["follow_up"]
        first, _, answer = (row["reason"] or "").partition("\n---\n") if follow_up else (row["reason"] or "", "", "")
        declined = self._declined_lately(row, now)
        health = row["category"] == "health"
        from exemptions.judge import ExemptionRuling
        from infractions.judge import brings_nothing_new, is_health_or_safety
        ruling = None
        if declined and not health and not is_health_or_safety(row["reason"]):
            if len(declined) > MAX_APPEALS:
                ruling = ExemptionRuling(Ruling.DECLINE, "You've already used your appeal today.", by_code=True)
            elif brings_nothing_new(answer if follow_up else first, declined):
                ruling = ExemptionRuling(Ruling.DECLINE, "Your appeal doesn't tell me anything new.", by_code=True)
        if ruling is None:
            ruling = self._judge.judge(what=what, length=_length(row), reason=answer if follow_up else first, earlier=first if follow_up else None,
                                       appeal_of="\n---\n".join(declined) or None, health=health)
        if ruling is None:
            decision("exemptions", "Exemption request not judged yet (no usable answer from the model); retried on the next pass.", exemption_id=exemption_id)
            return f"I can't decide that right now. I'll tell you as soon as I have. {SAFETY_LINE}", ()
        if ruling.ruling is Ruling.UNCLEAR and follow_up is None:
            def ask(tx: Transaction, _s: object) -> None:
                tx.execute("UPDATE wear_exemptions SET status = 'draft', follow_up = ? WHERE id = ? AND status = 'requested'", (ruling.question, exemption_id))

            apply_transition(self._core, write=ask)
            decision("exemptions", "Exemption reason unclear: one follow-up question asked.", exemption_id=exemption_id, question=ruling.question)
            return ruling.question, ()
        approve = ruling.ruling is Ruling.APPROVE
        said = ruling.reason or ("Approved." if approve else "That isn't a reason to take it off.")
        if ruling.ruling is Ruling.UNCLEAR:
            said = "Still not specific enough to take it off."
        by = "judge_health" if ruling.by_code and approve else "judge"

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE wear_exemptions SET status = ?, decided_at = ?, decided_by = ?, expires_at = ?, verdict_reason = ? WHERE id = ? AND status = 'requested'",
                              ("approved" if approve else "declined", _iso(now), by, _iso(now + _span(row)) if approve else None, said, exemption_id)).rowcount == 1
            if done and self._owner and self._owner != row["user_id"]:       # a separate keyholder still hears about it
                enqueue_in_transaction(tx, user_id=self._owner, kind="exemption_request", dedupe_key=f"exemption:ruled:{exemption_id}", now=now, immediate=True,
                                       text=f"{'Approved' if approve else 'Declined'}: {what}, {_length(row)}. {said}")
            return done

        if not apply_transition(self._core, write=write):
            return "That request was already decided.", ()
        decision("exemptions", f"Request {'approved' if approve else 'declined'} by the judge.", exemption_id=exemption_id, by=by, reason=said)
        if not approve:
            if len(declined) < MAX_APPEALS:
                return f"Declined. {said} You can appeal once: ask again and tell me what's new. {SAFETY_LINE}", ()
            if self._record_override is None:
                return f"Declined. {said} That was your appeal for today. {SAFETY_LINE}", ()
            return (f"Declined. {said} That was your appeal. If you need it anyway, you can take it: the lock opens as usual, and it counts as an "
                    f"incident you disclosed yourself (it counts against you and gives a warning, nothing more). {SAFETY_LINE}",
                    (ActionSpec(action_id=f"exemption:anyway:{exemption_id}", label="Take it anyway", style="danger"),))
        self.tick(now)
        return f"{said} {self._approval_text(row)}", self._back_button(exemption_id)

    def take_anyway(self, user_id: str, exemption_id: str, now: datetime) -> OutgoingMessage:
        """Owner, 2026-11-13: a request declined after its appeal, taken anyway. It opens as an approved one would, and in the same transaction it is
        recorded as an incident the wearer disclosed themselves (warning only)."""
        if self._record_override is None:
            return OutgoingMessage(text="That isn't available right now.")

        def write(tx: Transaction, _s: object) -> str:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND user_id = ? AND status = 'declined' AND decided_by = 'judge'", (exemption_id, user_id))
            if row is None:
                return "gone"
            if now - parse_iso(row["decided_at"]) > ANYWAY_VALID:
                return "late"
            if tx.fetch_one("SELECT 1 FROM wear_exemptions WHERE user_id = ? AND status IN ('requested', 'approved')", (user_id,)):
                return "busy"
            tx.execute("DELETE FROM wear_exemptions WHERE user_id = ? AND status = 'draft'", (user_id,))
            tx.execute("UPDATE wear_exemptions SET status = 'approved', decided_at = ?, decided_by = 'wearer_override', expires_at = ? WHERE id = ? AND status = 'declined'",
                       (_iso(now), _iso(now + _span(row)), exemption_id))
            if not self._record_override(tx, dict(row), now):
                raise RuntimeError("the override could not be recorded")
            return "ok"

        try:
            outcome = apply_transition(self._core, write=write)
        except Exception:
            logger.exception("Taking an exemption anyway failed; nothing was opened or recorded.")
            return OutgoingMessage(text="I couldn't do that just now, so nothing was opened or recorded. Try again in a moment.")
        if outcome == "gone":
            return OutgoingMessage(text="That request is no longer open to take.")
        if outcome == "late":
            return OutgoingMessage(text="That was too long ago. Ask again if you still need it.")
        if outcome == "busy":
            return OutgoingMessage(text="You already have a request open or running.")
        decision("exemptions", "Declined request taken anyway by the wearer: opening, recorded as a self-disclosed incident (warning only).", exemption_id=exemption_id)
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ?", (exemption_id,))
        self.tick(now)
        return OutgoingMessage(text=f"Taken anyway, and recorded as you disclosing it yourself. {self._approval_text(row)}", actions=self._back_button(exemption_id))

    def _rule_pending(self, now: datetime) -> None:
        """Requests the judge could not decide when they came in: retried each pass; after UNJUDGED_LIMIT closed, never as a decline."""
        if self._judge is None:
            return
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT id, user_id, requested_at FROM wear_exemptions WHERE status = 'requested' AND reason IS NOT NULL")
        for row in rows:
            if now - parse_iso(row["requested_at"]) > UNJUDGED_LIMIT:
                def close(tx: Transaction, _s: object, row=row) -> None:
                    if tx.execute("UPDATE wear_exemptions SET status = 'declined', decided_at = ?, decided_by = 'unjudged' WHERE id = ? AND status = 'requested'",
                                  (_iso(now), row["id"])).rowcount == 1:
                        enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_decision", dedupe_key=f"exemption:unjudged:{row['id']}", now=now,
                                               immediate=True, text=f"I couldn't decide your request in time, so nothing changed. Ask again if you still need it. {SAFETY_LINE}")

                apply_transition(self._core, write=close)
                decision("exemptions", "Exemption request closed undecided: the judge gave no usable answer in time (not a decline).", exemption_id=row["id"])
                continue
            text, actions = self._rule(row["id"], now)
            with self._core.transaction() as tx:
                still = tx.fetch_one("SELECT status FROM wear_exemptions WHERE id = ?", (row["id"],))["status"]
            if still == "requested":
                continue                    # still not judged: nothing to tell yet

            def tell(tx: Transaction, _s: object, row=row, text=text, actions=actions) -> None:
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_decision", dedupe_key=f"exemption:ruling:{row['id']}", now=now, immediate=True,
                                       text=text, actions=actions)

            apply_transition(self._core, write=tell)

    def end_early(self, user_id: str, exemption_id: str, now: datetime) -> OutgoingMessage:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND user_id = ? AND status = 'approved'", (exemption_id, user_id))
        if row is None:
            return OutgoingMessage(text="That exemption is already over.")
        if row["opened_at"] is None:
            self._end(row, now, "wearer_returned")
            return OutgoingMessage(text="Welcome back. Your exemption has ended.")
        status = self._opening.status(opened_at=parse_iso(row["opened_at"])) if self._opening is not None else None
        if status is None:
            return OutgoingMessage(text="I can't check Chaster right now, so I can't confirm you're back yet. Please try again in a moment.", actions=self._back_button(exemption_id))
        if status.is_open:
            return OutgoingMessage(text="Chaster still shows your lock as open. Relock it there, then press this again.", actions=self._back_button(exemption_id))
        return OutgoingMessage(text=self._settle(row, status.relocked_at, now))

    # ------------------------------------------------------------------ the keyholder

    def decide(self, *, actor: str, exemption_id: str, approve: bool, now: datetime) -> ExemptionDecision:
        """Reads only the request: no Penalty Window, no Trust, no history (I7). The Penalty Window is touched afterwards by `tick()`, and only on an approval."""
        if self._owner is None or not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return ExemptionDecision.NOT_OWNER
        decision = ExemptionDecision.APPROVED if approve else ExemptionDecision.DECLINED

        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT * FROM wear_exemptions WHERE id = ? AND status = 'requested'", (exemption_id,))
            if row is None:
                return False
            if row["user_id"] == actor:         # live, 2026-11-13: the wearer got the keyholder's button and approved their own request
                return None
            expires = now + _span(row)
            tx.execute("UPDATE wear_exemptions SET status = ?, decided_at = ?, decided_by = ?, expires_at = ? WHERE id = ?",
                       (decision.value, _iso(now), actor, _iso(expires) if approve else None, exemption_id))
            if approve:
                enqueue_in_transaction(
                    tx, user_id=row["user_id"], kind="exemption_decision", dedupe_key=f"exemption:decision:{exemption_id}", now=now, immediate=True,
                    text=self._approval_text(row),
                    actions=self._back_button(exemption_id))
            else:
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_decision", dedupe_key=f"exemption:decision:{exemption_id}", now=now, immediate=True,
                                       text=f"Your {'hygiene opening' if row['kind'] == 'hygiene' else 'exemption'} wasn't approved this time. {SAFETY_LINE}")
            return True

        done = apply_transition(self._core, write=write)
        if done is None:
            _log("exemptions", "A request's own wearer pressed the keyholder's button: refused.", exemption_id=exemption_id)
            return ExemptionDecision.NOT_OWNER
        if not done:
            return ExemptionDecision.NOT_PENDING
        _log("exemptions", f"Request {decision.value} by the keyholder.", exemption_id=exemption_id)
        if approve:
            self.tick(now)
        return decision

    # ------------------------------------------------------------------ the periodic pass

    def tick(self, now: datetime) -> None:
        """Brings the Penalty Window in line with the exemptions in force. Idempotent and never raises: a failure is retried on the next pass."""
        try:
            self._rule_pending(now)
        except Exception:
            logger.exception("Pending exemption requests could not be judged; retrying on the next pass.")
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT * FROM wear_exemptions WHERE status = 'approved'")
        for row in rows:
            try:
                expired = parse_iso(row["expires_at"]) <= now
                if self._opening is not None and row["opened_at"] is None and not expired:
                    self._open(row, now)
                if expired and row["opened_at"] is not None:
                    self._after_time(row, now)
                elif expired:
                    self._end(row, now, "expired")
                else:
                    self._apply(row, now)
            except Exception:
                logger.exception("An exemption could not be applied; retrying on the next pass.")

    def in_force(self, user_id: str) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT id FROM wear_exemptions WHERE user_id = ? AND status = 'approved'", (user_id,))
        return None if row is None else row["id"]

    # ------------------------------------------------------------------ internals

    def _planned_this_week(self, user_id: str, now: datetime) -> int:
        """Openings the bot really made for planned breaks. A health need never appears here."""
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT COUNT(*) AS n FROM wear_exemptions WHERE user_id = ? AND kind = 'hygiene' AND opened_at IS NOT NULL AND opened_at > ?",
                                (user_id, _iso(now - ALLOWANCE_WINDOW)))["n"]

    def _ask_keyholder(self, row, now: datetime) -> OutgoingMessage:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE wear_exemptions SET status = 'requested', requested_at = ? WHERE id = ? AND status = 'draft'", (_iso(now), row["id"]))
            if self._owner:
                enqueue_in_transaction(tx, user_id=self._owner, kind="exemption_request", dedupe_key=f"exemption:request:{row['id']}", now=now, immediate=True,
                                       text=f"Hygiene opening request: {row['minutes']} minutes, past this week's planned breaks. A health need never comes here.",
                                       actions=(ActionSpec(action_id=f"exemption:approve:{row['id']}", label="Approve", style="success"),
                                                ActionSpec(action_id=f"exemption:decline:{row['id']}", label="Decline", style="secondary")))

        apply_transition(self._core, write=write)
        decision("exemptions", "Hygiene opening past the allowance sent to the keyholder.", exemption_id=row["id"], minutes=row["minutes"])
        return OutgoingMessage(text=f"Sent to your keyholder. I'll tell you as soon as they answer. {SAFETY_LINE}")

    def _approval_text(self, row) -> str:
        length = _length(row)
        if row["kind"] != "exemption":
            return f"Approved: a {length} hygiene opening. I'm opening your lock now, and the combination appears in Chaster. When you're done, relock there and press the button below."
        if self._opening is None:
            return (f"Your exemption is approved for {length}. A Penalty Window, if there is one, is paused for that time and continues where it left off. Any penalty freeze on your lock is "
                    "lifted as soon as the system is allowed to act. Tell me when you're back, or it simply ends on time.")
        return (f"Your exemption is approved for {length}. I'm opening your lock now: the combination appears in Chaster, not here. A Penalty Window, if there is one, is paused for that time. "
                "When you're back, relock in Chaster and press the button below.")

    @staticmethod
    def _back_button(exemption_id: str) -> tuple[ActionSpec, ...]:
        return (ActionSpec(action_id=f"exemption:end:{exemption_id}", label="I'm back", style="primary"),)

    def _open(self, row, now: datetime) -> None:
        """Claimed before the call. Tried once, except that a halt or a provable refusal leaves it claimable, so the opening goes out when the halt clears."""
        if row["opening_claimed_at"] is not None:
            return

        def claim(tx: Transaction, _s: object) -> bool:
            return tx.execute("UPDATE wear_exemptions SET opening_claimed_at = ?, opening_lock_id = ? WHERE id = ? AND status = 'approved' AND opening_claimed_at IS NULL",
                              (_iso(now), self._opening.lock_id, row["id"])).rowcount == 1

        if not apply_transition(self._core, write=claim):
            return
        result = self._opening.open_for(row["hours"] if row["hours"] else row["minutes"] / 60)
        opened = result.outcome.value == "opened"
        retry = result.outcome.value == "halted"
        decision("exemptions", f"Chaster opening: {result.outcome.value}.", exemption_id=row["id"], kind=row["kind"], detail=result.detail,
                 lock_id=self._opening.lock_id, saved_settings=result.saved)

        def record(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE wear_exemptions SET opened_at = ?, opening_saved_json = ?, opening_outcome = ?, opening_claimed_at = CASE WHEN ? THEN NULL ELSE opening_claimed_at END WHERE id = ?",
                       (_iso(now) if opened else None, json.dumps(result.saved) if result.saved else None, result.detail or result.outcome.value, 1 if retry else 0, row["id"]))
            if retry:
                text = "Your exemption is approved, but I'm not allowed to act on Chaster right now, so your lock isn't open yet. I'll open it as soon as I can."
            elif not opened:
                text = f"Your exemption is approved, but I couldn't open your lock. Your keyholder has been told. {SAFETY_LINE}"
                if self._owner:
                    enqueue_in_transaction(tx, user_id=self._owner, kind="exemption_opening", dedupe_key=f"exemption:failed:{row['id']}", now=now, immediate=True,
                                           text=f"An approved exemption could not open the lock ({result.detail or result.outcome.value}). Please open it by hand if it's needed.")
            else:
                return
            enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_opening", dedupe_key=f"exemption:opening:{row['id']}", now=now, immediate=True, text=text)

        apply_transition(self._core, write=record)

    def _after_time(self, row, now: datetime) -> None:
        """The approved time is over: relocked means settled, still open means one reminder, and an unreadable Chaster means trying again next pass."""
        status = self._opening.status(opened_at=parse_iso(row["opened_at"])) if self._opening is not None else None
        if status is None:
            return
        if not status.is_open:
            self._settle(row, status.relocked_at, now)
            return
        if row["kind"] == "hygiene_health":
            return                                  # a health need is not chased

        def remind(tx: Transaction, _s: object) -> None:
            if tx.execute("UPDATE wear_exemptions SET overdue_notified_at = ? WHERE id = ? AND overdue_notified_at IS NULL", (_iso(now), row["id"])).rowcount == 1:
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_overdue", dedupe_key=f"exemption:overdue:{row['id']}", now=now, immediate=True,
                                       text=f"Your exemption time is over and Chaster still shows your lock as open. Please relock it there. {SAFETY_LINE}", actions=self._back_button(row["id"]))

        apply_transition(self._core, write=remind)

    def _settle(self, row, relocked_at: datetime | None, now: datetime) -> str:
        """Closed on Chaster. On time ends quietly. Late goes to the signals path. A close with no log entry is not guessed at: the keyholder is told."""
        expires = parse_iso(row["expires_at"])
        late = relocked_at is not None and relocked_at > expires and row["kind"] != "hygiene_health"
        reason = "relocked_late" if late else "relocked_on_time" if relocked_at is not None else "closed_unverified"

        def record(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE wear_exemptions SET relocked_at = ? WHERE id = ?", (_iso(relocked_at) if relocked_at else None, row["id"]))
            filed = late and self._raise_overrun is not None and self._raise_overrun(tx, dict(row), relocked_at, now)
            if self._owner and ((late and not filed) or relocked_at is None):
                text = (f"An exemption's lock was relocked after the approved time ({_length(row)})." if late else
                        "An exemption's lock shows closed on Chaster, but its log has no relock entry, so I can't say when.")
                enqueue_in_transaction(tx, user_id=self._owner, kind="exemption_opening", dedupe_key=f"exemption:settled:{row['id']}", now=now, immediate=True, text=text)

        apply_transition(self._core, write=record)
        self._end(row, now, reason)
        restored = None
        if self._opening is not None and row["opening_saved_json"]:
            restored = self._opening.restore(json.loads(row["opening_saved_json"]))
            if not restored:
                logger.warning("The lock's own opening settings could not be put back yet.")
                self._queue_restore(row, now)
        decision("exemptions", f"Lock closed again on Chaster: {reason}.", exemption_id=row["id"], kind=row["kind"], approved_until=row["expires_at"],
                 relocked_at=_iso(relocked_at) if relocked_at else None, settings_restored=restored, sent_to_overrun_check=late or None)
        if late:
            return "Welcome back. You relocked after the approved time, so this goes to your keyholder. If it was a health or safety need, you'll be able to say so."
        if row["kind"] != "exemption":
            return "Thanks, Chaster shows you're relocked."
        return "Welcome back. Chaster confirms you relocked in time, and your exemption has ended." if relocked_at else "Welcome back. Your exemption has ended."

    def _queue_restore(self, row, now: datetime) -> None:
        if self._owner:
            def write(tx: Transaction, _s: object) -> None:
                enqueue_in_transaction(tx, user_id=self._owner, kind="exemption_opening", dedupe_key=f"exemption:restore:{row['id']}", now=now,
                                       text=f"I couldn't put the hygiene opening's own settings back after an exemption. They should be: {row['opening_saved_json']}.")

            apply_transition(self._core, write=write)

    def _apply(self, row, now: datetime) -> None:
        if row["kind"] != "exemption":
            return                                  # a hygiene break does not pause a Penalty Window
        window = self._penalties.get_active_or_frozen_penalty_window()
        if window is None:
            return
        with self._core.transaction() as tx:
            done = tx.fetch_one("SELECT 1 FROM freeze_periods WHERE penalty_window_id = ? AND exemption_id = ?", (window.id, row["id"]))
        if done is None:
            self._penalties.freeze(window.id, FreezeReason.TEMPORARY_WEAR_EXEMPTION, now=now, exemption_id=row["id"], expires_at=parse_iso(row["expires_at"]))

    def _end(self, row, now: datetime, reason: str) -> None:
        with self._core.transaction() as tx:
            open_freeze = tx.fetch_one("SELECT penalty_window_id FROM freeze_periods WHERE exemption_id = ? AND ended_at IS NULL", (row["id"],))
        if open_freeze is not None:
            self._penalties.resume(open_freeze["penalty_window_id"], FreezeReason.TEMPORARY_WEAR_EXEMPTION, now=now)

        def write(tx: Transaction, _s: object) -> None:
            cursor = tx.execute("UPDATE wear_exemptions SET status = 'ended', ended_at = ?, end_reason = ? WHERE id = ? AND status = 'approved'", (_iso(now), reason, row["id"]))
            if cursor.rowcount == 1:
                decision("exemptions", f"Exemption ended: {reason}.", exemption_id=row["id"], kind=row["kind"])
            if cursor.rowcount == 1 and reason == "expired":
                enqueue_in_transaction(tx, user_id=row["user_id"], kind="exemption_ended", dedupe_key=f"exemption:ended:{row['id']}", now=now, immediate=True,
                                       text="Your exemption has ended. Welcome back.")

        apply_transition(self._core, write=write)

    def _running_card(self, exemption_id: str) -> OutgoingMessage:
        return OutgoingMessage(text="You already have an exemption running. Tell me when you're back, or it simply ends on time.",
                               actions=(ActionSpec(action_id=f"exemption:end:{exemption_id}", label="I'm back", style="primary"),))
