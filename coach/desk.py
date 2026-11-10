"""
coach/desk.py

The coach's desk (v0.39 Phase J3): the deterministic path between what the language model asks for and the Goal Management service.

  model output -> validate_action -> the gates in `submit` -> a pending `coach_proposals` row -> a card -> the USER's button -> GoalManager

Philosophy 2.5 is the order of these steps: proposal, explanation, user approval, application. Nothing the model writes changes anything. Accepting
applies exactly the recorded payload, never anything reconstructed later (GOAL-6), and an adaptation goes through GoalManager's own change proposal.

The gates, each a reason code in `coach_refusals` (never the text):
  not_this_user    the coach serves one wearer per database (goals have no user column)
  paused           a vacation is in force
  safety_topic     the user's message mentions pain, injury, distress or an emergency: no negotiation, safety comes first
  regime_topic / unsafe_content   coach/safety.py
  locked           an adaptation unless the wearer is known to be unlocked (a new goal or commitment is fine; an agreed target stays frozen)
  unknown_goal, too_many_goals, too_many_commitments
  other_kind_pending  while a proposal waits, only a revised one of the same kind may replace it: a counter-offer stays what it was
  already_waiting  the same proposal again: no second card, and it does not use up the day's limit
  daily_limit      at most MAX_PROPOSALS_PER_DAY a local day, whatever became of them
  declined_recently  nothing like a declined proposal again within DECLINE_MEMORY (2.5: no re-proposing after a rejection)

A commitment reported as NOT KEPT is a minor Trust reduction and nothing else (owner, 2026-11-04): the `coach.commitment_missed` event, whose only consumer is
the Trust Manager. It is not goal evidence, so it is no Incident, no Penalty Window, no freeze and no token taken away. Kept, partly kept and unanswered commitments
do nothing, and neither does a report during a vacation. This module imports no execution, release, penalty or Trust code (guard test), and reads no Trust state:
nothing it returns can carry it.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Callable, Mapping

from coach.actions import ActionKind, Refusal, ValidatedAction, validate_action
from coach.safety import content_refusal, fold
from infrastructure.text import plain_text
from goal_management.models import GoalInterventionType
from goal_management.repository import GoalManager
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from supervision.checkin_evaluator import mentions_safety_issue

logger = logging.getLogger("ai_coach_keyholder.coach.desk")

__all__ = [
    "CoachDesk", "Proposal", "SubmitResult", "Commitment", "Outcome", "MAX_PROPOSALS_PER_DAY", "MAX_COMMITMENTS_PER_DAY", "MAX_ACTIVE_GOALS",
    "PROPOSAL_TTL", "DECLINE_MEMORY", "OUTCOME_WINDOW", "without_day_words",
]

MAX_PROPOSALS_PER_DAY = 3
MAX_COMMITMENTS_PER_DAY = 3
MAX_ACTIVE_GOALS = 5
PROPOSAL_TTL = timedelta(hours=24)
DECLINE_MEMORY = timedelta(days=14)
OUTCOME_WINDOW = timedelta(days=3)               # how long an unanswered commitment is still asked about; then it lapses, at no cost
PROGRESS_WINDOW = timedelta(days=14)
TASK_WINDOW = timedelta(days=7)
_SIMILAR = 0.6
_WORD = re.compile(r"[a-z0-9]+")
_STALE_CLAIM = timedelta(minutes=5)
EVENING_HOUR = 18                       # from here on, planning is for tomorrow
# Not a commitment: what happens anyway, or no plan at all (owner, 2026-11-10: "let's say go to sleep").
# Only the bare phrase: "Sleep 8 hours" is a plan, "go to sleep" is not.
_FILLER = re.compile(r"^\s*(just\s+)?(go(ing)?\s+to\s+(sleep|bed)|sleep|rest|relax|chill(\s+out)?|take\s+it\s+easy|try(ing)?\s+to\s+(sleep|rest|relax)|"
                     r"get\s+(some\s+)?(sleep|rest)|have\s+a\s+(good\s+)?(night|rest))(\s+(early|tonight|now|well|soon|a\s+bit|properly|today))?\s*[.!]*\s*$",
                     re.IGNORECASE)
_DAY_WORDS = re.compile(r"\s*,?\s*\b(?:(?:for |by )?(?:today|tomorrow|tonight)|this (?:morning|afternoon|evening))\b(?!['\u2019\u00b4])", re.IGNORECASE)


def without_day_words(text: str) -> str:
    """A commitment has its own date, so "today" or "tomorrow" in its text turns false the next day: "Do a full workout tomorrow" was read back on its own
    day as the next day's. The words are dropped when it is stored and when an older one is shown. Unchanged if nothing else would be left."""
    cleaned = re.sub(r"\s{2,}", " ", _DAY_WORDS.sub("", text)).strip(" ,;-")
    return cleaned or text


