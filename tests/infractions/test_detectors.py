"""The two detectors and their shared path to an Incident. Each safeguard of the owner's request and of philosophy 2.12, 3.2, 3.8 and 3.9 is an assertion here. No network."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chaster.managed_locks import LockRole, ManagedLock, parse_lock
from infractions.excuses import Excuses
from infractions.hygiene import ROLLING_WEEK, HygieneWatch, OpeningNotAvailable, parse_opened_at, policy_for
from infractions.lock_claims import LockClaimWatch
from infractions.signals import EVIDENCE, GRACE, RULE_GROUPS, Decision, InfractionSignals, SignalKind
from infractions.wiring import DetectorWiring, build_detectors
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso
from lock_state.models import LockReportStatus
from lock_state.repository import LockStateAdministration
from penalty_engine.models import FreezeReason
from system.dm_actions import HygieneDm, InfractionDm
from system.penalty_policy import make_penalty_engine
from system.periodic import run_periodic_cycle
from infrastructure.clock import FrozenClock
from trust_manager.models import ConfirmationSource, IncidentConfirmation, SeverityTier
from trust_manager.repository import TrustManager
from trust_manager.severity import assess_severity

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
USER = "8f295e28-d129-45cd-9a90-2017f5d63318"
LOCK = "6ae98ca410556a82f4b0d1aa"
DOMAIN = "hygiene"
HIGH_TRUST, LOW_TRUST = 0.9, 0.3             # 7 breaks a week of 30 minutes; 3 of 20


def _lock(status="locked", unlocked_at=None, emergency=None, lock_id=LOCK) -> ManagedLock:
    return ManagedLock(role=LockRole.WEARER, lock_id=lock_id, title="t", status=status, is_frozen=False, is_test_lock=False, wearer_chaster_id=None, wearer_username=None,
                       keyholder_chaster_id=None, keyholder_username=None, unlocked_at=unlocked_at, emergency_unlocked=emergency)


class World:
    def __init__(self, tmp_path, *, score=HIGH_TRUST) -> None:
        self.core = CoreDatabase(tmp_path / "d.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO user_accounts (id, created_at, last_seen_at) VALUES (?, ?, ?)", (USER, iso(NOW), iso(NOW)))
            conn.execute("INSERT INTO user_channel_identities (id, user_account_id, channel, external_id, created_at) VALUES ('i1', ?, 'discord', '42', ?)", (USER, iso(NOW)))
            conn.commit()
        self.trust = TrustManager(self.core.db_path, core=self.core)
        self.trust.create_domain(domain_id=DOMAIN, display_name="H", description="d", created_via_consent_id="c", now=NOW - timedelta(days=30))
        self.excuses = Excuses(self.core)
        self.confirmed = []

        def register(signal, now):
            self.trust.register_incident_report(rule_group_id=RULE_GROUPS[signal.kind], trust_domain=signal.trust_domain, description=signal.detail,
                                                evidence=EVIDENCE[signal.kind], now=now, incident_id=signal.incident_id)

        def confirm(incident_id, evidence, now):
            self.trust.confirm_incident(incident_id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.KEYHOLDER_REVIEW,
                                        evidence_description=evidence, now=now)
            self.confirmed.append(incident_id)

        self.signals = InfractionSignals(core=self.core, owner_user_id=USER, excuses=self.excuses, register_incident=register, confirm_incident=confirm)
        self.locks, self.problems, self.opening, self.read_error = (_lock(),), [], None, None
        self.score = score

        def read(_lock_id, _now):
            if self.read_error:
                raise self.read_error
            return {"openedAt": self.opening, "requiresVerificationPicture": False}

        worn = lambda _now: (self.locks, self.problems)  # noqa: E731
        self.hygiene = HygieneWatch(core=self.core, user_id=USER, trust_domain=DOMAIN, signals=self.signals, list_worn_locks=worn, read_opening=read,
                                    get_trust_score=lambda: self.score, interval_seconds=120)
        self.claims = LockClaimWatch(core=self.core, user_id=USER, owner_user_id=USER, trust_domain=DOMAIN, signals=self.signals, excuses=self.excuses, list_worn_locks=worn)
        self.wiring = DetectorWiring(signals=self.signals, hygiene=self.hygiene, lock_claims=self.claims)
        self.dm = InfractionDm(self.signals, resolve_user=lambda d: USER if d == "42" else None, now=lambda: self.at)
        self.hygiene_dm = HygieneDm(self.hygiene, resolve_user=lambda d: USER if d == "42" else None, now=lambda: self.at)
        self.at = NOW

    def q(self, sql, args=()):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(sql, args)]

    def break_(self, start, minutes, answer="planned"):
        """One whole opening, followed poll by poll, answered with the wearer's own button."""
        self.opening = iso(start)
        self.hygiene.poll(start)
        ident = self.q("SELECT id FROM hygiene_openings ORDER BY opened_at DESC, rowid DESC LIMIT 1")[0]["id"]
        for tick in range(120, minutes * 60, 120):
            self.hygiene.poll(start + timedelta(seconds=tick))
        if answer:
            self.at = start + timedelta(minutes=1)
            self.hygiene_dm.press(discord_user_id="42", action=answer, ident=ident)
        self.opening = None
        self.hygiene.poll(start + timedelta(minutes=minutes))
        return ident

    def advance(self, at):
        self.wiring.run(at)

    def status(self):
        return [(r["kind"], r["status"]) for r in self.q("SELECT kind, status FROM infraction_signals ORDER BY created_at, rowid")]

    def report(self, status, at):
        LockStateAdministration(self.core.db_path, core=self.core).report_status(user_id=USER, status=status, reported_via_consent_id="c", now=at)

    def incidents(self):
        return self.q("SELECT * FROM incidents")

    def wearer_cards(self):
        return self.q("SELECT * FROM notifications WHERE kind = 'infraction_wearer'")


