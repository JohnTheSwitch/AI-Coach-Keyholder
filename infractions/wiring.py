"""
infractions/wiring.py

Builds the two detectors from the configuration and runs them as their own loop in the bot. Each is off unless switched on (`ENABLE_HYGIENE_WATCH`,
`ENABLE_LOCK_CLAIM_WATCH`), and each needs its Trust domain to exist: a domain is never created here (philosophy 3.7), and a hygiene finding is never filed under
another domain just because `hygiene` is missing. Both only read Chaster, write local records and queue notices, so like the community review watch they keep running during an execution
halt. Every finding is asked about and judged (infractions/signals.py), and the wiring passes the Trust Manager's register and confirm in.

The worn locks come from the connected account's own listing. The wearer is that same account; a tenant has no detectors, because its invitation never disclosed them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chaster.lock_client import ChasterLockApiError
from chaster.lock_directory import LockDirectory
from chaster.managed_locks import ManagedLock
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from infrastructure.time_format import iso as _iso
from infrastructure.time_format import parse_iso
from infractions.excuses import Excuses
from infractions.judge import ExplanationJudge
from infractions.hygiene import HygieneWatch, OpeningNotAvailable
from infractions.lock_claims import LockClaimWatch
from infractions.signals import EVIDENCE, RULE_GROUPS, InfractionSignals, Signal, SignalKind
from trust_manager.models import ConfirmationSource, CooperationAssessment, IncidentConfirmation
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.infractions.wiring")
SLEEP_GAP = timedelta(minutes=10)          # a pass this much later than planned means the PC slept

__all__ = ["DetectorWiring", "build_detectors", "detector_loop", "overrun_signal"]


@dataclass
class DetectorWiring:
    signals: InfractionSignals | None = None
    hygiene: HygieneWatch | None = None
    lock_claims: LockClaimWatch | None = None
    interval_seconds: int = 120
    notes: list[str] = field(default_factory=list)
    core: CoreDatabase | None = None

    @property
    def active(self) -> bool:
        return self.signals is not None

    def _exemption_window(self, signal: Signal) -> tuple[datetime, datetime]:
        with self.core.transaction() as tx:
            row = tx.fetch_one("SELECT expires_at, relocked_at FROM wear_exemptions WHERE id = ?", (signal.subject_id,))
        return parse_iso(row["expires_at"]), parse_iso(row["relocked_at"])

    def run(self, now: datetime) -> list[str]:
        """One pass. Each step on its own, so one failure never silences the other, and a read problem is only reported."""
        problems: list[str] = []
        for watch in (self.hygiene, self.lock_claims):
            if watch is None:
                continue
            try:
                problems += watch.poll(now)
            except Exception:
                logger.exception("A detector pass failed; will retry next interval.")
                problems.append("detector pass failed")
        if self.signals is not None:
            self.signals.advance(now, window_of=self._window_of)
        return problems

    def _window_of(self, signal: Signal) -> tuple[datetime, datetime]:
        if signal.kind is SignalKind.EXEMPTION_OVERRUN:
            return self._exemption_window(signal)
        watch = self.lock_claims if signal.kind is SignalKind.LOCK_CLAIM else self.hygiene
        if watch is None:
            return signal.review_after, signal.review_after
        return watch.signal_window(signal.subject_id)


def overrun_signal(signals: InfractionSignals, *, trust_domain: str):
    """`raise_overrun` for ExemptionService: a late relock takes the same path as every detection, health answer first."""

    def raise_overrun(tx, row: dict, relocked_at: datetime, now: datetime) -> bool:
        expires = parse_iso(row["expires_at"]) if row.get("expires_at") else relocked_at
        facts = (f"Approved {row.get('kind') or 'exemption'} until {_iso(expires)}; relocked at {_iso(relocked_at)}, "
                 f"{int((relocked_at - expires).total_seconds() // 60)} minutes late.")
        return signals.raise_in_transaction(tx, user_id=row["user_id"], kind=SignalKind.EXEMPTION_OVERRUN, subject_id=row["id"], trust_domain=trust_domain, now=now,
                                            detail="An exemption's lock was relocked after the approved time.",
                                            wearer_text="You relocked after the approved time. What happened?", facts=facts, window=(expires, relocked_at))

    return raise_overrun


def build_detectors(config, core: CoreDatabase, *, token_manager, lock_client, connections, vacation=None, complete=None) -> DetectorWiring:
    """`complete(system_prompt, user_message) -> raw model text` judges the wearer's explanations; None: only health and safety answers are judged (by code)."""
    out = DetectorWiring(interval_seconds=max(60, int(getattr(config, "detector_poll_seconds", 120) or 120)), core=core)
    want_hygiene = bool(getattr(config, "enable_hygiene_watch", False))
    want_claims = bool(getattr(config, "enable_lock_claim_watch", False))
    # The question-and-judge path also serves a late exemption relock, a rejected community review and the check-in escalation.
    want_signals = any(bool(getattr(config, name, False)) for name in ("enable_exemptions", "enable_peer_review_watch", "enable_check_ins"))
    if not want_hygiene and not want_claims and not want_signals:
        return out
    account = getattr(config, "chaster_execution_keyholder_user_id", None)
    owner = getattr(config, "owner_user_id", None)
    if token_manager is None or lock_client is None or connections is None or not account or not owner:
        out.notes.append("The hygiene and lock-claim watches are NOT active: they need Chaster fully configured, CHASTER_EXECUTION_KEYHOLDER_USER_ID and OWNER_USER_ID.")
        return out
    trust = TrustManager(config.db_path, core=core)
    directory = LockDirectory(token_manager=token_manager, lock_client=lock_client, connections=connections)
    on_vacation = (lambda u, start, end: vacation.is_paused(u, start) or vacation.is_paused(u, end)) if vacation is not None else None
    excuses = Excuses(core, on_vacation=on_vacation)

    def register(signal: Signal, now: datetime) -> None:
        trust.register_incident_report(rule_group_id=signal.rule_group or RULE_GROUPS[signal.kind], trust_domain=signal.trust_domain, description=signal.detail,
                                       evidence=EVIDENCE[signal.kind], now=now, incident_id=signal.incident_id)

    def confirm(incident_id: str, evidence: str, now: datetime, admitted: bool = False) -> None:
        # The system is the keyholder (owner, 2026-11-10): it confirms what the wearer could not explain. Owning up softens it (philosophy 5.3).
        trust.confirm_incident(incident_id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.SYSTEM_VERIFIED,
                               evidence_description=evidence[:1000], now=now,
                               cooperation=CooperationAssessment(self_disclosed=True, notes="Owned up when asked.") if admitted else None)

    out.signals = InfractionSignals(config.db_path, core=core, excuses=excuses, judge=ExplanationJudge(complete), register_incident=register, confirm_incident=confirm)
    out.signals.set_wearer(getattr(config, "check_in_user_id", None) or getattr(config, "chaster_execution_wearer_user_id", None) or owner)
    if complete is None:
        out.notes.append("Detector questions: no language model, so only health and safety answers can be judged; other answers wait and are then dropped.")

    def worn(now: datetime) -> tuple[tuple[ManagedLock, ...], list[str]]:
        listing = directory.list_locks(wearer_user_id=account, keyholder_user_id=None, now=now)
        return listing.wearer_locks, [p for p in listing.problems if not p.startswith("keyholder:")]

    def read_opening(lock_id: str, now: datetime) -> dict:
        try:
            return token_manager.call_with_token(account, now=now, operation=lambda token: lock_client.get_temporary_opening(access_token=token, lock_id=lock_id))
        except ChasterLockApiError as exc:
            if exc.status_code == 404:
                raise OpeningNotAvailable() from exc
            raise

    if want_hygiene:
        domain = getattr(config, "hygiene_trust_domain", None) or "hygiene"
        if trust.get_domain_state(domain) is None:
            out.notes.append(f"ENABLE_HYGIENE_WATCH is true but the hygiene watch is NOT active: the Trust domain '{domain}' (HYGIENE_TRUST_DOMAIN) does not exist.")
        else:
            def score(d=domain):
                state = trust.get_domain_state(d)
                return None if state is None else float(state.score)

            out.hygiene = HygieneWatch(config.db_path, core=core, user_id=account, trust_domain=domain, signals=out.signals, list_worn_locks=worn,
                                       read_opening=read_opening, get_trust_score=score, interval_seconds=out.interval_seconds)
            out.notes.append(f"Hygiene watch enabled: every {out.interval_seconds} s; a planned break past the limits is asked about, then judged.")
    if want_claims:
        domain = getattr(config, "lock_claim_trust_domain", None) or "routine"
        if trust.get_domain_state(domain) is None:
            out.notes.append(f"ENABLE_LOCK_CLAIM_WATCH is true but the lock-claim watch is NOT active: the Trust domain '{domain}' (LOCK_CLAIM_TRUST_DOMAIN) does not exist.")
        else:
            out.lock_claims = LockClaimWatch(config.db_path, core=core, user_id=account, owner_user_id=owner, trust_domain=domain, signals=out.signals,
                                             excuses=excuses, list_worn_locks=worn)
            out.notes.append(f"Lock-claim watch enabled: every {out.interval_seconds} s; a report that was false when made is asked about, then judged.")
    return out


async def detector_loop(wiring: DetectorWiring, clock: Clock, *, initial_delay_seconds: float = 30.0) -> None:
    await asyncio.sleep(initial_delay_seconds)
    previous = None
    while True:
        try:
            now = clock.now()
            # Owner, 2026-11-10: the PC slept (the process lived on, but nothing ran). Nobody could answer meanwhile, so open deadlines move by that time.
            if previous is not None and wiring.signals is not None and now - previous > timedelta(seconds=wiring.interval_seconds) + SLEEP_GAP:
                moved = await asyncio.to_thread(wiring.signals.extend_for_downtime, down_since=previous, back_at=now)
                if moved:
                    logger.info("The PC was asleep for a while: %d open question deadline(s) moved by that time.", moved)
            previous = now
            problems = await asyncio.to_thread(wiring.run, now)
            if problems:
                logger.warning("Detector pass: %s", "; ".join(problems)[:300])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The detector loop failed; will retry next interval.")
        await asyncio.sleep(wiring.interval_seconds)
