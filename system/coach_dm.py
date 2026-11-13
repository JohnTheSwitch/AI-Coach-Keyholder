"""
system/coach_dm.py

The coach in the Discord DM (v0.39 Phase J3): the port the Conversation Engine hands a model's `proposal` to, the cards and buttons, and the wiring.

- `guidance(...)` says whether a turn is a coach turn and what the model is told. The check-in reply gets the negotiation instruction.
- `present(...)` adds the card. Everything on a card comes from the stored proposal, never from the model's sentence, so the user accepts exactly what they read.
- `press(...)` is the bot's door for a button. The presser's Discord identity is resolved here and must be the proposal's own user.

Multi-wearer tenants are not wired: a tenant's invitation does not disclose the coach, and goals are per database.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, time
from typing import Callable

from application.models import ActionSpec, OutgoingMessage
from coach.actions import ACTION_INSTRUCTIONS, ACTION_SCHEMAS, ActionKind
from coach.safety import reports_done, strip_record_claims
from coach.desk import Commitment, CoachDesk, Outcome, Proposal, SubmitResult, is_night_commitment
from coaching_memory.service import MAX_CONTENT, WriteOutcome
from goal_management.repository import GoalManager
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from iron_contract.guard import IronContract, LockStatus
from supervision.checkin_evaluator import mentions_safety_issue

logger = logging.getLogger("ai_coach_keyholder.system.coach_dm")

__all__ = ["CoachDm", "build_coach", "goal_targets_frozen", "COACH_GUIDANCE", "CHECK_IN_GUIDANCE", "OUTCOME_BUTTONS"]

COACH_GUIDANCE = (
    "GOALS AND COMMITMENTS. You help the user set their own goals and small daily commitments. Offer only what they said they want or could do, in their own "
    "terms, and small enough to really happen. Never anything about the lock, time, release, rules, tokens or punishment, and never anything that could hurt "
    "them. Small talk gets no proposal. Change a goal's target only when they say the goal itself no longer fits. If a proposal is waiting and they want it changed, "
    "send the same action again with their change, and do not treat it as settled. If they say no, let it go. A goal is an "
    "aspiration, not an obligation: a missed one is something to understand together, never something to blame them for. Rule 7 still holds for time, "
    "deadlines, assigned tasks and check-ins. While they are locked they can still add goals and commitments, but an agreed goal's target stays as it is. "
    "Never ask whether they kept a commitment and never say it is recorded: when they tell you they did it, just be glad with them, and the app shows them buttons for that. "
    "A commitment listed as open only means those buttons were not pressed yet. It says nothing about whether they did it: never doubt them, and never tell them to do it again. "
    "Mind the time of day you are told. Help them PLAN: before the evening, today's one or two concrete goals; from the evening on, tomorrow's. "
    "Late in the evening, do not push anything new for tonight. A commitment is real work toward a goal, specific enough to check (what, how much, "
    "when): never filler like sleeping, resting or relaxing, and never vague like \"try to be better\". Do not suggest again what they already said they did."
)
CHECK_IN_GUIDANCE = (
    "This message answers today's check-in, which asked what they got done today and how they are holding up. It is their report on their day, not a "
    "goal and not a request: never call it wrong, not enough or \"not the goal\", never correct or doubt what they did, and never turn it into a "
    "lecture about their goals. Say in one short, warm sentence what you heard. Then, if they named something they could do, or you can see a real "
    "next step toward one of their goals, use `proposal` for exactly one commitment: for today before the evening, for tomorrow from the evening on, using "
    "their own words and numbers. Do not ask them to repeat it."
)

_MAX_MESSAGE = 1950                                       # a Discord message holds 2000 characters
OUTCOME_BUTTONS = (("kept", "Kept", "success", Outcome.KEPT), ("partly", "Partly", "secondary", Outcome.PARTLY),
                   ("missed", "Not kept", "secondary", Outcome.NOT_KEPT))
_OUTCOMES = {key: outcome for key, _label, _style, outcome in OUTCOME_BUTTONS}


def _weekday(day: date) -> str:
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def proposal_card(proposal: Proposal, parts: tuple[str, ...] = ()) -> tuple[str, tuple[ActionSpec, ...]]:
    payload = proposal.payload
    if proposal.kind is ActionKind.PROPOSE_GOAL:
        text = f"**Proposed goal:** {payload['title']}\nTarget: {payload['target']}"
    elif proposal.kind is ActionKind.ADAPT_GOAL:
        text = f"**Proposed new target for \"{payload['goal_title']}\":** {payload['target']}\nWhy: {payload['reason']}"
    else:
        text = f"**Proposed commitment for {_weekday(date.fromisoformat(payload['for_date']))}:** {payload['text']}"
        if payload.get("goal_title"):
            text += f"\nGoal: {payload['goal_title']}"
        if payload.get("task_title"):
            text += f"\nTask: {payload['task_title']}"
        if parts == ("night",):
            text += "\nIt's kept at night, so I'll ask you the next morning."
        elif parts:
            # Owner, 2026-11-13: accepting picks the part of the day, so the bot asks "Done?" when it ends (one tap, no extra question).
            text += "\nWhen will you do it? I'll ask you when that time ends."
            return text, tuple(ActionSpec(action_id=f"coach:{_PART_ACTIONS[p]}:{proposal.id}", label=_PART_LABELS[p], style="success") for p in parts) + (
                ActionSpec(action_id=f"coach:decline:{proposal.id}", label="Decline", style="secondary"),)
    return text, (ActionSpec(action_id=f"coach:accept:{proposal.id}", label="Accept", style="success"),
                  ActionSpec(action_id=f"coach:decline:{proposal.id}", label="Decline", style="secondary"))


_PART_ACTIONS = {"morning": "morning", "afternoon": "afternoon", "evening": "evening", "any": "accept", "night": "accept"}
_PART_LABELS = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "any": "Any time", "night": "Accept"}
_ACTION_PARTS = {"morning": "morning", "afternoon": "afternoon", "evening": "evening", "accept": "any"}
_PART_WORDS = {"morning": "this morning's", "afternoon": "this afternoon's", "evening": "this evening's", "any": "today's"}
ASK_BUTTONS = (("kept", "Done", "success"), ("partly", "Partly", "secondary"), ("missed", "Not done", "secondary"), ("later", "Still doing it", "secondary"))


def ask_card(commitment: Commitment, part: str) -> tuple[str, tuple[ActionSpec, ...]]:
    """The "Done?" the bot sends when a commitment's time ends (owner, 2026-11-13: proof only works right after the act)."""
    if part == "night":                     # roadmap slice 2: asked the next morning; nothing to extend
        text = f"**{commitment.text}**: that was last night's plan. Did you?"
        return text, tuple(ActionSpec(action_id=f"coach:{key}:{commitment.id}", label=label, style=style) for key, label, style in ASK_BUTTONS[:3])
    text = f"**{commitment.text}**: that was {_PART_WORDS.get(part, _PART_WORDS['any'])} plan. Done?"
    return text, tuple(ActionSpec(action_id=f"coach:{key}:{commitment.id}", label=label, style=style) for key, label, style in ASK_BUTTONS)