class TestPolicy:
    @pytest.mark.parametrize("score,week,minutes", [(0.0, 3, 20), (0.44, 3, 20), (0.45, 5, 30), (0.84, 6, 30), (0.85, 7, 30), (1.0, 7, 30)])
    def test_the_design_table(self, score, week, minutes) -> None:
        policy = policy_for(score)
        assert (policy.breaks_per_week, policy.session_minutes) == (week, minutes)

    def test_parsing_the_provider_answer(self) -> None:
        assert parse_opened_at({"openedAt": None}) is None
        assert parse_opened_at({"openedAt": "2026-11-04T10:00:00.000Z"}).startswith("2026-11-04T10:00:00")
        for bad in ({}, [], {"openedAt": 5}):
            with pytest.raises(ValueError):
                parse_opened_at(bad)

    def test_the_lock_fields_are_read_from_the_provider(self) -> None:
        lock = parse_lock({"_id": "a", "unlockedAt": "2026-11-04T10:00:00.000Z", "emergencyUnlocked": True}, LockRole.WEARER)
        assert lock.unlocked_at == "2026-11-04T10:00:00.000Z" and lock.emergency_unlocked is True


class TestDuration:
    def test_a_planned_break_within_the_limit_raises_nothing(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 35)                                                   # 30 minutes + the 10-minute grace
        assert w.status() == []

    def test_an_overrun_planned_break_raises_one_signal_and_asks_the_wearer(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        assert w.status() == [("hygiene_duration", "waiting")]
        assert w.incidents() == [] and len(w.wearer_cards()) == 1            # nothing is recorded yet

    def test_a_lower_trust_shortens_the_limit(self, tmp_path) -> None:
        w = World(tmp_path, score=LOW_TRUST)
        w.break_(NOW, 35)
        assert w.status() == [("hygiene_duration", "waiting")]

    def test_a_break_nobody_followed_is_never_judged_on_length(self, tmp_path) -> None:
        """If the bot was away the close is unknown, and a guess never counts against the wearer."""
        w = World(tmp_path)
        w.opening = iso(NOW)
        w.hygiene.poll(NOW)
        w.at = NOW
        w.hygiene_dm.press(discord_user_id="42", action="planned", ident=w.q("SELECT id FROM hygiene_openings")[0]["id"])
        w.opening = None
        w.hygiene.poll(NOW + timedelta(hours=9))
        assert w.q("SELECT duration_known FROM hygiene_openings") == [{"duration_known": 0}] and w.status() == []

    def test_an_opening_is_judged_only_once(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        w.score = LOW_TRUST
        for hour in range(3):
            w.hygiene.poll(NOW + timedelta(hours=hour + 1))
        assert len(w.status()) == 1


class TestFrequency:
    def test_the_break_past_the_weekly_allowance_raises_a_signal(self, tmp_path) -> None:
        w = World(tmp_path, score=LOW_TRUST)
        for day in range(3):
            w.break_(NOW + timedelta(days=day), 15)
        assert w.status() == []
        w.break_(NOW + timedelta(days=3), 15)
        assert w.status() == [("hygiene_frequency", "waiting")]

    def test_the_window_rolls(self, tmp_path) -> None:
        w = World(tmp_path, score=LOW_TRUST)
        for day in (0, 1, 2):
            w.break_(NOW + timedelta(days=day), 15)
        w.break_(NOW + ROLLING_WEEK + timedelta(days=1), 15)
        assert w.status() == []


class TestMandatoryAccessIsOutsideAllOfThis:
    """The owner's constraint: Mandatory Health Access and Emergency Overrides MUST NEVER trigger an incident (philosophy 3.8, 3.9, section 6)."""

    def test_a_health_need_is_never_measured_however_long(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 400, answer="health")
        assert w.status() == [] and w.incidents() == []

    def test_a_health_need_is_never_counted_however_often(self, tmp_path) -> None:
        w = World(tmp_path, score=LOW_TRUST)
        for hour in range(40):
            w.break_(NOW + timedelta(hours=hour * 2), 90, answer="health")
        assert w.status() == [] and w.incidents() == []

    def test_health_needs_do_not_use_up_the_allowance(self, tmp_path) -> None:
        w = World(tmp_path, score=LOW_TRUST)
        for hour in range(10):
            w.break_(NOW + timedelta(hours=hour), 5, answer="health")
        for day in range(3):
            w.break_(NOW + timedelta(days=day + 1), 15)
        assert w.status() == []

    def test_an_unanswered_opening_counts_for_nothing(self, tmp_path) -> None:
        """Silence is not evidence (2.12), and it may be someone dealing with exactly what this access exists for."""
        w = World(tmp_path, score=LOW_TRUST)
        for day in range(6):
            w.break_(NOW + timedelta(days=day), 300, answer=None)
        assert w.status() == [] and w.incidents() == []

    def test_planned_can_become_health_but_not_the_reverse(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.break_(NOW, 5, answer="health")
        assert w.hygiene_dm.press(discord_user_id="42", action="planned", ident=ident).text.startswith("Already noted")
        assert w.q("SELECT classification FROM hygiene_openings") == [{"classification": "health"}]
        other = w.break_(NOW + timedelta(hours=1), 5)
        w.hygiene_dm.press(discord_user_id="42", action="health", ident=other)
        assert {r["classification"] for r in w.q("SELECT classification FROM hygiene_openings")} == {"health"}

    def test_the_wearers_word_clears_a_signal_before_any_incident_exists(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        signal = w.q("SELECT id FROM infraction_signals")[0]["id"]
        w.at = NOW + timedelta(hours=1)
        assert "doesn't count" in w.dm.press(discord_user_id="42", action="health", ident=signal).text
        w.advance(NOW + GRACE * 2)
        assert w.status() == [("hygiene_duration", "cleared")] and w.incidents() == []

    def test_an_emergency_override_excuses_an_overlapping_break(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        with w.core.raw_connection() as conn:
            conn.execute("INSERT INTO penalty_windows (id, created_at, status, base_duration_hours, extensions_hours, accumulated_active_hours, max_target_hours) "
                         "VALUES ('p', ?, 'frozen', 24, 0, 0, 336)", (iso(NOW),))
            conn.execute("INSERT INTO freeze_periods (id, penalty_window_id, started_at, reason) VALUES ('f', 'p', ?, 'emergency_override')", (iso(NOW + timedelta(minutes=10)),))
            conn.commit()
        w.advance(NOW + GRACE + timedelta(hours=1))
        assert w.q("SELECT status, cleared_reason FROM infraction_signals") == [{"status": "cleared", "cleared_reason": "emergency_override"}]
        assert w.incidents() == []

    def test_an_approved_exemption_excuses_an_overlapping_break(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        with w.core.raw_connection() as conn:
            conn.execute("INSERT INTO wear_exemptions (id, user_id, category, hours, status, created_at, decided_at, expires_at) VALUES ('e', ?, 'family', 24, 'approved', ?, ?, ?)",
                         (USER, iso(NOW - timedelta(hours=1)), iso(NOW - timedelta(hours=1)), iso(NOW + timedelta(hours=23))))
            conn.commit()
        w.advance(NOW + GRACE + timedelta(hours=1))
        assert w.status() == [("hygiene_duration", "cleared")] and w.incidents() == []

    def test_a_doubt_about_an_excuse_never_counts_against_the_wearer(self, tmp_path) -> None:
        w = World(tmp_path)
        assert Excuses(w.core, on_vacation=lambda *_a: 1 / 0).reason(USER, NOW, NOW) == "unreadable"

    def test_an_emergency_unlocked_lock_is_not_followed(self, tmp_path) -> None:
        w = World(tmp_path)
        w.locks = (_lock(emergency=True),)
        w.opening = iso(NOW)
        w.hygiene.poll(NOW)
        assert w.q("SELECT * FROM hygiene_openings") == []

    def test_the_catalog_keeps_no_mandatory_or_emergency_entry(self) -> None:
        from penalty_engine.violations_catalog import CATALOG, get
        assert not any("MANDATORY" in v.code or "EMERGENCY" in v.code for v in CATALOG)
        assert get("HYGIENE_MISUSE").detectable and get("EPISTEMIC_MISREPRESENTATION").detectable


class TestOpeningsTheBotMadeItself:
    def test_it_does_not_ask_about_them(self, tmp_path) -> None:
        """The bot opened it for a reason it already knows, so a planned-or-health question would be wrong."""
        w = World(tmp_path)
        with w.core.raw_connection() as conn:
            conn.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, kind, minutes, opening_lock_id, opened_at) VALUES ('e', ?, 'approved', ?, 'hygiene', 30, ?, ?)",
                         (USER, iso(NOW), LOCK, iso(NOW)))
            conn.commit()
        w.opening = iso(NOW)
        w.hygiene.poll(NOW)
        assert w.q("SELECT * FROM hygiene_openings") == [] and w.q("SELECT * FROM notifications WHERE kind = 'hygiene_opening'") == []

    def test_a_planned_break_does_not_excuse_other_findings(self, tmp_path) -> None:
        w = World(tmp_path)
        with w.core.raw_connection() as conn:
            conn.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, kind, minutes, decided_at, expires_at) VALUES ('e', ?, 'approved', ?, 'hygiene', 30, ?, ?)",
                         (USER, iso(NOW), iso(NOW), iso(NOW + timedelta(minutes=30))))
            conn.commit()
        assert w.excuses.reason(USER, NOW, NOW + timedelta(minutes=5)) is None


class TestReads:
    def test_a_failed_read_never_closes_an_opening(self, tmp_path) -> None:
        w = World(tmp_path)
        w.opening = iso(NOW)
        w.hygiene.poll(NOW)
        w.read_error = OSError("network")
        assert w.hygiene.poll(NOW + timedelta(minutes=2))
        assert w.q("SELECT closed_at FROM hygiene_openings") == [{"closed_at": None}]

    def test_a_lock_without_the_extension_is_not_a_problem(self, tmp_path) -> None:
        w = World(tmp_path)
        w.read_error = OpeningNotAvailable()
        assert w.hygiene.poll(NOW) == []

    def test_a_new_opening_asks_once(self, tmp_path) -> None:
        w = World(tmp_path)
        w.opening = iso(NOW)
        w.hygiene.poll(NOW)
        w.hygiene.poll(NOW + timedelta(minutes=2))
        assert len(w.q("SELECT * FROM notifications WHERE kind = 'hygiene_opening'")) == 1

    def test_a_card_answers_only_its_own_user(self, tmp_path) -> None:
        w = World(tmp_path)
        ident = w.break_(NOW, 5, answer=None)
        assert w.hygiene_dm.press(discord_user_id="999", action="health", ident=ident).text == "This isn't yours to answer."


class TestKeyholderDecides:
    def _open_signal(self, w):
        w.break_(NOW, 45)
        w.advance(NOW + GRACE + timedelta(hours=1))
        return w.q("SELECT id, incident_id FROM infraction_signals")[0]

    def test_after_the_grace_there_is_one_unconfirmed_incident_and_a_card(self, tmp_path) -> None:
        w = World(tmp_path)
        self._open_signal(w)
        incident = w.incidents()[0]
        assert incident["confirmation"] == "unconfirmed" and incident["rule_group_id"] == "HYGIENE_MISUSE" and incident["trust_domain"] == DOMAIN
        assert len(w.q("SELECT * FROM notifications WHERE kind = 'infraction_keyholder'")) == 1

    def test_an_open_signal_changes_nothing_until_the_keyholder_presses(self, tmp_path) -> None:
        w = World(tmp_path)
        before = w.trust.get_domain_state(DOMAIN).score
        self._open_signal(w)
        assert w.trust.get_domain_state(DOMAIN).score == before and w.confirmed == []
        assert run_periodic_cycle(w.core, "p", FrozenClock(NOW + GRACE * 2), rules_engine=None, executor=None)
        assert w.q("SELECT * FROM incident_warnings") == [] and w.q("SELECT * FROM penalty_windows") == []

    def test_nothing_in_the_detectors_can_confirm(self) -> None:
        for name in ("hygiene.py", "lock_claims.py"):
            names = {n.attr for n in ast.walk(ast.parse((ROOT / "infractions" / name).read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
            assert not names & {"decide", "confirm_incident", "_confirm"}

    def test_a_confirmed_first_overrun_is_a_warning_and_never_a_window(self, tmp_path) -> None:
        w = World(tmp_path)
        signal = self._open_signal(w)
        at = NOW + GRACE * 2
        w.at = at
        assert w.dm.press(discord_user_id="42", action="confirm", ident=signal["id"]).text.startswith("Confirmed")
        assert run_periodic_cycle(w.core, "p", FrozenClock(at), rules_engine=None, executor=None)
        assert len(w.q("SELECT * FROM incident_warnings")) == 1 and w.q("SELECT * FROM penalty_windows") == []

    def test_a_confirmed_second_overrun_reaches_the_penalty_engine(self, tmp_path) -> None:
        w = World(tmp_path)
        first = self._open_signal(w)
        at = NOW + GRACE * 2
        w.at = at
        w.dm.press(discord_user_id="42", action="confirm", ident=first["id"])
        assert run_periodic_cycle(w.core, "p", FrozenClock(at), rules_engine=None, executor=None)
        w.break_(at + timedelta(days=1), 45)
        later = at + timedelta(days=1) + GRACE + timedelta(hours=1)
        w.advance(later)
        w.at = later
        second = w.q("SELECT id FROM infraction_signals WHERE status = 'open'")[0]["id"]
        w.dm.press(discord_user_id="42", action="confirm", ident=second)
        assert run_periodic_cycle(w.core, "q", FrozenClock(later), rules_engine=None, executor=None)
        assert len(w.q("SELECT * FROM penalty_windows")) == 1

    def test_a_dismissal_leaves_it_unconfirmed_for_good(self, tmp_path) -> None:
        w = World(tmp_path)
        signal = self._open_signal(w)
        assert w.dm.press(discord_user_id="42", action="dismiss", ident=signal["id"]).text.startswith("Dismissed")
        assert w.incidents()[0]["confirmation"] == "unconfirmed" and w.status() == [("hygiene_duration", "dismissed")]

    def test_only_the_keyholder_decides(self, tmp_path) -> None:
        w = World(tmp_path)
        signal = self._open_signal(w)
        assert w.signals.decide(actor="someone-else", signal_id=signal["id"], confirm=True, now=NOW) is Decision.NOT_OWNER
        assert w.confirmed == []

    def test_a_decision_counts_once(self, tmp_path) -> None:
        w = World(tmp_path)
        signal = self._open_signal(w)
        w.signals.decide(actor=USER, signal_id=signal["id"], confirm=False, now=NOW)
        assert w.signals.decide(actor=USER, signal_id=signal["id"], confirm=True, now=NOW) is Decision.NOT_OPEN

    def test_the_wearer_can_still_say_health_while_it_waits_for_the_keyholder(self, tmp_path) -> None:
        w = World(tmp_path)
        signal = self._open_signal(w)
        w.dm.press(discord_user_id="42", action="health", ident=signal["id"])
        assert w.status() == [("hygiene_duration", "cleared")]
        assert w.signals.decide(actor=USER, signal_id=signal["id"], confirm=True, now=NOW) is Decision.NOT_OPEN

    def test_a_retry_after_a_crash_does_not_duplicate_the_incident(self, tmp_path) -> None:
        w = World(tmp_path)
        w.break_(NOW, 45)
        row = w.q("SELECT * FROM infraction_signals")[0]
        from infractions.signals import _signal
        w.trust.register_incident_report(rule_group_id="HYGIENE_MISUSE", trust_domain=DOMAIN, description="d", evidence=EVIDENCE[SignalKind.HYGIENE_DURATION],
                                         now=NOW, incident_id=row["incident_id"])
        assert _signal(row).incident_id == row["incident_id"]
        w.advance(NOW + GRACE + timedelta(hours=1))
        assert len(w.incidents()) == 1 and w.status() == [("hygiene_duration", "open")]

    def test_proportionate_severity(self) -> None:
        assert assess_severity(EVIDENCE[SignalKind.HYGIENE_DURATION]) is SeverityTier.MINOR
        assert assess_severity(EVIDENCE[SignalKind.HYGIENE_FREQUENCY]) is SeverityTier.MINOR
        assert assess_severity(EVIDENCE[SignalKind.LOCK_CLAIM]) is SeverityTier.MAJOR


class TestNoTrustInAnyText:
    def test_no_notice_carries_a_trust_value_or_the_limits(self, tmp_path) -> None:
        w = World(tmp_path, score=LOW_TRUST)
        for day in range(4):
            w.break_(NOW + timedelta(days=day), 40)
        w.advance(NOW + timedelta(days=5))
        texts = [r["text"] for r in w.q("SELECT text FROM notifications")] + [r["detail"] for r in w.q("SELECT detail FROM infraction_signals")]
        assert len(texts) > 4
        for text in texts:
            lowered = text.lower()
            assert "trust" not in lowered and "score" not in lowered and "level" not in lowered and not any(c.isdigit() for c in text)


UNLOCKED_LONG_AGO = "2026-11-01T10:00:00.000Z"


class TestLockClaims:
    def _armed(self, w):
        """A first pass with an older report: the baseline."""
        w.report(LockReportStatus.LOCKED_USER_REPORTED, NOW - timedelta(days=30))
        w.claims.poll(NOW)
        assert w.q("SELECT outcome FROM lock_claim_checks") == [{"outcome": "baseline"}]

    def _claim(self, w, status=LockReportStatus.LOCKED_USER_REPORTED, at=NOW + timedelta(hours=1)):
        w.report(status, at)
        problems = w.claims.poll(at + timedelta(minutes=2))
        rows = w.q("SELECT outcome FROM lock_claim_checks ORDER BY checked_at DESC, rowid DESC LIMIT 1")
        return (rows[0]["outcome"] if rows else None), problems

    def test_a_report_after_the_lock_ended_is_contradicted(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO),)
        assert self._claim(w)[0] == "contradicted"
        assert w.status() == [("lock_claim", "waiting")] and w.incidents() == []

    def test_the_first_report_after_arming_is_judged(self, tmp_path) -> None:
        """With no earlier report the first pass has nothing to baseline, and the next report must not slip through."""
        w = World(tmp_path)
        w.claims.poll(NOW)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO),)
        assert self._claim(w)[0] == "contradicted"

    def test_a_report_that_was_true_when_made_is_never_a_lie(self, tmp_path) -> None:
        """The whole point: the lock ended AFTER the report, so the report simply aged."""
        w = World(tmp_path)
        self._armed(w)
        report_at = NOW + timedelta(hours=1)
        w.locks = (_lock("unlocked", (report_at + timedelta(hours=2)).isoformat()),)
        assert self._claim(w, at=report_at)[0] == "stale" and w.status() == []

    def test_a_race_between_two_clocks_is_not_a_lie(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        report_at = NOW + timedelta(hours=1)
        w.locks = (_lock("unlocked", (report_at - timedelta(minutes=4)).isoformat()),)
        assert self._claim(w, at=report_at)[0] == "stale"

    def test_any_worn_lock_still_locked_makes_the_report_true(self, tmp_path) -> None:
        """The owner's account really does wear an unlocked lock beside a locked one."""
        w = World(tmp_path)
        self._armed(w)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO, lock_id="a"), _lock("locked", lock_id="b"))
        assert self._claim(w)[0] == "consistent"

    @pytest.mark.parametrize("locks", [(), (_lock("unlocked", None),), (_lock("deserted", UNLOCKED_LONG_AGO),), (_lock("unlocked", "garbage"),)])
    def test_uncertainty_is_left_alone(self, tmp_path, locks) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks = locks
        assert self._claim(w)[0] == "stale" and w.status() == []

    def test_a_failed_read_judges_nothing_and_tries_again(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks, w.problems = (), ["wearer: api_error:500"]
        at = NOW + timedelta(hours=1)
        w.report(LockReportStatus.LOCKED_USER_REPORTED, at)
        assert w.claims.poll(at + timedelta(minutes=2)) == ["wearer: api_error:500"]
        assert len(w.q("SELECT * FROM lock_claim_checks")) == 1
        w.locks, w.problems = (_lock(),), []
        assert w.claims.poll(at + timedelta(minutes=4)) == [] and len(w.q("SELECT * FROM lock_claim_checks")) == 2

    def test_an_emergency_unlock_excuses_the_report(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO, emergency=True),)
        assert self._claim(w)[0] == "excused" and w.status() == []

    def test_an_exemption_excuses_the_report(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO),)
        with w.core.raw_connection() as conn:
            conn.execute("INSERT INTO wear_exemptions (id, user_id, category, hours, status, created_at, decided_at, expires_at) VALUES ('e', ?, 'family', 24, 'approved', ?, ?, ?)",
                         (USER, iso(NOW), iso(NOW), iso(NOW + timedelta(days=1))))
            conn.commit()
        assert self._claim(w)[0] == "excused" and w.status() == []

    def test_reporting_unlocked_while_chaster_says_locked_is_only_a_note(self, tmp_path) -> None:
        """Someone may be telling the truth about something their provider cannot see. Honesty never costs anything."""
        w = World(tmp_path)
        self._armed(w)
        assert self._claim(w, LockReportStatus.UNLOCKED_USER_REPORTED)[0] == "unlocked_while_locked"
        assert w.status() == [] and w.incidents() == []
        assert len(w.q("SELECT * FROM notifications WHERE kind = 'lock_claim_note'")) == 1

    def test_a_contradiction_follows_the_same_path_to_the_keyholder(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO),)
        self._claim(w)
        w.advance(NOW + timedelta(hours=1) + GRACE + timedelta(hours=1))
        incident = w.incidents()[0]
        assert incident["confirmation"] == "unconfirmed" and incident["rule_group_id"] == "EPISTEMIC_MISREPRESENTATION"

    def test_a_report_is_checked_once(self, tmp_path) -> None:
        w = World(tmp_path)
        self._armed(w)
        w.locks = (_lock("unlocked", UNLOCKED_LONG_AGO),)
        self._claim(w)
        w.claims.poll(NOW + timedelta(days=1))
        assert len(w.status()) == 1


