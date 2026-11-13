"""
system/proof_wiring.py

Builds the proof spot checks (owner, 2026-11-13; honesty and proof, slice 3) for the owner's own database: proof_checks.ProofChecks with its
Chaster tasks port, connected to the coach desk (selection and results), the infractions path (failures) and the community review watch
(a spot check's review is never a second Incident). None, with a note saying why, when anything it needs is missing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from chaster_execution.control import ExecutionControl
from infractions.signals import SignalKind
from proof_checks.chaster_tasks import ChasterTasks
from proof_checks.service import Ports, ProofChecks
from schedule_windows.windows import WindowKind, classify
from system.owner_lock import is_auto
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.system.proof_wiring")

__all__ = ["build_proof_checks", "proof_loop"]

_REASONS = {"missed": "no photo arrived by the deadline", "late": "the photo came after the deadline",
            "rejected": "the community rejected the photo"}


def build_proof_checks(config, core, *, coach_dm, signals, keyholder_tokens, lock_client, owner_lock, vacation, window_cfg, clock, notes: list):
    if not getattr(config, "enable_proof_checks", True):
        return None
    domain = getattr(config, "honesty_trust_domain", None) or "honesty"
    account = getattr(config, "chaster_execution_keyholder_user_id", None)
    lock_setting = getattr(config, "release_lock_id", None)
    missing = [name for name, value in (("the coach", coach_dm), ("the detector questions", signals), ("the keyholder connection", keyholder_tokens),
                                        ("CHASTER_EXECUTION_KEYHOLDER_USER_ID", account), ("a lock (RELEASE_LOCK_ID)", lock_setting)) if not value]
    if is_auto(lock_setting) and owner_lock is None:
        missing.append("the owner's recognised lock")
    if missing:
        notes.append(f"Proof spot checks are NOT active: they need {', '.join(missing)}.")
        return None
    trust = TrustManager(core.db_path, core=core)
    control = ExecutionControl(core.db_path, core=core)
    desk = coach_dm.desk
    user_id = desk.user_id

    def honesty_score() -> float | None:
        state = trust.get_domain_state(domain)
        return None if state is None else float(state.score)

    def lock_id(now: datetime) -> str | None:
        return owner_lock.current(now) if is_auto(lock_setting) else lock_setting

    def quiet_between(start: datetime, end: datetime) -> bool:
        step, at = (end - start) / 12, start
        for _ in range(13):
            if classify(window_cfg, at) is WindowKind.QUIET:
                return True
            at += step
        return False

    def raise_failure(row: dict, reason: str, now: datetime) -> None:
        text = row["commitment_text"]
        signals.raise_signal(user_id=user_id, kind=SignalKind.PROOF_FAILED, subject_id=row["id"], trust_domain=domain, now=now,
                             detail=f"A spot check of a Done report failed: {_REASONS.get(reason, reason)}.",
                             wearer_text=f"Your spot check for \"{text}\" failed: {_REASONS.get(reason, reason)}. What happened?",
                             facts=f"Commitment: {text}. Reported done; spot check {reason}.", window=(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")), now))

    checks = ProofChecks(core, user_id=user_id, honesty_domain=domain,
                         chaster=ChasterTasks(token_manager=keyholder_tokens, account_user_id=account, lock_client=lock_client, clock_now=clock.now),
                         ports=Ports(lock_id=lock_id, halted=control.is_halted, honesty_score=honesty_score,
                                     paused=lambda now: vacation.is_paused(user_id, now), today=lambda now: window_cfg.local(now).date(),
                                     local_clock=lambda instant: window_cfg.local(instant).strftime("%H:%M"),
                                     settle=desk.settle_check, raise_failure=raise_failure, quiet_between=quiet_between))
    desk.proof_selector = checks.select
    coach_dm.after_kept = checks.start_pending
    coach_dm.forget_also = checks.forget_all
    if trust.get_domain_state(domain) is None:
        notes.append(f"Proof spot checks are wired but NOT active yet: the '{domain}' Trust domain does not exist "
                     f"(python -m cli trust-domain create {domain} ..., with consent).")
    else:
        notes.append("Proof spot checks are active.")
    return checks


async def proof_loop(checks: ProofChecks, clock, *, interval_seconds: float = 60.0, poll_every: int = 2) -> None:
    """Starts picked checks every minute and reads the open ones' Chaster tasks every `poll_every` minutes."""
    tick = 0
    while True:
        try:
            await asyncio.to_thread(checks.start_pending, clock.now())
            if tick % poll_every == 0 and checks.get_open_check() is not None:
                await asyncio.to_thread(checks.poll, clock.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The spot-check loop failed a pass; it continues.")
        tick += 1
        await asyncio.sleep(interval_seconds)
