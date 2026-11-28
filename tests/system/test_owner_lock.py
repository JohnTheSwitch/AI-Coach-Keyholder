"""Owner, 2026-11-13: the owner joins the keyholder's premade shared lock and the system recognises it (lock ids set to `auto`). Anyone else's lock
is never acted on. A real database; fake Chaster reads; no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.clock import FrozenClock
from infrastructure.database import Database as CoreDatabase
from system.keyholder_lock_source import KeyholderLockObservationSource
from system.owner_lock import RESOLVE_EVERY, OwnerLockResolver, is_auto

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
ME, OTHER, KH = "me-account", "stranger-account", "kh-account"


def lock(lock_id, wearer=ME, *, test=False, start="2026-11-13T19:00:00.000Z", frozen=True):
    return {"_id": lock_id, "status": "locked", "isTestLock": test, "isFrozen": frozen, "startDate": start, "role": "keyholder",
            "user": {"_id": wearer, "username": f"user-{wearer}"}, "keyholder": {"_id": KH, "username": "kh"}}


class Tokens:
    def call_with_token(self, user, *, now, operation):
        return operation("token")


class Client:
    def __init__(self, locks):
        self.locks, self.searches, self.fail = locks, 0, False

    def search_keyholder_locks(self, *, access_token, status="locked"):
        self.searches += 1
        if self.fail:
            raise RuntimeError("chaster down")
        return list(self.locks)

    def get_lock(self, *, access_token, lock_id):
        return next(dict(l) for l in self.locks if l["_id"] == lock_id)


@pytest.fixture
def core(tmp_path):
    c = CoreDatabase(tmp_path / "t.db")
    with c.raw_connection() as conn:
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
    return c


def resolver(core, client, told=None):
    return OwnerLockResolver(core, owner_chaster_id=ME, keyholder_user_id="kh-user", keyholder_tokens=Tokens(), lock_client=client,
                             clock=FrozenClock(NOW), notify=(lambda key, text: told.append((key, text))) if told is not None else None)


class TestRecognition:
    def test_the_owners_lock_is_found(self, core) -> None:
        assert resolver(core, Client([lock("a")])).current(NOW) == "a"

    def test_a_test_lock_is_not_used(self, core) -> None:
        assert resolver(core, Client([lock("t", test=True)])).current(NOW) is None

    def test_the_newest_of_several_and_the_owner_is_told_once(self, core) -> None:
        told = []
        r = resolver(core, Client([lock("old", start="2026-11-01T10:00:00.000Z"), lock("new")]), told)
        assert r.current(NOW) == "new"
        r.current(NOW + RESOLVE_EVERY)
        assert [k for k, _ in told] == ["owner_lock:several:new"]

    def test_anyone_elses_lock_is_never_used_and_the_owner_is_told(self, core) -> None:
        told = []
        r = resolver(core, Client([lock("x", wearer=OTHER, start="2026-11-13T19:59:00.000Z"), lock("mine")]), told)
        assert r.current(NOW) == "mine"
        assert told and told[0][0] == "owner_lock:foreign:x" and "never acts on it" in told[0][1]
        assert resolver(core, Client([lock("x", wearer=OTHER)])).current(NOW) is None

    def test_searches_at_most_every_ten_minutes(self, core) -> None:
        client = Client([lock("a")])
        r = resolver(core, client)
        r.current(NOW)
        r.current(NOW + timedelta(minutes=5))
        assert client.searches == 1
        r.current(NOW + RESOLVE_EVERY)
        assert client.searches == 2

    def test_a_failed_search_keeps_the_last_choice(self, core) -> None:
        client = Client([lock("a")])
        r = resolver(core, client)
        r.current(NOW)
        client.fail = True
        assert r.current(NOW + RESOLVE_EVERY) == "a"

    def test_a_lock_that_ended_is_dropped(self, core) -> None:
        client = Client([lock("a")])
        r = resolver(core, client)
        r.current(NOW)
        client.locks = []
        assert r.current(NOW + RESOLVE_EVERY) is None and r.cached() is None

    def test_after_a_restart_the_last_observed_lock_is_used_until_the_first_search(self, core) -> None:
        with core.transaction() as tx:
            tx.execute("INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, wearer_chaster_id, keyholder_chaster_id, fetched_at, "
                       "created_at) VALUES ('o', 'seen', 'observed', 'keyholder_search', 'locked', ?, ?, ?, ?)", (ME, KH, "2026-11-13T19:00:00Z", "2026-11-13T19:00:00Z"))
        assert resolver(core, Client([])).cached() == "seen"

    def test_is_auto(self) -> None:
        assert is_auto("auto") and is_auto(" AUTO ") and not is_auto("6aca95cdc5ee0f6d4989cd25") and not is_auto(None)


class TestTheConsumers:
    def test_the_release_lock_is_read_through_the_resolver(self, core) -> None:
        client = Client([lock("a")])
        r = resolver(core, client)
        source = KeyholderLockObservationSource(core.db_path, core=core, lock_id=None, wearer_user_id="u", keyholder_user_id="kh-user",
                                                keyholder_tokens=Tokens(), lock_client=client, clock=FrozenClock(NOW), resolver=r)
        assert source.get_latest() is None
        assert source.refresh(NOW) == "observed"
        obs = source.get_latest()
        assert obs.chaster_lock_id == "a" and obs.is_frozen is True

    def test_no_recognised_lock_reads_nothing(self, core) -> None:
        source = KeyholderLockObservationSource(core.db_path, core=core, lock_id=None, wearer_user_id="u", keyholder_user_id="kh-user",
                                                keyholder_tokens=Tokens(), lock_client=Client([]), clock=FrozenClock(NOW), resolver=resolver(core, Client([])))
        assert source.refresh(NOW) is None and source.get_latest() is None

    def test_the_barrier_waits_without_a_lock(self, core) -> None:
        from chaster_execution.penalty_barrier import PenaltyBarrierExecutor
        barrier = PenaltyBarrierExecutor(core.db_path, core=core, enabled=True, control=SimpleNamespace(is_halted=lambda: False),
                                         transport=object(), read_lock=lambda _id: {}, lock_id=None, resolver=resolver(core, Client([])))
        assert barrier._enabled is True and barrier._lock_id is None

    def test_the_exemption_opening_follows_the_resolver(self, core) -> None:
        from chaster_execution.temporary_opening import ExemptionOpening
        opening = ExemptionOpening(lock_id=None, account_user_id="kh-user", control=None, reader=None, transport=None, token_manager=None,
                                   lock_client=None, send=None, clock=FrozenClock(NOW), resolver=resolver(core, Client([lock("a")])))
        assert opening.lock_id == "a"


class TestWiring:
    def base(self, core, **kw):
        return SimpleNamespace(db_path=core.db_path, enable_rules_engine=False, enable_chaster_execution=False, chaster_execution_keyholder_user_id="kh-user",
                               owner_user_id="owner", **kw)

    def test_auto_needs_the_owners_account(self, core) -> None:
        from system.composition import build_rules_and_execution
        out = build_rules_and_execution(self.base(core, release_lock_id="auto"), core, lock_client=Client([]), keyholder_token_manager=Tokens())
        assert out.owner_lock is None and any("OWNER_CHASTER_ACCOUNT_ID" in n for n in out.notes)

    def test_auto_with_the_account_builds_one_resolver(self, core) -> None:
        from system.composition import build_rules_and_execution
        out = build_rules_and_execution(self.base(core, release_lock_id="auto", owner_chaster_account_id=ME), core, lock_client=Client([lock("a")]),
                                        keyholder_token_manager=Tokens())
        assert isinstance(out.owner_lock, OwnerLockResolver)

    def test_fixed_ids_build_none(self, core) -> None:
        from system.composition import build_rules_and_execution
        out = build_rules_and_execution(self.base(core, release_lock_id="6aca95cdc5ee0f6d4989cd25", owner_chaster_account_id=ME), core,
                                        lock_client=Client([]), keyholder_token_manager=Tokens())
        assert out.owner_lock is None

    def test_the_config_reads_the_account(self, monkeypatch) -> None:
        text = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
        assert 'get("OWNER_CHASTER_ACCOUNT_ID")' in text


class TestNoCheckInsWhileUnlocked:
    """Owner, 2026-11-18: "I think there should be no check-ins while not locked" (like a vacation, also for the day's evaluation)."""

    def test_unlocked_only_after_a_search_found_none(self, core) -> None:
        client = Client([])
        client.fail = True
        r = resolver(core, client)
        assert r.known_unlocked(NOW) is False                       # a failed first search is never a free pass
        client.fail = False
        assert r.known_unlocked(NOW + RESOLVE_EVERY) is True
        client.locks = [lock("L1")]
        assert r.known_unlocked(NOW + 2 * RESOLVE_EVERY) is False

    def test_a_day_without_a_lock(self, core) -> None:
        import uuid
        from infrastructure.time_format import iso
        from system.keyholder_lock_source import get_unlocked_throughout

        def seen(at, outcome="observed", status="locked"):
            with core.transaction() as tx:
                tx.execute("INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, fetched_at, created_at) "
                           "VALUES (?, 'L1', ?, 'keyholder_search', ?, 0, ?, ?)", (str(uuid.uuid4()), outcome, status, iso(at), iso(at)))

        day = (NOW + timedelta(days=1), NOW + timedelta(days=2))
        assert get_unlocked_throughout(core, *day) is False         # nothing known
        seen(NOW)
        assert get_unlocked_throughout(core, *day) is False         # last seen locked: the bot may just have been off
        seen(NOW + timedelta(hours=1), outcome="not_found", status=None)
        assert get_unlocked_throughout(core, *day) is True          # the lock had ended and nothing locked was seen that day
        seen(day[0] + timedelta(hours=15))
        assert get_unlocked_throughout(core, *day) is False         # a new lock that afternoon: the day counts

    def test_a_lock_that_ended_while_the_bot_was_off(self, core) -> None:
        """Live, 2026-11-28: he ended his lock on 25 Nov evening with the PC off until the 28th; the last reading said locked, so the 26th and
        27th earned token progress. Chaster's `unlockedAt`, read when the bot is back, says the lock had ended."""
        import uuid
        from infrastructure.time_format import iso
        from system.keyholder_lock_source import get_unlocked_throughout

        def seen(at, outcome="observed", status="locked", ended=None, lock="L1"):
            with core.transaction() as tx:
                tx.execute("INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, fetched_at, created_at, "
                           "ended_at) VALUES (?, ?, ?, 'keyholder_search', ?, 0, ?, ?, ?)",
                           (str(uuid.uuid4()), lock, outcome, status, iso(at), iso(at), iso(ended) if ended else None))

        seen(NOW)                                                    # locked, then the bot is off
        day1, day2 = (NOW + timedelta(days=1), NOW + timedelta(days=2)), (NOW + timedelta(days=2), NOW + timedelta(days=3))
        assert get_unlocked_throughout(core, *day1) is False
        seen(NOW + timedelta(days=3, hours=1), outcome="not_found", status=None, ended=NOW + timedelta(hours=2))   # back: ended 2 h after
        assert get_unlocked_throughout(core, *day1) is True
        assert get_unlocked_throughout(core, *day2) is True
        assert get_unlocked_throughout(core, NOW, NOW + timedelta(days=1)) is False   # the day it ended counts as it was
        seen(NOW + timedelta(days=1, hours=5), lock="L2")            # a new lock was seen on day 1: day 1 counts
        assert get_unlocked_throughout(core, *day1) is False

    def test_the_ended_lock_is_read_once_when_the_search_finds_none(self, core) -> None:
        """Live, 2026-11-28: after a restart the search found no lock and nothing read the old one again."""
        import uuid
        from infrastructure.time_format import iso
        from system.keyholder_lock_source import KeyholderLockObservationSource

        with core.transaction() as tx:
            tx.execute("INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, fetched_at, created_at) "
                       "VALUES (?, 'L1', 'observed', 'keyholder_search', 'locked', 0, ?, ?)", (str(uuid.uuid4()), iso(NOW), iso(NOW)))

        class Resolver:
            def current(self, now=None):
                return None

            def cached(self):
                return None

            def known_unlocked(self, now=None):
                return True

        class Tokens:
            def call_with_token(self, user_id, *, now, operation):
                return operation("t")

        class Client:
            calls = []

            def get_lock(self, *, access_token, lock_id):
                self.calls.append(lock_id)
                return {"_id": lock_id, "role": "keyholder", "status": "unlocked", "unlockedAt": "2026-11-25T19:10:00.000Z"}

        from infrastructure.clock import FrozenClock
        client = Client()
        src = KeyholderLockObservationSource(core.db_path, core=core, lock_id=None, wearer_user_id="w", keyholder_user_id="k",
                                             keyholder_tokens=Tokens(), lock_client=client, clock=FrozenClock(NOW), resolver=Resolver())
        later = NOW + timedelta(days=3)
        assert src.refresh(later) == "not_found"
        assert src.refresh(later + timedelta(minutes=5)) is None     # read once: the newest reading no longer says locked
        assert client.calls == ["L1"]
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT ended_at FROM managed_lock_observations WHERE outcome = 'not_found'")
        assert row["ended_at"].startswith("2026-11-25T19:10:00")


    def test_the_ended_lock_is_read_once_when_a_new_lock_is_found(self, core) -> None:
        """Live, 2026-11-28: he relocked before the bot had read the old lock's end. The old one is read first, then the new one."""
        import uuid
        from infrastructure.clock import FrozenClock
        from infrastructure.time_format import iso
        from system.keyholder_lock_source import KeyholderLockObservationSource

        with core.transaction() as tx:
            tx.execute("INSERT INTO managed_lock_observations (id, lock_id, outcome, source, status, is_frozen, fetched_at, created_at) "
                       "VALUES (?, 'L1', 'observed', 'keyholder_search', 'locked', 0, ?, ?)", (str(uuid.uuid4()), iso(NOW), iso(NOW)))

        class Resolver:
            def current(self, now=None):
                return "L2"

            def cached(self):
                return "L2"

            def known_unlocked(self, now=None):
                return False

        class Tokens:
            def call_with_token(self, user_id, *, now, operation):
                return operation("t")

        class Client:
            calls = []

            def get_lock(self, *, access_token, lock_id):
                self.calls.append(lock_id)
                if lock_id == "L1":
                    return {"_id": "L1", "role": "keyholder", "status": "unlocked", "unlockedAt": "2026-11-25T19:10:00.000Z"}
                return {"_id": "L2", "role": "keyholder", "status": "locked", "isFrozen": False}

        client = Client()
        src = KeyholderLockObservationSource(core.db_path, core=core, lock_id=None, wearer_user_id="w", keyholder_user_id="k",
                                             keyholder_tokens=Tokens(), lock_client=client, clock=FrozenClock(NOW), resolver=Resolver())
        assert src.refresh(NOW + timedelta(days=3)) == "observed"
        assert src.refresh(NOW + timedelta(days=3, minutes=5)) == "observed"
        assert client.calls == ["L1", "L2", "L2"]
