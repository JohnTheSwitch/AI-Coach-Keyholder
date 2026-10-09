"""
chaster/provider_observation_service.py

docs/architecture/chaster_integration_technical_design.md, Section
25a Milestone D / Section 26 (CHASTER-01B Increment 3). On-demand
fetch only -- no scheduler, no background thread, no asyncio task,
no timer (Section 16, "Polling": this project has no such
infrastructure for anything, and none is introduced here). A single
synchronous call, triggered explicitly by a future caller (a Discord
command, not built in this increment).

Composes, in order: connection lookup -> token-expiry check ->
transparent refresh if needed -> `ChasterLockClient.list_active_locks()`
(already routed through the confirmed `send_with_ordered_headers()`
transport) -> parse -> record via `ChasterProviderObservationRecorder`.

DELIBERATE DEVIATION FROM THE LITERAL `ChasterProviderObservation |
None` SIGNATURE REQUESTED: returns `FetchObservationResult` instead
-- a small (outcome, observation) pair. A bare `None` cannot
distinguish "no connection exists" from "token refresh failed" from
"the live fetch failed but here's what we knew before" from
"multiple active locks, cannot safely record one" -- and Section 14
requires each of these be handled honestly and distinctly, not
collapsed into one undifferentiated failure. `observation` carries
the freshly recorded row on success, or the LAST KNOWN observation
(with its own real, un-touched `fetched_at`) on any failure where one
exists -- so a caller can always show "here is the last thing we
knew, and how old it is" per Section 14's own language, never a
default assumption in either direction.

NOT wired into `assemble_context()` or any prompt builder -- that
remains its own, separately-gated decision. `task_runtime/eligibility.py`
and `lock_state/` are not imported or touched anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.models import ChasterProviderObservation, ChasterProviderStatus
from chaster.oauth_client import ChasterOAuthClient
from chaster.provider_observation_repository import (
    ChasterProviderObservationRecorder,
    ChasterProviderObservations,
)
from chaster.repository import ChasterConnectionRepository
from chaster.token_manager import (
    ChasterNotConnectedError,
    ChasterReauthorizationRequiredError,
    ChasterRefreshUnavailableError,
    ChasterTokenManager,
)

__all__ = ["FetchObservationOutcome", "FetchObservationResult", "ChasterProviderObservationService"]

PROVIDER_NAME = "chaster"


class FetchObservationOutcome(StrEnum):
    """Every branch Section 14 requires be handled distinctly. None of
    these ever implies UNLOCKED except RECORDED with a genuinely
    fetched status of `unlocked` (or ZERO_ACTIVE_LOCKS, a real,
    successful, positive report from the provider itself -- not an
    assumption from missing data; see ChasterProviderObservationService.fetch_and_record()'s
    own docstring for why these two are treated differently)."""
    RECORDED = "recorded"
    ZERO_ACTIVE_LOCKS = "zero_active_locks"
    NO_CONNECTION = "no_connection"
    NEEDS_REAUTHORIZATION = "needs_reauthorization"
    API_UNAVAILABLE = "api_unavailable"
    AMBIGUOUS_MULTIPLE_LOCKS = "ambiguous_multiple_locks"
    UNPARSEABLE_STATUS = "unparseable_status"


@dataclass(frozen=True, kw_only=True)
class FetchObservationResult:
    """`observation` is the freshly recorded row when `outcome` is
    RECORDED or ZERO_ACTIVE_LOCKS; otherwise it is the LAST KNOWN
    observation for this connection if one exists (its own real
    `fetched_at`, never rewritten to look fresh), or `None` if none
    exists yet. `outcome` alone tells the caller whether what they're
    looking at is fresh or stale -- never infer freshness from
    `observation`'s mere presence."""
    outcome: FetchObservationOutcome
    observation: ChasterProviderObservation | None


