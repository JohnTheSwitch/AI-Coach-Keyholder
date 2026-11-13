"""v0.39 Phase G6: the two-step `forget all`, the Iron Contract (lock-state guard, [STATE: LOCKED] and guardrail rule 7, what is never blocked),
scheduled vacations (pause mode: check-ins, daily evaluation, conditioning, telemetry) and the cross-tenant audit of everything Phase G added. No network."""

from __future__ import annotations

import ast
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from advanced_mode.models import ModeTransitionStatus, OperatingMode
from application.models import IncomingMessage
from application.service import FORGET_ALL_WARNING, ApplicationService
from application.user_service import UserService
from behavioral_telemetry.collector import TelemetryCollector
from behavioral_telemetry.research import ResearchPreferences
from behavioral_telemetry.store import TelemetryStore, record_in
from coaching_memory.service import CoachingMemoryService
from conversation_engine.context_providers.iron_contract_provider import IronContractContextProvider
from conversation_engine.engine import ConversationEngine
from conversation_engine.guardrails import CONVERSATION_GUARDRAILS
from conversation_engine.subject_queue import SubjectConversationQueue
from infrastructure.database import Database as CoreDatabase
from iron_contract.guard import IRON_CONTRACT_DISCLOSURE, LOCKED_REFUSAL, UNKNOWN_REFUSAL, IronContract, LockStatus
from iron_contract.vacation import MAX_LEAD_DAYS, MAX_VACATION_DAYS, ScheduleOutcome, VacationService
from memory_system.working_memory import InMemoryWorkingMemory
from system.tenant_gateway import CONSENT_TARGET, invitation_text

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)        # 11:00 in Prague
TZ = "Europe/Prague"


