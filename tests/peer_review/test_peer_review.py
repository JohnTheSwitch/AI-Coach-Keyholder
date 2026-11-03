"""v0.39 Phase I4: community review results -> the ledger -> exactly one UNCONFIRMED Incident per rejected review, the owner's decision,
the bot loop and the `peer-reviews` CLI. No network."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from chaster.lock_client import TASK_HISTORY_URL, VERIFICATION_PICTURES_URL, ChasterLockApiError, ChasterLockClient
from chaster.peer_reviews import (PeerReviewReader, ReviewSource, ReviewStatus, parse_task_history,
                                  parse_verification_pictures)
from chaster.token_manager import ChasterNotConnectedError
from cli.main import main, render_peer_reviews
from infrastructure.database import Database as CoreDatabase
from peer_review.watch import RULE_GROUP, DecisionOutcome, PeerReviewWatch
from peer_review.wiring import PEER_REVIEW_EVIDENCE, build_peer_review_decisions, build_peer_review_watch, peer_review_loop
from trust_manager.models import ConfirmationSource, IncidentConfirmation
from trust_manager.repository import TrustManager

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
OWNER = "owner-1"
LOCK = "6ae98ca410556a82f4b0d1aa"
DOMAIN = "routine"


def task(review_id, status, *, verified=1, rejected=0, text="SECRET TASK TEXT"):
    return {"id": f"t-{review_id}", "status": "pending_verification" if status == "ongoing" else status, "task": text, "points": 0,
            "grantedPoints": 0, "createdAt": "2026-11-04T09:00:00.000Z",
            "peerVerification": {"_id": review_id, "status": status, "nbVerifiedVotes": verified, "nbRejectedVotes": rejected,
                                 "checks": [{"name": "task", "task": text, "code": "SECRET-CODE"}],
                                 "rejectionReasons": [{"slug": "x", "text": "SECRET REASON", "count": 1}]}}


def picture(review_id, status, *, verified=0, rejected=3, legacy=False):
    entry = {"verificationCode": "SECRET-CODE", "imageKey": "SECRET-IMAGE-KEY", "submittedAt": "2026-11-04T09:30:00.000Z",
             "peerVerificationId": review_id}
    if legacy:
        entry["votes"] = {"status": status, "_id": review_id, "verifiedVotes": verified, "rejectedVotes": rejected}
    else:
        entry["votes"] = None
        entry["peerVerification"] = {"_id": review_id, "status": status, "nbVerifiedVotes": verified, "nbRejectedVotes": rejected}
    return entry


# ---------------------------------------------------------------------------------------------------------------- parsing

class TestParsing:
    def test_task_history_keeps_only_ids_status_and_votes(self) -> None:
        obs, = parse_task_history([task("pv1", "rejected", verified=1, rejected=4)], LOCK)
        assert (obs.lock_id, obs.source, obs.review_id, obs.status, obs.verified_votes, obs.rejected_votes) == \
               (LOCK, ReviewSource.TASKS, "pv1", ReviewStatus.REJECTED, 1, 4)
        assert "SECRET" not in repr(obs)

    def test_entries_without_a_valid_review_are_skipped(self) -> None:
        raw = [{"status": "completed", "task": "x"}, task("pv1", "weird"), task("../bad", "rejected"), "junk", task("pv2", "ongoing")]
        assert [o.review_id for o in parse_task_history(raw, LOCK)] == ["pv2"]

    def test_verification_pictures_new_and_legacy_shapes(self) -> None:
        new, legacy = parse_verification_pictures([picture("pa", "rejected"), picture("pb", "verified", verified=5, rejected=0, legacy=True)], LOCK)
        assert (new.review_id, new.status, new.rejected_votes, new.source) == ("pa", ReviewStatus.REJECTED, 3, ReviewSource.VERIFICATION_PICTURE)
        assert (legacy.review_id, legacy.status, legacy.verified_votes) == ("pb", ReviewStatus.VERIFIED, 5)
        assert "SECRET" not in repr((new, legacy))

    def test_a_picture_without_peer_verification_is_skipped(self) -> None:
        assert parse_verification_pictures([{"imageKey": "k", "submittedAt": "t", "votes": None, "peerVerificationId": None}], LOCK) == []


# ---------------------------------------------------------------------------------------------------------------- the HTTP reads

class TestLockClient:
    @pytest.mark.parametrize("method, url", [("get_task_history", TASK_HISTORY_URL), ("get_verification_pictures", VERIFICATION_PICTURES_URL)])
    def test_get_with_bearer_token(self, method, url) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = [{"a": 1}, "junk"]
        with patch("requests.Session.send", return_value=response) as send:
            body = getattr(ChasterLockClient(), method)(access_token="tok", lock_id=LOCK)
        prepared = send.call_args.args[0]
        assert prepared.method == "GET" and prepared.url == url.format(lock_id=LOCK) and prepared.headers["Authorization"] == "Bearer tok"
        assert body == [{"a": 1}]

    @pytest.mark.parametrize("status, payload", [(403, []), (200, {"not": "a list"})])
    def test_errors(self, status, payload) -> None:
        response = MagicMock(status_code=status)
        response.json.return_value = payload
        with patch("requests.Session.send", return_value=response), pytest.raises(ChasterLockApiError):
            ChasterLockClient().get_task_history(access_token="tok", lock_id=LOCK)

    def test_a_bad_lock_id_is_never_sent(self) -> None:
        with patch("requests.Session.send") as send, pytest.raises(ValueError):
            ChasterLockClient().get_verification_pictures(access_token="tok", lock_id="../x")
        send.assert_not_called()


class FakeTokens:
    def __init__(self, raises=None) -> None:
        self.raises = raises

    def call_with_token(self, user_id, *, now, operation):
        if self.raises:
            raise self.raises
        return operation("tok")


class FakeClient:
    def __init__(self, tasks=None, pictures=None, fail=None) -> None:
        self.tasks, self.pictures, self.fail, self.calls = tasks or {}, pictures or {}, fail or set(), []

    def get_task_history(self, *, access_token, lock_id):
        self.calls.append(("tasks", lock_id))
        if "tasks" in self.fail:
            raise ChasterLockApiError("x", status_code=500)
        return self.tasks.get(lock_id, [])

    def get_verification_pictures(self, *, access_token, lock_id):
        self.calls.append(("pictures", lock_id))
        if "pictures" in self.fail:
            raise ChasterLockApiError("x", status_code=404)
        return self.pictures.get(lock_id, [])


class TestReader:
    def test_reads_both_sources_of_every_lock_once(self) -> None:
        client = FakeClient(tasks={LOCK: [task("pv1", "ongoing")]}, pictures={LOCK: [picture("pa", "verified")]})
        report = PeerReviewReader(token_manager=FakeTokens(), lock_client=client).read(user_id="acct", lock_ids=[LOCK, LOCK], now=NOW)
        assert sorted(o.review_id for o in report.observations) == ["pa", "pv1"] and report.problems == []
        assert client.calls == [("tasks", LOCK), ("pictures", LOCK)]

    def test_a_failing_source_is_a_problem_and_the_other_still_counts(self) -> None:
        client = FakeClient(tasks={LOCK: [task("pv1", "ongoing")]}, fail={"pictures"})
        report = PeerReviewReader(token_manager=FakeTokens(), lock_client=client).read(user_id="acct", lock_ids=[LOCK], now=NOW)
        assert [o.review_id for o in report.observations] == ["pv1"] and report.problems == [f"{LOCK} verification-picture: api_error:404"]

    def test_token_problems(self) -> None:
        report = PeerReviewReader(token_manager=FakeTokens(ChasterNotConnectedError("x")), lock_client=FakeClient()).read(
            user_id="acct", lock_ids=[LOCK], now=NOW)
        assert report.problems == [f"{LOCK} tasks: not_connected", f"{LOCK} verification-picture: not_connected"]


# ---------------------------------------------------------------------------------------------------------------- the watch

class World:
    def __init__(self, tmp_path: Path, *, client=None, locks=(LOCK,), register=None) -> None:
        self.core = CoreDatabase(tmp_path / "w.db")
        with self.core.raw_connection() as conn:
            for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                conn.executescript(path.read_text(encoding="utf-8"))
        self.trust = TrustManager(self.core.db_path, core=self.core)
        self.trust.create_domain(domain_id=DOMAIN, display_name="Routine", description="d", created_via_consent_id="c",
                                 now=NOW - timedelta(days=30))
        self.client = client or FakeClient()
        self.locks = list(locks)
        self.registered = []

        def real_register(incident_id, description, now):
            self.registered.append(incident_id)
            self.trust.register_incident_report(rule_group_id=RULE_GROUP, trust_domain=DOMAIN, description=description,
                                                evidence=PEER_REVIEW_EVIDENCE, now=now, incident_id=incident_id)

        self.watch = PeerReviewWatch(self.core.db_path, core=self.core,
                                     reader=PeerReviewReader(token_manager=FakeTokens(), lock_client=self.client),
                                     account_user_id="acct", list_lock_ids=lambda now: (self.locks, []),
                                     register_incident=register or real_register)
        self.decisions = build_peer_review_decisions(SimpleNamespace(db_path=self.core.db_path, owner_user_id=OWNER), self.core)

    def set(self, tasks=(), pictures=()) -> None:
        self.client.tasks[LOCK] = list(tasks)
        self.client.pictures[LOCK] = list(pictures)

    def q(self, sql, args=()):
        with self.core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(sql, args)]

    def incidents(self):
        return self.q("SELECT * FROM incidents ORDER BY created_at")


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


class TestWatch:
    def test_reviews_that_ended_before_the_watch_never_create_an_incident(self, w) -> None:
        w.set(tasks=[task("old1", "rejected"), task("old2", "verified")], pictures=[picture("oldp", "rejected")])
        result = w.watch.poll(NOW)
        assert result.transitions == 3 and result.incidents_registered == 0 and w.incidents() == []
        assert w.q("SELECT lock_id FROM peer_review_locks") == [{"lock_id": LOCK}]

    def test_an_ongoing_review_that_is_rejected_creates_exactly_one_unconfirmed_incident(self, w) -> None:
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected", verified=1, rejected=4)])
        assert w.watch.poll(NOW + timedelta(minutes=10)).incidents_registered == 1
        for i in range(3):
            assert w.watch.poll(NOW + timedelta(minutes=20 + i)).incidents_registered == 0
        incident, = w.incidents()
        assert incident["confirmation"] == "unconfirmed" and incident["rule_group_id"] == RULE_GROUP and incident["trust_domain"] == DOMAIN
        assert incident["assessment_intrinsic_severity"] is None and incident["evidence_confidence"] == "low"
        assert "REJECTED a task photo on lock" in incident["description"] and "1 verified / 4 rejected votes" in incident["description"]
        assert "SECRET" not in json.dumps(incident)
        row, = w.q("SELECT * FROM peer_reviews")
        assert row["incident_id"] == incident["id"] and row["incident_registered_at"] is not None

    def test_a_new_review_already_rejected_after_the_baseline_creates_one(self, w) -> None:
        w.set()
        w.watch.poll(NOW)                                            # baseline: nothing yet
        w.set(pictures=[picture("pa", "rejected")])
        assert w.watch.poll(NOW + timedelta(minutes=10)).incidents_registered == 1
        assert "verification picture" in w.incidents()[0]["description"]

    def test_every_status_change_is_recorded_once_and_vote_moves_are_not_transitions(self, w) -> None:
        w.set(tasks=[task("pv1", "ongoing", verified=0, rejected=0)])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "ongoing", verified=1, rejected=2)])
        w.watch.poll(NOW + timedelta(minutes=1))
        w.set(tasks=[task("pv1", "rejected", verified=1, rejected=5)])
        w.watch.poll(NOW + timedelta(minutes=2))
        w.watch.poll(NOW + timedelta(minutes=3))
        assert [(t["previous_status"], t["new_status"]) for t in w.q("SELECT * FROM peer_review_transitions ORDER BY created_at")] == \
               [(None, "ongoing"), ("ongoing", "rejected")]
        assert w.q("SELECT rejected_votes FROM peer_reviews") == [{"rejected_votes": 5}]

    def test_a_verified_review_creates_nothing(self, w) -> None:
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "verified", verified=5)])
        w.watch.poll(NOW + timedelta(minutes=1))
        assert w.incidents() == []

    def test_two_watchers_on_one_database_create_one_incident(self, tmp_path, w) -> None:
        other = PeerReviewWatch(w.core.db_path, core=w.core, reader=w.watch._reader, account_user_id="acct",
                                list_lock_ids=lambda now: ([LOCK], []), register_incident=w.watch._register)
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected")])
        w.watch.poll(NOW + timedelta(minutes=1))
        other.poll(NOW + timedelta(minutes=1))
        assert len(w.incidents()) == 1

    def test_a_failed_registration_is_retried_with_the_same_id(self, tmp_path) -> None:
        calls = []

        def flaky(incident_id, description, now):
            calls.append(incident_id)
            if len(calls) == 1:
                raise RuntimeError("database busy")
            world.trust.register_incident_report(rule_group_id=RULE_GROUP, trust_domain=DOMAIN, description=description,
                                                 evidence=PEER_REVIEW_EVIDENCE, now=now, incident_id=incident_id)

        world = World(tmp_path, register=flaky)
        world.set(tasks=[task("pv1", "ongoing")])
        world.watch.poll(NOW)
        world.set(tasks=[task("pv1", "rejected")])
        assert world.watch.poll(NOW + timedelta(minutes=1)).incidents_registered == 0
        assert world.watch.poll(NOW + timedelta(minutes=2)).incidents_registered == 1
        assert calls[0] == calls[1] and len(world.incidents()) == 1

    def test_an_incident_that_already_exists_is_not_duplicated(self, w) -> None:
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected")])
        original = w.watch._register_claimed
        w.watch._register_claimed = lambda now: 0                    # simulate a crash right after the claim
        w.watch.poll(NOW + timedelta(minutes=1))
        w.watch._register_claimed = original
        incident_id = w.q("SELECT incident_id FROM peer_reviews")[0]["incident_id"]
        w.trust.register_incident_report(rule_group_id=RULE_GROUP, trust_domain=DOMAIN, description="d", evidence=PEER_REVIEW_EVIDENCE,
                                         now=NOW, incident_id=incident_id)   # ...and the registration happened before the crash
        assert w.watch.poll(NOW + timedelta(minutes=2)).incidents_registered == 1
        assert len(w.incidents()) == 1 and w.q("SELECT incident_registered_at FROM peer_reviews")[0]["incident_registered_at"]

    def test_the_trust_manager_rejects_a_duplicate_incident_id(self, w) -> None:
        w.trust.register_incident_report(rule_group_id="g", trust_domain=DOMAIN, description="d", evidence=PEER_REVIEW_EVIDENCE, now=NOW,
                                          incident_id="fixed-id")
        with pytest.raises(sqlite3.IntegrityError):
            w.trust.register_incident_report(rule_group_id="g", trust_domain=DOMAIN, description="d", evidence=PEER_REVIEW_EVIDENCE,
                                              now=NOW, incident_id="fixed-id")

    def test_a_partial_first_read_postpones_the_baseline(self, tmp_path) -> None:
        world = World(tmp_path, client=FakeClient(fail={"pictures"}))
        world.set(tasks=[task("old1", "rejected")])
        result = world.watch.poll(NOW)
        assert result.transitions == 0 and world.q("SELECT * FROM peer_review_locks") == [] and result.problems
        world.client.fail = set()
        world.set(tasks=[task("old1", "rejected")], pictures=[picture("oldp", "rejected")])
        world.watch.poll(NOW + timedelta(minutes=1))
        assert world.incidents() == [] and len(world.q("SELECT * FROM peer_reviews")) == 2

    def test_a_partial_read_after_the_baseline_still_records_what_it_saw(self, tmp_path) -> None:
        world = World(tmp_path)
        world.set()
        world.watch.poll(NOW)
        world.client.fail = {"pictures"}
        world.set(tasks=[task("pv1", "rejected")])
        assert world.watch.poll(NOW + timedelta(minutes=1)).incidents_registered == 1

    def test_no_locks_no_reads(self, tmp_path) -> None:
        world = World(tmp_path, locks=())
        assert world.watch.poll(NOW).locks == 0 and world.client.calls == []


class TestNoDoubleJeopardy:
    def _rejected(self, w):
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected")])
        w.watch.poll(NOW + timedelta(minutes=1))

    def test_a_rejection_counts_nowhere_until_the_keyholder_confirms_it(self, w) -> None:
        self._rejected(w)
        assert w.trust.get_confirmed_incidents_since(NOW - timedelta(days=1)) == []
        assert w.q("SELECT * FROM trust_evidence") == [] and w.q("SELECT * FROM confirmation_records") == []
        events = [e["event_type"] for e in w.q("SELECT event_type FROM domain_events")]
        assert events.count("incident.reported") == 1 and "incident.confirmation_changed" not in events

    def test_nothing_consumes_an_unconfirmed_incident(self, w) -> None:
        from penalty_engine.repository import PenaltyEngine
        from recovery_plan.repository import RecoveryPlanManager
        from system.startup import build_consumer_registry
        registry = build_consumer_registry(w.trust, PenaltyEngine(w.core.db_path, core=w.core), RecoveryPlanManager(w.core.db_path, core=w.core))
        assert registry._registrations.get("incident.reported", []) == []

    def test_the_watch_package_never_reaches_a_chaster_write_or_a_penalty(self) -> None:
        banned = ("chaster_execution", "penalty_engine", "release_engine", "rules_engine", "set_extensions", "set_frozen")
        for path in (ROOT / "peer_review").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + \
                    [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
            assert not [n for n in names if n.startswith(banned)], path
        import inspect
        poll_code = inspect.getsource(PeerReviewWatch)                 # the automatic path: it can only register, never confirm
        assert "confirm_incident" not in poll_code and "_confirm" not in poll_code and "IncidentConfirmation" not in poll_code
        reader_text = (ROOT / "chaster" / "peer_reviews.py").read_text(encoding="utf-8")
        assert "post" not in reader_text.lower().replace("postpone", "") and "send_with_ordered_headers" not in reader_text


# ---------------------------------------------------------------------------------------------------------------- notices and decisions

class TestNoticesAndDecisions:
    def _incident(self, w) -> str:
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected", verified=0, rejected=3)])
        w.watch.poll(NOW + timedelta(minutes=1))
        return w.incidents()[0]["id"]

    def test_the_owner_is_told_once(self, w) -> None:
        incident_id = self._incident(w)
        notice, = w.watch.pending_notifications()
        assert notice.incident_id == incident_id and "UNCONFIRMED incident" in notice.text and "Nothing was deducted" in notice.text
        assert f"peer-reviews confirm|dismiss {incident_id}" in notice.text and "SECRET" not in notice.text
        w.watch.mark_notified(incident_id, NOW)
        assert w.watch.pending_notifications() == []

    def test_confirm_advances_the_incident_by_keyholder_review(self, w) -> None:
        incident_id = self._incident(w)
        assert w.decisions.decide(actor=OWNER, incident_id=incident_id, confirm=True, confirmed=True, now=NOW) is DecisionOutcome.CONFIRMED
        assert w.incidents()[0]["confirmation"] == "confirmed"
        record, = w.q("SELECT * FROM confirmation_records")
        assert record["source"] == ConfirmationSource.KEYHOLDER_REVIEW.value and record["new_confirmation"] == IncidentConfirmation.CONFIRMED.value
        assert w.decisions.decide(actor=OWNER, incident_id=incident_id, confirm=False, confirmed=True, now=NOW) is DecisionOutcome.ALREADY_DECIDED

    def test_dismiss_leaves_it_unconfirmed_for_good(self, w) -> None:
        incident_id = self._incident(w)
        assert w.decisions.decide(actor=OWNER, incident_id=incident_id, confirm=False, confirmed=True, now=NOW) is DecisionOutcome.DISMISSED
        assert w.incidents()[0]["confirmation"] == "unconfirmed" and w.watch.pending_notifications() == []
        assert w.decisions.rows()[0].owner_decision == "dismissed"

    @pytest.mark.parametrize("kw, outcome", [({"actor": "x"}, DecisionOutcome.NOT_OWNER), ({"confirmed": False}, DecisionOutcome.NOT_CONFIRMED),
                                             ({"incident_id": "nope"}, DecisionOutcome.NOT_FOUND)])
    def test_guards(self, w, kw, outcome) -> None:
        incident_id = self._incident(w)
        args = {"actor": OWNER, "incident_id": incident_id, "confirm": True, "confirmed": True, "now": NOW, **kw}
        assert w.decisions.decide(**args) is outcome and w.incidents()[0]["confirmation"] == "unconfirmed"

    def test_an_owner_must_be_configured(self, w) -> None:
        decisions = build_peer_review_decisions(SimpleNamespace(db_path=w.core.db_path, owner_user_id=None), w.core)
        assert decisions.decide(actor=OWNER, incident_id="x", confirm=True, confirmed=True, now=NOW) is DecisionOutcome.OWNER_NOT_CONFIGURED


# ---------------------------------------------------------------------------------------------------------------- wiring and loop

def _config(db_path, **kw):
    base = dict(db_path=db_path, enable_peer_review_watch=True, chaster_execution_keyholder_user_id="acct", peer_review_trust_domain=DOMAIN,
                peer_review_poll_seconds=600, owner_user_id=OWNER)
    base.update(kw)
    return SimpleNamespace(**base)


class TestWiring:
    def test_off_by_default(self, w) -> None:
        out = build_peer_review_watch(_config(w.core.db_path, enable_peer_review_watch=False), w.core, token_manager=FakeTokens(),
                                      lock_client=FakeClient(), connections=object())
        assert out.watch is None and out.notes == []

    @pytest.mark.parametrize("kw, connections", [({"chaster_execution_keyholder_user_id": None}, object()), ({}, None),
                                                 ({"peer_review_trust_domain": "missing"}, object())])
    def test_not_active_without_its_prerequisites(self, w, kw, connections) -> None:
        out = build_peer_review_watch(_config(w.core.db_path, **kw), w.core, token_manager=FakeTokens(), lock_client=FakeClient(),
                                      connections=connections)
        assert out.watch is None and "NOT active" in out.notes[0]

    def test_enabled(self, w) -> None:
        out = build_peer_review_watch(_config(w.core.db_path, peer_review_poll_seconds=30), w.core, token_manager=FakeTokens(),
                                      lock_client=FakeClient(), connections=object())
        assert out.watch is not None and out.interval_seconds == 120 and "Community review watch enabled" in out.notes[0]

    def test_the_loop_polls_and_delivers_the_notice(self, w) -> None:
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected")])
        sent = []

        async def send(target, text):
            sent.append((target, text))
            return True

        async def run():
            clock = SimpleNamespace(now=lambda: NOW + timedelta(minutes=5))
            loop_task = asyncio.create_task(peer_review_loop(w.watch, clock, interval_seconds=3600, send=send, owner_target="123",
                                                             initial_delay_seconds=0))
            for _ in range(1000):                                     # up to 10 s: the poll runs in a worker thread
                await asyncio.sleep(0.01)
                if sent and not w.watch.pending_notifications():
                    break
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

        asyncio.run(run())
        assert len(sent) == 1 and sent[0][0] == "123" and w.watch.pending_notifications() == []

    def test_an_undelivered_notice_stays_pending(self, w) -> None:
        w.set(tasks=[task("pv1", "ongoing")])
        w.watch.poll(NOW)
        w.set(tasks=[task("pv1", "rejected")])

        attempts = []

        async def refuse(target, text):
            attempts.append(text)
            return False

        async def run():
            clock = SimpleNamespace(now=lambda: NOW + timedelta(minutes=5))
            loop_task = asyncio.create_task(peer_review_loop(w.watch, clock, interval_seconds=3600, send=refuse, owner_target="123",
                                                             initial_delay_seconds=0))
            for _ in range(1000):
                await asyncio.sleep(0.01)
                if attempts:
                    break
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

        asyncio.run(run())
        assert len(attempts) == 1 and len(w.watch.pending_notifications()) == 1

    def test_the_bot_starts_and_stops_the_loop(self) -> None:
        text = (ROOT / "bot" / "discord_bot.py").read_text(encoding="utf-8")
        assert 'asyncio.create_task(self.peer_reviews, name="peer-review-watch")' in text and "self._peer_review_task.cancel()" in text


# ---------------------------------------------------------------------------------------------------------------- the CLI

class TestCli:
    def _run(self, w, *args):
        out = io.StringIO()
        code = main(["peer-reviews", *args], out=out, peer_factory=lambda: w.decisions, now=lambda: NOW)
        return code, out.getvalue()

    def _incident(self, w) -> str:
        return TestNoticesAndDecisions()._incident(w)

    def test_list(self, w) -> None:
        code, text = self._run(w, "list", "--owner-id", OWNER)
        assert code == 0 and text.strip() == "No community review results recorded."
        incident_id = self._incident(w)
        code, text = self._run(w, "list", "--owner-id", OWNER)
        assert code == 0 and f"incident {incident_id} UNCONFIRMED, awaiting your decision" in text and "rejected" in text
        assert "SECRET" not in text

    def test_list_is_owner_only(self, w) -> None:
        code, text = self._run(w, "list", "--owner-id", "x")
        assert code == 3 and "not the owner" in text

    def test_confirm_and_dismiss(self, w) -> None:
        incident_id = self._incident(w)
        code, text = self._run(w, "confirm", incident_id, "--owner-id", OWNER)
        assert code == 3 and "add --yes" in text
        code, text = self._run(w, "dismiss", incident_id, "--owner-id", OWNER, "--yes")
        assert code == 0 and text.startswith("Dismissed") and "DISMISSED" in self._run(w, "list", "--owner-id", OWNER)[1]

    def test_every_outcome_has_a_text(self) -> None:
        from cli.main import _PEER_TEXT
        assert set(_PEER_TEXT) == {o.value for o in DecisionOutcome}

    def test_render_without_rows(self) -> None:
        assert render_peer_reviews([]) == "No community review results recorded."


def test_migration_043(w) -> None:
    assert w.q("SELECT version FROM schema_version WHERE version = 43")
