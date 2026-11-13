"""Owner, 2026-11-13: "freeze the lock automatically when it sees it for the first time." Once per lock; never against a release. No network."""

from __future__ import annotations

from datetime import timedelta

from tests.system.test_release_tick import MON, U, World, local


def new_lock_world(tmp_path, **kw):
    world = World(tmp_path, **kw)
    world.task.first_sight_every = timedelta(minutes=10)
    world.at(local(MON, 18, 0))
    return world


class TestTheFirstSighting:
    def test_a_new_unfrozen_lock_is_frozen(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=False)
        report = world.cycle()
        assert "barrier_first_freeze" in report.notes and world.transport.calls == [("freeze", "lock-1")]
        assert world.decisions()[0]["rule_id"] == "barrier.freeze"

    def test_only_once_per_lock(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=False)
        world.cycle()
        world.frozen = False                                         # unfrozen again later (a release, or by hand)
        world.cycle(local(MON, 19, 0))
        world.cycle(local(MON, 20, 0))
        assert world.transport.calls == [("freeze", "lock-1")]

    def test_an_already_frozen_lock_is_only_recorded(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=True)
        assert "first_sighting:already_frozen" in world.cycle().notes and world.transport.calls == []
        world.frozen = False
        world.cycle(local(MON, 19, 0))
        assert world.transport.calls == []                           # seen before: never frozen by this rule

    def test_a_release_is_never_fought(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=True, frozen_since=timedelta(hours=2))
        world.cycle()                                                # first sighting: frozen, recorded
        world.tokens()
        world.cycle()
        target = world.target()
        world.cycle(target)
        assert world.auth_states() == ["provider_confirmed"]
        world.frozen = False
        world.cycle(target + timedelta(minutes=20))
        assert [c for c in world.transport.calls if c[0] == "freeze"] == []

    def test_waits_while_halted(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=False)
        world.control.set_halted(True, reason="test", now=world.now)
        world.cycle()
        assert world.transport.calls == [] and not world.engine.has_seen_lock("lock-1")
        world.control.set_halted(False, reason="test", now=world.now)
        world.cycle(local(MON, 18, 30))
        assert world.transport.calls == [("freeze", "lock-1")]

    def test_not_a_locked_keyholder_lock_is_left_alone(self, tmp_path) -> None:
        from chaster.models import ChasterProviderStatus
        world = new_lock_world(tmp_path, frozen=False, status=ChasterProviderStatus.UNLOCKED)
        world.cycle()
        assert world.transport.calls == [] and not world.engine.has_seen_lock("lock-1")