@pytest.fixture
def core(tmp_path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "t.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def report(core, user, status, seq=[0]):                        # noqa: B006 - a running sequence for the test
    seq[0] += 1
    with core.transaction() as tx:
        tx.execute("INSERT OR IGNORE INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)", (user, NOW.isoformat(), NOW.isoformat()))
        cols = [r["name"] for r in tx.fetch_all("PRAGMA table_info(lock_reports)")]
        values = {"id": f"r{seq[0]}", "user_id": user, "status": f"{status}_user_reported", "sequence_number": seq[0], "reported_at": NOW.isoformat(),
                  "created_at": NOW.isoformat(), "reported_via_consent_id": "c", "updated_at": NOW.isoformat()}
        used = [c for c in cols if c in values]
        tx.execute(f"INSERT INTO lock_reports ({', '.join(used)}) VALUES ({', '.join('?' for _ in used)})", tuple(values[c] for c in used))


class World:
    """One database with an onboarded user `42` and every Phase F/G service injected."""

    def __init__(self, core, *, consent_target=None) -> None:
        from tests.application.test_chaster_llm_tool_integration import _onboard
        self.core = core
        self.memory = CoachingMemoryService(core.db_path, core=core)
        self.telemetry = TelemetryStore(core.db_path, core=core)
        self.research = ResearchPreferences(core.db_path, core=core, export_active=True)
        self.contract = IronContract(core.db_path, core=core, consent_target=consent_target)
        self.vacation = VacationService(core.db_path, core=core, timezone_name=TZ)
        self.service = ApplicationService(core.db_path, core=core, coaching_memory=self.memory, telemetry=self.telemetry, research=self.research,
                                          iron_contract=self.contract, vacation=self.vacation)
        _onboard(self.service)
        self.uid = UserService(core.db_path, core=core).get_or_create_user("discord", "42", now=NOW).id
        self.n = 0

    def say(self, text, *, at=NOW, who="42"):
        self.n += 1
        return self.service.handle_message(IncomingMessage(channel="discord", external_user_id=who, text=text, received_at=at,
                                                           external_message_id=f"m{self.n}")).text

    def seed(self):
        self.memory.record(user_id=self.uid, category="stated_goal", content="Wants to run 5k by December", user_message="ok", now=NOW)
        with self.core.transaction() as tx:
            record_in(tx, user_id=self.uid, occurred_at=NOW, event_type="token_burn", metrics={"amount": 1}, source_key="b1", now=NOW)
        self.research.enable(self.uid, now=NOW - timedelta(days=1))

    def counts(self):
        with self.core.transaction() as tx:
            erases = tx.fetch_one("SELECT COUNT(*) AS n FROM research_erase_requests")["n"]
        return len(self.memory.entries(self.uid)), sum(self.telemetry.summary(self.uid).counts.values()), self.research.is_enabled(self.uid), erases


class TestForgetAllConfirmation:
    def test_the_first_step_only_warns(self, core) -> None:
        w = World(core)
        w.seed()
        reply = w.say("forget all")
        assert reply == FORGET_ALL_WARNING and "This will permanently delete your psychological profile and revoke sharing consent" in reply
        assert "Reply `yes`" in reply and w.counts() == (1, 1, True, 0)

    def test_yes_erases_everything_and_queues_the_receiver_erase(self, core) -> None:
        w = World(core)
        w.seed()
        w.say("forget all")
        reply = w.say("Yes", at=NOW + timedelta(minutes=2))
        assert "Erased 1 entry" in reply and "Erased 1 behavioral telemetry record." in reply and "Research sharing is off" in reply
        assert w.counts() == (0, 0, False, 1)
        assert "Coaching Memory" not in w.say("yes", at=NOW + timedelta(minutes=3))       # a second `yes` confirms nothing

    def test_anything_else_cancels(self, core) -> None:
        w = World(core)
        w.seed()
        w.say("forget all")
        w.say("memory")
        w.say("yes")
        assert w.counts() == (1, 1, True, 0)

    def test_a_late_yes_does_nothing(self, core) -> None:
        w = World(core)
        w.seed()
        w.say("forget all")
        assert "too late" in w.say("yes", at=NOW + timedelta(minutes=6)) and w.counts() == (1, 1, True, 0)

    def test_the_request_is_per_user_and_never_confirmed_from_the_untrusted_channel(self, core) -> None:
        w = World(core)
        w.seed()
        w.say("forget all")
        w.say("anything", who="77")
        w.say("english", who="77")
        w.say("yes", who="77")
        w.service.handle_conversation_only(w.uid, "yes", NOW)
        assert w.counts() == (1, 1, True, 0)
        assert "Erased" in w.say("yes")


class TestLockStatusAndGuard:
    def test_status_sources(self, core) -> None:
        c = IronContract(core.db_path, core=core, wearer_user_id="u")
        assert c.lock_status("u") is LockStatus.UNKNOWN
        report(core, "u", "unlocked")
        assert c.lock_status("u") is LockStatus.UNLOCKED
        c.get_observation = lambda: SimpleNamespace(status=SimpleNamespace(value="locked"))
        assert c.lock_status("u") is LockStatus.LOCKED                         # Chaster says locked: a self-report cannot override it
        assert c.lock_status("someone-else") is LockStatus.UNKNOWN               # the observation belongs to the observed wearer only
        report(core, "u", "locked")
        c.get_observation = lambda: SimpleNamespace(status=SimpleNamespace(value="unlocked"))
        assert c.lock_status("u") is LockStatus.LOCKED                         # either source saying locked wins
        c.get_observation = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        assert c.lock_status("u") is LockStatus.UNKNOWN                        # unreadable: unknown (a guard then refuses)

    def test_refusals_and_consent(self, core) -> None:
        c = IronContract(core.db_path, core=core)
        assert c.refusal("u") == UNKNOWN_REFUSAL
        report(core, "u", "locked")
        assert c.refusal("u") == LOCKED_REFUSAL and c.locked_for_context("u")
        report(core, "u", "unlocked")
        assert c.refusal("u") is None and not c.locked_for_context("u")
        tenant = IronContract(core.db_path, core=core, consent_target=CONSENT_TARGET)
        report(core, "u", "locked")
        assert not tenant.applies() and tenant.refusal("u") is None and tenant.refusal("u", always=True) == LOCKED_REFUSAL
        assert not tenant.locked_for_context("u")
        with core.transaction() as tx:
            tx.execute("INSERT INTO consent_log (id, created_at, target_type, target_id, action, explanation_shown) VALUES ('c1', ?, 'integration', ?, "
                       "'approved', ?)", (NOW.isoformat(), CONSENT_TARGET, invitation_text("Alex")))
        assert tenant.applies() and tenant.refusal("u") == LOCKED_REFUSAL and tenant.locked_for_context("u")

    def test_the_invitation_discloses_it(self) -> None:
        text = invitation_text("Alex")
        assert IRON_CONTRACT_DISCLOSURE in text
        for part in ("While you are locked", "Iron Contract", "Standard Mode", "vacation", "will not negotiate", "health, safety", "`halt`", "`leave`",
                     "`conditioning off`", "`research off`", "`forget all`", "always work"):
            assert part in text, part

    def test_lowering_the_difficulty_is_blocked_while_locked(self, core, monkeypatch) -> None:
        w = World(core)
        report(core, w.uid, "locked")
        assert w.say("mode request standard") == LOCKED_REFUSAL
        assert w.say("mode request advanced") != LOCKED_REFUSAL                  # raising the difficulty is never blocked
        monkeypatch.setattr(w.service.advanced_mode, "get_active_request",
                            lambda: SimpleNamespace(target_mode=OperatingMode.STANDARD, status=ModeTransitionStatus.AWAITING_CONFIRMATION))
        assert w.say("mode confirm") == LOCKED_REFUSAL
        report(core, w.uid, "unlocked")
        assert w.say("mode request standard") == "You're already in Standard Mode."

    def test_consent_safety_and_erasure_commands_are_never_blocked(self, core) -> None:
        w = World(core)
        w.seed()
        report(core, w.uid, "locked")
        for text in ("help", "status", "telemetry", "memory", "conditioning off", "conditioning on", "research off", "research status", "forget all",
                     "vacation", "vacation end", "lock status", "lock report unlocked"):
            assert w.say(text) not in (LOCKED_REFUSAL, UNKNOWN_REFUSAL), text

    def test_only_the_intended_handlers_carry_the_guard(self) -> None:
        tree = ast.parse((ROOT / "application" / "service.py").read_text(encoding="utf-8"))
        guarded = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                   for d in f.decorator_list if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "requires_unlocked"}
        assert guarded == {"_handle_mode_request_standard", "_handle_vacation_cancel"}


