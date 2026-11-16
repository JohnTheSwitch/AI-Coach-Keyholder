"""The protocol ladder, no debate, forms of address, the wellbeing check (owner, 2026-11-15/16; autonomous queue A2-A4). No network."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from protocol.service import (ADDRESS_ME, ASK_PROPERLY, CALM_CHECK, HOLD_DAYS, RULES_STAND, RUNGS, THANKS_SLOTS, WINDOW_OVER, Ports,
                              ProtocolLadder, get_protocol_state, is_pushback)
from tests.lines.test_lines import core

U = "wearer"
MON = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)          # a Monday (UTC = local in these tests)


class World:
    def __init__(self, tmp_path) -> None:
        self.core = core(tmp_path)
        self.paused, self.lines, self.task_open, self.left, self.verified, self.misses = False, 0, False, None, [], 0
        self.duties, self.answered = [], []
        self.ladder = ProtocolLadder(self.core, user_id=U, ports=Ports(
            local=lambda t: t, to_utc=lambda d, t: datetime.combine(d, t, tzinfo=timezone.utc), quiet_end=time(7, 0),
            paused=lambda now: self.paused, open_lines=lambda: self.lines, todays_task_open=lambda now: self.task_open,
            window_left_hours=lambda now: self.left, verified_since=lambda since: list(self.verified),
            open_duty=self._open, answer_duties=self._answer, duty_misses=lambda since: self.misses))

    def _open(self, slot, day, deadline, now):
        if any(d[:2] == (slot, day) for d in self.duties):
            return None
        self.duties.append((slot, day, deadline))
        return f"{slot}{day}"

    def _answer(self, slots, now):
        self.answered.append(tuple(slots))
        return 1

    def notes(self):
        with self.core.transaction() as tx:
            return [r["text"] for r in tx.fetch_all("SELECT text FROM notifications WHERE kind = 'protocol' ORDER BY created_at, rowid")]

    def rung(self):
        return get_protocol_state(self.core, U)["rung"]

    def climb_to(self, rung, start=MON):
        at = start
        self.ladder.tick(at)
        while self.rung() < rung:
            at += timedelta(days=HOLD_DAYS)
            self.ladder.tick(at)
            at += timedelta(days=1, minutes=1)
            self.ladder.tick(at)
        return at


class TestTheLadder:
    def test_starts_at_the_morning_report_and_climbs_after_two_weeks_announced_a_day_ahead(self, tmp_path) -> None:
        w = World(tmp_path)
        w.ladder.tick(MON)
        assert w.rung() == 1
        w.ladder.tick(MON + timedelta(days=HOLD_DAYS - 1))
        assert w.notes() == []
        w.ladder.tick(MON + timedelta(days=HOLD_DAYS))
        assert w.rung() == 1 and "Tomorrow a new rule starts" in w.notes()[-1] and RUNGS[2] in w.notes()[-1]
        w.ladder.tick(MON + timedelta(days=HOLD_DAYS + 1, minutes=1))
        assert w.rung() == 2 and "from now on" in w.notes()[-1]

    def test_two_misses_hold_it_and_a_pause_too(self, tmp_path) -> None:
        w = World(tmp_path)
        w.ladder.tick(MON)
        w.misses = 2
        w.ladder.tick(MON + timedelta(days=HOLD_DAYS))
        assert w.notes() == []
        w.misses, w.paused = 0, True
        w.ladder.tick(MON + timedelta(days=HOLD_DAYS + 1))
        assert w.notes() == []
        w.paused = False
        w.ladder.tick(MON + timedelta(days=HOLD_DAYS + 2))
        assert len(w.notes()) == 1

    def test_never_past_rung_five_until_the_kneel_is_built(self, tmp_path) -> None:
        w = World(tmp_path)
        at = w.climb_to(5)
        w.ladder.tick(at + timedelta(days=60))
        assert w.rung() == 5


class TestTheGate:
    def test_pushback_gets_one_fixed_reply_naming_the_open_item(self, tmp_path) -> None:
        w = World(tmp_path)
        w.ladder.tick(MON)
        assert w.ladder.gate(U, "That's so unfair", MON) == RULES_STAND
        w.lines = 20
        assert w.ladder.gate(U, "why do I have to do this", MON) == "The rule stands: the 20 lines you owe. Arguing changes nothing."
        w.left = 30.2
        assert "Penalty Window, about 30 hours left" in w.ladder.gate(U, "to je blbost", MON)
        assert w.ladder.gate(U, "How was your day?", MON) is None
        assert is_pushback("I won't do it") and not is_pushback("I wonder") and not is_pushback("I won't be home tonight")

    def test_health_always_passes(self, tmp_path) -> None:
        w = World(tmp_path)
        w.climb_to(4)
        w.ladder.gate(U, "title Sir", MON + timedelta(days=60))
        assert w.ladder.gate(U, "It hurts, this is unfair, I need to unlock", MON + timedelta(days=60)) is None

    def test_asking_properly_from_rung_two(self, tmp_path) -> None:
        w = World(tmp_path)
        w.ladder.tick(MON)
        assert w.ladder.gate(U, "can I have a hygiene opening", MON) is None          # rung 1: not yet
        at = w.climb_to(2)
        assert w.ladder.gate(U, "can I have a hygiene opening", at) == ASK_PROPERLY
        assert w.ladder.gate(U, "Please, a hygiene opening, I need a shower", at) is None
        assert w.ladder.gate(U, "when is my next release?", at) is None                 # a question, not a request

    def test_the_title_from_rung_four(self, tmp_path) -> None:
        w = World(tmp_path)
        at = w.climb_to(4)
        assert "title Sir" in w.notes()[-1]
        assert w.ladder.gate(U, "hello", at) is None                                  # no title chosen yet
        assert "start every message with \"Sir\"" in w.ladder.gate(U, "title sir", at)
        assert w.ladder.gate(U, "hello", at) == ADDRESS_ME
        assert w.ladder.gate(U, "Sir, hello", at) is None

    def test_thank_yous_from_rung_three(self, tmp_path) -> None:
        w = World(tmp_path)
        at = w.climb_to(3)
        w.verified = ["t1"]
        w.ladder.tick(at + timedelta(minutes=5))
        assert [d[0] for d in w.duties] == [THANKS_SLOTS[0]] and w.duties[0][2] == at + timedelta(minutes=5, hours=2)
        w.ladder.gate(U, "Thank you for the task", at + timedelta(minutes=30))
        assert w.answered == [THANKS_SLOTS]

    def test_the_reflection_from_rung_five(self, tmp_path) -> None:
        w = World(tmp_path)
        at = w.climb_to(5)
        sunday = at + timedelta(days=(6 - at.weekday()) % 7)
        sunday = sunday.replace(hour=19, minute=30)
        w.ladder.tick(sunday)
        assert w.duties[-1][2] == datetime.combine(sunday.date() + timedelta(days=1), time(12, 0), tzinfo=timezone.utc)
        assert "weekly reflection" in w.notes()[-1]
        assert w.ladder.gate(U, "I obeyed the tasks, pushed back on lines, and my aim is to answer every check in", sunday + timedelta(hours=1)) \
            == "Reflection received."

    def test_a_hard_answer_after_the_sunday_review_gets_a_calm_check(self, tmp_path) -> None:
        w = World(tmp_path)
        w.ladder.tick(MON)
        sunday = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
        assert w.ladder.gate(U, "Honestly I'm struggling a bit", sunday) == CALM_CHECK
        assert w.ladder.gate(U, "Honestly I'm struggling a bit", sunday + timedelta(days=2)) is None


def test_the_praise_comes_back_when_the_window_ends(tmp_path) -> None:
    w = World(tmp_path)
    w.ladder.tick(MON)
    w.left = 10.0
    w.ladder.tick(MON + timedelta(minutes=5))
    assert w.notes() == []
    w.left = None
    w.ladder.tick(MON + timedelta(hours=11))
    assert w.notes() == [WINDOW_OVER]


def test_the_address_fragment() -> None:
    from conversation_engine.context_providers.address_provider import AddressContextProvider
    from conversation_engine.prompt_builder import _render_address
    window = AddressContextProvider(window_active=lambda now: True, praise_earned=lambda u, now: True).provide_context(subject_key=U, now=MON)
    assert window.data["praise"] is False and "no title or pet name" in _render_address(window.data)
    earned = AddressContextProvider(window_active=lambda now: False, praise_earned=lambda u, now: True,
                                    title=lambda u: "Sir").provide_context(subject_key=U, now=MON)
    text = _render_address(earned.data)
    assert "'slave'" in text and "'good boy' is fine" in text and "'Sir'" in text


class TestDutyCheckIns:
    """The duties are check-ins written by supervision: a miss is a missed check-in, and ordinary messages never answer them."""

    def test_missed_answered_and_never_taken_by_a_chat_message(self, tmp_path) -> None:
        from supervision.checkins import get_duty_misses
        from tests.supervision.test_checkins import MON as SMON, Env, U as SU, local
        e = Env(tmp_path, score=0.9, fraction=0.0)
        now = local(SMON, 18)
        day = now.date().isoformat()
        first = e.svc.open_duty(91, day, now + timedelta(hours=2), now)
        assert first is not None and e.svc.open_duty(91, day, now + timedelta(hours=2), now) is None
        assert e.svc.on_user_message(SU, "just chatting", now + timedelta(minutes=5)) is False
        e.svc.open_duty(92, day, now + timedelta(hours=2), now)
        assert e.svc.answer_duties((92,), now + timedelta(minutes=10)) == 1
        e.svc.tick(now + timedelta(hours=2, minutes=5))
        status = {r["slot_index"]: r["status"] for r in e.rows() if r["slot_index"] in (91, 92)}
        assert status == {91: "missed", 92: "answered"}
        assert get_duty_misses(e.core, SU, (now - timedelta(days=1)).isoformat()) == 1


def test_the_wiring_builds(tmp_path) -> None:
    """Smoke test for system/protocol_wiring.py (the bot imports it at start)."""
    from types import SimpleNamespace
    from system.protocol_wiring import build_address_provider, build_protocol
    db = core(tmp_path)
    cfg = SimpleNamespace(local=lambda t: t, to_utc=lambda d, t: datetime.combine(d, t, tzinfo=timezone.utc), quiet_end=time(7, 0))
    check_ins = SimpleNamespace(open_duty=lambda *a: None, answer_duties=lambda *a: 0)
    ladder = build_protocol(db, user_id=U, check_ins=check_ins, window_cfg=cfg, paused=lambda now: False, todays_task_open=lambda now: False)
    ladder.tick(MON)
    assert ladder.gate(U, "this is unfair", MON) == RULES_STAND
    assert build_address_provider(db).provide_context(subject_key=U, now=MON).data == {"window": False, "praise": False, "title": None}