class ChasterProviderObservationService:
    def __init__(
        self, *, connections: ChasterConnectionRepository, oauth_client: ChasterOAuthClient,
        lock_client: ChasterLockClient, observations: ChasterProviderObservations,
        recorder: ChasterProviderObservationRecorder,
        token_manager: ChasterTokenManager | None = None,
    ) -> None:
        self._connections = connections
        self._oauth_client = oauth_client
        self._lock_client = lock_client
        self._observations = observations
        self._recorder = recorder
        # Refresh / 401-retry logic lives in ChasterTokenManager (shared
        # with `chaster status`); this service no longer owns a copy.
        self._token_manager = token_manager or ChasterTokenManager(connections=connections, oauth_client=oauth_client)

    def fetch_and_record(self, *, connection_id: str, now: datetime) -> FetchObservationResult:
        """The on-demand fetch workflow. Never raises for an ordinary,
        expected failure (no connection, expired/revoked token,
        network error, ambiguous multiple locks, unparseable status)
        -- every one of these is a normal, honestly-reported outcome,
        not an exceptional program state. An unexpected internal bug
        would still propagate, deliberately -- this method does not
        swallow everything indiscriminately."""
        connection = self._connections.get(connection_id)
        if connection is None:
            return FetchObservationResult(outcome=FetchObservationOutcome.NO_CONNECTION, observation=None)

        try:
            raw_locks = self._token_manager.call_with_token(
                connection_id, now=now,
                operation=lambda token: self._lock_client.list_active_locks(access_token=token),
            )
        except ChasterNotConnectedError:
            return FetchObservationResult(outcome=FetchObservationOutcome.NO_CONNECTION, observation=None)
        except ChasterReauthorizationRequiredError:
            return FetchObservationResult(
                outcome=FetchObservationOutcome.NEEDS_REAUTHORIZATION,
                observation=self._observations.get_latest(connection_id),
            )
        except (ChasterRefreshUnavailableError, ChasterLockApiError):
            # Network error, a transient token-endpoint failure, or any
            # non-2xx HTTP response (an HTTP 401 has already been
            # refreshed-and-retried once by the token manager) -- the
            # live fetch failed, here is what we knew before
            # (Section 14, "API failure").
            return FetchObservationResult(
                outcome=FetchObservationOutcome.API_UNAVAILABLE,
                observation=self._observations.get_latest(connection_id),
            )

        if len(raw_locks) == 0:
            # A real, successful, POSITIVE report from the provider
            # itself ("you have zero active locks") -- structurally
            # different from a failure/absence of data, so this is
            # the one case this service records as UNLOCKED without
            # having parsed an actual lock object. Not previously
            # specified in the original CHASTER-01B design review --
            # flagged as this implementation's own reasonable
            # extension, for your review.
            return FetchObservationResult(
                outcome=FetchObservationOutcome.ZERO_ACTIVE_LOCKS,
                observation=self._recorder.record(
                    connection_id=connection_id, provider=PROVIDER_NAME, chaster_lock_id=None,
                    status=ChasterProviderStatus.UNLOCKED, fetched_at=now, now=now,
                ),
            )

        if len(raw_locks) > 1:
            # The model can represent exactly one lock's status per
            # observation row -- more than one active lock is
            # genuinely ambiguous, not a case to resolve by picking
            # the first one arbitrarily. Fails closed: no row is
            # written, the caller gets the last known observation
            # instead (mirrors chaster/emergency_unlock_provider.py's
            # own established "fail closed on 0 or multiple
            # candidates" discipline, applied here for a different,
            # structural reason).
            return FetchObservationResult(
                outcome=FetchObservationOutcome.AMBIGUOUS_MULTIPLE_LOCKS,
                observation=self._observations.get_latest(connection_id),
            )

        raw_lock = raw_locks[0]
        lock_id = raw_lock.get("_id")
        raw_status = raw_lock.get("status")
        try:
            status = ChasterProviderStatus(raw_status)
        except ValueError:
            # The real, confirmed `status` field's TYPE was verified
            # (a str) against a real response, but this project has
            # never had direct confirmation that every real VALUE
            # always matches one of the three documented
            # LockStatusEnum members -- so an unexpected value is
            # treated as an honest parse failure, never guessed at or
            # coerced to a default.
            return FetchObservationResult(
                outcome=FetchObservationOutcome.UNPARSEABLE_STATUS,
                observation=self._observations.get_latest(connection_id),
            )

        recorded = self._recorder.record(
            connection_id=connection_id, provider=PROVIDER_NAME,
            chaster_lock_id=lock_id if isinstance(lock_id, str) else None,
            status=status, fetched_at=now, now=now,
        )
        return FetchObservationResult(outcome=FetchObservationOutcome.RECORDED, observation=recorded)
