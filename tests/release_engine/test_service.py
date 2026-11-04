"""Release engine domain core: ledger, plans, commit point, provider states, backstop, secrecy."""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import parse_iso
from release_engine.ledger import LedgerEntryType, insert_entry, reverse_entry
from release_engine.service import CommitStatus as C
from release_engine.service import ProviderState as P
from release_engine.service import ReleaseEngine, ReleaseGates
from schedule_windows.windows import WindowConfig, WindowKind, classify

ROOT = Path(__file__).parent.parent.parent
PRAGUE = ZoneInfo("Europe/Prague")
CFG = WindowConfig.build(timezone_name="Europe/Prague", quiet_start="22:00", quiet_end="07:00",
                         work_start="07:00", work_end="16:30", holiday_country="CZ")
SEED = bytes(range(32))
U = "user-1"
MON = date(2026, 10, 12)


def local(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=PRAGUE).astimezone(timezone.utc)


OK = ReleaseGates(lock_barrier_confirmed=True)


@pytest.fixture
def core(tmp_path: Path) -> CoreDatabase:
    c = CoreDatabase(tmp_path / "test.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def make(core, **kw) -> ReleaseEngine:
    args = dict(window_config=CFG, min_offset_minutes=30, max_offset_minutes=210, max_hold_hours=72,
                seed_factory=lambda: SEED)
    args.update(kw)
    return ReleaseEngine(core.db_path, core=core, **args)


def target_of(core) -> datetime:
    with core.transaction() as tx:
        return parse_iso(tx.fetch_one("SELECT target_at FROM release_plans WHERE status = 'planned'")["target_at"])


def plan_status(core) -> list[str]:
    with core.transaction() as tx:
        return [r["status"] for r in tx.fetch_all("SELECT status FROM release_plans ORDER BY rowid")]


def ready(core, eng, now=None):
    """One token earned, plan created Monday morning; returns (now, target)."""
    now = now or local(MON, 10, 0)
    eng.earn_tokens(user_id=U, amount=1, reason_code="goal_met", source_event_id="e1", now=now)
    assert eng.ensure_plan(user_id=U, now=now) is True
    return now, target_of(core)


class TestLedger:
    def test_balance_is_the_sum(self, core) -> None:
        eng = make(core)
        eng.earn_tokens(user_id=U, amount=2, reason_code="a", source_event_id="x", now=local(MON, 9))
        eng.earn_tokens(user_id=U, amount=1, reason_code="b", source_event_id="y", now=local(MON, 9))
        assert eng.get_balance(U) == 3 and eng.get_balance("other") == 0

    def test_earn_is_idempotent_per_source_event(self, core) -> None:
        eng = make(core)
        assert eng.earn_tokens(user_id=U, amount=1, reason_code="a", source_event_id="x", now=local(MON, 9))
        assert eng.earn_tokens(user_id=U, amount=1, reason_code="a", source_event_id="x", now=local(MON, 9)) is None
        assert eng.get_balance(U) == 1

    @pytest.mark.parametrize("amount", [0, -1, 1.5, True, "1"])
    def test_invalid_earn_amount(self, core, amount) -> None:
        with pytest.raises(ValueError):
            make(core).earn_tokens(user_id=U, amount=amount, reason_code="a", source_event_id=None, now=local(MON, 9))

    def test_entry_validation(self, core) -> None:
        with core.transaction() as tx:
            with pytest.raises(ValueError):
                insert_entry(tx, user_id=U, delta=0, entry_type=LedgerEntryType.EARN, reason_code="r", now=local(MON, 9))
            with pytest.raises(ValueError):
                insert_entry(tx, user_id=U, delta=-1, entry_type=LedgerEntryType.EARN, reason_code="r", now=local(MON, 9))
            with pytest.raises(ValueError):
                insert_entry(tx, user_id=U, delta=1, entry_type=LedgerEntryType.SPEND, reason_code="r", now=local(MON, 9))
            with pytest.raises(ValueError):
                insert_entry(tx, user_id=U, delta=1, entry_type=LedgerEntryType.EARN, reason_code=" ", now=local(MON, 9))

    def test_one_spend_per_authorization_is_enforced_by_the_database(self, core) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with core.transaction() as tx:
                for _ in range(2):
                    insert_entry(tx, user_id=U, delta=-1, entry_type=LedgerEntryType.SPEND, reason_code="r",
                                 now=local(MON, 9), authorization_decision_id="auth-1")

    def test_reversal_is_idempotent_and_cannot_be_reversed(self, core) -> None:
        with core.transaction() as tx:
            e = insert_entry(tx, user_id=U, delta=-1, entry_type=LedgerEntryType.SPEND, reason_code="r", now=local(MON, 9))
            rev = reverse_entry(tx, original_entry_id=e, reason_code="undo", now=local(MON, 9))
            assert rev is not None and reverse_entry(tx, original_entry_id=e, reason_code="undo", now=local(MON, 9)) is None
            with pytest.raises(ValueError):
                reverse_entry(tx, original_entry_id=rev, reason_code="undo", now=local(MON, 9))
            with pytest.raises(LookupError):
                reverse_entry(tx, original_entry_id="nope", reason_code="undo", now=local(MON, 9))

    def test_ledger_exposes_no_update_or_delete(self) -> None:
        import release_engine.ledger as ledger
        assert not [n for n in dir(ledger) if n.startswith(("update", "delete", "remove"))]


class TestPlans:
    def test_no_plan_without_a_token(self, core) -> None:
        assert make(core).ensure_plan(user_id=U, now=local(MON, 10)) is False

    def test_reaching_the_requirement_never_unlocks_immediately(self, core) -> None:
        eng = make(core)
        now, target = ready(core, eng)
        assert target > now
        out = eng.commit_authorization(user_id=U, gates=OK, now=now)
        assert out.status is C.NOT_DUE and eng.get_balance(U) == 1

    def test_one_planned_plan_per_user(self, core) -> None:
        eng = make(core)
        now, target = ready(core, eng)
        assert eng.ensure_plan(user_id=U, now=now + timedelta(minutes=5)) is False
        assert target_of(core) == target

    def test_plan_survives_a_restart_without_redrawing(self, core) -> None:
        eng = make(core)
        now, target = ready(core, eng)
        again = make(core, seed_factory=lambda: bytes(32 * [9]))     # a "restarted" process, different RNG
        assert again.ensure_plan(user_id=U, now=now + timedelta(hours=1)) is False
        assert target_of(core) == target

    def test_cancel_plan(self, core) -> None:
        eng = make(core)
        ready(core, eng)
        assert eng.cancel_plan(user_id=U, reason="manual", now=local(MON, 11)) is True
        assert plan_status(core) == ["cancelled"] and eng.cancel_plan(user_id=U, reason="x", now=local(MON, 11)) is False


class TestCommitAuthorization:
    def test_no_plan(self, core) -> None:
        assert make(core).commit_authorization(user_id=U, gates=OK, now=local(MON, 18)).status is C.NO_PLAN

    def test_due_in_the_window_spends_exactly_one_token(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        out = eng.commit_authorization(user_id=U, gates=OK, now=target)
        assert out.status is C.AUTHORIZED and out.authorization_id
        assert eng.get_balance(U) == 0 and plan_status(core) == ["authorized"]
        assert eng.get_authorization_state(out.authorization_id) is P.AUTHORIZED
        with core.transaction() as tx:
            spend = tx.fetch_one("SELECT * FROM token_ledger WHERE entry_type = 'spend'")
        assert spend["delta"] == -1 and spend["authorization_decision_id"] == out.authorization_id

    def test_never_spends_twice(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        eng.commit_authorization(user_id=U, gates=OK, now=target)
        again = eng.commit_authorization(user_id=U, gates=OK, now=target + timedelta(minutes=1))
        assert again.status is C.ALREADY_OPEN and eng.get_balance(U) == 0

    def test_late_but_still_in_the_window_is_authorised(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        out = eng.commit_authorization(user_id=U, gates=OK, now=target + timedelta(minutes=5))
        assert out.status is C.AUTHORIZED

    @pytest.mark.parametrize("gates,reason", [
        (ReleaseGates(lock_barrier_confirmed=True, penalty_window_active=True), "penalty_window_active"),
        (ReleaseGates(lock_barrier_confirmed=True, supervision_active=True), "supervision_active"),
    ])
    def test_hard_vetoes_cancel_the_plan_and_keep_the_token(self, core, gates, reason) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        out = eng.commit_authorization(user_id=U, gates=gates, now=target)
        assert out.status is C.VETOED and out.reason == reason
        assert plan_status(core) == ["cancelled"] and eng.get_balance(U) == 1

    def test_unconfirmed_lock_state_keeps_the_plan_for_a_retry(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        out = eng.commit_authorization(user_id=U, gates=ReleaseGates(lock_barrier_confirmed=False), now=target)
        assert out.status is C.GATE_NOT_MET and plan_status(core) == ["planned"] and eng.get_balance(U) == 1
        assert eng.commit_authorization(user_id=U, gates=OK, now=target + timedelta(minutes=1)).status in (
            C.AUTHORIZED, C.RETARGETED)

    @pytest.mark.parametrize("hour,minute", [(3, 0), (10, 0), (6, 59)])
    def test_due_but_window_closed_retargets_into_a_valid_window(self, core, hour, minute) -> None:
        eng = make(core)
        ready(core, eng)
        next_day = local(date(2026, 10, 13), hour, minute)          # Tuesday: quiet / work
        assert classify(CFG, next_day) is not WindowKind.DISCRETIONARY
        out = eng.commit_authorization(user_id=U, gates=OK, now=next_day)
        assert out.status is C.RETARGETED and eng.get_balance(U) == 1 and plan_status(core) == ["planned"]
        new_target = target_of(core)
        assert new_target > next_day and classify(CFG, new_target) is WindowKind.DISCRETIONARY

    def test_balance_recheck_inside_the_commit(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        with core.transaction() as tx:      # the token disappears between planning and commit
            insert_entry(tx, user_id=U, delta=-1, entry_type=LedgerEntryType.ADJUSTMENT, reason_code="manual",
                         now=local(MON, 12))
        out = eng.commit_authorization(user_id=U, gates=OK, now=target)
        assert out.status is C.INSUFFICIENT_TOKENS and plan_status(core) == ["cancelled"]
        assert eng.get_balance(U) == 0                      # never negative from a release

    def test_daily_tasks_alone_do_not_create_a_plan(self, core) -> None:
        assert make(core).ensure_plan(user_id=U, now=local(MON, 10)) is False

    def test_outcome_carries_no_timestamp(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        out = eng.commit_authorization(user_id=U, gates=OK, now=target)
        assert not any(isinstance(v, datetime) for v in vars(out).values())


class TestProviderStates:
    def authorised(self, core):
        eng = make(core)
        _, target = ready(core, eng)
        return eng, eng.commit_authorization(user_id=U, gates=OK, now=target).authorization_id

    def test_happy_path(self, core) -> None:
        eng, a = self.authorised(core)
        assert eng.mark_provider_requested(authorization_id=a, now=local(MON, 18))
        assert eng.mark_provider_confirmed(authorization_id=a, now=local(MON, 18))
        assert eng.get_authorization_state(a) is P.PROVIDER_CONFIRMED and eng.get_balance(U) == 0

    def test_failure_compensates_the_spend_exactly_once(self, core) -> None:
        eng, a = self.authorised(core)
        eng.mark_provider_requested(authorization_id=a, now=local(MON, 18))
        assert eng.mark_provider_failed(authorization_id=a, detail="rejected", now=local(MON, 18))
        assert eng.get_authorization_state(a) is P.PROVIDER_FAILED and eng.get_balance(U) == 1
        assert eng.mark_provider_failed(authorization_id=a, detail="again", now=local(MON, 18)) is False
        assert eng.get_balance(U) == 1
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM token_ledger WHERE entry_type = 'reversal'")["n"] == 1

    def test_unknown_physical_state_keeps_the_spend(self, core) -> None:
        eng, a = self.authorised(core)
        eng.mark_provider_requested(authorization_id=a, now=local(MON, 18))
        assert eng.mark_physical_state_unknown(authorization_id=a, detail="timeout", now=local(MON, 18))
        assert eng.get_balance(U) == 0
        assert eng.commit_authorization(user_id=U, gates=OK, now=local(MON, 19)).status is C.ALREADY_OPEN

    def test_unknown_can_later_be_confirmed_by_a_human_check(self, core) -> None:
        eng, a = self.authorised(core)
        eng.mark_provider_requested(authorization_id=a, now=local(MON, 18))
        eng.mark_physical_state_unknown(authorization_id=a, detail="timeout", now=local(MON, 18))
        assert eng.mark_provider_confirmed(authorization_id=a, now=local(MON, 20))

    @pytest.mark.parametrize("first,second", [
        ("confirmed", "requested"), ("confirmed", "failed"), ("failed", "confirmed"), ("failed", "requested"),
    ])
    def test_illegal_transitions_are_refused(self, core, first, second) -> None:
        eng, a = self.authorised(core)
        eng.mark_provider_requested(authorization_id=a, now=local(MON, 18))
        {"confirmed": eng.mark_provider_confirmed,
         "failed": lambda **k: eng.mark_provider_failed(detail="d", **k)}[first](authorization_id=a, now=local(MON, 18))
        fn = {"requested": eng.mark_provider_requested, "confirmed": eng.mark_provider_confirmed,
              "failed": lambda **k: eng.mark_provider_failed(detail="d", **k)}[second]
        assert fn(authorization_id=a, now=local(MON, 18)) is False

    def test_confirm_requires_a_request_first(self, core) -> None:
        eng, a = self.authorised(core)
        assert eng.mark_provider_confirmed(authorization_id=a, now=local(MON, 18)) is False

    def test_a_failed_release_can_be_planned_again(self, core) -> None:
        eng, a = self.authorised(core)
        eng.mark_provider_failed(authorization_id=a, detail="not sent", now=local(MON, 18))
        assert eng.ensure_plan(user_id=U, now=local(MON, 18, 30)) is True


class TestBackstop:
    def test_not_due_before_the_maximum_hold(self, core) -> None:
        eng = make(core)
        now = local(MON, 18)
        out = eng.commit_backstop(user_id=U, barrier_since=now - timedelta(hours=71), gates=OK, now=now)
        assert out.status is C.NOT_DUE

    def test_after_the_hold_it_releases_without_a_token(self, core) -> None:
        eng = make(core)
        now = local(MON, 18)
        out = eng.commit_backstop(user_id=U, barrier_since=now - timedelta(hours=73), gates=OK, now=now)
        assert out.status is C.AUTHORIZED and eng.get_balance(U) == 0
        with core.transaction() as tx:
            assert tx.fetch_one("SELECT COUNT(*) AS n FROM token_ledger")["n"] == 0
            assert tx.fetch_one("SELECT kind FROM release_authorizations")["kind"] == "backstop"

    @pytest.mark.parametrize("hour", [3, 10])
    def test_backstop_still_respects_quiet_and_work_hours(self, core, hour) -> None:
        eng = make(core)
        now = local(date(2026, 10, 13), hour)
        out = eng.commit_backstop(user_id=U, barrier_since=now - timedelta(days=5), gates=OK, now=now)
        assert out.status is C.WINDOW_CLOSED

    def test_backstop_needs_a_confirmed_barrier(self, core) -> None:
        now = local(MON, 18)
        out = make(core).commit_backstop(user_id=U, barrier_since=now - timedelta(days=5),
                                         gates=ReleaseGates(lock_barrier_confirmed=False), now=now)
        assert out.status is C.GATE_NOT_MET

    def test_backstop_ignores_vetoes_and_adopts_a_planned_plan(self, core) -> None:
        eng = make(core)
        ready(core, eng)
        now = local(MON, 19)
        gates = ReleaseGates(lock_barrier_confirmed=True, penalty_window_active=True, supervision_active=True)
        out = eng.commit_backstop(user_id=U, barrier_since=now - timedelta(days=5), gates=gates, now=now)
        assert out.status is C.AUTHORIZED and plan_status(core) == ["authorized"] and eng.get_balance(U) == 1

    def test_invalid_hold(self, core) -> None:
        with pytest.raises(ValueError):
            make(core, max_hold_hours=0)


class TestSecrecy:
    def test_no_event_payload_or_outcome_contains_the_secret_target_or_seed(self, core) -> None:
        eng = make(core)
        _, target = ready(core, eng)
        a = eng.commit_authorization(user_id=U, gates=OK, now=target).authorization_id
        eng.mark_provider_requested(authorization_id=a, now=target)
        eng.mark_provider_confirmed(authorization_id=a, now=target)
        with core.transaction() as tx:
            payloads = [r["payload_json"] for r in tx.fetch_all("SELECT payload_json FROM domain_events")]
            stored_target = tx.fetch_one("SELECT target_at FROM release_plans")["target_at"]
        assert payloads
        for p in payloads:
            assert stored_target not in p and SEED.hex() not in p and "target" not in p and "seed" not in p
            assert not any(k in json.loads(p) for k in ("target_at", "seed", "scheduled_at"))

    def test_nothing_model_or_user_facing_can_import_the_release_engine(self) -> None:
        for pkg in ("conversation_engine", "application", "bot", "chaster", "ai", "memory_system", "rules_engine"):
            for path in (ROOT / pkg).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                    assert not any(n.split(".")[0] == "release_engine" for n in names), (path, names)

    def test_release_engine_imports_nothing_that_reaches_a_model_or_chaster(self) -> None:
        forbidden = ("discord", "bot", "conversation_engine", "application", "chaster", "chaster_execution", "ai",
                     "memory_system", "requests")
        for path in (ROOT / "release_engine").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                for n in names:
                    assert n.split(".")[0] not in forbidden, (path.name, n)


class TestReleaseSession:
    def _confirmed(self, core, eng, at):
        eng.earn_tokens(user_id=U, amount=1, reason_code="r", source_event_id="e", now=local(MON, 9))
        eng.ensure_plan(user_id=U, now=local(MON, 9))
        out = eng.commit_authorization(user_id=U, gates=OK, now=target_of(core))
        eng.mark_provider_requested(authorization_id=out.authorization_id, now=at)
        eng.mark_provider_confirmed(authorization_id=out.authorization_id, now=at)
        return out.authorization_id

    def test_no_confirmed_release_means_no_session(self, core) -> None:
        assert make(core).last_confirmed_release(U) is None

    def test_the_last_confirmed_release_is_reported(self, core) -> None:
        eng = make(core)
        auth = self._confirmed(core, eng, local(MON, 18))
        assert eng.last_confirmed_release(U) == (auth, local(MON, 18))
        assert eng.last_confirmed_release("someone-else") is None

    def test_failed_or_unknown_releases_are_not_confirmed(self, core) -> None:
        eng = make(core)
        eng.earn_tokens(user_id=U, amount=1, reason_code="r", source_event_id="e", now=local(MON, 9))
        eng.ensure_plan(user_id=U, now=local(MON, 9))
        out = eng.commit_authorization(user_id=U, gates=OK, now=target_of(core))
        eng.mark_provider_requested(authorization_id=out.authorization_id, now=local(MON, 18))
        eng.mark_physical_state_unknown(authorization_id=out.authorization_id, detail="x", now=local(MON, 18))
        assert eng.last_confirmed_release(U) is None

    @pytest.mark.parametrize("hh,mm,over", [(18, 0, False), (21, 59, False), (22, 0, True), (23, 30, True), (3, 0, True)])
    def test_the_session_ends_with_the_discretionary_window(self, core, hh, mm, over) -> None:
        eng = make(core)
        day = MON if hh >= 7 else MON + timedelta(days=1)
        assert eng.release_session_over(local(MON, 17, 0), local(day, hh, mm)) is over

    def test_work_hours_of_the_next_day_end_the_session(self, core) -> None:
        eng = make(core)
        assert eng.release_session_over(local(MON, 17, 0), local(MON + timedelta(days=1), 10, 0)) is True

    def test_the_next_days_discretionary_window_is_a_new_session(self, core) -> None:
        eng = make(core)
        assert eng.release_session_over(local(MON, 17, 0), local(MON + timedelta(days=1), 18, 0)) is True

    def test_a_weekend_session_lasts_until_quiet_hours(self, core) -> None:
        eng = make(core)
        sat = date(2026, 10, 17)
        assert eng.release_session_over(local(sat, 11, 0), local(sat, 15, 0)) is False
        assert eng.release_session_over(local(sat, 11, 0), local(sat, 22, 0)) is True

    def test_earning_in_a_transaction_is_idempotent_per_event(self, core) -> None:
        eng = make(core)
        with core.transaction() as tx:
            assert eng._earn_in_transaction(tx, user_id=U, amount=1, reason_code="goal.completed", source_event_id="ev", now=local(MON, 9))
            assert eng._earn_in_transaction(tx, user_id=U, amount=1, reason_code="goal.completed", source_event_id="ev", now=local(MON, 9)) is None
        assert eng.get_balance(U) == 1
        with pytest.raises(ValueError):
            with core.transaction() as tx:
                eng._earn_in_transaction(tx, user_id=U, amount=0, reason_code="x", source_event_id="e2", now=local(MON, 9))
