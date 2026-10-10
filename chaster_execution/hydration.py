"""
chaster_execution/hydration.py

Builds a Rules Engine LockSnapshot from a PERSISTED Chaster provider
observation (migration 026 columns). Anything not confirmed is None, which
the engine fails closed on. `canBeUnlocked` is deprecated by Chaster and is
never read. Freshness is enforced by the engine (15 minutes), not here.
"""

from __future__ import annotations

from typing import Callable

from chaster.models import ChasterProviderObservation
from rules_engine.models import LockSnapshot

__all__ = ["snapshot_from_observation", "make_snapshot_provider"]


def snapshot_from_observation(obs: ChasterProviderObservation | None) -> LockSnapshot | None:
    if obs is None:
        return None
    return LockSnapshot(
        observed_at=obs.fetched_at, status=obs.status.value, is_frozen=obs.is_frozen,
        can_be_unlocked=None, is_keyholder_lock=obs.has_keyholder,
    )


def make_snapshot_provider(get_latest: Callable[[], ChasterProviderObservation | None]) -> Callable[[], LockSnapshot | None]:
    return lambda: snapshot_from_observation(get_latest())
