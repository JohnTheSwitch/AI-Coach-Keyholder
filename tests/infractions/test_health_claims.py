"""G5 and long emergencies (owner, 2026-11-15; philosophy v1.17): a health opening, a health exemption or an emergency unlock is granted at once;
afterwards it is reviewed only when a stored fact puts it in doubt (longer than 3 hours, or right after a declined request). The wearer explains,
the judge decides with its claim instructions, one appeal; a claim confirmed false is FALSE_HEALTH_CLAIM, a full Honesty Incident. No network."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from chaster_execution.temporary_opening import OpeningStatus
from infractions.health_claims import DECLINED_BEFORE, LONG_CLAIM, STILL_OPEN, EmergencyWatch, claim_review_signal, review_reasons
from infractions.signals import APPEAL_WINDOW, CLAIM_ASK_TEXT, EVIDENCE, RULE_GROUPS, SignalKind
from infrastructure.database import apply_transition
from infrastructure.time_format import iso
from lines.service import sentence_for
from penalty_engine.repository import WARNING_ONLY_RULE_GROUPS
from penalty_engine.violations_catalog import get as violation
from tests.exemptions.test_exemptions import WEARER
from tests.infractions.test_detectors import NOW, ROOT, USER, World, _lock, verdict


def test_the_rule() -> None:
    assert LONG_CLAIM == timedelta(hours=3) and DECLINED_BEFORE == timedelta(hours=12) and STILL_OPEN == timedelta(hours=24)
    assert RULE_GROUPS[SignalKind.HEALTH_CLAIM] == "FALSE_HEALTH_CLAIM" and "FALSE_HEALTH_CLAIM" not in WARNING_ONLY_RULE_GROUPS
    assert violation("FALSE_HEALTH_CLAIM").domain == "integrity" and EVIDENCE[SignalKind.HEALTH_CLAIM] is EVIDENCE[SignalKind.LOCK_CLAIM]
    assert sentence_for("FALSE_HEALTH_CLAIM") == "I claim a health need only when it is real."


def test_every_signal_kind_can_be_stored(tmp_path) -> None:
    """Bug found 2026-11-15: four kinds were missing from the table's CHECK, and INSERT OR IGNORE dropped them without a word."""
    conn = sqlite3.connect(tmp_path / "k.db")
    for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
        conn.executescript(path.read_text(encoding="utf-8"))
    for i, kind in enumerate(SignalKind):
        conn.execute("INSERT INTO infraction_signals (id, user_id, kind, subject_id, trust_domain, detail, status, review_after, incident_id, created_at) "
                     "VALUES (?, 'u', ?, ?, 'd', 'x', 'waiting', 'r', ?, 'c')", (f"s{i}", kind.value, f"sub{i}", f"inc{i}"))


class TestWhenAClaimIsReviewed:
    def test_short_and_undisputed_is_never_questioned(self) -> None:
        assert review_reasons(started_at=NOW, ended_at=NOW + LONG_CLAIM, declined=[], now=NOW + timedelta(days=1)) == []

    def test_longer_than_three_hours_always_is(self) -> None:
        reasons = review_reasons(started_at=NOW, ended_at=NOW + LONG_CLAIM + timedelta(minutes=1), declined=[], now=NOW + timedelta(days=1))
        assert reasons == ["it lasted 3 h 01 min, longer than the 3-hour limit"]

    def test_still_open_counts_up_to_now(self) -> None:
        assert "still open" in review_reasons(started_at=NOW, ended_at=None, declined=[], now=NOW + timedelta(hours=4))[0]

    def test_a_declined_request_just_before_is_a_reason_even_when_short(self) -> None:
        reasons = review_reasons(started_at=NOW, ended_at=NOW + timedelta(minutes=20), declined=["an extra hygiene opening declined at x"], now=NOW)
        assert reasons and "declined shortly before" in reasons[0]


class Claims(World):
    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self.trust.create_domain(domain_id="honesty", display_name="Honesty", description="d", created_via_consent_id="c", now=NOW - timedelta(days=30))
        self.raise_claim = claim_review_signal(self.signals, trust_domain="honesty")

    def claim(self, *, kind="hygiene_health", hours=4.0, declined=(), at=None):
        row = {"id": "x1", "user_id": USER, "kind": kind, "opened_at": iso(NOW), "reason": None}
        ended = NOW + timedelta(hours=hours)
        return apply_transition(self.core, write=lambda tx, _s: self.raise_claim(tx, row, ended, list(declined), at or ended))

    def signal_id(self):
        return self.q("SELECT id FROM infraction_signals WHERE kind = 'health_claim'")[0]["id"]