def describe_time_of_day(local: datetime) -> str:
    part = "night" if local.hour < 5 or local.hour >= 23 else "morning" if local.hour < 12 else "afternoon" if local.hour < 18 else "evening" if local.hour < 21 else "late evening"
    return f"{local.strftime('%A')} {part}"


def review_card(commitment: Commitment, late: bool = False) -> tuple[str, tuple[ActionSpec, ...]]:
    text = f"**Your commitment for {_weekday(commitment.for_date)}:** {commitment.text}\nHow did it go?"
    if late:
        text += "\n(Its time to report is over, so a Kept no longer counts. Not kept still does.)"
    return text, tuple(ActionSpec(action_id=f"coach:{key}:{commitment.id}", label=label, style=style) for key, label, style, _ in OUTCOME_BUTTONS)


def _fit(reply: str, cards: str) -> str:
    """The reply gives way to the card: the card is what the user decides on, and it must be whole."""
    room = _MAX_MESSAGE - len(cards) - 2
    if len(reply) <= room:
        return reply
    return reply[:max(room - 1, 0)].rsplit(" ", 1)[0].rstrip() + "…"


class CoachDm:
    schemas = ACTION_SCHEMAS
    instructions = ACTION_INSTRUCTIONS

    def __init__(self, desk: CoachDesk, *, core: CoreDatabase, clock: Clock, memory=None) -> None:
        """`memory` is coaching_memory.service.CoachingMemoryService when ENABLE_COACHING_MEMORY is on: check-in replies are kept there for multi-day context."""
        self._desk = desk
        self._core = core
        self._clock = clock
        self._memory = memory
        self._reminded: dict[str, date] = {}               # commitment id -> the local day its card was last shown outside a check-in
        self.after_kept: Callable[[datetime], object] | None = None    # proof_checks: start a picked spot check at once (owner, 2026-11-13)
        self.forget_also: Callable[[str], int] | None = None

    @property
    def desk(self) -> CoachDesk:
        return self._desk

    # ------------------------------------------------------------------ the engine's port

    def guidance(self, user_id: str, text: str, now: datetime, *, check_in_reply: bool = False) -> str | None:
        if not self._desk.may_propose(user_id, text, now):
            return None
        return f"{COACH_GUIDANCE}\n\n{CHECK_IN_GUIDANCE}" if check_in_reply else COACH_GUIDANCE

    def submit(self, subject_key: str, raw: object, now: datetime, user_message: str = "") -> SubmitResult:
        return self._desk.submit(subject_key, raw, now, user_message)

    def context(self, user_id: str, now: datetime) -> dict | None:
        return self._desk.context(user_id, now)

    def prompt_suffix(self, now: datetime) -> str:
        return self._desk.prompt_suffix(now)

    def check_ready(self, model) -> str | None:
        """A warning for the log when the configured model cannot serve the coach, or None."""
        check = getattr(model, "check_ready", None)
        readiness = check() if check is not None else None
        if readiness is None or (readiness.reachable and readiness.installed):
            return None
        if not readiness.reachable:
            return "Ollama did not answer: the coach falls back to its fixed messages until it is up."
        return "The configured OLLAMA_MODEL is not installed in Ollama: the coach cannot reply until it is pulled or OLLAMA_MODEL is changed."

    # ------------------------------------------------------------------ the reply

    def present(self, outgoing: OutgoingMessage, *, user_id: str, outcome: object, check_in_reply: bool, now: datetime, user_message: str = "") -> OutgoingMessage:
        cards: list[tuple[str, tuple[ActionSpec, ...]]] = []
        proposed = isinstance(outcome, SubmitResult) and outcome.proposal is not None
        if check_in_reply and user_id == self._desk.user_id:
            self._remember_check_in(user_id, user_message, now)
        reply, claimed = strip_record_claims(outgoing.text)      # only the buttons record anything, whatever a model says it did
        if claimed:
            logger.info("A reply claiming something was recorded was corrected.")
            outgoing = replace(outgoing, text=reply)
        done = reports_done(user_message) or claimed
        if user_id == self._desk.user_id and not mentions_safety_issue(user_message):
            # Someone reporting pain or an emergency is never asked to account for a commitment in the same breath. An earlier day's open commitment is
            # asked about before anything new is planned: with every proposal, and otherwise once a day in any conversation, not only after a check-in.
            commitment = self._desk.commitment_to_review(now, include_today=done)
            if commitment is not None and (check_in_reply or done or proposed or self._reminded.get(commitment.id) != self._desk.today(now)):
                self._reminded[commitment.id] = self._desk.today(now)
                cards.append(review_card(commitment, late=self._desk.past_report_window(commitment.id, now)))
        if proposed:
            proposal = outcome.proposal
            parts = ()
            if proposal.kind is ActionKind.PROPOSE_COMMITMENT and self._desk.to_utc is not None:
                parts = (("night",) if is_night_commitment(proposal.payload["text"])
                         else self._desk.open_parts(date.fromisoformat(proposal.payload["for_date"]), now) or ("any",))
            cards.append(proposal_card(proposal, parts))
        if claimed:
            has_card = any(action.action_id.startswith(("coach:kept", "coach:partly", "coach:missed")) for _, actions in cards for action in actions)
            note = "Nothing is recorded until you press a button below." if has_card else "Nothing was recorded: there is no open commitment to mark."
            outgoing = replace(outgoing, text=f"{outgoing.text}\n\n{note}".strip())
        if not cards:
            return outgoing
        block = "\n\n".join(text for text, _ in cards)
        return replace(outgoing, text=f"{_fit(outgoing.text, block)}\n\n{block}",
                       actions=outgoing.actions + tuple(action for _, actions in cards for action in actions))

    def _remember_check_in(self, user_id: str, text: str, now: datetime) -> None:
        """The user's own words, dated, through every filter of Coaching Memory: nothing about health or distress is stored, and `forget all` erases it."""
        if self._memory is None or not text.strip():
            return
        note = f"Check-in {self._desk.today(now).isoformat()}: {' '.join(text.split())}"
        if len(note) > MAX_CONTENT:
            note = note[:MAX_CONTENT - 1].rsplit(" ", 1)[0].rstrip() + "…"
        try:
            outcome = self._memory.record(user_id=user_id, category="quote", content=note, user_message=text, now=now)
            if outcome is not WriteOutcome.STORED:
                logger.info("A check-in was not kept in Coaching Memory.", extra={"outcome": outcome.value})
        except Exception:
            logger.exception("A check-in could not be kept in Coaching Memory; the reply is unaffected.")

    def forget_all(self, user_id: str) -> int:
        erased = self._desk.forget_all(user_id)
        if self.forget_also is not None:
            erased += self.forget_also(user_id)              # the spot checks' copy of the commitment text
        return erased

    # ------------------------------------------------------------------ "Done?" at the end of a commitment's time (owner, 2026-11-13)

    def queue_asks(self, now: datetime) -> int:
        """Queues the due "Done?" cards (notification queue, never in quiet hours: the windows end before them)."""
        from notifications.queue import enqueue_in_transaction

        def enqueue(tx, commitment: Commitment, part: str) -> None:
            text, actions = ask_card(commitment, part)
            enqueue_in_transaction(tx, user_id=self._desk.user_id, kind="commitment_ask", dedupe_key=f"commitment:ask:{commitment.id}:{now.isoformat()}",
                                   text=text, now=now, actions=actions)

        return self._desk.due_asks(now, enqueue)

    async def ask_loop(self, interval_seconds: float = 60.0) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.queue_asks, self._clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Queuing the commitment questions failed; will retry.")
            await asyncio.sleep(interval_seconds)

    # ------------------------------------------------------------------ the buttons

    def press(self, *, discord_user_id: str, action: str, ident: str) -> str:
        user_id = self._internal_user(discord_user_id)
        if user_id is None:
            return "That isn't yours to decide."
        now = self._clock.now()
        if action == "decline":
            return self._desk.decide(user_id, ident, accept=False, now=now)
        if action in _ACTION_PARTS:
            return self._desk.decide(user_id, ident, accept=True, now=now, part=_ACTION_PARTS[action])
        if action == "later":
            return self._desk.extend(user_id, ident, now)
        outcome = _OUTCOMES.get(action)
        if outcome is None:
            return "That isn't a choice here."
        reply = self._desk.record_outcome(user_id, ident, outcome, now)
        if outcome is Outcome.KEPT and self.after_kept is not None:
            try:
                self.after_kept(now)                       # a picked spot check's task is assigned now, so its challenge follows this reply
            except Exception:
                logger.exception("Starting a spot check failed; the loop retries it.")
        return reply

    def _internal_user(self, discord_user_id: str) -> str | None:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT user_account_id FROM user_channel_identities WHERE channel = 'discord' AND external_id = ?", (str(discord_user_id),))
        return None if row is None else row["user_account_id"]


