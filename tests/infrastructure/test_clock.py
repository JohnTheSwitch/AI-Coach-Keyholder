"""
tests/infrastructure/test_clock.py

Unit tests for infrastructure/clock.py, plus one repository-wide guard
test (`test_no_direct_datetime_now_calls_outside_clock_module`) that
enforces the "no production code calls datetime.now()/utcnow()
directly" convention mechanically, not just as a written rule —
consistent with this project's own established discipline of backing
every convention with a test wherever practical
(`implementation_conventions.md` Section 11).
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from infrastructure.clock import Clock, FrozenClock, SystemClock

PROJECT_ROOT = Path(__file__).parent.parent.parent


# =============================================================================
# SystemClock
# =============================================================================

class TestSystemClock:
    def test_now_returns_timezone_aware_datetime(self) -> None:
        result = SystemClock().now()
        assert result.tzinfo is not None

    def test_now_returns_utc(self) -> None:
        result = SystemClock().now()
        assert result.utcoffset() == timedelta(0)

    def test_now_is_close_to_real_time(self) -> None:
        before = datetime.now(timezone.utc)
        result = SystemClock().now()
        after = datetime.now(timezone.utc)
        assert before <= result <= after

    def test_satisfies_clock_protocol(self) -> None:
        assert isinstance(SystemClock(), Clock)

    # NOTE: a test asserting successive now() calls never go backward was
    # deliberately removed here (review feedback on Phase 1.1). SystemClock
    # does not, and cannot, guarantee monotonicity -- the operating system's
    # clock can move backward underneath it. A test that happens to pass
    # under normal conditions would misrepresent SystemClock's actual
    # contract. That guarantee belongs to a future MonotonicGuardedClock
    # (see the SystemClock docstring and infrastructure/README.md), which
    # will have its own test proving it holds even when the underlying
    # clock moves backward.


# =============================================================================
# FrozenClock
# =============================================================================

class TestFrozenClockConstruction:
    def test_now_returns_the_initial_value(self) -> None:
        initial = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        clock = FrozenClock(initial)
        assert clock.now() == initial

    def test_does_not_change_on_its_own(self) -> None:
        initial = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        clock = FrozenClock(initial)
        first_read = clock.now()
        second_read = clock.now()
        assert first_read == second_read == initial

    def test_rejects_naive_initial_datetime(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            FrozenClock(naive)

    def test_normalizes_non_utc_initial_datetime_to_utc(self) -> None:
        plus_two = timezone(timedelta(hours=2))
        aware_non_utc = datetime(2026, 1, 1, 14, 0, 0, tzinfo=plus_two)
        clock = FrozenClock(aware_non_utc)
        # 14:00+02:00 == 12:00 UTC
        assert clock.now() == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert clock.now().tzinfo == timezone.utc

    def test_satisfies_clock_protocol(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert isinstance(clock, Clock)


class TestFrozenClockAdvance:
    def test_advance_moves_forward(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        clock.advance(timedelta(minutes=30))
        assert clock.now() == datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)

    def test_advance_returns_the_new_value(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        returned = clock.advance(timedelta(hours=1))
        assert returned == clock.now() == datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)

    def test_advance_accepts_a_negative_delta(self) -> None:
        """FrozenClock is deliberately unopinionated about direction --
        see the class docstring for why (it must be usable to construct
        RT14-style backward-clock-jump scenarios)."""
        clock = FrozenClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        clock.advance(timedelta(minutes=-15))
        assert clock.now() == datetime(2026, 1, 1, 11, 45, 0, tzinfo=timezone.utc)

    def test_repeated_advances_accumulate(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        clock.advance(timedelta(days=1))
        clock.advance(timedelta(hours=6))
        clock.advance(timedelta(minutes=30))
        assert clock.now() == datetime(2026, 1, 2, 6, 30, 0, tzinfo=timezone.utc)


class TestFrozenClockSet:
    def test_set_jumps_to_an_absolute_time(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        target = datetime(2027, 6, 15, 9, 30, 0, tzinfo=timezone.utc)
        clock.set(target)
        assert clock.now() == target

    def test_set_allows_jumping_backward(self) -> None:
        """Deliberate: simulating a system clock moving backward after a
        restart (activity_authorization_technical_design.md RT14) requires
        the test double to allow exactly this."""
        clock = FrozenClock(datetime(2026, 6, 1, tzinfo=timezone.utc))
        earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock.set(earlier)
        assert clock.now() == earlier

    def test_set_rejects_naive_datetime(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="timezone-aware"):
            clock.set(datetime(2026, 6, 1))  # no tzinfo

    def test_set_normalizes_non_utc_timezone(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        minus_five = timezone(timedelta(hours=-5))
        clock.set(datetime(2026, 3, 1, 7, 0, 0, tzinfo=minus_five))
        # 07:00-05:00 == 12:00 UTC
        assert clock.now() == datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# =============================================================================
# Repository-wide convention guard
# =============================================================================

# Phase 1.2: database/models.py and database/backup.py were migrated
# to an injected Clock (created_at is now a required constructor
# parameter; backup.py takes `now: datetime` as an explicit parameter).
# This set is therefore empty -- kept as a named place for any future,
# deliberately documented exception, rather than deleted outright, so
# the pattern stays visible if (and when) it's needed again.
KNOWN_PRE_CLOCK_VIOLATIONS: set[Path] = set()

# Deliberate, documented exception (Phase G5): hive_mind_receiver/ is a STANDALONE service that shares no code with the bot (it cannot use
# infrastructure/clock.py). Its single wall-clock reading lives in its own clock module; everything else in it takes an injected `now`.
STANDALONE_SERVICE_CLOCKS: set[Path] = {PROJECT_ROOT / "hive_mind_receiver" / "clock.py"}

# Directories that are never expected to contain application code.
EXCLUDED_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", "node_modules"}


def _iter_project_python_files() -> list[Path]:
    return [
        path
        for path in PROJECT_ROOT.rglob("*.py")
        if not any(part in EXCLUDED_DIR_NAMES for part in path.parts)
    ]


def _calls_forbidden_datetime_function(source_path: Path) -> list[int]:
    """
    Parses `source_path` and returns the line numbers of any
    `datetime.now(...)` or `datetime.utcnow(...)` call — an AST check
    rather than a text search, so this does not false-positive on the
    string appearing inside a comment or docstring (as it legitimately
    does in this module's own docstrings and in
    database/models.py's docstring).
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offending_lines: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("now", "utcnow"):
            continue
        if isinstance(func.value, ast.Name) and func.value.id == "datetime":
            offending_lines.append(node.lineno)

    return offending_lines


def test_no_direct_datetime_now_calls_outside_clock_module() -> None:
    clock_module = PROJECT_ROOT / "infrastructure" / "clock.py"
    this_test_file = Path(__file__)

    violations: dict[Path, list[int]] = {}
    for path in _iter_project_python_files():
        if path in (clock_module, this_test_file):
            continue
        if path in KNOWN_PRE_CLOCK_VIOLATIONS or path in STANDALONE_SERVICE_CLOCKS:
            continue
        offending_lines = _calls_forbidden_datetime_function(path)
        if offending_lines:
            violations[path] = offending_lines

    assert not violations, (
        "Found direct datetime.now()/datetime.utcnow() calls outside "
        "infrastructure/clock.py. Use an injected Clock instead "
        f"(see infrastructure/README.md). Violations: {violations}"
    )


def test_known_pre_clock_violations_list_is_still_accurate() -> None:
    """
    Guards the guard: if KNOWN_PRE_CLOCK_VIOLATIONS ever drifts from
    reality (a listed file was migrated and no longer calls
    datetime.now() directly, or a new violation appears that was never
    added to the list), this test fails loudly instead of the
    allowlist silently going stale.
    """
    for path in KNOWN_PRE_CLOCK_VIOLATIONS:
        assert path.exists(), f"{path} no longer exists -- remove it from KNOWN_PRE_CLOCK_VIOLATIONS"
        assert _calls_forbidden_datetime_function(path), (
            f"{path} no longer calls datetime.now()/utcnow() directly -- "
            "remove it from KNOWN_PRE_CLOCK_VIOLATIONS so the guard test "
            "protects it too."
        )
