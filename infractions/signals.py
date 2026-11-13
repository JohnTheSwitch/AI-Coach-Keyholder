"""
infractions/signals.py

The one path from a detection to an Incident, for every detector (owner, 2026-11-10). There is no second person to press a keyholder button: the Keyholder
is the system's own role (philosophy 2.9), so the system asks the wearer, judges the answer and decides by itself.

    detected -> `waiting`: the wearer is asked to explain, with 12 hours to answer
             -> an answer: health or safety -> cleared, by code alone (3.9, 3.10)
                           otherwise judged (infractions/judge.py): explained -> cleared
                                                                    unclear   -> one follow-up question; still unclear -> cleared (2.12)
                                                                    not explained -> `decided`
             -> no answer by the deadline: a hygiene finding -> cleared (an unanswered break may be a health need and never counts)
                                           anything else -> `decided` (silence, filed warning-only: Trust and a Warning, never a Penalty Window)
    `decided` -> one appeal within 2 hours (owner, 2026-11-13), and only one that brings something new: an appeal that only repeats is rejected in
             code without asking the model again. Accepted -> cleared. Otherwise `confirmed`: the Incident is registered and confirmed by the system.

A model failure is never a verdict against the wearer: the answer waits as `judging` and is retried; after 3 days it is dropped (cleared). An Emergency
Override, an approved exemption or a vacation that overlaps the finding clears it at every step. "It was a health or safety need" is a button that clears
it at any point before it counts. The wearer's own words are kept for the audit and erased by `forget all`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from application.models import ActionSpec, OutgoingMessage
from infrastructure.database import Database as CoreDatabase
from infrastructure.decision_log import decision
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from infractions.excuses import Excuses
from infractions.judge import ExplanationJudge, Judgement, Verdict
from notifications.queue import enqueue_in_transaction
from trust_manager.models import BreachDirectness, EvidenceConfidenceLevel, ImpactLevel, IncidentEvidence, IntentAssessment, RepetitionEvidence

logger = logging.getLogger("ai_coach_keyholder.infractions")

__all__ = ["SignalKind", "InfractionSignals", "GRACE", "APPEAL_WINDOW", "CAPTURE_WINDOW", "JUDGE_GIVE_UP", "RULE_GROUPS", "EVIDENCE", "Signal",
           "UNANSWERED_RULE_GROUP", "ADOPTED_RULE_GROUPS", "ASK_TEXT"]

GRACE = timedelta(hours=12)                 # how long the wearer has to answer before silence counts
APPEAL_WINDOW = timedelta(hours=2)          # between "unexplained" and the Incident counting
MAX_APPEALS = 1                              # owner, 2026-11-13: one appeal for any judge, and it must bring something new
SECOND_APPEAL_TIME = timedelta(hours=1)      # after a rejected first appeal, at least this long for the second
_APPEAL_SEP = "\n---\n"                      # the appeals are kept in one column, in order
CAPTURE_WINDOW = timedelta(hours=1)         # after pressing Explain or Appeal, the next message in this time is the answer
JUDGE_GIVE_UP = timedelta(days=3)           # an answer the model never managed to judge is dropped, never held against anyone
UNANSWERED_RULE_GROUP = "DETECTION_UNANSWERED"   # warning-only in penalty_engine: silence is never a Penalty Window

_MINOR_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.LOW, rule_breach_directness=BreachDirectness.PARTIAL,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=30),
    evidence_confidence=EvidenceConfidenceLevel.MEDIUM, intentionality=IntentAssessment.UNCLEAR)
_SEVERE_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.HIGH, rule_breach_directness=BreachDirectness.DIRECT,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=30),
    evidence_confidence=EvidenceConfidenceLevel.MEDIUM, intentionality=IntentAssessment.UNCLEAR)
_COMMUNITY_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.LOW, rule_breach_directness=BreachDirectness.PARTIAL,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=7),
    evidence_confidence=EvidenceConfidenceLevel.LOW, intentionality=IntentAssessment.UNCLEAR)


class SignalKind(StrEnum):
    HYGIENE_DURATION = "hygiene_duration"
    HYGIENE_FREQUENCY = "hygiene_frequency"
    LOCK_CLAIM = "lock_claim"
    EXEMPTION_OVERRUN = "exemption_overrun"
    PHOTO_REJECTED = "photo_rejected"
    CHECKIN_NONCOMPLIANCE = "checkin_noncompliance"
    SELF_REPORT_HYGIENE = "self_report_hygiene"      # owner, 2026-11-12 (option A): the wearer owned up to misusing a hygiene opening
    EXEMPTION_OVERRIDE = "exemption_override"        # owner, 2026-11-13: took a declined exemption anyway (warning only)


RULE_GROUPS = {SignalKind.HYGIENE_DURATION: "HYGIENE_MISUSE", SignalKind.HYGIENE_FREQUENCY: "HYGIENE_MISUSE", SignalKind.LOCK_CLAIM: "EPISTEMIC_MISREPRESENTATION",
               SignalKind.EXEMPTION_OVERRUN: "HYGIENE_MISUSE", SignalKind.PHOTO_REJECTED: "peer_review_rejection",
               SignalKind.CHECKIN_NONCOMPLIANCE: "CHECKIN_REPEATED_NONCOMPLIANCE", SignalKind.SELF_REPORT_HYGIENE: "HYGIENE_MISUSE",
               SignalKind.EXEMPTION_OVERRIDE: "EXEMPTION_OVERRIDE"}
# Proportionate (3.8): a break that ran long is minor, so a first one ends in a Warning; a false lock report is serious; a community vote is one weak signal.
EVIDENCE = {SignalKind.HYGIENE_DURATION: _MINOR_EVIDENCE, SignalKind.HYGIENE_FREQUENCY: _MINOR_EVIDENCE, SignalKind.LOCK_CLAIM: _SEVERE_EVIDENCE,
            SignalKind.EXEMPTION_OVERRUN: _MINOR_EVIDENCE, SignalKind.PHOTO_REJECTED: _COMMUNITY_EVIDENCE, SignalKind.CHECKIN_NONCOMPLIANCE: _MINOR_EVIDENCE,
            SignalKind.SELF_REPORT_HYGIENE: _MINOR_EVIDENCE, SignalKind.EXEMPTION_OVERRIDE: _MINOR_EVIDENCE}
# Incidents another module registers UNCONFIRMED by itself; they are taken into this path instead of waiting for a button nobody presses.
ADOPTED_RULE_GROUPS = {"CHECKIN_REPEATED_NONCOMPLIANCE": SignalKind.CHECKIN_NONCOMPLIANCE}
_SILENCE_NEVER_COUNTS = frozenset({SignalKind.HYGIENE_DURATION, SignalKind.HYGIENE_FREQUENCY})

ASK_TEXT = ("Press **Explain** and tell me what happened in your next message; you have twelve hours. If it was a health or safety need, press that button "
            "instead and that's the end of it.")
_EXPLAINED = "Thank you, that explains it. It doesn't count."
_FOLLOW_UP = "I need a little more to go on. What exactly happened, and when? Tell me in your next message."
_UNCERTAIN = "Thank you. I can't be sure either way, so it doesn't count."
_NOT_EXPLAINED = ("That doesn't explain it, so it will count as an incident in two hours. If I got something wrong, or there's something you haven't told me, "
                  "you can appeal once with something new: press **Appeal**.")
_SILENCE = ("You didn't explain this in time, so it will count as an incident in two hours. If there's something I should know, you can appeal once with something new: "
            "press **Appeal**.")
_APPEAL_ACCEPTED = "You're right. It doesn't count."
_ADMITTED = ("Owning it counts for something, and it'll weigh less because you did. But a sorry isn't a reason, so it counts as an incident in two hours. "
             "If there was an actual reason, press **Appeal** and tell me.")
_APPEAL_REJECTED = "I've looked at your appeal, and it doesn't change the decision. It counts as an incident."
_APPEAL_NOTHING_NEW = "Your appeal doesn't tell me anything new, so it doesn't change the decision. It counts as an incident."
_APPEAL_REJECTED_ONE_LEFT = ("I've looked at your appeal, and it doesn't change the decision. You have one more appeal before it counts: press **Appeal** "
                             "and tell me what I'm missing.")
_COUNTED = "It now counts as an incident."
_PENDING = "Thank you. I'll look at it shortly and let you know."
_UNJUDGED = "I couldn't look at your explanation in time, so it doesn't count."
_HEALTH = "Thank you for telling me. It doesn't count, and that's the end of it."
SELF_REPORT_DETAIL = "The wearer said they used a hygiene opening for something other than hygiene."
OVERRIDE_DETAIL = "The wearer took an exemption the judge had declined, by their own choice."
SELF_REPORT_QUESTION = ("**Owning up?** Did you just tell me you used a hygiene opening for something other than hygiene? If you did, say so: it counts, "
                        "but it weighs less because you told me yourself.")
_OWNED = ("Thank you for owning it. It counts as hygiene misuse in two hours, and it weighs less because you told me yourself. If I misread you, "
          "you can appeal once.")
_DISOWNED = "Okay, I misread you. Nothing is recorded."


def self_report_buttons(signal_id: str) -> tuple[ActionSpec, ...]:
    return (ActionSpec(action_id=f"infraction:own:{signal_id}", label="Yes, I did", style="danger"),
            ActionSpec(action_id=f"infraction:disown:{signal_id}", label="No, you misread me"))


@dataclass(frozen=True, kw_only=True)
class Signal:
    id: str
    user_id: str
    kind: SignalKind
    subject_id: str
    trust_domain: str
    detail: str
    status: str
    review_after: datetime
    incident_id: str
    rule_group: str = ""


def _signal(row) -> Signal:
    kind = SignalKind(row["kind"])
    keys = row.keys()
    group = row["rule_group_id"] if "rule_group_id" in keys and row["rule_group_id"] else RULE_GROUPS[kind]
    return Signal(id=row["id"], user_id=row["user_id"], kind=kind, subject_id=row["subject_id"], trust_domain=row["trust_domain"], detail=row["detail"],
                  status=row["status"], review_after=parse_iso(row["review_after"]), incident_id=row["incident_id"], rule_group=group)


def _explain_buttons(signal_id: str) -> tuple[ActionSpec, ...]:
    return (ActionSpec(action_id=f"infraction:explain:{signal_id}", label="Explain"),
            ActionSpec(action_id=f"infraction:health:{signal_id}", label="It was a health or safety need", style="success"))


def _appeals(row) -> int:
    """How many appeals were made: they are kept in order in the one `appeal` column."""
    return 0 if row["appeal"] is None else row["appeal"].count(_APPEAL_SEP) + 1


def _appeal_buttons(signal_id: str) -> tuple[ActionSpec, ...]:
    return (ActionSpec(action_id=f"infraction:appeal:{signal_id}", label="Appeal"),
            ActionSpec(action_id=f"infraction:health:{signal_id}", label="It was a health or safety need", style="success"))


class InfractionSignals:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None, excuses: Excuses, judge: ExplanationJudge,
                 register_incident: Callable[[Signal, datetime], None], confirm_incident: Callable[[str, str, datetime], None], owner_user_id: str | None = None) -> None:
        """`register_incident(signal, now)` files the Incident under `signal.incident_id` and `signal.rule_group`, chosen in advance so a retry cannot duplicate it.
        `confirm_incident(incident_id, evidence, now, admitted)` confirms it as the system's own decision; `admitted` = the wearer owned up. `owner_user_id` is no longer used: nobody else decides."""
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._excuses = excuses
        self._judge = judge
        self._register = register_incident
        self._confirm = confirm_incident
        self._wearer: str | None = None

    # ------------------------------------------------------------------ raising

    def raise_in_transaction(self, tx: Transaction, *, user_id: str, kind: SignalKind, subject_id: str, trust_domain: str, detail: str, wearer_text: str,
                             now: datetime, facts: str = "", window: tuple[datetime, datetime] | None = None, incident_id: str | None = None) -> bool:
        """False when this subject already raised this kind. The question goes out with the signal, so the 12 hours are never spent in silence."""
        signal_id = str(uuid4())
        start, end = window if window is not None else (None, None)
        cursor = tx.execute(
            "INSERT OR IGNORE INTO infraction_signals (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, created_at, facts, "
            "window_start, window_end) VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?)",
            (signal_id, user_id, kind.value, subject_id, trust_domain, detail, _iso(now + GRACE), incident_id or str(uuid4()), _iso(now), facts or "",
             _iso(start) if start else None, _iso(end) if end else None))
        if cursor.rowcount != 1:
            return False
        decision("infractions", "Detector finding: the wearer is asked to explain.", kind=kind.value, detail=detail, facts=facts,
                 answer_by=_iso(now + GRACE))
        enqueue_in_transaction(tx, user_id=user_id, kind="infraction_wearer", dedupe_key=f"infraction:wearer:{signal_id}", text=f"{wearer_text}\n\n{ASK_TEXT}",
                               now=now, actions=_explain_buttons(signal_id))
        return True

    def offer_self_report(self, *, user_id: str, statement: str, trust_domain: str, now: datetime) -> str | None:
        """Owner, 2026-11-12 (option A): the wearer's own words read as a confession. Nothing is decided yet: the returned id goes on a card, and only
        their "Yes" decides it. One open offer at a time; an offer nobody answers lapses without a trace (`advance`)."""
        def write(tx: Transaction, _s: object) -> str | None:
            existing = tx.fetch_one("SELECT id FROM infraction_signals WHERE user_id = ? AND kind = ? AND status = 'offered'",
                                    (user_id, SignalKind.SELF_REPORT_HYGIENE.value))
            if existing is not None:
                return existing["id"]
            signal_id = str(uuid4())
            tx.execute("INSERT INTO infraction_signals (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, created_at, "
                       "facts, explanation, explanation_at) VALUES (?, ?, ?, ?, ?, ?, 'offered', ?, ?, ?, ?, ?, ?)",
                       (signal_id, user_id, SignalKind.SELF_REPORT_HYGIENE.value, signal_id, trust_domain, SELF_REPORT_DETAIL, _iso(now + GRACE), str(uuid4()),
                        _iso(now), f"Self-reported in the chat at {_iso(now)}.", (statement or "")[:2000], _iso(now)))
            return signal_id

        signal_id = apply_transition(self._core, write=write)
        decision("infractions", "The wearer's message read as owning up to hygiene misuse: asked whether that is right.", signal_id=signal_id)
        return signal_id

    def record_override_in_transaction(self, tx: Transaction, *, user_id: str, subject_id: str, trust_domain: str, facts: str, now: datetime) -> bool:
        """Owner, 2026-11-13: the wearer took a declined exemption anyway. Nothing to ask or judge: they chose it, so it is decided at once as
        self-disclosed and counts on the next pass (warning only, EXEMPTION_OVERRIDE). In the caller's transaction, with the opening itself."""
        if tx.fetch_one("SELECT 1 FROM infraction_signals WHERE kind = ? AND subject_id = ?", (SignalKind.EXEMPTION_OVERRIDE.value, subject_id)):
            return False
        tx.execute("INSERT INTO infraction_signals (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, created_at, facts, "
                   "verdict, verdict_reason, admitted, judged_at, decided_at, confirm_after, rule_group_id) "
                   "VALUES (?, ?, ?, ?, ?, ?, 'decided', ?, ?, ?, ?, 'self_reported', ?, 1, ?, ?, ?, ?)",
                   (str(uuid4()), user_id, SignalKind.EXEMPTION_OVERRIDE.value, subject_id, trust_domain, OVERRIDE_DETAIL, _iso(now), str(uuid4()), _iso(now),
                    facts[:1000], "Took a declined exemption anyway, by their own choice.", _iso(now), _iso(now), _iso(now),
                    RULE_GROUPS[SignalKind.EXEMPTION_OVERRIDE]))
        return True

    def raise_signal(self, **kwargs) -> bool:
        """`raise_in_transaction` in its own transaction, for a caller outside this package's transactions."""
        return apply_transition(self._core, write=lambda tx, _s: self.raise_in_transaction(tx, **kwargs))

    # ------------------------------------------------------------------ the wearer

    def press(self, *, user_id: str, signal_id: str, action: str, now: datetime) -> OutgoingMessage:
        row = self._row(signal_id)
        if row is None or row["user_id"] != user_id:
            return OutgoingMessage(text="This isn't yours to answer.")
        if action == "health":
            return OutgoingMessage(text=_HEALTH if self.clear_as_health(user_id=user_id, signal_id=signal_id, now=now) else "This one is already settled.")
        if action == "explain":
            if row["status"] not in ("waiting", "open"):
                return OutgoingMessage(text="This one is already settled." if row["status"] in ("cleared", "confirmed", "dismissed") else "I already have your answer.")
            stage = "follow_up" if row["follow_up_asked_at"] else "explanation"
            self._await(signal_id, stage, now)
            return OutgoingMessage(text="Go ahead: tell me what happened in your next message." if stage == "explanation" else _FOLLOW_UP)
        if action in ("own", "disown"):
            return self._answer_offer(row, own=action == "own", now=now)
        if action == "appeal":
            if row["status"] != "decided":
                return OutgoingMessage(text="You can appeal only before it counts.")
            if row["judge_pending_since"] is not None:
                return OutgoingMessage(text="I'm still looking at your appeal.")
            if _appeals(row) >= MAX_APPEALS:
                return OutgoingMessage(text="You've already used your appeal.")
            self._await(signal_id, "appeal", now)
            return OutgoingMessage(text="Write your appeal in your next message: what did I get wrong, or what haven't you told me yet?")
        return OutgoingMessage(text="That isn't one of the options.")

    def _answer_offer(self, row, *, own: bool, now: datetime) -> OutgoingMessage:
        if row["status"] != "offered":
            return OutgoingMessage(text="This one is already settled.")
        if not own:
            self._clear(row["id"], "wearer_said_misread", now)
            return OutgoingMessage(text=_DISOWNED)

        def write(tx: Transaction, _s: object) -> bool:
            return tx.execute("UPDATE infraction_signals SET status = 'decided', verdict = 'self_reported', verdict_reason = ?, admitted = 1, judged_at = ?, "
                              "decided_at = ?, confirm_after = ?, rule_group_id = ? WHERE id = ? AND status = 'offered'",
                              ("The wearer owned up by themselves.", _iso(now), _iso(now), _iso(now + APPEAL_WINDOW),
                               RULE_GROUPS[SignalKind.SELF_REPORT_HYGIENE], row["id"])).rowcount == 1

        if not apply_transition(self._core, write=write):
            return OutgoingMessage(text="This one is already settled.")
        decision("infractions", "The wearer confirmed owning up to hygiene misuse: it counts after the appeal time, as self-disclosed.", signal_id=row["id"])
        return OutgoingMessage(text=_OWNED, actions=(ActionSpec(action_id=f"infraction:appeal:{row['id']}", label="Appeal"),))

    def capture(self, user_id: str, text: str, now: datetime) -> OutgoingMessage | None:
        """The wearer's next message after Explain or Appeal is their answer, judged at once. None: this message is not an answer to anything."""
        if not isinstance(text, str) or not text.strip():
            return None
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT * FROM infraction_signals WHERE user_id = ? AND awaiting IS NOT NULL AND awaiting_since >= ? "
                               "AND status IN ('waiting', 'open', 'decided') ORDER BY awaiting_since DESC LIMIT 1", (user_id, _iso(now - CAPTURE_WINDOW)))
        if row is None:
            return None
        stage = row["awaiting"]
        column = {"explanation": "explanation", "follow_up": "follow_up", "appeal": "appeal"}[stage]
        status = "decided" if stage == "appeal" else "judging"

        def write(tx: Transaction, _s: object) -> bool:
            cursor = tx.execute(f"UPDATE infraction_signals SET {column} = ?, {column}_at = ?, awaiting = NULL, awaiting_since = NULL, status = ?, "
                                f"judge_pending_since = ? WHERE id = ? AND awaiting = ?",
                                (text.strip()[:4000], _iso(now), status, _iso(now), row["id"], stage)) if stage != "appeal" else tx.execute(
                "UPDATE infraction_signals SET appeal = ?, appealed_at = ?, awaiting = NULL, awaiting_since = NULL, judge_pending_since = ? WHERE id = ? AND awaiting = ?",
                (text.strip()[:4000] if row["appeal"] is None else (row["appeal"] + _APPEAL_SEP + text.strip()[:4000])[-12000:], _iso(now), _iso(now), row["id"], stage))
            return cursor.rowcount == 1

        if not apply_transition(self._core, write=write):
            return None
        return self._judge_now(row["id"], now)

    def clear_as_health(self, *, user_id: str, signal_id: str, now: datetime) -> bool:
        """The wearer's word is enough (3.9), at any point before it counts."""
        row = self._row(signal_id)
        if row is None or row["user_id"] != user_id:
            return False
        return self._clear(signal_id, "wearer_health", now)

    # ------------------------------------------------------------------ the loop

    def advance(self, now: datetime, *, window_of: Callable[[Signal], tuple[datetime, datetime]]) -> int:
        """One pass: take in what waits for nobody, retry judging, act on deadlines, confirm what stayed unexplained. Returns how many Incidents counted."""
        self._adopt(now)
        with self._core.transaction() as tx:
            lapsed = tx.fetch_all("SELECT id FROM infraction_signals WHERE status = 'offered' AND review_after <= ?", (_iso(now),))
        for row in lapsed:
            self._clear(row["id"], "self_report_not_confirmed", now)          # an unanswered offer records nothing
        with self._core.transaction() as tx:
            pending = tx.fetch_all("SELECT id FROM infraction_signals WHERE judge_pending_since IS NOT NULL AND status IN ('judging', 'decided') ORDER BY created_at")
        for row in pending:
            try:
                message = self._judge_now(row["id"], now)
                if message is not None and message.text != _PENDING:
                    self._tell(row["id"], f"judged:{now.isoformat()}", message, now)
            except Exception:
                logger.exception("Could not judge an explanation; it waits for the next pass.")
        with self._core.transaction() as tx:
            due = tx.fetch_all("SELECT * FROM infraction_signals WHERE status = 'waiting' AND review_after <= ? ORDER BY created_at, rowid", (_iso(now),))
        for row in due:
            try:
                self._deadline(row, now, window_of)
            except Exception:
                logger.exception("Could not act on an unanswered signal; it stays waiting.")
        with self._core.transaction() as tx:
            ripe = tx.fetch_all("SELECT * FROM infraction_signals WHERE status = 'decided' AND confirm_after <= ? AND judge_pending_since IS NULL "
                                "AND (awaiting IS NULL OR awaiting_since < ?) ORDER BY created_at, rowid", (_iso(now), _iso(now - CAPTURE_WINDOW)))
        counted = 0
        for row in ripe:
            try:
                counted += self._count(row, now, window_of)
            except Exception:
                logger.exception("Could not confirm a decided signal; it stays decided.")
        return counted

    def extend_for_downtime(self, *, down_since: datetime, back_at: datetime) -> int:
        """Owner, 2026-11-10: nobody can answer a bot that is off. Every deadline still open when it went down moves by the time it was down."""
        gap = back_at - down_since
        if gap <= timedelta(minutes=5):
            return 0
        seconds = int(gap.total_seconds())

        def write(tx: Transaction, _s: object) -> int:
            moved = tx.execute("UPDATE infraction_signals SET review_after = strftime('%Y-%m-%dT%H:%M:%fZ', review_after, ?) "
                               "WHERE status = 'waiting' AND review_after > ?", (f"+{seconds} seconds", _iso(down_since))).rowcount
            moved += tx.execute("UPDATE infraction_signals SET confirm_after = strftime('%Y-%m-%dT%H:%M:%fZ', confirm_after, ?) "
                                "WHERE status = 'decided' AND confirm_after > ?", (f"+{seconds} seconds", _iso(down_since))).rowcount
            return moved

        return apply_transition(self._core, write=write)

    def rows(self) -> list[dict]:
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT id, kind, status, verdict, created_at, decided_at, cleared_reason FROM infraction_signals "
                                                  "ORDER BY created_at DESC, rowid DESC")]

    def forget_words(self, user_id: str) -> int:
        """`forget all`: the wearer's own words go; the finding and the decision stay as the audit."""
        def write(tx: Transaction, _s: object) -> int:
            return tx.execute("UPDATE infraction_signals SET explanation = NULL, follow_up = NULL, appeal = NULL WHERE user_id = ?", (user_id,)).rowcount

        return apply_transition(self._core, write=write)

    # ------------------------------------------------------------------ internals

    def _row(self, signal_id: str):
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT * FROM infraction_signals WHERE id = ?", (signal_id,))

    def _await(self, signal_id: str, stage: str, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE infraction_signals SET awaiting = ?, awaiting_since = ? WHERE id = ?", (stage, _iso(now), signal_id))

        apply_transition(self._core, write=write)

    def _judge_now(self, signal_id: str, now: datetime) -> OutgoingMessage | None:
        row = self._row(signal_id)
        if row is None or row["judge_pending_since"] is None:
            return None
        if row["status"] == "decided":
            appeals = (row["appeal"] or "").split(_APPEAL_SEP)
            stage, text, earlier = "appeal", appeals[-1], "\n".join(t for t in (row["explanation"], row["follow_up"], *appeals[:-1]) if t) or None
        elif row["follow_up"] is not None:
            stage, text, earlier = "follow_up", row["follow_up"], row["explanation"]
        else:
            stage, text, earlier = "explanation", row["explanation"], None
        with self._core.transaction() as tx:
            related = tuple(r["explanation"] for r in tx.fetch_all(
                "SELECT explanation FROM infraction_signals WHERE user_id = ? AND kind = ? AND id != ? AND explanation IS NOT NULL AND explanation_at >= ? "
                "ORDER BY explanation_at DESC LIMIT 3", (row["user_id"], row["kind"], signal_id, _iso(now - timedelta(days=3)))))
        judgement = self._judge.judge(detail=row["detail"], facts=row["facts"] or "", explanation=text or "", earlier=earlier, appeal=stage == "appeal",
                                      related=related)
        if judgement is None:
            if now - parse_iso(row["judge_pending_since"]) > JUDGE_GIVE_UP:
                self._clear(signal_id, "unjudged", now)
                return OutgoingMessage(text=_UNJUDGED)
            return OutgoingMessage(text=_PENDING)
        return self._apply(row, stage, judgement, now)

    def _apply(self, row, stage: str, judgement: Judgement, now: datetime) -> OutgoingMessage:
        decision("infractions", f"Judge verdict on the {stage}: {judgement.verdict.value}.", kind=row["kind"], detail=row["detail"],
                 reason=judgement.reason, decided_by="code (health/safety)" if judgement.by_code else "model", admitted=judgement.admitted or None)
        signal_id = row["id"]
        if judgement.admitted:
            def mark(tx: Transaction, _s: object) -> None:
                tx.execute("UPDATE infraction_signals SET admitted = 1 WHERE id = ?", (signal_id,))

            apply_transition(self._core, write=mark)
        if judgement.verdict is Verdict.EXPLAINED:
            self._clear(signal_id, "appeal_accepted" if stage == "appeal" else "health" if judgement.by_code else "explained", now, note=judgement.reason)
            return OutgoingMessage(text=_HEALTH if judgement.by_code else _APPEAL_ACCEPTED if stage == "appeal" else _EXPLAINED)
        if stage == "appeal":
            self._record_appeal_verdict(signal_id, judgement, now)
            if _appeals(row) < MAX_APPEALS:
                def extend(tx: Transaction, _s: object) -> None:
                    tx.execute("UPDATE infraction_signals SET confirm_after = MAX(confirm_after, ?) WHERE id = ? AND status = 'decided'",
                               (_iso(now + SECOND_APPEAL_TIME), signal_id))

                apply_transition(self._core, write=extend)
                return OutgoingMessage(text=_APPEAL_REJECTED_ONE_LEFT, actions=_appeal_buttons(signal_id))
            self._count(self._row(signal_id), now, None, quiet=True)
            return OutgoingMessage(text=_APPEAL_NOTHING_NEW if judgement.nothing_new else _APPEAL_REJECTED)
        if judgement.verdict is Verdict.UNCLEAR and stage == "explanation":
            def write(tx: Transaction, _s: object) -> None:
                tx.execute("UPDATE infraction_signals SET status = 'waiting', judge_pending_since = NULL, verdict = 'unclear', verdict_reason = ?, judged_at = ?, "
                           "follow_up_asked_at = ?, awaiting = 'follow_up', awaiting_since = ?, review_after = MAX(review_after, ?) WHERE id = ?",
                           (judgement.reason, _iso(now), _iso(now), _iso(now), _iso(now + GRACE), signal_id))

            apply_transition(self._core, write=write)
            return OutgoingMessage(text=_FOLLOW_UP)
        if judgement.verdict is Verdict.UNCLEAR:
            self._clear(signal_id, "uncertain", now, note=judgement.reason)        # 2.12: still uncertain after asking -> no consequence
            return OutgoingMessage(text=_UNCERTAIN)
        self._decide(signal_id, Verdict.NOT_EXPLAINED.value, judgement.reason, RULE_GROUPS[SignalKind(row["kind"])], now)
        return OutgoingMessage(text=_ADMITTED if judgement.admitted else _NOT_EXPLAINED, actions=_appeal_buttons(signal_id))

    def _record_appeal_verdict(self, signal_id: str, judgement: Judgement, now: datetime) -> None:
        def write(tx: Transaction, _s: object) -> None:
            tx.execute("UPDATE infraction_signals SET judge_pending_since = NULL, verdict_reason = ?, judged_at = ? WHERE id = ?",
                       (f"Appeal: {judgement.reason}"[:400], _iso(now), signal_id))

        apply_transition(self._core, write=write)

    def _decide(self, signal_id: str, verdict: str, reason: str, rule_group: str, now: datetime) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            cursor = tx.execute("UPDATE infraction_signals SET status = 'decided', verdict = ?, verdict_reason = ?, judged_at = ?, decided_at = ?, confirm_after = ?, "
                                "rule_group_id = COALESCE(rule_group_id, ?), judge_pending_since = NULL, awaiting = NULL, awaiting_since = NULL "
                                "WHERE id = ? AND status IN ('waiting', 'judging', 'open')",
                                (verdict, reason, _iso(now), _iso(now), _iso(now + APPEAL_WINDOW), rule_group, signal_id))
            return cursor.rowcount == 1

        return apply_transition(self._core, write=write)

    def _deadline(self, row, now: datetime, window_of) -> None:
        signal = _signal(row)
        excuse = self._excused(row, signal, window_of)
        if excuse is not None or signal.kind in _SILENCE_NEVER_COUNTS:
            self._clear(signal.id, excuse or "unanswered", now)
            return
        group = signal.rule_group if signal.kind in ADOPTED_RULE_GROUPS.values() else UNANSWERED_RULE_GROUP
        if self._decide(signal.id, "silence", "No answer by the deadline.", group, now):
            self._tell(signal.id, "silence", OutgoingMessage(text=_SILENCE, actions=_appeal_buttons(signal.id)), now)

    def _count(self, row, now: datetime, window_of, *, quiet: bool = False) -> int:
        if row is None or row["status"] != "decided":
            return 0
        signal = _signal(row)
        excuse = self._excused(row, signal, window_of)
        if excuse is not None:
            self._clear(signal.id, excuse, now)
            return 0
        with self._core.transaction() as tx:
            exists = tx.fetch_one("SELECT 1 FROM incidents WHERE id = ?", (signal.incident_id,))
        if exists is None:
            self._register(signal, now)                 # outside the write below: the Trust Manager opens its own transaction
        admitted = "admitted" in row.keys() and row["admitted"] == 1
        self._confirm(signal.incident_id, f"Judged by the system: {signal.detail} Verdict: {row['verdict']}. {row['verdict_reason'] or ''}".strip(), now,
                      admitted)

        def write(tx: Transaction, _s: object) -> bool:
            cursor = tx.execute("UPDATE infraction_signals SET status = 'confirmed', incident_registered_at = COALESCE(incident_registered_at, ?), decided_at = ? "
                                "WHERE id = ? AND status = 'decided'", (_iso(now), _iso(now), signal.id))
            return cursor.rowcount == 1

        counted = apply_transition(self._core, write=write)
        if counted:
            decision("infractions", "Detector finding confirmed as an incident after the appeal window.", signal_id=signal.id, detail=signal.detail,
                     verdict=row["verdict"], reason=row["verdict_reason"], admitted=admitted or None)
        if counted and not quiet:
            self._tell(signal.id, "counted", OutgoingMessage(text=_COUNTED), now)
        return 1 if counted else 0

    def _excused(self, row, signal: Signal, window_of) -> str | None:
        if signal.kind in (SignalKind.SELF_REPORT_HYGIENE, SignalKind.EXEMPTION_OVERRIDE):
            return None                              # it happened during an approved opening by definition: that cannot excuse it
        if row["window_start"] and row["window_end"]:
            start, end = parse_iso(row["window_start"]), parse_iso(row["window_end"])
        elif window_of is not None:
            start, end = window_of(signal)
        else:
            return None
        # An overrun's own exemption is what was overrun: it cannot also excuse it.
        return self._excuses.reason(signal.user_id, start, end, ignore_exemption=signal.subject_id if signal.kind is SignalKind.EXEMPTION_OVERRUN else None)

    def _clear(self, signal_id: str, reason: str, now: datetime, *, note: str | None = None) -> bool:
        detail = note
        decision("infractions", f"Detector question closed without consequence: {reason}.", signal_id=signal_id, note=note)

        def write(tx: Transaction, _s: object) -> bool:
            cursor = tx.execute("UPDATE infraction_signals SET status = 'cleared', cleared_reason = ?, decided_at = ?, verdict_reason = COALESCE(?, verdict_reason), "
                                "judge_pending_since = NULL, awaiting = NULL, awaiting_since = NULL WHERE id = ? AND status IN ('waiting', 'judging', 'decided', 'open', 'offered')",
                                (reason, _iso(now), detail, signal_id))
            return cursor.rowcount == 1

        return apply_transition(self._core, write=write)

    def _tell(self, signal_id: str, step: str, message: OutgoingMessage, now: datetime) -> None:
        row = self._row(signal_id)
        if row is None:
            return

        def write(tx: Transaction, _s: object) -> None:
            enqueue_in_transaction(tx, user_id=row["user_id"], kind="infraction_wearer", dedupe_key=f"infraction:{step}:{signal_id}", text=message.text, now=now,
                                   actions=message.actions)

        apply_transition(self._core, write=write)

    def _adopt(self, now: datetime) -> None:
        """Rows from before this path asked a keyholder; and another module's own UNCONFIRMED Incidents would wait for a button nobody presses."""
        with self._core.transaction() as tx:
            legacy = tx.fetch_all("SELECT id, user_id, detail FROM infraction_signals WHERE status = 'open'")
            marks = ",".join("?" for _ in ADOPTED_RULE_GROUPS)
            orphans = tx.fetch_all(f"SELECT i.id, i.rule_group_id, i.trust_domain, i.created_at, i.description FROM incidents i WHERE i.confirmation = 'unconfirmed' "
                                   f"AND i.rule_group_id IN ({marks}) AND NOT EXISTS (SELECT 1 FROM infraction_signals s WHERE s.incident_id = i.id)",
                                   tuple(ADOPTED_RULE_GROUPS))
        for row in legacy:
            def write(tx: Transaction, _s: object, r=row) -> None:
                if tx.execute("UPDATE infraction_signals SET status = 'waiting', review_after = ? WHERE id = ? AND status = 'open'",
                              (_iso(now + GRACE), r["id"])).rowcount == 1:
                    enqueue_in_transaction(tx, user_id=r["user_id"], kind="infraction_wearer", dedupe_key=f"infraction:reask:{r['id']}", now=now,
                                           text=f"{r['detail']}\n\n{ASK_TEXT}", actions=_explain_buttons(r["id"]))

            apply_transition(self._core, write=write)
        if not orphans or self._wearer is None:
            return
        for incident in orphans:
            created = parse_iso(incident["created_at"])
            self.raise_signal(user_id=self._wearer, kind=ADOPTED_RULE_GROUPS[incident["rule_group_id"]], subject_id=incident["id"], trust_domain=incident["trust_domain"],
                              detail=_ADOPTED_DETAIL[incident["rule_group_id"]], now=now,
                              wearer_text=_ADOPTED_TEXT[incident["rule_group_id"]].format(day=f"{created.strftime('%A')} {created.day} {created.strftime('%B')}"),
                              facts=f"{incident['description']} Registered at {incident['created_at']}.", window=(created, created), incident_id=incident["id"])

    def set_wearer(self, user_id: str | None) -> None:
        """Whom an adopted Incident's question goes to (the single wearer of this database)."""
        self._wearer = user_id


_ADOPTED_DETAIL = {"CHECKIN_REPEATED_NONCOMPLIANCE": "Several check-ins in a row were missed."}
_ADOPTED_TEXT = {"CHECKIN_REPEATED_NONCOMPLIANCE": "{day}: you left my check-ins hanging, several in a row. What was going on?"}


_STAGE = {"waiting": "waiting_for_answer", "judging": "being_judged", "decided": "appeal_open", "confirmed": "counted", "cleared": "dropped",
          "offered": "awaiting_confirmation"}


def get_recent_questions(core: CoreDatabase, user_id: str, now: datetime, *, local: Callable[[datetime], datetime],
                         days: int = 7) -> list[dict]:
    """Owner, 2026-11-12: what the conversation model may know about detector questions, so it never says "no report" about a counted one.
    Open ones and those from the last `days` days, newest first: the finding's wording, the stage, and a local time. Never a score."""
    with core.transaction() as tx:
        rows = tx.fetch_all("SELECT detail, status, review_after, confirm_after, decided_at FROM infraction_signals WHERE user_id = ? "
                            "AND (status IN ('waiting', 'judging', 'decided', 'offered') OR created_at >= ?) ORDER BY created_at DESC, rowid DESC LIMIT 3",
                            (user_id, _iso(now - timedelta(days=days))))

    def at(value: str | None) -> str | None:
        return local(parse_iso(value)).strftime("%a %d %b %H:%M") if value else None

    out = []
    for r in rows:
        stage = _STAGE.get(r["status"])
        if stage is None:
            continue
        out.append({"detail": r["detail"], "stage": stage,
                    "until": at(r["review_after"]) if stage in ("waiting_for_answer", "awaiting_confirmation") else at(r["confirm_after"]) if stage == "appeal_open" else None,
                    "closed": at(r["decided_at"]) if stage in ("counted", "dropped") else None})
    return out