def active_task_reader(task_runtime, task_catalog) -> Callable[[str], tuple[str, str | None] | None]:
    """(assignment id, title of the exact version it was assigned from), read through the same read-only types as the context providers."""
    def read(user_id: str) -> tuple[str, str | None] | None:
        assignment = task_runtime.get_active_assignment(user_id)
        if assignment is None:
            return None
        version = task_catalog.get_template(assignment.template_id, assignment.template_version)
        return assignment.id, None if version is None else version.title
    return read


def goal_targets_frozen(contract: IronContract) -> Callable[[str], bool]:
    """Fails closed like every Iron Contract gate that needs "unlocked": an agreed target can change only when the wearer is KNOWN to be unlocked."""
    return lambda user_id: contract.lock_status(user_id) is not LockStatus.UNLOCKED


def build_coach(config, core: CoreDatabase, *, clock: Clock, targets_frozen: Callable[[str], bool], paused: Callable[[str, datetime], bool],
                today: Callable[[datetime], date], active_task: Callable[[str], tuple[str, str | None] | None], memory=None,
                local_now: Callable[[datetime], datetime] | None = None,
                day_bounds: Callable[[date], tuple[datetime, datetime]] | None = None,
                to_utc: Callable[[date, time], datetime] | None = None, quiet: tuple[time, time] | None = None) -> CoachDm | None:
    """None unless ENABLE_COACH is on and there is a wearer and a Trust domain to label goals with."""
    if not getattr(config, "enable_coach", False):
        return None
    user_id = (getattr(config, "check_in_user_id", None) or getattr(config, "chaster_execution_wearer_user_id", None)
               or getattr(config, "owner_user_id", None))
    domain = getattr(config, "check_in_trust_domain", None)
    if not user_id or not domain:
        logger.warning("ENABLE_COACH is true but the coach is NOT active: it needs a user (CHECK_IN_USER_ID, CHASTER_EXECUTION_WEARER_USER_ID or OWNER_USER_ID) "
                       "and CHECK_IN_TRUST_DOMAIN.")
        return None
    desk = CoachDesk(core, user_id=user_id, trust_domain=domain, goals=GoalManager(core.db_path, core=core), today=today, targets_frozen=targets_frozen,
                     paused=lambda now: paused(user_id, now), active_task=active_task)
    if local_now is not None:
        desk.time_of_day = lambda now: describe_time_of_day(local_now(now))
        desk.local_now = local_now
    desk.day_bounds = day_bounds
    desk.to_utc = to_utc
    if quiet is not None:
        desk.quiet = quiet
    return CoachDm(desk, core=core, clock=clock, memory=memory if getattr(config, "enable_coaching_memory", False) else None)
