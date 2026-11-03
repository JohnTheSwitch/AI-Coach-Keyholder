"""
chaster/emergency_unlock_provider.py

The narrow seam the real, confirmed Chaster unlock operation plugs
into. See docs/architecture/chaster_integration_technical_design.md
Section 25c/26 for the full history: the operation itself
(`POST /locks/{lockId}/emergency-unlock`) is confirmed via the
official OpenAPI spec; Option A (dynamic active-lock discovery at
request time, fail closed on zero/multiple candidates) is the
approved target-lock semantics.

`EmergencyUnlockProvider.attempt_unlock(access_token)`'s signature is
DELIBERATELY UNCHANGED -- it still accepts only a plaintext access
token, never a lock ID -- target selection happens entirely INSIDE
`RealChasterEmergencyUnlockProvider`, using Chaster's own live
response, so no caller (including the local HTTP listener) can ever
supply or influence which lock is targeted.

**Lock fields (resolved 2026-11-03).** The field-level `LockForWearer` schema is now confirmed from Chaster's official OpenAPI
document (version 0.35.0): `_id`, `status` (LockStatusEnum locked / unlocked / deserted), `role`, `lockType` (chastity / bondage),
`emergencyUnlocked` and `emergencyUnlock` (EmergencyUnlockConfig `{enabled, phrase}`). `lock_for_wearer_eligibility` reads exactly
those fields and never guesses: a missing or malformed field makes the lock INELIGIBLE with a reason, it never raises. The endpoint's
own 400 ("No emergency-unlock safety feature is enabled, or the lock cannot be released") stays the final authority.

Target discovery: only locks whose `status` is `locked` are candidates (an unlocked or deserted lock in the wearer's active list can
never be the target); zero or several candidates still fail closed, so the provider never chooses arbitrarily.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from chaster.lock_client import ChasterLockApiError, ChasterLockClient, EmergencyUnlockHttpOutcome

__all__ = [
    "EmergencyUnlockProvider",
    "ProviderUnlockOutcome",
    "ProviderUnlockResult",
    "UnwiredEmergencyUnlockProvider",
    "RealChasterEmergencyUnlockProvider",
    "LockEligibility",
    "lock_for_wearer_eligibility",
]


class ProviderUnlockOutcome(StrEnum):
    """Exactly two terminal outcomes for one attempted provider call --
    deliberately not more; a provider that couldn't be reached at all
    (network/timeout), found zero/multiple active locks, or found an
    ineligible lock are all FAILED, distinguished by `detail`, not
    separate enum values the caller would need to handle differently."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class ProviderUnlockResult:
    """`detail` is always safe to log/audit -- never a token, never raw
    provider response content, a short human-readable reason only."""
    outcome: ProviderUnlockOutcome
    detail: str


class EmergencyUnlockProvider(Protocol):
    """Receives ONLY a plaintext access token (decrypted by the caller
    immediately before this call, per `chaster/repository.py`'s own
    load-only-at-point-of-use principle) -- no lock ID, no arbitrary
    API path, no other caller-supplied parameter, by design (Option A:
    target selection is the provider's own internal responsibility,
    never the caller's)."""

    def attempt_unlock(self, *, access_token: str) -> ProviderUnlockResult: ...


class UnwiredEmergencyUnlockProvider:
    """A provider that never touches the network at all -- used only
    in tests/fixtures that need a provider guaranteed not to make any
    HTTP call. Production now uses
    `RealChasterEmergencyUnlockProvider` (see
    chaster/emergency_unlock_server.py)."""

    def attempt_unlock(self, *, access_token: str) -> ProviderUnlockResult:
        return ProviderUnlockResult(
            outcome=ProviderUnlockOutcome.FAILED,
            detail="This provider never contacts Chaster. No request was sent.",
        )


@dataclass(frozen=True, kw_only=True)
class LockEligibility:
    """The result of examining ONE active lock's raw dict for
    emergency-unlock eligibility (see `lock_for_wearer_eligibility`)."""
    lock_id: str
    eligible: bool
    reason: str


_LOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def lock_for_wearer_eligibility(raw_lock: dict) -> LockEligibility:
    """Emergency-unlock eligibility from the confirmed LockForWearer fields (OpenAPI 0.35.0). Eligible only when ALL hold: a valid
    `_id`, `status` == `locked`, `role` == `wearer` (the endpoint answers 403 to anyone else), `emergencyUnlocked` is not true, and
    `emergencyUnlock.enabled` is true. Anything missing or malformed is ineligible with a reason; nothing here raises."""
    lock_id = raw_lock.get("_id") if isinstance(raw_lock, dict) else None
    if not isinstance(lock_id, str) or not _LOCK_ID.match(lock_id):
        return LockEligibility(lock_id="", eligible=False, reason="the lock has no valid id")
    status = raw_lock.get("status")
    if status != "locked":
        return LockEligibility(lock_id=lock_id, eligible=False, reason=f"the lock is not locked (status {status!r})")
    role = raw_lock.get("role")
    if role != "wearer":
        return LockEligibility(lock_id=lock_id, eligible=False, reason=f"only the wearer can emergency-unlock (role {role!r})")
    if raw_lock.get("emergencyUnlocked") is True:
        return LockEligibility(lock_id=lock_id, eligible=False, reason="the lock was already emergency-unlocked")
    config = raw_lock.get("emergencyUnlock")
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return LockEligibility(lock_id=lock_id, eligible=False, reason="the lock has no emergency-unlock safety feature enabled")
    lock_type = raw_lock.get("lockType")
    kind = f"{lock_type} lock" if isinstance(lock_type, str) else "lock"
    return LockEligibility(lock_id=lock_id, eligible=True, reason=f"locked {kind} with the emergency-unlock safety feature enabled")


class RealChasterEmergencyUnlockProvider:
    """Implements Option A (docs/architecture/chaster_integration_technical_design.md
    Section 26): discover the account's active lock(s) live, at
    request time, via the confirmed `GET /locks` endpoint; proceed
    ONLY if exactly one is found; check its documented emergency-unlock
    eligibility via the injected (currently always-failing, honestly)
    `lock_field_extractor`; call the confirmed
    `POST /locks/{lockId}/emergency-unlock` endpoint only for that one,
    freshly-discovered lock. Never accepts a lock ID from any caller."""

    def __init__(
        self, *, lock_client: ChasterLockClient | None = None,
        lock_field_extractor: Callable[[dict], LockEligibility] = lock_for_wearer_eligibility,
    ) -> None:
        self._lock_client = lock_client if lock_client is not None else ChasterLockClient()
        self._lock_field_extractor = lock_field_extractor

    def attempt_unlock(self, *, access_token: str) -> ProviderUnlockResult:
        try:
            listed = self._lock_client.list_active_locks(access_token=access_token)
        except ChasterLockApiError as exc:
            return ProviderUnlockResult(outcome=ProviderUnlockOutcome.FAILED, detail=f"Could not retrieve active locks: {exc}")
        # An unlocked or deserted lock can never be the target; a lock without a status string stays a candidate (fail closed).
        active_locks = [l for l in listed if not (isinstance(l, dict) and isinstance(l.get("status"), str) and l["status"] != "locked")]

        if len(active_locks) == 0:
            return ProviderUnlockResult(outcome=ProviderUnlockOutcome.FAILED, detail="No active Chaster lock found.")
        if len(active_locks) > 1:
            return ProviderUnlockResult(
                outcome=ProviderUnlockOutcome.FAILED,
                detail=f"Found {len(active_locks)} active Chaster locks -- cannot determine an unambiguous target. No lock was unlocked.",
            )

        raw_lock = active_locks[0]
        try:
            eligibility = self._lock_field_extractor(raw_lock)
        except Exception:
            return ProviderUnlockResult(
                outcome=ProviderUnlockOutcome.FAILED,
                detail="Could not determine emergency-unlock eligibility for the active lock.",
            )

        if not eligibility.eligible:
            return ProviderUnlockResult(
                outcome=ProviderUnlockOutcome.FAILED,
                detail=f"The active lock does not meet Chaster's documented emergency-unlock prerequisites: {eligibility.reason}",
            )

        try:
            http_result = self._lock_client.emergency_unlock(access_token=access_token, lock_id=eligibility.lock_id)
        except ChasterLockApiError as exc:
            return ProviderUnlockResult(outcome=ProviderUnlockOutcome.FAILED, detail=f"Emergency-unlock request failed: {exc}")

        if http_result.outcome is EmergencyUnlockHttpOutcome.SUCCEEDED:
            return ProviderUnlockResult(outcome=ProviderUnlockOutcome.SUCCEEDED, detail="Chaster confirmed the emergency unlock (HTTP 204).")

        detail_by_outcome = {
            EmergencyUnlockHttpOutcome.NOT_ELIGIBLE: "Chaster rejected the emergency unlock -- the lock is not a bondage lock, has no safety feature enabled, or is not locked.",
            EmergencyUnlockHttpOutcome.UNAUTHORIZED: "Chaster rejected the emergency unlock -- not authorized.",
            EmergencyUnlockHttpOutcome.FORBIDDEN: "Chaster rejected the emergency unlock -- only the wearer can emergency-unlock.",
            EmergencyUnlockHttpOutcome.NOT_FOUND: "Chaster rejected the emergency unlock -- the target lock could not be found (it may have changed since discovery).",
            EmergencyUnlockHttpOutcome.UNEXPECTED: f"Chaster returned an unexpected response (HTTP {http_result.http_status}).",
        }
        return ProviderUnlockResult(outcome=ProviderUnlockOutcome.FAILED, detail=detail_by_outcome[http_result.outcome])
