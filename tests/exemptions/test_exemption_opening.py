"""An exemption opens the lock, and "I'm back" is checked against Chaster, not believed. Fake Chaster, no network."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from chaster.extension_config import ConfigurationReport, ExtensionSnapshot, LockConfiguration
from chaster.extension_plan import plan_opening_settings
from chaster.managed_locks import LockRole
from chaster_execution.models import TransportOutcome, TransportResult
from chaster_execution.temporary_opening import CLEAR_COOLDOWN_REGULARITY, ExemptionOpening, OpeningOutcome, OpeningResult, OpeningStatus
from infractions.excuses import Excuses
from infractions.judge import ExplanationJudge
from infractions.signals import APPEAL_WINDOW, UNANSWERED_RULE_GROUP, GRACE, InfractionSignals, SignalKind
from infrastructure.clock import FrozenClock
from infrastructure.time_format import iso
from infractions.wiring import DetectorWiring, overrun_signal
from penalty_engine.models import FreezeReason
from tests.exemptions.test_exemptions import OWNER, OWNER_DISCORD, WEARER, WEARER_DISCORD
from tests.exemptions.test_exemptions import World as ExemptionWorld
from tests.system.test_penalty_pipeline import LOCK, NOW

ROOT = Path(__file__).parent.parent.parent
SAVED = {"openingTime": 900, "regularity": 172800}


class FakeOpening:
    """The port: Chaster as a scripted door."""

    lock_id = LOCK

    def __init__(self, result=None) -> None:
        self.result = result or OpeningResult(outcome=OpeningOutcome.OPENED, saved=SAVED)
        self.state = OpeningStatus(is_open=True, relocked_at=None)
        self.opens, self.restored = [], []
        self.restore_ok = True

    def open_for(self, hours):
        self.opens.append(hours)
        return self.result

    def status(self, *, opened_at):
        return self.state

    def restore(self, saved):
        self.restored.append(saved)
        return self.restore_ok


class World(ExemptionWorld):
    def __init__(self, tmp_path, opening=None, **kwargs) -> None:
        super().__init__(tmp_path, **kwargs)
        self.opening = opening or FakeOpening()
        self.service._opening = self.opening

    def approved(self, hours=6, at=NOW):
        ident = self.request(hours=hours, at=at)
        self.approve(ident, at)
        return ident

    def back(self, ident, at):
        self.at = at
        return self.dm.press(discord_user_id=WEARER_DISCORD, action="end", ident=ident)


class TestApprovalOpensTheLock:
    def test_an_approval_opens_it_for_the_approved_length(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approved(hours=6)
        assert w.opening.opens == [6]
        row = w.exemption()
        assert row["opened_at"] is not None and row["opening_lock_id"] == LOCK and row["status"] == "approved"

    def test_with_no_penalty_window_too(self, tmp_path) -> None:
        """The owner's decision: useful any time, not only during a penalty."""
        w = World(tmp_path)
        assert w.window() is None
        w.approved()
        assert w.opening.opens == [6]

    def test_a_decline_opens_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.request()
        w.dm.press(discord_user_id=OWNER_DISCORD, action="decline", ident=ident)
        w.service.tick(NOW + timedelta(minutes=1))
        assert w.opening.opens == []

    def test_a_request_alone_opens_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.request()
        w.service.tick(NOW + timedelta(minutes=1))
        assert w.opening.opens == []

    def test_only_the_keyholder_can_cause_an_opening(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.request()
        w.dm.press(discord_user_id=WEARER_DISCORD, action="approve", ident=ident)
        assert w.opening.opens == []

    def test_it_opens_once_however_many_passes_run(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approved()
        for minute in range(1, 5):
            w.service.tick(NOW + timedelta(minutes=minute))
        assert len(w.opening.opens) == 1

    def test_the_approval_says_where_the_combination_is(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approved()
        text = w.notices("exemption_decision")[0]["text"]
        assert "combination appears in Chaster" in text and "relock in Chaster" in text

    def test_without_a_configured_lock_nothing_opens_and_the_text_says_so(self, tmp_path) -> None:
        w = World(tmp_path)
        w.service._opening = None
        w.approved()
        assert "opening your lock" not in w.notices("exemption_decision")[0]["text"]

    def test_a_window_still_pauses_as_before(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        at = NOW + timedelta(hours=1)
        w.approved(at=at)
        assert w.window().status.value == "frozen" and w.opening.opens == [6]


class TestWhenItCannotOpen:
    def test_a_halt_sends_nothing_and_it_opens_when_the_halt_clears(self, tmp_path) -> None:
        w = World(tmp_path, FakeOpening(OpeningResult(outcome=OpeningOutcome.HALTED)))
        w.approved()
        row = w.exemption()
        assert row["opened_at"] is None and row["opening_claimed_at"] is None
        assert "not allowed to act" in w.notices("exemption_opening")[0]["text"]
        w.opening.result = OpeningResult(outcome=OpeningOutcome.OPENED, saved=SAVED)
        w.service.tick(NOW + timedelta(minutes=1))
        assert w.exemption()["opened_at"] is not None and len(w.opening.opens) == 2

    def test_a_halt_that_outlasts_the_exemption_never_opens_late(self, tmp_path) -> None:
        w = World(tmp_path, FakeOpening(OpeningResult(outcome=OpeningOutcome.HALTED)))
        w.approved(hours=6)
        w.opening.result = OpeningResult(outcome=OpeningOutcome.OPENED, saved=SAVED)
        calls = len(w.opening.opens)
        w.service.tick(NOW + timedelta(hours=7))
        assert len(w.opening.opens) == calls and w.exemption()["status"] == "ended"

    def test_an_ambiguous_opening_is_never_sent_twice(self, tmp_path) -> None:
        w = World(tmp_path, FakeOpening(OpeningResult(outcome=OpeningOutcome.AMBIGUOUS, saved=SAVED, detail="raised")))
        w.approved()
        w.service.tick(NOW + timedelta(minutes=1))
        w.service.tick(NOW + timedelta(minutes=2))
        assert len(w.opening.opens) == 1

    def test_a_refusal_reaches_both_people_and_keeps_the_safety_line(self, tmp_path) -> None:
        w = World(tmp_path, FakeOpening(OpeningResult(outcome=OpeningOutcome.UNAVAILABLE, detail="http_400")))
        w.approved()
        rows = w.notices("exemption_opening")
        assert {r["user_id"] for r in rows} == {OWNER, WEARER} and any("do what keeps you safe" in r["text"] for r in rows)

    def test_a_failure_in_one_pass_never_escapes(self, tmp_path) -> None:
        w = World(tmp_path)

        def boom(_hours):
            raise RuntimeError("boom")

        w.opening.open_for = boom
        assert w.approved() is not None


class TestImBackIsChecked:
    def test_still_open_keeps_the_exemption_running(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.approved()
        reply = w.back(ident, NOW + timedelta(hours=1))
        assert "still shows your lock as open" in reply.text and reply.actions
        assert w.exemption()["status"] == "approved"

    def test_relocked_in_time_ends_it_and_says_so(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.approved()
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=5))
        reply = w.back(ident, NOW + timedelta(hours=5, minutes=1))
        row = w.exemption()
        assert "relocked in time" in reply.text and row["status"] == "ended" and row["end_reason"] == "relocked_on_time"
        assert w.opening.restored == [SAVED]

    def test_chaster_unreadable_is_not_believed_either_way(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.approved()
        w.opening.state = None
        reply = w.back(ident, NOW + timedelta(hours=1))
        assert "can't check Chaster" in reply.text and w.exemption()["status"] == "approved"

    def test_the_check_uses_chasters_time_not_the_press(self, tmp_path) -> None:
        """Relocked in time and pressed later is on time."""
        w = World(tmp_path)
        ident = w.approved(hours=6)
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=5))
        w.back(ident, NOW + timedelta(hours=20))
        assert w.exemption()["end_reason"] == "relocked_on_time"

    def test_relocked_late_is_recorded_as_late(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.approved(hours=6)
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=7))
        reply = w.back(ident, NOW + timedelta(hours=7, minutes=1))
        assert "after the approved time" in reply.text and w.exemption()["end_reason"] == "relocked_late"
        assert any("after the approved time" in r["text"] for r in w.notices("exemption_opening"))

    def test_a_close_with_no_log_entry_is_not_guessed_at(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.approved()
        w.opening.state = OpeningStatus(is_open=False, relocked_at=None)
        w.back(ident, NOW + timedelta(hours=1))
        assert w.exemption()["end_reason"] == "closed_unverified" and any("no relock entry" in r["text"] for r in w.notices("exemption_opening"))

    def test_settings_that_cannot_be_restored_are_reported(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.approved()
        w.opening.restore_ok = False
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=1))
        w.back(ident, NOW + timedelta(hours=1))
        assert any("couldn't put" in r["text"] for r in w.notices("exemption_opening"))


class TestAfterTheTime:
    def test_still_open_gets_one_reminder_and_stays_open(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approved(hours=6)
        for hour in (7, 8, 9):
            w.service.tick(NOW + timedelta(hours=hour))
        assert len(w.notices("exemption_overdue")) == 1 and w.exemption()["status"] == "approved"

    def test_a_relock_after_the_reminder_settles_it_as_late(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approved(hours=6)
        w.service.tick(NOW + timedelta(hours=7))
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=8))
        w.service.tick(NOW + timedelta(hours=9))
        assert w.exemption()["end_reason"] == "relocked_late"

    def test_a_silent_relock_in_time_settles_at_the_end(self, tmp_path) -> None:
        w = World(tmp_path)
        w.approved(hours=6)
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=2))
        w.service.tick(NOW + timedelta(hours=7))
        assert w.exemption()["end_reason"] == "relocked_on_time" and w.notices("exemption_overdue") == []

    def test_the_penalty_window_resumes_at_the_end_of_the_time_either_way(self, tmp_path) -> None:
        w = World(tmp_path)
        w.open_window()
        at = NOW + timedelta(hours=1)
        w.approved(hours=6, at=at)
        w.run(at + timedelta(hours=7))
        assert w.window().status.value == "active"