class TestTheReview:
    def test_a_long_health_opening_is_asked_about_without_a_health_button(self, tmp_path) -> None:
        w = Claims(tmp_path)
        assert w.claim() is True
        card = w.wearer_cards()[0]
        assert CLAIM_ASK_TEXT in card["text"] and "granted at once" in card["text"] and "It was a health or safety need" not in card["actions_json"]
        assert w.claim() is False                                          # one review per claim

    def test_a_short_one_is_not(self, tmp_path) -> None:
        w = Claims(tmp_path)
        assert w.claim(hours=2) is False and w.status() == []

    def test_the_health_button_is_no_shortcut_and_a_health_word_goes_to_the_judge(self, tmp_path) -> None:
        w = Claims(tmp_path)
        w.claim()
        sid = w.signal_id()
        assert "next message" in w.signals.press(user_id=USER, signal_id=sid, action="health", now=NOW).text
        assert w.signals.clear_as_health(user_id=USER, signal_id=sid, now=NOW) is False
        w.model.replies = [verdict("explained", "A swollen testicle needed a doctor; the wait fits.")]
        reply = w.signals.capture(USER, "My skin was irritated and swollen, I went to the clinic and waited there.", NOW + timedelta(minutes=30))
        assert "doesn't count" in reply.text and w.status() == [("health_claim", "cleared")]
        assert "health or emergency claim was genuine" in w.model.prompts[0][0]

    def test_confirmed_false_is_a_full_honesty_incident(self, tmp_path) -> None:
        w = Claims(tmp_path)
        w.claim()
        sid = w.signal_id()
        w.signals.press(user_id=USER, signal_id=sid, action="explain", now=NOW + timedelta(hours=5))
        w.model.replies = ['{"verdict": "not_explained", "admitted": true, "reason": "Used it to edge, not for health."}']
        w.signals.capture(USER, "Honestly I just wanted out for a while.", NOW + timedelta(hours=5))
        w.signals._excuses.reason = lambda *a, **k: "exemption"           # its own opening never excuses it
        w.advance(NOW + timedelta(hours=5) + APPEAL_WINDOW + timedelta(minutes=1))
        assert w.status() == [("health_claim", "confirmed")]
        incident = w.incidents()[0]
        assert incident["rule_group_id"] == "FALSE_HEALTH_CLAIM" and incident["trust_domain"] == "honesty"

    def test_still_unclear_after_one_question_gets_the_benefit_of_the_doubt(self, tmp_path) -> None:
        w = Claims(tmp_path)
        w.claim()
        sid = w.signal_id()
        w.signals.press(user_id=USER, signal_id=sid, action="explain", now=NOW + timedelta(hours=5))
        w.model.replies = [verdict("unclear"), verdict("unclear")]
        w.signals.capture(USER, "It hurt.", NOW + timedelta(hours=5))
        w.signals.capture(USER, "It hurt a lot, down there.", NOW + timedelta(hours=5, minutes=5))
        assert w.status() == [("health_claim", "cleared")] and w.incidents() == []