class TestLockedContext:
    class Model:
        def __init__(self) -> None:
            self.prompts = []

        def generate(self, *, request):
            self.prompts.append("\n".join(m.content for m in request.messages))
            return "Hold on."

    def test_the_marker_is_application_state_not_fenced_data(self) -> None:
        model = self.Model()
        wm = InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
        locked = {"s1": True}
        engine = ConversationEngine(model=model, working_memory_reader=wm, working_memory_writer=wm, queue=SubjectConversationQueue(),
                                    providers=(IronContractContextProvider(is_locked=lambda s: locked.get(s, False)),))
        engine.generate_response(subject_key="s1", current_user_message="please, one more day", language="en", identity_id="alex", now=NOW)
        engine.generate_response(subject_key="s2", current_user_message="hi", language="en", identity_id="alex", now=NOW)
        prompt, other = model.prompts
        assert "[STATE: LOCKED]" in prompt and "[STATE: LOCKED]" not in other.split("GUARDRAILS")[0]
        before_fence = prompt.split("<context>")[0]
        assert "[STATE: LOCKED] The user's lock is locked" in before_fence

    def test_the_provider_yields_nothing_unless_locked(self) -> None:
        for value in (False, None, "yes", 1):
            assert IronContractContextProvider(is_locked=lambda _s, v=value: v).provide_context(subject_key="s", now=NOW) is None

    def test_rule_7_is_firm_but_keeps_real_life_and_safety_first(self) -> None:
        rule = CONVERSATION_GUARDRAILS.split("7. LOCKED STATE:")[1]
        for part in ("[STATE: LOCKED]", "do not negotiate", "extend deadlines", "excuses", "Never mock", "illness", "family crisis",
                     "wellbeing comes first", "decided", "safety exception in rule 3 always wins"):
            assert part in rule, part