class TestALateRelockTakesTheSignalsPath:
    def _world(self, tmp_path):
        w = World(tmp_path)
        self.registered = []
        signals = InfractionSignals(core=w.core, excuses=Excuses(w.core), judge=ExplanationJudge(None), register_incident=lambda s, n: self.registered.append(s),
                                    confirm_incident=lambda *a: None)
        w.service.set_overrun_signal(overrun_signal(signals, trust_domain="routine"))
        return w, signals

    def _late(self, w):
        ident = w.approved(hours=6)
        w.opening.state = OpeningStatus(is_open=False, relocked_at=NOW + timedelta(hours=7))
        w.back(ident, NOW + timedelta(hours=7))

    def test_it_waits_for_the_wearer_first(self, tmp_path) -> None:
        w, _ = self._world(tmp_path)
        self._late(w)
        assert [r["status"] for r in w.q("SELECT status FROM infraction_signals")] == ["waiting"] and self.registered == []
        assert len(w.notices("infraction_wearer")) == 1 and w.notices("exemption_opening") == []

    def test_a_health_answer_ends_it(self, tmp_path) -> None:
        w, signals = self._world(tmp_path)
        self._late(w)
        signal = w.q("SELECT id FROM infraction_signals")[0]["id"]
        assert signals.clear_as_health(user_id=WEARER, signal_id=signal, now=NOW + timedelta(hours=8))
        wiring = DetectorWiring(signals=signals, core=w.core)
        signals.advance(NOW + timedelta(days=2), window_of=wiring._window_of)
        assert self.registered == []

    def test_its_own_exemption_does_not_excuse_it(self, tmp_path) -> None:
        """Otherwise every overrun would clear itself."""
        w, signals = self._world(tmp_path)
        self._late(w)
        wiring = DetectorWiring(signals=signals, core=w.core)
        silent = NOW + timedelta(hours=7) + GRACE + timedelta(minutes=1)
        assert signals.advance(silent, window_of=wiring._window_of) == 0               # unanswered: decided, with time to appeal
        assert signals.advance(silent + APPEAL_WINDOW + timedelta(minutes=1), window_of=wiring._window_of) == 1
        assert self.registered[0].kind is SignalKind.EXEMPTION_OVERRUN and self.registered[0].rule_group == UNANSWERED_RULE_GROUP

    def test_it_is_minor_so_a_first_one_is_a_warning(self) -> None:
        from infractions.signals import EVIDENCE
        from trust_manager.models import SeverityTier
        from trust_manager.severity import assess_severity
        assert assess_severity(EVIDENCE[SignalKind.EXEMPTION_OVERRUN]) is SeverityTier.MINOR

    def test_it_never_moves_time(self) -> None:
        names = {n.attr for n in ast.walk(ast.parse((ROOT / "exemptions" / "service.py").read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
        assert not names & {"update_time", "add_time", "remove_time", "unlock"}


# ---------------------------------------------------------------- the real executor against a fake Chaster

def _ext(slug, config, regularity=172800.0):
    return ExtensionSnapshot(slug=slug, extension_id=f"id-{slug}", mode="non_cumulative", regularity=regularity, is_partner=False, peer_verification=None, config=config)


OPENING = {"openingTime": 900, "penaltyTime": 0, "requireVerificationPictureAfter": False}
TASKS = {"tasks": [{"task": "Private"}]}


def _config(*, opening_time=900, regularity=172800.0, penalty=0, status="locked", subjects=frozenset({"keyholder"})):
    return LockConfiguration(lock_id=LOCK, role=LockRole.KEYHOLDER, status=status, is_test_lock=True, edit_subjects=subjects,
                             extensions=(_ext("temporary-opening", {**OPENING, "openingTime": opening_time, "penaltyTime": penalty}, regularity), _ext("tasks", TASKS, 3600.0)))


class Chaster:
    """Applies what is posted and hands it back on the next read, like the real thing."""

    def __init__(self, config=None, *, ignores_writes=False, open_status=201, open_raises=None) -> None:
        self.config = config or _config()
        self.ignores_writes, self.open_status, self.open_raises = ignores_writes, open_status, open_raises
        self.posts, self.opens = [], []
        self.opened_at, self.history = None, []

    def read(self, *, user_id, lock_ids, now):
        return ConfigurationReport(configurations=[self.config], problems=[])

    def set_extensions(self, *, lock_id, body):
        self.posts.append(body)
        if not self.ignores_writes:
            entry = next(e for e in body["extensions"] if e["slug"] == "temporary-opening")
            self.config = _config(opening_time=entry["config"]["openingTime"], regularity=float(entry["regularity"]), penalty=entry["config"]["penaltyTime"])
        return TransportResult(outcome=TransportOutcome.OK, http_status=201)

    def call_with_token(self, _account, *, now, operation):
        return operation("token")

    def send(self, method, url, **kwargs):
        self.opens.append((method, url))
        if self.open_raises:
            raise self.open_raises
        return SimpleNamespace(status_code=self.open_status)

    def get_temporary_opening(self, *, access_token, lock_id):
        return {"openedAt": self.opened_at, "requiresVerificationPicture": False}

    def get_lock_history(self, *, access_token, lock_id):
        return self.history


class Control:
    def __init__(self, halted=False) -> None:
        self.halted = halted

    def is_halted(self):
        return self.halted


def _executor(chaster, halted=False):
    return ExemptionOpening(lock_id=LOCK, account_user_id="owner", control=Control(halted), reader=chaster, transport=chaster, token_manager=chaster, lock_client=chaster, send=chaster.send,
                            clock=FrozenClock(NOW))


class TestTheExecutor:
    def test_it_prepares_opens_and_puts_the_regularity_back(self) -> None:
        chaster = Chaster()
        result = _executor(chaster).open_for(6)
        assert result.outcome is OpeningOutcome.OPENED and result.saved == {"openingTime": 900, "regularity": 172800.0}
        assert chaster.opens == [("POST", f"https://api.chaster.app/extensions/temporary-opening/{LOCK}/open")]
        first = next(e for e in chaster.posts[0]["extensions"] if e["slug"] == "temporary-opening")
        assert first["config"]["openingTime"] == 21600 and first["regularity"] == CLEAR_COOLDOWN_REGULARITY
        assert chaster.config.extension("temporary-opening").regularity == 172800.0 and chaster.config.extension("temporary-opening").config["openingTime"] == 21600

    def test_a_short_opening_is_a_whole_number_of_seconds(self) -> None:
        chaster = Chaster()
        _executor(chaster).open_for(0.5)
        value = next(e for e in chaster.posts[0]["extensions"] if e["slug"] == "temporary-opening")["config"]["openingTime"]
        assert value == 1800 and isinstance(value, int)

    def test_the_late_penalty_stays_zero(self) -> None:
        chaster = Chaster(_config(penalty=43200))
        _executor(chaster).open_for(6)
        assert next(e for e in chaster.posts[0]["extensions"] if e["slug"] == "temporary-opening")["config"]["penaltyTime"] == 0

    def test_a_halt_sends_nothing_at_all(self) -> None:
        chaster = Chaster()
        assert _executor(chaster, halted=True).open_for(6).outcome is OpeningOutcome.HALTED
        assert chaster.posts == [] and chaster.opens == []

    def test_a_halt_arriving_between_the_edit_and_the_opening_stops_it_and_undoes_the_edit(self) -> None:
        chaster = Chaster()
        executor = _executor(chaster)
        answers = iter([False, True, True, False])
        real = chaster.set_extensions

        class Late:
            def is_halted(self):
                return next(answers)

        executor._control = Late()
        chaster.set_extensions = real
        assert executor.open_for(6).outcome is OpeningOutcome.HALTED and chaster.opens == []

    @pytest.mark.parametrize("config", [_config(status="unlocked"), _config(subjects=frozenset({"wearer"})), replace(_config(), extensions=(_ext("tasks", TASKS),))])
    def test_a_lock_it_cannot_open_is_left_alone(self, config) -> None:
        chaster = Chaster(config)
        assert _executor(chaster).open_for(6).outcome is OpeningOutcome.UNAVAILABLE and chaster.posts == [] and chaster.opens == []

    def test_an_edit_chaster_ignored_never_reaches_the_opening(self) -> None:
        chaster = Chaster(ignores_writes=True)
        result = _executor(chaster).open_for(6)
        assert result.outcome is OpeningOutcome.UNAVAILABLE and result.detail == "settings_not_applied" and chaster.opens == []

    def test_a_refused_opening_puts_everything_back(self) -> None:
        chaster = Chaster(open_status=400)
        assert _executor(chaster).open_for(6).outcome is OpeningOutcome.UNAVAILABLE
        ext = chaster.config.extension("temporary-opening")
        assert ext.config["openingTime"] == 900 and ext.regularity == 172800.0

    def test_a_server_error_or_a_raised_call_is_ambiguous(self) -> None:
        assert _executor(Chaster(open_status=502)).open_for(6).outcome is OpeningOutcome.AMBIGUOUS
        assert _executor(Chaster(open_raises=OSError("reset"))).open_for(6).outcome is OpeningOutcome.AMBIGUOUS

    def test_the_status_reads_the_relock_from_chasters_log(self) -> None:
        chaster = Chaster()
        chaster.history = [{"type": "temporary_opening_locked", "createdAt": iso(NOW + timedelta(hours=1))}, {"type": "temporary_opening_locked", "createdAt": iso(NOW - timedelta(days=9))},
                           {"type": "tasks_task_completed", "createdAt": iso(NOW)}]
        status = _executor(chaster).status(opened_at=NOW)
        assert status.is_open is False and status.relocked_at == NOW + timedelta(hours=1)

    def test_open_on_chaster_is_open(self) -> None:
        chaster = Chaster()
        chaster.opened_at = iso(NOW)
        assert _executor(chaster).status(opened_at=NOW).is_open is True

    def test_an_unreadable_chaster_is_none(self) -> None:
        chaster = Chaster()

        def boom(**_k):
            raise OSError("down")

        chaster.get_temporary_opening = boom
        assert _executor(chaster).status(opened_at=NOW) is None

    def test_it_restores_the_locks_own_values(self) -> None:
        chaster = Chaster(_config(opening_time=21600))
        assert _executor(chaster).restore(SAVED) is True
        assert chaster.config.extension("temporary-opening").config["openingTime"] == 900

    def test_the_plan_keeps_everything_else(self) -> None:
        plan = plan_opening_settings(_config(), opening_seconds=21600, regularity=1)
        assert next(e for e in plan.body["extensions"] if e["slug"] == "tasks")["config"] == TASKS


class TestItsBoundaries:
    SOURCE = (ROOT / "chaster_execution" / "temporary_opening.py").read_text(encoding="utf-8")

    def test_the_one_chaster_url_is_the_opening(self) -> None:
        assert "https://api.chaster.app/extensions/temporary-opening/{self.lock_id}/open" in re_urls(self.SOURCE)
        assert all(url.startswith("https://api.chaster.app/extensions/temporary-opening/") for url in re_urls(self.SOURCE))

    def test_it_never_reads_the_combination_or_resumes(self) -> None:
        for url in re_urls(self.SOURCE):
            assert "combination" not in url and "resume" not in url and "unlock" not in url
        names = {n.attr for n in ast.walk(ast.parse(self.SOURCE)) if isinstance(n, ast.Attribute)}
        assert not names & {"unlock", "emergency_unlock", "add_time", "remove_time", "set_frozen"}

    def test_only_the_wiring_builds_it(self) -> None:
        found = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if p.parts[len(ROOT.parts)] not in ("tests",) and "ExemptionOpening(" in p.read_text(encoding="utf-8", errors="ignore")]
        assert found == ["system/composition.py"]

    def test_the_plan_import_is_narrow(self) -> None:
        tree = ast.parse(self.SOURCE)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "chaster.extension_plan" for a in n.names}
        assert imported == {"ReviewPlan", "plan_opening_settings"}


def strings(source):
    return [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)] + \
           ["".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}" for v in n.values) for n in ast.walk(ast.parse(source)) if isinstance(n, ast.JoinedStr)]


def re_urls(source):
    return {s for s in strings(source) if s.startswith("https://")}


def test_a_freeze_reason_exists_for_it() -> None:
    assert FreezeReason.TEMPORARY_WEAR_EXEMPTION.value == "temporary_wear_exemption"