class Outcome(StrEnum):
    KEPT = "kept"
    PARTLY = "partly"
    NOT_KEPT = "not_kept"


@dataclass(frozen=True, kw_only=True)
class Proposal:
    id: str
    kind: ActionKind
    payload: Mapping[str, str | None]


@dataclass(frozen=True, kw_only=True)
class SubmitResult:
    proposal: Proposal | None = None
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class Commitment:
    id: str
    for_date: date
    text: str


def _fingerprint(texts: tuple[str, ...]) -> str:
    return " ".join(sorted({w for w in _WORD.findall(fold(" ".join(texts)).lower()) if len(w) > 2 or w.isdigit()}))


def _similar(a: str, b: str) -> bool:
    left, right = set(a.split()), set(b.split())
    if {w for w in left if w.isdigit()} != {w for w in right if w.isdigit()}:
        return False                                 # 25 minutes is a counter-offer to 30 minutes, not the same proposal again
    return bool(left and right) and len(left & right) / len(left | right) >= _SIMILAR


class CoachDesk:
    def __init__(self, core: CoreDatabase, *, user_id: str, trust_domain: str, goals: GoalManager, today: Callable[[datetime], date],
                 targets_frozen: Callable[[str], bool] = lambda _user: False, paused: Callable[[datetime], bool] = lambda _now: False,
                 active_task: Callable[[str], tuple[str, str | None] | None] = lambda _user: None) -> None:
        """`active_task(user_id)` returns (assignment id, title) or None. `trust_domain` only labels a new goal; it is never shown or scored here."""
        self._core = core
        self.user_id = user_id
        self._domain = trust_domain
        self._goals = goals
        self.today = today
        self.time_of_day: Callable[[datetime], str] | None = None      # a coarse word for the model; set by build_coach
        self.local_now: Callable[[datetime], datetime] | None = None   # the user's local time; set by build_coach (None: UTC)
        self._targets_frozen = targets_frozen
        self._paused = paused
        self._active_task = active_task

    # ------------------------------------------------------------------ model side

    def may_propose(self, user_id: str, user_message: str, now: datetime) -> bool:
        """Whether this turn offers the model the proposal field at all. `submit` still checks everything: this only saves the instruction."""
        if self._turn_refusal(user_id, user_message, now) is not None:
            return False
        return self._proposals_today(now) < MAX_PROPOSALS_PER_DAY

    def submit(self, subject_key: str, raw: object, now: datetime, user_message: str = "") -> SubmitResult:
        """Validates one model request and runs it through the gates. Never raises: a failure is a refusal, the reply is unaffected."""
        try:
            action = validate_action(raw)
            name = raw.get("action") if isinstance(raw, Mapping) else None
            if isinstance(action, Refusal):
                return self._refuse(subject_key, name, action.reason, now)
            reason = self._turn_refusal(subject_key, user_message, now) or content_refusal(action.texts())
            if reason is not None:
                return self._refuse(subject_key, name, reason, now)
            self._expire(now)
            payload = self._resolve(action, subject_key, now)
            if isinstance(payload, Refusal):
                return self._refuse(subject_key, name, payload.reason, now)
            fingerprint = _fingerprint(action.texts())
            limit = self._limit_refusal(action.kind, fingerprint, now)
            if limit is not None:
                return self._refuse(subject_key, name, limit, now)
            return SubmitResult(proposal=self._store(action.kind, payload, fingerprint, now))
        except Exception:
            logger.exception("A coach proposal could not be processed; the reply is unaffected.")
            return SubmitResult(reason="internal_error")

    def _turn_refusal(self, user_id: str, user_message: str, now: datetime) -> str | None:
        if user_id != self.user_id:
            return "not_this_user"
        if self._paused(now):
            return "paused"
        if mentions_safety_issue(user_message or ""):
            return "safety_topic"
        return None

    def _resolve(self, action: ValidatedAction, user_id: str, now: datetime) -> dict | Refusal:
        """Turns the model's references into stored facts, so the card and the acceptance use the same exact content."""
        goals = self.active_goals()
        goal = None
        if action.goal_ref is not None:
            if action.goal_ref > len(goals):
                return Refusal(reason="unknown_goal")
            goal = goals[action.goal_ref - 1]
        if action.kind is ActionKind.PROPOSE_GOAL:
            if len(goals) >= MAX_ACTIVE_GOALS:
                return Refusal(reason="too_many_goals")
            return {"title": action.title, "target": action.target}
        if action.kind is ActionKind.ADAPT_GOAL:
            if self._targets_frozen(user_id):
                return Refusal(reason="locked")
            return {"goal_group_id": goal["id"], "goal_title": goal["title"], "target": action.target, "reason": action.reason}
        if _FILLER.search(plain_text(action.text)):
            return Refusal(reason="filler")
        today = self.today(now)
        # From the evening on, the day is done: whatever is planned belongs to tomorrow, whatever the model said (owner, 2026-11-10).
        for_date = today if action.when == "today" and not self._evening(now) else today + timedelta(days=1)
        task = self._active_task(user_id) if action.active_task else None      # only a link: without an active task the commitment stands on its own
        task_id, task_title = task if task is not None else (None, None)
        with self._core.transaction() as tx:
            count = tx.fetch_one("SELECT COUNT(*) AS n FROM daily_commitments WHERE user_id = ? AND for_date = ?",
                                 (self.user_id, for_date.isoformat()))["n"]
        if count >= MAX_COMMITMENTS_PER_DAY:
            return Refusal(reason="too_many_commitments")
        return {"text": without_day_words(action.text), "for_date": for_date.isoformat(), "goal_group_id": goal["id"] if goal else None,
                "goal_title": goal["title"] if goal else None, "task_assignment_id": task_id, "task_title": task_title}

    def _limit_refusal(self, kind: ActionKind, fingerprint: str, now: datetime) -> str | None:
        with self._core.transaction() as tx:
            waiting = tx.fetch_one("SELECT kind, fingerprint FROM coach_proposals WHERE user_id = ? AND status IN ('pending', 'claimed')", (self.user_id,))
            if waiting is not None and waiting["kind"] != kind.value:
                return "other_kind_pending"
            if waiting is not None and waiting["fingerprint"] == fingerprint:
                return "already_waiting"
            declined = tx.fetch_all("SELECT fingerprint FROM coach_proposals WHERE user_id = ? AND kind = ? AND status = 'declined' AND resolved_at >= ?",
                                    (self.user_id, kind.value, _iso(now - DECLINE_MEMORY)))
        if any(_similar(fingerprint, r["fingerprint"]) for r in declined):
            return "declined_recently"
        return "daily_limit" if self._proposals_today(now) >= MAX_PROPOSALS_PER_DAY else None

    def _proposals_today(self, now: datetime) -> int:
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT COUNT(*) AS n FROM coach_proposals WHERE user_id = ? AND local_date = ?",
                                (self.user_id, self.today(now).isoformat()))["n"]

    def _store(self, kind: ActionKind, payload: dict, fingerprint: str, now: datetime) -> Proposal:
        proposal_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE coach_proposals SET status = 'superseded', resolved_at = ? WHERE user_id = ? AND status = 'pending'",
                       (_iso(now), self.user_id))
            tx.execute("INSERT INTO coach_proposals (id, user_id, local_date, kind, payload_json, fingerprint, status, created_at, expires_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                       (proposal_id, self.user_id, self.today(now).isoformat(), kind.value, json.dumps(payload, ensure_ascii=False), fingerprint,
                        _iso(now), _iso(now + PROPOSAL_TTL)))
            self._event(tx, "coach.proposal_created", now, proposal_id=proposal_id, kind=kind.value)

        apply_transition(self._core, write=write)
        return Proposal(id=proposal_id, kind=kind, payload=payload)

    def _refuse(self, user_id: str, name: object, reason: str, now: datetime) -> SubmitResult:
        action = name if isinstance(name, str) and name in tuple(ActionKind) else "other"

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("INSERT INTO coach_refusals (id, user_id, local_date, action, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), user_id, self.today(now).isoformat(), action, reason, _iso(now)))

        apply_transition(self._core, write=write)
        logger.info("A coach proposal was refused.", extra={"error_code": "coach_refused", "reason": reason})
        return SubmitResult(reason=reason)

    # ------------------------------------------------------------------ the user's buttons

    def decide(self, user_id: str, proposal_id: str, *, accept: bool, now: datetime) -> str:
        """The ONLY way a proposal takes effect. Returns the text for the user. A second press, an expired card or someone else's press changes
        nothing."""
        self._expire(now)
        row = self._proposal(proposal_id)
        if row is None or row["user_id"] != user_id or user_id != self.user_id:
            return "That proposal isn't yours to decide."
        if row["status"] != "pending":
            return {"accepted": "You already accepted that.", "declined": "You already declined that.",
                    "superseded": "That proposal was replaced by a newer one.", "expired": "That proposal has expired."}.get(row["status"], "That proposal is being handled.")
        if not accept:
            return "Declined. I won't press it." if self._close(proposal_id, "pending", "declined", now) else "That proposal is being handled."
        kind, payload = ActionKind(row["kind"]), json.loads(row["payload_json"])
        if kind is ActionKind.PROPOSE_COMMITMENT:
            return self._accept_commitment(row, payload, now)
        if kind is ActionKind.ADAPT_GOAL and self._targets_frozen(user_id):
            return "Not while you are locked. An agreed target stays as it is until you are known to be unlocked. Your card stays open until it expires."
        return self._accept_goal_change(row, kind, payload, now)

    def _accept_commitment(self, row, payload: dict, now: datetime) -> str:
        commitment_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE coach_proposals SET status = 'accepted', resolved_at = ?, result_id = ? WHERE id = ? AND status = 'pending'",
                              (_iso(now), commitment_id, row["id"])).rowcount
            if not done:
                return False
            tx.execute("INSERT INTO daily_commitments (id, user_id, for_date, text, goal_group_id, task_assignment_id, proposal_id, status, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                       (commitment_id, self.user_id, payload["for_date"], payload["text"], payload.get("goal_group_id"),
                        payload.get("task_assignment_id"), row["id"], _iso(now)))
            self._event(tx, "coach.proposal_resolved", now, proposal_id=row["id"], resolution="accepted")
            return True

        return "Done, that's your commitment." if apply_transition(self._core, write=write) else "That proposal is being handled."

    def _accept_goal_change(self, row, kind: ActionKind, payload: dict, now: datetime) -> str:
        """GoalManager has its own transactions, so the proposal is CLAIMED first: a double press applies once. A failure puts the card back."""
        if not self._close(row["id"], "pending", "claimed", now):
            return "That proposal is being handled."
        try:
            if kind is ActionKind.PROPOSE_GOAL:
                result_id = self._goals.create_goal(payload["title"], payload["target"], self._domain, "coach_proposed_user_approved", now=now).goal_group_id
                text = "Done, that's a goal now."
            else:
                change = self._goals.create_change_proposal(
                    payload["goal_group_id"], GoalInterventionType.ADAPT_TARGET, payload["reason"], now + PROPOSAL_TTL,
                    proposed_target_description=payload["target"], now=now)
                self._goals.accept_proposal(change.id, now=now)
                result_id, text = payload["goal_group_id"], "Done, the target is changed."
        except Exception:
            logger.exception("Applying an accepted coach proposal failed; it goes back to pending.")
            self._close(row["id"], "claimed", "pending", now, reopen=True)
            return "That didn't work, nothing was changed. Please try again."

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE coach_proposals SET status = 'accepted', resolved_at = ?, result_id = ? WHERE id = ? AND status = 'claimed'",
                       (_iso(now), result_id, row["id"]))
            self._event(tx, "coach.proposal_resolved", now, proposal_id=row["id"], resolution="accepted")

        apply_transition(self._core, write=write)
        return text

    def record_outcome(self, user_id: str, commitment_id: str, outcome: Outcome, now: datetime) -> str:
        """The user's own report on a commitment. Not kept also raises `coach.commitment_missed` for the Trust Manager, unless a vacation is in force."""
        counts = outcome is Outcome.NOT_KEPT and not self._paused(now)

        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE daily_commitments SET status = ?, resolved_at = ? WHERE id = ? AND user_id = ? AND status = 'open'",
                              (outcome.value, _iso(now), commitment_id, user_id)).rowcount
            if done:
                self._event(tx, "coach.commitment_resolved", now, commitment_id=commitment_id, outcome=outcome.value)
                if counts:
                    self._event(tx, "coach.commitment_missed", now, commitment_id=commitment_id, trust_domain=self._domain)
            return bool(done)

        if user_id != self.user_id or not apply_transition(self._core, write=write):
            return "That commitment isn't open."
        return {Outcome.KEPT: "Noted, well done.", Outcome.PARTLY: "Noted. Part of it still counts for you.",
                Outcome.NOT_KEPT: "Noted, thanks for being straight about it."}[outcome]

    def forget_all(self, user_id: str) -> int:
        """`forget all`: the proposals, refusal codes and commitments of this user. Goals are agreed records with an append-only history and stay."""
        def write(tx: Transaction, _s: object) -> int:
            erased = tx.execute("DELETE FROM daily_commitments WHERE user_id = ?", (user_id,)).rowcount
            erased += tx.execute("DELETE FROM coach_proposals WHERE user_id = ?", (user_id,)).rowcount
            tx.execute("DELETE FROM coach_refusals WHERE user_id = ?", (user_id,))
            return erased

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ reads

    def active_goals(self) -> list[dict]:
        """Numbered by this order in the prompt, so a goal number from the model can only ever point at an active goal."""
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT g.goal_group_id, v.title, v.target_description FROM goals g JOIN goal_versions v ON v.id = g.current_version_id "
                                "WHERE g.status = 'active' AND g.archived_at IS NULL ORDER BY g.created_at, g.goal_group_id")
        return [{"id": r["goal_group_id"], "title": r["title"], "target": r["target_description"]} for r in rows]

    def pending_proposal(self, now: datetime) -> Proposal | None:
        self._expire(now)
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM coach_proposals WHERE user_id = ? AND status = 'pending'", (self.user_id,))
        return None if row is None else Proposal(id=row["id"], kind=ActionKind(row["kind"]), payload=json.loads(row["payload_json"]))

    def commitment_to_review(self, now: datetime, *, include_today: bool = False) -> Commitment | None:
        """The oldest open commitment from before today, or from today too when the user just said it is done. Anything older than OUTCOME_WINDOW lapses first: unanswered costs nothing."""
        today = self.today(now)
        oldest = (today - OUTCOME_WINDOW).isoformat()

        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE daily_commitments SET status = 'lapsed', resolved_at = ? WHERE user_id = ? AND status = 'open' AND for_date < ?",
                       (_iso(now), self.user_id, oldest))

        apply_transition(self._core, write=write)
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT id, for_date, text FROM daily_commitments WHERE user_id = ? AND status = 'open' AND for_date < ? "
                               "ORDER BY for_date, created_at LIMIT 1", (self.user_id, (today + timedelta(days=1) if include_today else today).isoformat()))
        return None if row is None else Commitment(id=row["id"], for_date=date.fromisoformat(row["for_date"]), text=without_day_words(row["text"]))

    def context(self, user_id: str, now: datetime) -> dict | None:
        """Everything the model sees about goals, commitments and tasks. Plain facts for this user; no Trust, no ids, no domain name."""
        if user_id != self.user_id:
            return None
        today = self.today(now)
        goals = []
        active = self.active_goals()
        open_proposal = _describe(self.pending_proposal(now))
        with self._core.transaction() as tx:
            for number, goal in enumerate(active, start=1):
                evidence = tx.fetch_all("SELECT outcome, COUNT(*) AS n FROM goal_evidence WHERE goal_group_id = ? AND period_end >= ? GROUP BY outcome",
                                        (goal["id"], _iso(now - PROGRESS_WINDOW)))
                kept = tx.fetch_all("SELECT status, COUNT(*) AS n FROM daily_commitments WHERE goal_group_id = ? AND for_date >= ? GROUP BY status",
                                    (goal["id"], (today - PROGRESS_WINDOW).isoformat()))
                goals.append({"number": number, "title": goal["title"], "target": goal["target"],
                              "evidence": {r["outcome"]: r["n"] for r in evidence}, "commitments": {r["status"]: r["n"] for r in kept}})
            commitments = tx.fetch_all("SELECT for_date, text, status FROM daily_commitments WHERE user_id = ? AND for_date >= ? ORDER BY for_date, created_at",
                                       (self.user_id, (today - timedelta(days=1)).isoformat()))
            tasks = tx.fetch_all("SELECT a.status, v.title FROM task_assignments a JOIN task_template_versions v ON v.template_id = a.template_id "
                                 "AND v.version = a.template_version WHERE a.user_id = ? AND a.resolved_at >= ? ORDER BY a.resolved_at DESC",
                                 (self.user_id, _iso(now - TASK_WINDOW)))
        return {
            "time_of_day": self.time_of_day(now) if self.time_of_day is not None else None,
            "goals": goals,
            "commitments": [{"day": _day_word(date.fromisoformat(c["for_date"]), today), "text": without_day_words(c["text"]), "status": c["status"]} for c in commitments],
            "tasks": {"completed": sum(1 for t in tasks if t["status"] == "completed"), "cancelled": sum(1 for t in tasks if t["status"] == "cancelled"),
                      "recent": [{"title": t["title"], "status": t["status"]} for t in tasks[:3] if t["title"]]},
            "open_proposal": open_proposal,
        }

    def prompt_suffix(self, now: datetime) -> str:
        """Added to a scheduled check-in question (never to the release check-in, whose wording is the owner's). Before the evening it asks for today's
        plan when there is none; from the evening on, for tomorrow's (owner, 2026-11-10: plan the day ahead, never filler for tonight)."""
        today = self.today(now)
        evening = self._evening(now)
        with self._core.transaction() as tx:
            rows = tx.fetch_all("SELECT for_date, text FROM daily_commitments WHERE user_id = ? AND for_date IN (?, ?) AND status IN ('open', 'kept', 'partly') "
                                "ORDER BY created_at", (self.user_id, today.isoformat(), (today + timedelta(days=1)).isoformat()))
        todays = [without_day_words(r["text"]) for r in rows if r["for_date"] == today.isoformat()]
        tomorrows = [without_day_words(r["text"]) for r in rows if r["for_date"] != today.isoformat()]
        if not evening:
            return f' Today\'s plan: "{"; ".join(todays)}".' if todays else " And what are you getting done today? Give me one or two concrete things."
        said = f' Today you said: "{"; ".join(todays)}".' if todays else ""
        return said + (f' Tomorrow: "{"; ".join(tomorrows)}".' if tomorrows else " What's the plan for tomorrow?")

    def _evening(self, now: datetime) -> bool:
        local = self.local_now(now) if self.local_now is not None else now
        return local.hour >= EVENING_HOUR or local.hour < 4

    # ------------------------------------------------------------------ internals

    def _proposal(self, proposal_id: str):
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT * FROM coach_proposals WHERE id = ?", (proposal_id,))

    def _expire(self, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE coach_proposals SET status = 'expired', resolved_at = ? WHERE user_id = ? AND status = 'pending' AND expires_at < ?",
                       (_iso(now), self.user_id, _iso(now)))
            tx.execute("UPDATE coach_proposals SET status = 'expired', resolved_at = ? WHERE user_id = ? AND status = 'claimed' AND resolved_at < ?",
                       (_iso(now), self.user_id, _iso(now - _STALE_CLAIM)))

        apply_transition(self._core, write=write)

    def _close(self, proposal_id: str, from_status: str, to_status: str, now: datetime, *, reopen: bool = False) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            done = tx.execute("UPDATE coach_proposals SET status = ?, resolved_at = ? WHERE id = ? AND status = ?",
                              (to_status, None if reopen else _iso(now), proposal_id, from_status)).rowcount
            if done and to_status == "declined":
                self._event(tx, "coach.proposal_resolved", now, proposal_id=proposal_id, resolution="declined")
            return bool(done)

        return apply_transition(self._core, write=write)

    @staticmethod
    def _event(tx: Transaction, event_type: str, now: datetime, **payload: str) -> None:
        write_event(tx, DomainEvent(event_type=event_type, source_module="coach", payload=payload, occurred_at=now))


def _day_word(day: date, today: date) -> str:
    return {-1: "yesterday", 0: "today", 1: "tomorrow"}.get((day - today).days, day.isoformat())


def _describe(proposal: Proposal | None) -> str | None:
    if proposal is None:
        return None
    payload = proposal.payload
    if proposal.kind is ActionKind.PROPOSE_GOAL:
        return f"new goal: {payload['title']}: {payload['target']}"
    if proposal.kind is ActionKind.ADAPT_GOAL:
        return f"new target for {payload['goal_title']}: {payload['target']}"
    return f"commitment: {payload['text']}"