class TestVacationRules:
    def test_scheduling_rules(self, core) -> None:
        v = VacationService(core.db_path, core=core, timezone_name=TZ)
        today = date(2026, 11, 3)
        assert v.schedule("u", today, today - timedelta(days=1), now=NOW)[0] is ScheduleOutcome.INVALID
        assert v.schedule("u", today - timedelta(days=1), today, now=NOW)[0] is ScheduleOutcome.IN_THE_PAST
        assert v.schedule("u", today, today + timedelta(days=MAX_VACATION_DAYS), now=NOW)[0] is ScheduleOutcome.TOO_LONG
        assert v.schedule("u", today + timedelta(days=MAX_LEAD_DAYS + 1), today + timedelta(days=MAX_LEAD_DAYS + 2), now=NOW)[0] is ScheduleOutcome.TOO_FAR_AHEAD
        outcome, vac = v.schedule("u", today, today + timedelta(days=MAX_VACATION_DAYS - 1), now=NOW)
        assert outcome is ScheduleOutcome.SCHEDULED and vac.start_at == NOW                          # today: from now, never retroactive
        assert v.schedule("u", today + timedelta(days=30), today + timedelta(days=31), now=NOW)[0] is ScheduleOutcome.ALREADY_PLANNED
        assert v.schedule("other", today, today, now=NOW)[0] is ScheduleOutcome.SCHEDULED             # per user

    def test_running_end_and_cancel(self, core) -> None:
        v = VacationService(core.db_path, core=core, timezone_name=TZ)
        _, vac = v.schedule("u", date(2026, 11, 10), date(2026, 11, 12), now=NOW)
        assert vac.start_at == datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc) and vac.end_at == datetime(2026, 11, 12, 23, 0, tzinfo=timezone.utc)
        assert not v.is_paused("u", NOW) and v.is_paused("u", vac.start_at) and not v.is_paused("u", vac.end_at)
        assert not v.end_now("u", now=NOW) and v.cancel_upcoming("u", now=NOW) and v.current_or_next("u", NOW) is None
        _, vac = v.schedule("u", date(2026, 11, 10), date(2026, 11, 12), now=NOW)
        inside = vac.start_at + timedelta(days=1)
        assert not v.cancel_upcoming("u", now=inside) and v.end_now("u", now=inside) and not v.is_paused("u", inside + timedelta(seconds=1))
        day = (datetime(2026, 11, 9, 23, tzinfo=timezone.utc), datetime(2026, 11, 10, 23, tzinfo=timezone.utc))
        assert v.covers("u", *day) and not v.covers("u", day[1], day[1] + timedelta(days=1))   # the day it ended early is evaluated normally


class TestVacationCommands:
    def test_only_while_unlocked_and_end_always(self, core) -> None:
        w = World(core)
        assert w.say("vacation schedule 2026-11-10 2026-11-12") == UNKNOWN_REFUSAL
        report(core, w.uid, "locked")
        assert w.say("vacation schedule 2026-11-10 2026-11-12") == LOCKED_REFUSAL and w.vacation.current_or_next(w.uid, NOW) is None
        report(core, w.uid, "unlocked")
        assert w.say("vacation schedule 2026-11-10 2026-11-12").startswith("Vacation scheduled: 2026-11-10 to 2026-11-12.")
        assert "planned" in w.say("vacation")
        report(core, w.uid, "locked")
        assert w.say("vacation cancel") == LOCKED_REFUSAL and w.vacation.current_or_next(w.uid, NOW) is not None
        inside = datetime(2026, 11, 11, 9, tzinfo=timezone.utc)
        assert "running now" in w.say("vacation", at=inside)
        assert w.say("vacation end", at=inside).startswith("Your vacation has ended") and not w.vacation.is_paused(w.uid, inside + timedelta(minutes=1))

    def test_input_errors(self, core) -> None:
        w = World(core)
        report(core, w.uid, "unlocked")
        assert "Use `vacation schedule" in w.say("vacation schedule 2026-11-10")
        assert "Dates must look like" in w.say("vacation schedule tomorrow friday")
        assert f"at most {MAX_VACATION_DAYS} days" in w.say("vacation schedule 2026-11-10 2026-12-30")

    def test_without_the_contract_disclosed_vacations_still_need_unlocked(self, core) -> None:
        w = World(core, consent_target=CONSENT_TARGET)
        report(core, w.uid, "locked")
        assert w.say("vacation schedule 2026-11-10 2026-11-12") == LOCKED_REFUSAL
        assert w.say("mode request standard") == "You're already in Standard Mode."   # the difficulty gate needs the disclosure


