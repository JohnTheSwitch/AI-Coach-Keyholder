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

    def test_a_new_lock_after_a_release_is_frozen_once(self, tmp_path) -> None:
        """Live, 2026-11-23: the first freeze of a new lock, then a second one a minute later for the previous lock's release, decided
        from the reading taken before the first freeze."""
        from tests.system.test_release_tick import released_world
        world = released_world(tmp_path)
        world.lock_id, world.frozen = "lock-2", False                # the old lock ended; a new one, not frozen yet
        world.task.first_sight_every = timedelta(minutes=10)
        world.cycle(local(MON, 22, 5))                                # the old release's session is over too
        world.refresh_effective = False                               # the stored reading stays the one from before the freeze
        world.fetched_at = world.now - timedelta(minutes=1)
        world.now += timedelta(minutes=1)
        world.task.run(world.now)
        world.executor.run_once(now=world.now)
        assert [c for c in world.transport.calls if c[0] == "freeze"] == [("freeze", "lock-2")]

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


class TestTheTimerTo0:
    """Owner, 2026-11-28: "a higher initial timer ... when the system picks it up and freezes it, it would also set the timer to 0"."""

    def test_a_new_lock_is_frozen_then_its_timer_goes_to_0_once(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=False)
        world.task.zero_time_from = world.now - timedelta(hours=1)
        world.end_date = world.now + timedelta(days=3)
        world.cycle()                                                # first sighting: frozen
        world.frozen, world.frozen_since = True, timedelta(0)
        report = world.cycle(world.now + timedelta(minutes=1))
        assert "barrier_zero_time" in report.notes
        assert world.transport.calls == [("freeze", "lock-1"), ("zero_time", "lock-1", 3 * 24 * 3600 - 60)]
        world.end_date = world.now
        world.end_date = world.now + timedelta(days=1)               # even with time again later: once per lock only
        world.cycle(world.now + timedelta(hours=1))
        assert [c for c in world.transport.calls if c[0] == "zero_time"] == [("zero_time", "lock-1", 3 * 24 * 3600 - 60)]

    def test_a_lock_first_seen_before_the_switch_keeps_its_timer(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=True)
        world.end_date = world.now + timedelta(days=3)
        world.cycle()                                                # seen with the switch off
        world.task.zero_time_from = world.now + timedelta(minutes=5)
        world.cycle(world.now + timedelta(minutes=10))
        assert world.transport.calls == []

    def test_off_unless_his_wiring_sets_it(self, tmp_path) -> None:
        world = new_lock_world(tmp_path, frozen=True)
        world.end_date = world.now + timedelta(days=3)
        world.cycle()
        world.cycle(world.now + timedelta(minutes=10))
        assert world.task.zero_time_from is None and world.transport.calls == []