class Config:
    db_path = None
    owner_user_id = USER
    chaster_execution_keyholder_user_id = USER
    enable_hygiene_watch = True
    enable_lock_claim_watch = True
    hygiene_trust_domain = "missing"
    lock_claim_trust_domain = "routine"
    detector_poll_seconds = 120


class Tokens:
    def call_with_token(self, *_a, **_k):
        raise AssertionError("wiring must not call Chaster")


class TestWiring:
    def test_off_by_default(self, tmp_path) -> None:
        w = World(tmp_path)
        config = Config()
        config.db_path, config.enable_hygiene_watch, config.enable_lock_claim_watch = w.core.db_path, False, False
        out = build_detectors(config, w.core, token_manager=Tokens(), lock_client=object(), connections=object())
        assert not out.active and out.notes == []

    def test_a_missing_domain_is_never_created_or_substituted(self, tmp_path) -> None:
        """3.7: the runtime never creates a Trust domain, and a hygiene finding stays in its own area."""
        w = World(tmp_path)
        config = Config()
        config.db_path = w.core.db_path
        out = build_detectors(config, w.core, token_manager=Tokens(), lock_client=object(), connections=object())
        assert out.hygiene is None and any("NOT active" in n and "missing" in n for n in out.notes)
        assert w.trust.get_domain_state("missing") is None

    def test_a_failing_watch_does_not_silence_the_other(self, tmp_path) -> None:
        w = World(tmp_path)

        class Boom:
            def poll(self, _now):
                raise RuntimeError("boom")

        w.wiring.hygiene = Boom()
        w.report(LockReportStatus.LOCKED_USER_REPORTED, NOW - timedelta(days=1))
        assert "detector pass failed" in w.wiring.run(NOW)
        assert w.q("SELECT outcome FROM lock_claim_checks") == [{"outcome": "baseline"}]

    def test_the_detectors_import_nothing_that_can_write_to_a_lock(self) -> None:
        for path in (ROOT / "infractions").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                    assert not any(n.startswith(("chaster_execution", "release_engine", "rules_engine", "penalty_engine")) for n in names), (path.name, names)

    def test_the_only_new_chaster_call_is_a_get(self) -> None:
        source = (ROOT / "chaster" / "lock_client.py").read_text(encoding="utf-8")
        body = source[source.index("def get_temporary_opening"):source.index("def get_lock_history")]
        assert '"GET", TEMPORARY_OPENING_URL' in body and "POST" not in body


def test_the_excuse_covers_a_freeze_that_already_ended(tmp_path) -> None:
    """Retroactive use of an Emergency Override is the thing that must never happen (3.10), so an override that was resolved in the meantime still excuses."""
    w = World(tmp_path)
    engine = make_penalty_engine(w.core)
    with w.core.raw_connection() as conn:
        conn.execute("INSERT INTO penalty_windows (id, created_at, status, base_duration_hours, extensions_hours, accumulated_active_hours, active_period_started_at, max_target_hours) "
                     "VALUES ('p', ?, 'active', 24, 0, 0, ?, 336)", (iso(NOW), iso(NOW)))
        conn.commit()
    engine.emergency_freeze("p", now=NOW + timedelta(minutes=5))
    engine.resume("p", FreezeReason.EMERGENCY_OVERRIDE, now=NOW + timedelta(minutes=30))
    assert w.excuses.reason(USER, NOW, NOW + timedelta(minutes=10)) == "emergency_override"
    assert w.excuses.reason(USER, NOW + timedelta(hours=2), NOW + timedelta(hours=3)) is None