class TestPauseEffects:
    def test_check_ins_are_closed_without_a_miss_and_nothing_is_sent(self, core) -> None:
        from schedule_windows.windows import WindowConfig
        from supervision.checkins import CheckInService
        sent = []
        cfg = WindowConfig.build(timezone_name=TZ, quiet_start="22:00", quiet_end="07:00", work_start="07:00", work_end="16:30", holiday_country="CZ")
        service = CheckInService(core.db_path, core=core, window_config=cfg, user_id="u", trust_domain="routine", get_trust_score=lambda: 0.3,
                                 sender=SimpleNamespace(send=lambda **kw: sent.append(kw)), evaluate=lambda p, r: None, paused=lambda now: True)
        with core.transaction() as tx:
            for i, (status, deadline) in enumerate((("scheduled", None), ("sent", NOW - timedelta(minutes=5)), ("insufficient_pending", None))):
                tx.execute("INSERT INTO check_ins (id, user_id, local_date, slot_index, level, due_at, status, deadline_at, follow_up_until, created_at, "
                           "updated_at) VALUES (?, 'u', '2026-11-03', ?, 2, ?, ?, ?, ?, ?, ?)",
                           (f"c{i}", i, (NOW - timedelta(hours=1)).isoformat(), status, deadline and deadline.isoformat(),
                            (NOW - timedelta(minutes=1)).isoformat(), NOW.isoformat(), NOW.isoformat()))
        service.tick(NOW)
        with core.transaction() as tx:
            statuses = [r["status"] for r in tx.fetch_all("SELECT status FROM check_ins ORDER BY id")]
            events = tx.fetch_all("SELECT event_type FROM domain_events WHERE event_type LIKE 'check_in.%'")
        assert statuses == ["skipped", "skipped", "skipped"] and sent == [] and events == []

    def test_a_fully_paused_day_changes_nothing(self, tmp_path) -> None:
        from tests.system.test_daily_evaluation import MON, TUE, WED, World as DayWorld, local
        w = DayWorld(tmp_path)
        v = VacationService(w.core.db_path, core=w.core, timezone_name=TZ)
        from system.daily_evaluation import DailyEvaluationTask
        from tests.system.test_daily_evaluation import CFG, U
        w.task = DailyEvaluationTask(w.core.db_path, core=w.core, window_config=CFG, user_id=U, trust_domain="routine", engine=w.engine, trust=w.tm,
                                     paused_between=lambda s, e: v.covers(U, s, e))
        w.task.run(local(TUE, 0, 5))                        # Monday: a normal day
        pool, streak = w.pool(), w.streak()
        v.schedule(U, TUE, TUE, now=local(MON, 12))
        w.check_in(TUE, 0, "missed")
        assert w.task.run(local(WED, 0, 5)) == [TUE.isoformat()]
        paused = w.rows()[-1]
        assert paused["paused"] == 1 and paused["tokens_earned"] == 0 and w.pool() == pool and w.streak() == streak
        store = TelemetryStore(w.core.db_path, core=w.core)
        TelemetryCollector(store).run(local(WED, 1))
        assert [d.get("local_date") for d in _daily_metrics(w.core)] == [MON.isoformat()]

    def test_a_release_during_a_vacation_never_raises_the_factor(self, tmp_path) -> None:
        from release_engine.conditioning import IncrementOutcome, ProgressiveConditioning
        from tests.release_engine.test_progressive_conditioning import MON, U, W
        w = W(tmp_path)
        v = VacationService(w.core.db_path, core=w.core, timezone_name=TZ)
        cond = ProgressiveConditioning(w.core.db_path, core=w.core, vacation_in=v.running_id_in)
        first = w.release()
        v.schedule(U, MON, MON, now=datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc) - timedelta(hours=1))
        with w.core.transaction() as tx:
            outcome = cond.on_release_confirmed_in_transaction(tx, authorization_id=first, now=datetime(2026, 10, 12, 20, tzinfo=timezone.utc))
            again = cond.on_release_confirmed_in_transaction(tx, authorization_id=first, now=datetime(2026, 10, 12, 20, tzinfo=timezone.utc))
            paused_rows = tx.fetch_all("SELECT * FROM conditioning_paused_releases")
            increments = tx.fetch_all("SELECT * FROM training_increments")
        assert outcome is IncrementOutcome.PAUSED and again is None and len(paused_rows) == 1 and increments == []
        assert cond.factor(U) == 1


def _daily_metrics(core):
    with core.transaction() as tx:
        return [json.loads(r["metrics"]) for r in tx.fetch_all("SELECT metrics FROM behavioral_telemetry WHERE event_type = 'daily_summary'")]