class TestEmergencyUnlocks:
    def watch(self, w, declined=()):
        return EmergencyWatch(w.core, user_id=USER, trust_domain="honesty", signals=w.signals, list_worn_locks=lambda _now: (w.locks, w.problems),
                              declined_between=lambda user, lo, hi: list(declined))

    def outcome(self, w):
        return w.q("SELECT outcome FROM emergency_unlocks")[0]["outcome"]

    def test_relocked_after_four_hours_is_reviewed(self, tmp_path) -> None:
        w = Claims(tmp_path)
        e = self.watch(w)
        w.locks = (_lock(status="unlocked", unlocked_at=iso(NOW), emergency=True),)
        e.poll(NOW + timedelta(minutes=5))
        assert w.status() == []                                            # never asked while it may still be the emergency
        w.locks = (_lock(status="unlocked", unlocked_at=iso(NOW), emergency=True), _lock(lock_id="new"))
        e.poll(NOW + timedelta(hours=4))
        assert w.status() == [("health_claim", "waiting")] and self.outcome(w) == "reviewed"
        assert "emergency unlock" in w.wearer_cards()[0]["text"]

    def test_relocked_within_three_hours_is_never_questioned(self, tmp_path) -> None:
        w = Claims(tmp_path)
        e = self.watch(w)
        w.locks = (_lock(status="unlocked", unlocked_at=iso(NOW), emergency=True), _lock(lock_id="new"))
        e.poll(NOW + timedelta(hours=2))
        assert w.status() == [] and self.outcome(w) == "not_questioned"

    def test_a_declined_request_before_it_makes_even_a_short_one_reviewed(self, tmp_path) -> None:
        w = Claims(tmp_path)
        e = self.watch(w, declined=["an extra hygiene opening declined at 2026-11-04T10:00:00Z"])
        w.locks = (_lock(status="unlocked", unlocked_at=iso(NOW), emergency=True), _lock(lock_id="new"))
        e.poll(NOW + timedelta(minutes=30))
        assert w.status() == [("health_claim", "waiting")]

    def test_never_relocked_is_reviewed_after_a_day(self, tmp_path) -> None:
        w = Claims(tmp_path)
        e = self.watch(w)
        w.locks = (_lock(status="unlocked", unlocked_at=iso(NOW), emergency=True),)
        e.poll(NOW + STILL_OPEN - timedelta(minutes=1))
        assert w.status() == []
        e.poll(NOW + STILL_OPEN)
        assert w.status() == [("health_claim", "waiting")]

    def test_an_old_unlock_seen_first_is_never_reviewed(self, tmp_path) -> None:
        w = Claims(tmp_path)
        e = self.watch(w)
        w.locks = (_lock(status="unlocked", unlocked_at=iso(NOW - timedelta(days=30)), emergency=True), _lock(lock_id="new"))
        e.poll(NOW)
        assert w.status() == [] and self.outcome(w) == "too_old"

    def test_an_unreadable_listing_decides_nothing(self, tmp_path) -> None:
        w = Claims(tmp_path)
        e = self.watch(w)
        w.locks, w.problems = (), ["wearer: not_connected"]
        assert e.poll(NOW) == ["wearer: not_connected"] and w.q("SELECT * FROM emergency_unlocks") == []


class TestExemptionServiceHandsItOver:
    @pytest.fixture
    def world(self, tmp_path):
        from tests.exemptions.test_hygiene_requests import World as HygieneWorld
        w = HygieneWorld(tmp_path)
        w.claims = []
        w.service.raise_claim = lambda tx, row, ended, declined, now: w.claims.append((row["id"], ended, declined)) or True
        return w

    def _row(self, w, *, kind="hygiene_health", category=None, opened_hours_ago=5, status="approved", ident="h"):
        opened = NOW - timedelta(hours=opened_hours_ago)
        with w.service._core.transaction() as tx:
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, requested_at, kind, category, minutes, opened_at, expires_at, decided_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, 60, ?, ?, ?)",
                       (ident, WEARER, status, iso(opened), iso(opened), kind, category, iso(opened), iso(opened + timedelta(hours=1)), iso(opened)))

    def test_a_closed_health_opening_is_handed_over_with_its_relock(self, world) -> None:
        self._row(world)
        world.opening.state = OpeningStatus(is_open=False, relocked_at=NOW - timedelta(minutes=10))
        world.service.tick(NOW)
        assert world.claims == [("h", NOW - timedelta(minutes=10), [])]

    def test_a_planned_opening_is_not(self, world) -> None:
        self._row(world, kind="hygiene")
        world.opening.state = OpeningStatus(is_open=False, relocked_at=NOW - timedelta(minutes=10))
        world.service.tick(NOW)
        assert world.claims == []

    def test_a_health_opening_still_open_after_a_day_is_handed_over_once_open(self, world) -> None:
        self._row(world, opened_hours_ago=23)
        world.service.tick(NOW)
        assert world.claims == []
        world.service.tick(NOW + timedelta(hours=1))
        assert world.claims == [("h", None, [])]

    def test_a_declined_request_before_it_is_passed_along(self, world) -> None:
        with world.service._core.transaction() as tx:
            tx.execute("INSERT INTO wear_exemptions (id, user_id, status, created_at, kind, minutes, decided_at, decided_by) "
                       "VALUES ('d', ?, 'declined', ?, 'hygiene', 15, ?, 'judge')", (WEARER, iso(NOW - timedelta(hours=7)), iso(NOW - timedelta(hours=7))))
        self._row(world)
        world.opening.state = OpeningStatus(is_open=False, relocked_at=NOW - timedelta(minutes=10))
        world.service.tick(NOW)
        assert world.claims[0][2] and "extra hygiene opening declined" in world.claims[0][2][0]
