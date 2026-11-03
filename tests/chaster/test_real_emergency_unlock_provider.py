"""
tests/chaster/test_real_emergency_unlock_provider.py

All Chaster HTTP traffic mocked via a fake/mocked ChasterLockClient --
no real network calls, no real Chaster credentials required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from chaster.emergency_unlock_provider import (
    LockEligibility,
    ProviderUnlockOutcome,
    RealChasterEmergencyUnlockProvider,
    lock_for_wearer_eligibility,
)
from chaster.lock_client import (
    ChasterLockApiError,
    ChasterLockClient,
    EmergencyUnlockHttpOutcome,
    EmergencyUnlockHttpResult,
)


def _mock_client() -> MagicMock:
    return MagicMock(spec=ChasterLockClient)


def _eligible_extractor(lock_id: str = "the-lock-id"):
    return lambda raw: LockEligibility(lock_id=lock_id, eligible=True, reason="bondage lock with emergency release enabled")


def _ineligible_extractor(reason: str = "not a bondage lock"):
    return lambda raw: LockEligibility(lock_id="the-lock-id", eligible=False, reason=reason)


class TestDiscoveryZeroActiveLocks:
    def test_zero_active_locks_fails_safely_without_calling_unlock(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = []
        provider = RealChasterEmergencyUnlockProvider(lock_client=client)
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        assert "No active Chaster lock" in result.detail
        client.emergency_unlock.assert_not_called()


class TestDiscoveryExactlyOneActiveLock:
    def test_exactly_one_lock_proceeds_to_eligibility_check(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "only-lock"}]
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor("only-lock"))
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.SUCCEEDED, http_status=204)
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.SUCCEEDED
        client.emergency_unlock.assert_called_once_with(access_token="at", lock_id="only-lock")


class TestDiscoveryMultipleActiveLocks:
    def test_two_active_locks_fails_safely_and_does_not_choose_arbitrarily(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}, {"_id": "lock2"}]
        provider = RealChasterEmergencyUnlockProvider(lock_client=client)
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        assert "2 active Chaster locks" in result.detail
        client.emergency_unlock.assert_not_called()

    def test_many_active_locks_still_fails_safely_with_correct_count(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": f"lock{i}"} for i in range(5)]
        provider = RealChasterEmergencyUnlockProvider(lock_client=client)
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        assert "5 active Chaster locks" in result.detail

    def test_no_deterministic_bias_toward_first_or_last_result(self) -> None:
        """Confirms the provider never even inspects individual
        elements of a multi-lock result -- the eligibility extractor
        must never be called when the candidate count is not exactly
        one."""
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}, {"_id": "lock2"}]
        extractor_calls = []
        provider = RealChasterEmergencyUnlockProvider(
            lock_client=client, lock_field_extractor=lambda raw: extractor_calls.append(raw) or LockEligibility(lock_id="x", eligible=True, reason="ok"),
        )
        provider.attempt_unlock(access_token="at")
        assert extractor_calls == []


class TestEligibility:
    def test_bondage_with_emergency_release_enabled_is_eligible_and_proceeds(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.SUCCEEDED, http_status=204)
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor("lock1"))
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.SUCCEEDED

    def test_non_bondage_lock_is_rejected_without_calling_unlock(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_ineligible_extractor("not a bondage lock"))
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        assert "not a bondage lock" in result.detail
        client.emergency_unlock.assert_not_called()

    def test_bondage_without_emergency_release_enabled_is_rejected(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_ineligible_extractor("bondage lock but emergency release is not enabled"))
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        client.emergency_unlock.assert_not_called()

    def test_the_default_extractor_reads_the_confirmed_fields_and_unlocks_an_eligible_lock(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [_wearer_lock()]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.SUCCEEDED, http_status=204)
        result = RealChasterEmergencyUnlockProvider(lock_client=client).attempt_unlock(access_token="at")   # default extractor
        assert result.outcome is ProviderUnlockOutcome.SUCCEEDED
        client.emergency_unlock.assert_called_once_with(access_token="at", lock_id="6ae98ca410556a82f4b0d1aa")

    def test_the_default_extractor_refuses_a_lock_without_the_safety_feature(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [_wearer_lock(emergencyUnlock={"enabled": False, "phrase": "x"})]
        result = RealChasterEmergencyUnlockProvider(lock_client=client).attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED and "no emergency-unlock safety feature" in result.detail
        client.emergency_unlock.assert_not_called()

    def test_an_extractor_that_raises_fails_closed(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        def boom(raw):
            raise KeyError("x")
        result = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=boom).attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        client.emergency_unlock.assert_not_called()


def _wearer_lock(**overrides) -> dict:
    lock = {"_id": "6ae98ca410556a82f4b0d1aa", "status": "locked", "role": "wearer", "lockType": "chastity", "emergencyUnlocked": False,
            "emergencyUnlock": {"enabled": True, "phrase": "SECRET PHRASE"}, "combination": "SECRET"}
    lock.update(overrides)
    return lock


class TestLockForWearerEligibility:
    def test_eligible(self) -> None:
        result = lock_for_wearer_eligibility(_wearer_lock(lockType="bondage"))
        assert result == LockEligibility(lock_id="6ae98ca410556a82f4b0d1aa", eligible=True,
                                         reason="locked bondage lock with the emergency-unlock safety feature enabled")
        assert "SECRET" not in repr(result)

    @pytest.mark.parametrize("overrides, reason", [
        ({"_id": None}, "no valid id"), ({"_id": "../x"}, "no valid id"),
        ({"status": "unlocked"}, "not locked"), ({"status": None}, "not locked"),
        ({"role": "keyholder"}, "only the wearer"), ({"emergencyUnlocked": True}, "already emergency-unlocked"),
        ({"emergencyUnlock": None}, "no emergency-unlock safety feature"), ({"emergencyUnlock": {"enabled": "yes"}}, "no emergency-unlock safety feature"),
    ])
    def test_every_missing_or_wrong_field_is_ineligible_never_an_error(self, overrides, reason) -> None:
        result = lock_for_wearer_eligibility(_wearer_lock(**overrides))
        assert result.eligible is False and reason in result.reason

    def test_a_non_dict_is_ineligible(self) -> None:
        assert lock_for_wearer_eligibility(None).eligible is False


class TestCandidateFiltering:
    def test_unlocked_locks_in_the_active_list_are_never_candidates(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [_wearer_lock(_id="oldlock", status="unlocked"), _wearer_lock()]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.SUCCEEDED, http_status=204)
        result = RealChasterEmergencyUnlockProvider(lock_client=client).attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.SUCCEEDED
        client.emergency_unlock.assert_called_once_with(access_token="at", lock_id="6ae98ca410556a82f4b0d1aa")

    def test_two_locked_locks_still_fail_closed(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [_wearer_lock(), _wearer_lock(_id="second")]
        result = RealChasterEmergencyUnlockProvider(lock_client=client).attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED and "2 active Chaster locks" in result.detail
        client.emergency_unlock.assert_not_called()

    def test_only_unlocked_locks_means_no_target(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [_wearer_lock(status="unlocked"), _wearer_lock(_id="b", status="deserted")]
        result = RealChasterEmergencyUnlockProvider(lock_client=client).attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED and "No active Chaster lock" in result.detail


class TestUnlockCallCorrectness:
    def test_lock_id_comes_only_from_the_extractor_never_from_the_raw_dict_directly(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "raw-dict-id-should-not-be-used-directly"}]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.SUCCEEDED, http_status=204)
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor("extractor-chosen-id"))
        provider.attempt_unlock(access_token="at")
        client.emergency_unlock.assert_called_once_with(access_token="at", lock_id="extractor-chosen-id")

    def test_attempt_unlock_accepts_no_lock_id_parameter_at_all(self) -> None:
        """Confirms the Protocol boundary itself -- a caller (the
        local HTTP listener) structurally cannot inject a lock ID."""
        import inspect
        sig = inspect.signature(RealChasterEmergencyUnlockProvider.attempt_unlock)
        assert list(sig.parameters) == ["self", "access_token"]

    def test_204_is_the_only_success_status(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.SUCCEEDED, http_status=204)
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor())
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.SUCCEEDED

    @pytest.mark.parametrize("http_outcome", [
        EmergencyUnlockHttpOutcome.NOT_ELIGIBLE, EmergencyUnlockHttpOutcome.UNAUTHORIZED,
        EmergencyUnlockHttpOutcome.FORBIDDEN, EmergencyUnlockHttpOutcome.NOT_FOUND,
        EmergencyUnlockHttpOutcome.UNEXPECTED,
    ])
    def test_every_non_204_outcome_is_a_failure_never_success(self, http_outcome) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=http_outcome, http_status=400)
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor())
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED


class TestRaceAndStaleTargetHandling:
    def test_lock_disappearing_between_discovery_and_unlock_is_an_honest_failure(self) -> None:
        """Simulates the lock returning 404 on the emergency-unlock
        call despite being present during discovery -- the provider
        must report this honestly, never silently select another
        lock or retry."""
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        client.emergency_unlock.return_value = EmergencyUnlockHttpResult(outcome=EmergencyUnlockHttpOutcome.NOT_FOUND, http_status=404)
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor("lock1"))
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        assert "changed since discovery" in result.detail
        client.list_active_locks.assert_called_once()  # never re-discovers or retries

    def test_discovery_api_failure_is_an_honest_failure_never_success(self) -> None:
        client = _mock_client()
        client.list_active_locks.side_effect = ChasterLockApiError("network error")
        provider = RealChasterEmergencyUnlockProvider(lock_client=client)
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED
        client.emergency_unlock.assert_not_called()

    def test_unlock_call_network_failure_is_an_honest_failure_never_success(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = [{"_id": "lock1"}]
        client.emergency_unlock.side_effect = ChasterLockApiError("timed out")
        provider = RealChasterEmergencyUnlockProvider(lock_client=client, lock_field_extractor=_eligible_extractor("lock1"))
        result = provider.attempt_unlock(access_token="at")
        assert result.outcome is ProviderUnlockOutcome.FAILED


class TestNoLeakage:
    def test_detail_never_contains_the_access_token(self) -> None:
        client = _mock_client()
        client.list_active_locks.return_value = []
        provider = RealChasterEmergencyUnlockProvider(lock_client=client)
        result = provider.attempt_unlock(access_token="UNIQUE-SECRET-ACCESS-TOKEN-VALUE")
        assert "UNIQUE-SECRET-ACCESS-TOKEN-VALUE" not in result.detail