class TestCrossTenantAudit:
    """Phase G6 item 4: nothing of Phase G crosses from one tenant to another."""

    def test_two_tenants_share_nothing(self, tmp_path) -> None:
        from behavioral_telemetry.analysis import summarize
        from behavioral_telemetry.research import ResearchExporter, subject_pseudonym
        from database.database import Database
        from database.models import ConsentAction, ConsentRecord, ConsentTargetType
        from tests.system.test_tenant_factory import World as TenantWorld

        tw = TenantWorld(tmp_path)
        a = tw.factory.build(tw.wearer("Alex", "2000002").id)
        b = tw.factory.build(tw.wearer("Blake", "2000003").id)
        for runtime, name in ((a, "Alex"), (b, "Blake")):
            Database(runtime.db_path, core=runtime.core).save_consent_record(ConsentRecord(
                created_at=tw.clock.now(), target_type=ConsentTargetType.INTEGRATION, target_id=CONSENT_TARGET, action=ConsentAction.APPROVED,
                explanation_shown=invitation_text(name), user_comment="accept"))
        wa, wb = a.wiring, b.wiring
        for attr in ("telemetry_buffer", "telemetry_store", "research_preferences", "iron_contract", "vacation"):
            assert getattr(wa, attr) is not getattr(wb, attr), attr
        for i, text in enumerate(("anything", "english", "neutral", "alex", "what should I focus on today?")):
            tw.say(a, "2000002", text, f"a{i}")
        assert len(wa.telemetry_buffer) == 1 and len(wb.telemetry_buffer) == 0
        a.cycle()
        b.cycle()
        rows = {}
        for runtime in (a, b):
            with runtime.core.transaction() as tx:
                rows[runtime.tenant_id] = [r["user_id"] for r in tx.fetch_all("SELECT user_id FROM behavioral_telemetry WHERE event_type = 'llm_generation'")]
                # 2026-11-13: each tenant's daily evaluation now runs too; its rows stay its own wearer's, in its own database
                assert {r["user_id"] for r in tx.fetch_all("SELECT user_id FROM behavioral_telemetry")} <= {runtime.wearer_user_id}
        assert rows[a.tenant_id] == [a.wearer_user_id] and rows[b.tenant_id] == []
        with tw.main.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM behavioral_telemetry")["n"] == 0
        # research: opt-in, erase and pseudonyms stay inside each tenant
        wa.research_preferences.enable(a.wearer_user_id, now=tw.clock.now() - timedelta(days=1))
        wa.research_preferences.revoke(a.wearer_user_id, now=tw.clock.now())
        assert not wb.research_preferences.is_enabled(b.wearer_user_id)
        with b.core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM research_erase_requests")["n"] == 0
        key = "k" * 40
        ea, eb = ResearchExporter(a.core, tenant_id=a.tenant_id, key=key, export_records=True), ResearchExporter(b.core, tenant_id=b.tenant_id, key=key,
                                                                                                                export_records=True)
        assert [e["subject"] for _, e in ea.pending_erases()] == [subject_pseudonym(key, a.tenant_id, a.wearer_user_id)] and eb.pending_erases() == []
        assert subject_pseudonym(key, a.tenant_id, "same-id") != subject_pseudonym(key, b.tenant_id, "same-id")
        # analysis reads one database only
        assert [s.user_id for s in summarize(a.db_path, days=30, now=tw.clock.now())] == [a.wearer_user_id]
        assert {x.user_id for x in summarize(b.db_path, days=30, now=tw.clock.now())} <= {b.wearer_user_id}   # its own daily evaluation only
        # vacations and the contract are per tenant
        wa.vacation.schedule(a.wearer_user_id, date(2026, 11, 20), date(2026, 11, 21), now=tw.clock.now())
        assert wb.vacation.current_or_next(a.wearer_user_id, tw.clock.now()) is None
        assert wb.vacation.current_or_next(b.wearer_user_id, tw.clock.now()) is None

    def test_the_receiver_keeps_senders_apart(self) -> None:
        text = (ROOT / "hive_mind_receiver" / "app.py").read_text(encoding="utf-8")
        assert "PRIMARY KEY (sender, id)" in text and "DELETE FROM records WHERE sender = ? AND subject = ?" in text
        assert "WHERE sender = ? AND id = ?" in text
