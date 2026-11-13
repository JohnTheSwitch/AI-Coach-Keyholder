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
