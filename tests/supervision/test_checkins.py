"""Check-in service (v0.39 Phase 2): scheduling, at-most-once delivery, replies, rubric verdicts, deadlines, Trust."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from schedule_windows.windows import WindowConfig, WindowKind, classify
from supervision.checkin_evaluator import Verdict
from supervision.checkins import FOLLOW_UP_WINDOW, MAX_LATE, PROMPTS, RESPONSE_TIMEOUT, CheckInService, SendOutcome
from supervision.models import TrustLevel
from system.startup import on_system_startup
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent
PRAGUE = ZoneInfo("Europe/Prague")
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
U = "wearer"
MON, SAT = date(2026, 10, 12), date(2026, 10, 17)


def local(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=PRAGUE).astimezone(timezone.utc)


class Sender:
    def __init__(self, outcome=SendOutcome.DELIVERED, raises=None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.outcome, self.raises = outcome, raises

    def send(self, *, user_id, text):
        self.sent.append((user_id, text))
        if self.raises:
            raise self.raises
        return self.outcome


class Env:
    def __init__(self, tmp_path: Path, score=0.6, verdict=Verdict.SUFFICIENT, sender=None, fraction=0.5) -> None:
        self.core = CoreDatabase(tmp_path / "test.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.tm = TrustManager(self.core.db_path, core=self.core)
        self.tm.create_domain(domain_id="d", display_name="D", description="d", created_via_consent_id="c1",
                              now=local(MON, 9))
        self.score = score
        self.verdicts: list = [verdict]
        self.evaluated: list[tuple[str, str]] = []
        self.sender = sender or Sender()
        self.svc = CheckInService(
            self.core.db_path, core=self.core, window_config=CFG, user_id=U, trust_domain="d",
            get_trust_score=lambda: self.score, sender=self.sender, evaluate=self._evaluate, rand_fraction=lambda: fraction,
        )

    def _evaluate(self, prompt, reply):
        self.evaluated.append((prompt, reply))
        v = self.verdicts[0] if len(self.verdicts) == 1 else self.verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    def rows(self):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all("SELECT * FROM check_ins ORDER BY due_at, slot_index")]

    def statuses(self):
        return [r["status"] for r in self.rows()]

    def events(self, name):
        with self.core.transaction() as tx:
            return tx.fetch_all("SELECT payload_json FROM domain_events WHERE event_type = ?", (name,))

    def first_due(self):
        return datetime.fromisoformat(self.rows()[0]["due_at"].replace("Z", "+00:00"))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


class TestScheduling:
    @pytest.mark.parametrize("score,count", [(0.9, 0), (0.85, 0), (0.84, 1), (0.70, 1), (0.69, 2), (0.45, 2), (0.44, 3), (0.1, 3)])
    def test_check_ins_per_day_follow_the_trust_level(self, tmp_path, score, count) -> None:
        e = Env(tmp_path, score=score)
        e.svc.tick(local(MON, 9, 0))
        assert len(e.rows()) == count

    def test_level_3_single_check_in_is_in_the_evening_half(self, tmp_path) -> None:
        e = Env(tmp_path, score=0.75)
        e.svc.tick(local(MON, 9, 0))
        due = e.first_due().astimezone(PRAGUE)
        assert (19, 15) <= (due.hour, due.minute) <= (22, 0)

    def test_every_slot_is_in_the_discretionary_window(self, tmp_path) -> None:
        for i, (fraction, d) in enumerate([(0.0, MON), (0.5, MON), (0.999, MON), (0.0, SAT), (0.999, SAT), (0.5, date(2026, 10, 28))]):
            (tmp_path / str(i)).mkdir()
            e = Env(tmp_path / str(i), score=0.1, fraction=fraction)
            e.svc.tick(local(d, 6, 0))
            rows = e.rows()
            assert len(rows) == 3
            for r in rows:
                due = datetime.fromisoformat(r["due_at"].replace("Z", "+00:00"))
                assert classify(CFG, due) is WindowKind.DISCRETIONARY

    def test_scheduling_is_idempotent(self, env) -> None:
        env.svc.tick(local(MON, 9))
        env.svc.tick(local(MON, 9, 5))
        env.svc.tick(local(MON, 9, 10))
        assert len(env.rows()) == 2

    def test_slots_already_in_the_past_are_not_created(self, tmp_path) -> None:
        e = Env(tmp_path, score=0.1)
        e.svc.tick(local(MON, 23, 0))             # quiet hours: the day's slots are all behind us
        assert e.rows() == []

    def test_a_new_day_gets_new_slots(self, env) -> None:
        env.svc.tick(local(MON, 9))
        env.svc.tick(local(date(2026, 10, 13), 9))
        assert len(env.rows()) == 4

    def test_improved_trust_skips_unsent_slots(self, env) -> None:
        env.svc.tick(local(MON, 9))
        env.score = 0.9
        env.svc.tick(local(MON, 10))
        assert env.statuses() == ["skipped", "skipped"]

    def test_no_trust_data_schedules_nothing(self, tmp_path) -> None:
        e = Env(tmp_path)
        e.svc._score = lambda: None
        e.svc.tick(local(MON, 9))
        assert e.rows() == []


class TestDelivery:
    def test_prompt_is_sent_at_the_due_time_and_never_before(self, env) -> None:
        env.svc.tick(local(MON, 9))
        due = env.first_due()
        env.svc.tick(due - timedelta(minutes=1))
        assert env.sender.sent == []
        env.svc.tick(due)
        assert env.sender.sent == [(U, PROMPTS[TrustLevel.LEVEL_2])]
        row = env.rows()[0]
        assert row["status"] == "sent" and row["prompt_text"] == PROMPTS[TrustLevel.LEVEL_2]
        assert datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00")) == due + RESPONSE_TIMEOUT

    def test_prompt_due_event_carries_ids_only(self, env) -> None:
        env.svc.tick(local(MON, 9))
        env.svc.tick(env.first_due())
        (e,) = env.events("check_in.prompt_due")
        assert set(json.loads(e["payload_json"])) == {"check_in_id", "trust_domain"}

    def test_a_prompt_is_sent_exactly_once(self, env) -> None:
        env.svc.tick(local(MON, 9))
        due = env.first_due()
        env.svc.tick(due)
        env.svc.tick(due + timedelta(minutes=1))
        env.svc.tick(due + timedelta(minutes=2))
        assert len(env.sender.sent) == 1

    def test_only_one_open_check_in_at_a_time(self, tmp_path) -> None:
        e = Env(tmp_path, score=0.1)
        e.svc.tick(local(MON, 9))
        due0 = e.first_due()
        e.svc.tick(due0)
        assert len(e.sender.sent) == 1
        later = due0 + timedelta(minutes=5)
        with e.core.transaction() as tx:                    # make slot 1 due while slot 0 still awaits an answer
            tx.execute("UPDATE check_ins SET due_at = ? WHERE slot_index = 1", (later.isoformat().replace("+00:00", "Z"),))
        e.svc.tick(later)
        assert len(e.sender.sent) == 1 and e.statuses()[1] == "scheduled"

    def test_a_slot_that_is_too_late_is_skipped_not_sent(self, env) -> None:
        env.svc.tick(local(MON, 9))
        env.svc.tick(env.first_due() + MAX_LATE + timedelta(minutes=1))
        assert env.sender.sent == [] and env.statuses()[0] == "skipped"

    def test_a_slot_in_a_forbidden_window_is_skipped(self, env) -> None:
        env.svc.tick(local(MON, 9))
        with env.core.transaction() as tx:                # force an overdue-but-recent slot into quiet hours
            tx.execute("UPDATE check_ins SET due_at = ?", (local(MON, 21, 50).isoformat().replace("+00:00", "Z"),))
        env.svc.tick(local(MON, 22, 5))
        assert env.sender.sent == [] and set(env.statuses()) == {"skipped"}

    def test_undeliverable_is_never_a_miss(self, tmp_path) -> None:
        e = Env(tmp_path, sender=Sender(SendOutcome.UNDELIVERABLE))
        e.svc.tick(local(MON, 9))
        e.svc.tick(e.first_due())
        e.svc.tick(e.first_due() + timedelta(hours=3))
        assert e.statuses()[0] == "undeliverable" and not e.events("check_in.missed") and not e.svc.last_check_in_missed()

    @pytest.mark.parametrize("sender", [Sender(SendOutcome.UNKNOWN), Sender(raises=RuntimeError("boom"))])
    def test_ambiguous_delivery_is_needs_review_and_never_resent(self, tmp_path, sender) -> None:
        e = Env(tmp_path, sender=sender)
        e.svc.tick(local(MON, 9))
        due = e.first_due()
        e.svc.tick(due)
        e.svc.tick(due + timedelta(minutes=2))
        assert e.statuses()[0] == "needs_review" and len(sender.sent) == 1 and not e.events("check_in.missed")

    def test_a_crash_between_claim_and_result_is_needs_review(self, env) -> None:
        env.svc.tick(local(MON, 9))
        due = env.first_due()
        with env.core.transaction() as tx:
            tx.execute("UPDATE check_ins SET status = 'sending', updated_at = ? WHERE slot_index = 0",
                       (due.isoformat().replace("+00:00", "Z"),))
        env.svc.tick(due + timedelta(minutes=10))
        assert env.statuses()[0] == "needs_review" and env.sender.sent == []


def sent_env(tmp_path, **kw):
    e = Env(tmp_path, **kw)
    e.svc.tick(local(MON, 9))
    e.due = e.first_due()
    e.svc.tick(e.due)
    assert e.statuses()[0] == "sent"
    return e


class TestReplies:
    def test_sufficient_reply_is_answered_without_consequence(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        assert e.svc.on_user_message(U, "I trained and finished my chores", e.due + timedelta(minutes=5))
        e.svc.tick(e.due + timedelta(minutes=6))
        row = e.rows()[0]
        assert row["status"] == "answered" and row["verdict"] == "SUFFICIENT" and row["rubric_version"] == "1"
        assert e.evaluated == [(PROMPTS[TrustLevel.LEVEL_2], "I trained and finished my chores")]
        assert not e.events("check_in.missed") and not e.events("check_in.insufficient")

    def test_reply_text_never_appears_in_an_event_payload(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc.on_user_message(U, "secret reply text", e.due + timedelta(minutes=5))
        e.svc.tick(e.due + timedelta(minutes=6))
        with e.core.transaction() as tx:
            payloads = " ".join(r["payload_json"] for r in tx.fetch_all("SELECT payload_json FROM domain_events"))
        assert "secret reply text" not in payloads

    def test_reply_after_the_deadline_is_not_a_reply_and_the_check_in_is_missed(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        late = e.due + RESPONSE_TIMEOUT + timedelta(minutes=1)
        assert e.svc.on_user_message(U, "sorry", late) is False
        e.svc.tick(late)
        assert e.statuses()[0] == "missed" and len(e.events("check_in.missed")) == 1

    def test_reply_exactly_at_the_deadline_counts(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        assert e.svc.on_user_message(U, "done", e.due + RESPONSE_TIMEOUT)

    @pytest.mark.parametrize("user,text", [("someone-else", "hello there"), (U, ""), (U, "   "), (U, None)])
    def test_irrelevant_messages_are_ignored(self, tmp_path, user, text) -> None:
        e = sent_env(tmp_path)
        assert e.svc.on_user_message(user, text, e.due + timedelta(minutes=1)) is False
        assert e.statuses()[0] == "sent"

    def test_no_open_check_in_means_not_a_reply(self, env) -> None:
        assert env.svc.on_user_message(U, "hello there", local(MON, 12)) is False

    def test_several_messages_before_evaluation_are_merged(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc.on_user_message(U, "first part", e.due + timedelta(minutes=1))
        e.svc.on_user_message(U, "second part", e.due + timedelta(minutes=2))
        e.svc.tick(e.due + timedelta(minutes=3))
        assert e.evaluated[0][1] == "first part\nsecond part"


class TestVerdictConsequences:
    @pytest.mark.parametrize("verdict", [Verdict.UNCERTAIN, Verdict.NOT_EVALUATED])
    def test_uncertain_and_not_evaluated_never_carry_a_consequence(self, tmp_path, verdict) -> None:
        e = sent_env(tmp_path, verdict=verdict)
        e.svc.on_user_message(U, "hmm", e.due + timedelta(minutes=1))
        e.svc.tick(e.due + timedelta(minutes=2))
        e.svc.tick(e.due + timedelta(hours=5))
        assert e.statuses()[0] == "answered_uncertain"
        assert not e.events("check_in.missed") and not e.events("check_in.insufficient")

    def test_evaluator_failure_is_not_a_penalty(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=RuntimeError("model down"))
        e.svc.on_user_message(U, "I did things", e.due + timedelta(minutes=1))
        e.svc.tick(e.due + timedelta(minutes=2))
        e.svc.tick(e.due + timedelta(hours=5))
        assert e.statuses()[0] == "answered_uncertain" and not e.events("check_in.insufficient")

    def test_insufficient_opens_a_30_minute_follow_up(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.INSUFFICIENT)
        e.svc.on_user_message(U, "ok", e.due + timedelta(minutes=1))
        evaluated_at = e.due + timedelta(minutes=2)
        e.svc.tick(evaluated_at)
        row = e.rows()[0]
        assert row["status"] == "insufficient_pending" and row["verdict"] == "INSUFFICIENT"
        assert datetime.fromisoformat(row["follow_up_until"].replace("Z", "+00:00")) == evaluated_at + FOLLOW_UP_WINDOW
        assert not e.events("check_in.insufficient")

    def test_a_sufficient_follow_up_clears_it(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.INSUFFICIENT)
        e.svc.on_user_message(U, "ok", e.due + timedelta(minutes=1))
        e.svc.tick(e.due + timedelta(minutes=2))
        e.verdicts = [Verdict.SUFFICIENT]
        assert e.svc.on_user_message(U, "Sorry. I trained for an hour and tidied the flat.", e.due + timedelta(minutes=10))
        e.svc.tick(e.due + timedelta(minutes=11))
        e.svc.tick(e.due + timedelta(hours=3))
        assert e.statuses()[0] == "answered" and not e.events("check_in.insufficient")

    def test_no_follow_up_becomes_check_in_insufficient(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.INSUFFICIENT)
        e.svc.on_user_message(U, "ok", e.due + timedelta(minutes=1))
        t = e.due + timedelta(minutes=2)
        e.svc.tick(t)
        e.svc.tick(t + FOLLOW_UP_WINDOW + timedelta(minutes=1))
        assert e.statuses()[0] == "insufficient" and len(e.events("check_in.insufficient")) == 1
        e.svc.tick(t + timedelta(hours=5))
        assert len(e.events("check_in.insufficient")) == 1

    def test_a_second_insufficient_reply_does_not_extend_the_window(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.INSUFFICIENT)
        e.svc.on_user_message(U, "ok", e.due + timedelta(minutes=1))
        t = e.due + timedelta(minutes=2)
        e.svc.tick(t)
        first_follow_up = e.rows()[0]["follow_up_until"]
        e.svc.on_user_message(U, "still nothing", t + timedelta(minutes=10))
        e.svc.tick(t + timedelta(minutes=11))
        row = e.rows()[0]
        assert row["status"] == "insufficient_pending" and row["follow_up_until"] == first_follow_up
        e.svc.tick(t + FOLLOW_UP_WINDOW + timedelta(minutes=1))
        assert e.statuses()[0] == "insufficient"

    def test_silence_for_60_minutes_is_missed(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc.tick(e.due + RESPONSE_TIMEOUT)
        assert e.statuses()[0] == "sent"                              # the deadline itself is still in time
        e.svc.tick(e.due + RESPONSE_TIMEOUT + timedelta(seconds=1))
        assert e.statuses()[0] == "missed" and len(e.events("check_in.missed")) == 1
        e.svc.tick(e.due + timedelta(hours=4))
        assert len(e.events("check_in.missed")) == 1

    def test_last_check_in_missed_is_cleared_by_the_next_concluded_one(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc.tick(e.due + RESPONSE_TIMEOUT + timedelta(minutes=1))
        assert e.svc.last_check_in_missed() is True
        second_due = datetime.fromisoformat(e.rows()[1]["due_at"].replace("Z", "+00:00"))
        e.svc.tick(second_due)
        e.svc.on_user_message(U, "all good, finished my tasks", second_due + timedelta(minutes=5))
        e.svc.tick(second_due + timedelta(minutes=6))
        assert e.svc.last_check_in_missed() is False

    def test_a_pending_insufficient_does_not_count_as_missed(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.INSUFFICIENT)
        e.svc.on_user_message(U, "ok", e.due + timedelta(minutes=1))
        e.svc.tick(e.due + timedelta(minutes=2))
        assert e.svc.last_check_in_missed() is False


class TestTrustConsequences:
    def _score(self, e):
        return e.tm.get_domain_state("d").score

    def test_missed_lowers_trust_through_the_real_event_wiring(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc.tick(e.due + RESPONSE_TIMEOUT + timedelta(minutes=1))
        before = self._score(e)
        on_system_startup(e.core, "p", FrozenClock(e.due + timedelta(hours=2)))
        assert self._score(e) == pytest.approx(before - 0.02)

    def test_insufficient_lowers_trust_by_a_minor_amount(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.INSUFFICIENT)
        e.svc.on_user_message(U, "ok", e.due + timedelta(minutes=1))
        t = e.due + timedelta(minutes=2)
        e.svc.tick(t)
        e.svc.tick(t + FOLLOW_UP_WINDOW + timedelta(minutes=1))
        before = self._score(e)
        on_system_startup(e.core, "p", FrozenClock(e.due + timedelta(hours=2)))
        assert self._score(e) == pytest.approx(before - 0.01)

    def test_uncertain_and_answered_never_touch_trust(self, tmp_path) -> None:
        e = sent_env(tmp_path, verdict=Verdict.UNCERTAIN)
        e.svc.on_user_message(U, "hmm", e.due + timedelta(minutes=1))
        e.svc.tick(e.due + timedelta(minutes=2))
        before = self._score(e)
        on_system_startup(e.core, "p", FrozenClock(e.due + timedelta(hours=2)))
        assert self._score(e) == before

    def test_redelivery_never_double_counts(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc.tick(e.due + RESPONSE_TIMEOUT + timedelta(minutes=1))
        on_system_startup(e.core, "p1", FrozenClock(e.due + timedelta(hours=2)))
        after = self._score(e)
        with e.core.transaction() as tx:
            tx.execute("UPDATE domain_events SET published_at = NULL WHERE event_type = 'check_in.missed'")
            tx.execute("DELETE FROM domain_event_consumers WHERE consumer_name = 'trust_manager'")
        on_system_startup(e.core, "p2", FrozenClock(e.due + timedelta(hours=3)))
        assert self._score(e) == pytest.approx(after)
        with e.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM trust_evidence WHERE evidence_type = 'checkin_missed'")["n"] == 1

    def test_unknown_domain_is_ignored(self, tmp_path) -> None:
        e = sent_env(tmp_path)
        e.svc._domain = "nope"
        e.svc.tick(e.due + RESPONSE_TIMEOUT + timedelta(minutes=1))
        on_system_startup(e.core, "p", FrozenClock(e.due + timedelta(hours=2)))
        with e.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM trust_evidence")["n"] == 0


class TestMissedCheckInMatrix:
    """Owner matrix: 1st miss -0.02, 2nd consecutive -0.04, 3rd and later -0.06; an answer restarts the count."""

    def _miss(self, e, due_at, n):
        with e.core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at, "
                       "sent_at, deadline_at) VALUES (?, ?, ?, ?, 2, ?, 'sent', ?, ?, ?, ?)",
                       (f"m{n}", U, due_at.date().isoformat(), 20 + n, due_at.isoformat(), due_at.isoformat(), due_at.isoformat(),
                        due_at.isoformat(), (due_at + timedelta(hours=1)).isoformat()))

    def _streaks(self, e):
        return [json.loads(r["payload_json"]).get("streak") for r in e.events("check_in.missed")]

    def test_consecutive_misses_carry_their_position_and_weights(self, tmp_path) -> None:
        e = Env(tmp_path, score=0.6)
        base = local(MON, 18)
        for n in range(1, 5):
            self._miss(e, base + timedelta(days=n), n)
            e.svc.tick(base + timedelta(days=n, hours=2))
        assert self._streaks(e) == [1, 2, 3, 4]
        scores = [e.tm.get_domain_state("d").score]
        for n in range(1, 5):
            on_system_startup(e.core, f"p{n}", FrozenClock(base + timedelta(days=n, hours=3)))
            scores.append(e.tm.get_domain_state("d").score)
        # event delivery is per startup; compare the final drop with the matrix (0.02 + 0.04 + 0.06 + 0.06)
        assert scores[0] - scores[-1] == pytest.approx(0.18)

    def test_an_answer_restarts_the_count(self, tmp_path) -> None:
        e = Env(tmp_path, score=0.6)
        base = local(MON, 18)
        self._miss(e, base + timedelta(days=1), 1)
        e.svc.tick(base + timedelta(days=1, hours=2))
        with e.core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                       "VALUES ('a', ?, '2026-10-14', 30, 2, ?, 'answered', ?, ?)",
                       (U, (base + timedelta(days=2)).isoformat(), base.isoformat(), base.isoformat()))
        self._miss(e, base + timedelta(days=3), 2)
        e.svc.tick(base + timedelta(days=3, hours=2))
        assert self._streaks(e) == [1, 1]

    def test_skipped_prompts_do_not_extend_or_break_the_run(self, tmp_path) -> None:
        e = Env(tmp_path, score=0.6)
        base = local(MON, 18)
        self._miss(e, base + timedelta(days=1), 1)
        e.svc.tick(base + timedelta(days=1, hours=2))
        with e.core.transaction() as tx:
            tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, created_at, updated_at) "
                       "VALUES ('s', ?, '2026-10-14', 31, 2, ?, 'skipped', ?, ?)",
                       (U, (base + timedelta(days=2)).isoformat(), base.isoformat(), base.isoformat()))
        self._miss(e, base + timedelta(days=3), 2)
        e.svc.tick(base + timedelta(days=3, hours=2))
        assert self._streaks(e) == [1, 2]

    def test_weights_by_position(self) -> None:
        from trust_manager.checkin_scoring import checkin_evidence_spec
        assert [checkin_evidence_spec("missed", n)[1] for n in (1, 2, 3, 4, 9)] == [-0.02, -0.04, -0.06, -0.06, -0.06]
        assert checkin_evidence_spec("insufficient")[1] == -0.01
        with pytest.raises(ValueError):
            checkin_evidence_spec("missed", 0)
