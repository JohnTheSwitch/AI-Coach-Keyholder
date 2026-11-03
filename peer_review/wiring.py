"""
peer_review/wiring.py

Builds the Phase I4 community review watch from the configuration and runs it as its own background loop in the bot (it does not depend on
the Rules Engine's periodic cycle, which may be off). Off unless ENABLE_PEER_REVIEW_WATCH; it then needs Chaster fully configured,
CHASTER_EXECUTION_KEYHOLDER_USER_ID (the connected account) and an existing PEER_REVIEW_TRUST_DOMAIN.

The Incidents use one fixed, low evidence profile: a community vote is a single external signal (philosophy 2.12), so confidence is LOW
and the impact LOW; severity is only assessed if the keyholder confirms the Incident.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from chaster.lock_directory import LockDirectory
from chaster.peer_reviews import PeerReviewReader
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from peer_review.watch import RULE_GROUP, PeerReviewDecisions, PeerReviewWatch
from trust_manager.models import (
    BreachDirectness,
    ConfirmationSource,
    EvidenceConfidenceLevel,
    ImpactLevel,
    IncidentConfirmation,
    IncidentEvidence,
    IntentAssessment,
    RepetitionEvidence,
)
from trust_manager.repository import TrustManager

logger = logging.getLogger("ai_coach_keyholder.peer_review.wiring")

__all__ = ["PEER_REVIEW_EVIDENCE", "PeerReviewWiring", "build_peer_review_watch", "build_peer_review_decisions", "owner_discord_id",
           "peer_review_loop"]

PEER_REVIEW_EVIDENCE = IncidentEvidence(
    actual_or_potential_impact=ImpactLevel.LOW, rule_breach_directness=BreachDirectness.PARTIAL,
    repetition=RepetitionEvidence(same_rule_confirmed_count=0, evaluation_window_days=7),
    evidence_confidence=EvidenceConfidenceLevel.LOW, intentionality=IntentAssessment.UNCLEAR,
)


@dataclass
class PeerReviewWiring:
    watch: PeerReviewWatch | None = None
    interval_seconds: int = 600
    owner_target: str | None = None                 # the owner's Discord id for the notice, when known
    notes: list[str] = field(default_factory=list)


def owner_discord_id(core: CoreDatabase, owner_user_id: str | None) -> str | None:
    if not owner_user_id:
        return None
    try:
        with core.transaction() as tx:
            row = tx.fetch_one("SELECT external_id FROM user_channel_identities WHERE user_account_id = ? AND channel = 'discord'",
                               (owner_user_id,))
        return None if row is None else row["external_id"]
    except Exception:
        logger.exception("Could not look up the owner's Discord id.")
        return None


def build_peer_review_watch(config, core: CoreDatabase, *, token_manager, lock_client, connections) -> PeerReviewWiring:
    out = PeerReviewWiring(interval_seconds=max(120, int(getattr(config, "peer_review_poll_seconds", 600) or 600)))
    if not getattr(config, "enable_peer_review_watch", False):
        return out
    account = getattr(config, "chaster_execution_keyholder_user_id", None)
    if token_manager is None or lock_client is None or connections is None or not account:
        out.notes.append("ENABLE_PEER_REVIEW_WATCH is true but the community review watch is NOT active: it needs Chaster fully configured "
                         "and CHASTER_EXECUTION_KEYHOLDER_USER_ID (the connected account).")
        return out
    trust = TrustManager(config.db_path, core=core)
    domain = getattr(config, "peer_review_trust_domain", None) or "routine"
    if trust.get_domain_state(domain) is None:
        out.notes.append(f"ENABLE_PEER_REVIEW_WATCH is true but the community review watch is NOT active: the Trust domain "
                         f"'{domain}' (PEER_REVIEW_TRUST_DOMAIN) does not exist.")
        return out
    directory = LockDirectory(token_manager=token_manager, lock_client=lock_client, connections=connections)

    def list_lock_ids(now: datetime) -> tuple[list[str], list[str]]:
        listing = directory.list_account_locks(user_id=account, now=now)
        ids = [l.lock_id for l in listing.wearer_locks + listing.keyholder_locks if l.status == "locked"]
        return list(dict.fromkeys(ids)), list(listing.problems)

    out.watch = PeerReviewWatch(
        config.db_path, core=core, reader=PeerReviewReader(token_manager=token_manager, lock_client=lock_client), account_user_id=account,
        list_lock_ids=list_lock_ids,
        register_incident=lambda incident_id, description, now: trust.register_incident_report(
            rule_group_id=RULE_GROUP, trust_domain=domain, description=description, evidence=PEER_REVIEW_EVIDENCE, now=now,
            incident_id=incident_id))
    out.owner_target = owner_discord_id(core, getattr(config, "owner_user_id", None))
    out.notes.append(f"Community review watch enabled: every {out.interval_seconds} s; a rejection becomes one UNCONFIRMED incident in "
                     f"Trust domain '{domain}'" + ("" if out.owner_target else " (no owner Discord id: no DM notice)") + ".")
    return out


def build_peer_review_decisions(config, core: CoreDatabase) -> PeerReviewDecisions:
    trust = TrustManager(config.db_path, core=core)
    return PeerReviewDecisions(
        config.db_path, core=core, owner_user_id=getattr(config, "owner_user_id", None),
        confirm_incident=lambda incident_id, evidence, now: trust.confirm_incident(
            incident_id, new_confirmation=IncidentConfirmation.CONFIRMED, source=ConfirmationSource.KEYHOLDER_REVIEW,
            evidence_description=evidence, now=now))


async def peer_review_loop(watch: PeerReviewWatch, clock: Clock, *, interval_seconds: float, send=None, owner_target: str | None = None,
                           initial_delay_seconds: float = 30.0) -> None:
    """Poll, then deliver the pending notices to the owner (`send(target, text) -> bool`, e.g. the Discord adapter); a notice is marked
    delivered only when the channel accepted it. Never raises out of an iteration."""
    await asyncio.sleep(initial_delay_seconds)          # let the Discord client become ready first
    while True:
        try:
            await asyncio.to_thread(watch.poll, clock.now())
            if send is not None and owner_target:
                for notice in await asyncio.to_thread(watch.pending_notifications):
                    if await send(owner_target, notice.text):
                        await asyncio.to_thread(watch.mark_notified, notice.incident_id, clock.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Community review poll failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)
