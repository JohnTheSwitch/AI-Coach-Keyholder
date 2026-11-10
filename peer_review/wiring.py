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
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from chaster.lock_directory import LockDirectory
from chaster.peer_reviews import PeerReviewReader
from infrastructure.clock import Clock
from infrastructure.database import Database as CoreDatabase
from peer_review.notices import health_embed, incident_embed
from peer_review.watch import RULE_GROUP, PeerReviewDecisions, PeerReviewWatch, PollHealth
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
           "peer_review_loop", "queued_send"]

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
    incident_notices: bool = True                   # False: rejections go to the wearer as questions, not to the owner as cards
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


def build_peer_review_watch(config, core: CoreDatabase, *, token_manager, lock_client, connections, ask_wearer=None) -> PeerReviewWiring:
    """`ask_wearer(incident_id, description, now)` (owner, 2026-11-10): a rejection becomes the detectors' question to the wearer instead of an UNCONFIRMED
    Incident for a keyholder button nobody presses; the system judges the answer. No owner card is sent then."""
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
        register_incident=ask_wearer if ask_wearer is not None else (lambda incident_id, description, now: trust.register_incident_report(
            rule_group_id=RULE_GROUP, trust_domain=domain, description=description, evidence=PEER_REVIEW_EVIDENCE, now=now,
            incident_id=incident_id)))
    out.owner_target = owner_discord_id(core, getattr(config, "owner_user_id", None))
    if ask_wearer is not None:
        out.incident_notices = False                    # the wearer is asked instead; the owner still hears when polling breaks
        out.notes.append(f"Community review watch enabled: every {out.interval_seconds} s; a rejection is asked about, then judged (Trust domain '{domain}').")
        return out
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


def queued_send(queue, *, user_id: str, clock: Clock):
    """`send` for `peer_review_loop` when the notification queue is in use: True means durably queued, so the watch marks the notice as handed over and the queue
    owns the retries, quiet hours and channel choice. `view` is a tuple of ActionSpec and `embed` an EmbedSpec, because a card may wait across a restart."""

    async def send(_target: str, text: str, *, view=None, embed=None) -> bool:
        now = clock.now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        await asyncio.to_thread(queue.enqueue, user_id=user_id, kind="peer_review", dedupe_key=f"peer_review:{now.date().isoformat()}:{digest}", text=text, now=now,
                                embed=embed, actions=view or ())
        return True

    return send


async def peer_review_loop(watch: PeerReviewWatch, clock: Clock, *, interval_seconds: float, send=None, owner_target: str | None = None,
                           initial_delay_seconds: float = 30.0, view_for: Callable[[str], object] | None = None,
                           health: PollHealth | None = None, embed_for: Callable[[object], object] | None = None, incident_notices: bool = True) -> None:
    """Poll, then deliver the pending notices to the owner (`send(target, text) -> bool`, e.g. the Discord adapter); a notice is marked
    delivered only when the channel accepted it. With `view_for(incident_id)` the notice carries the decision buttons
    (`send(target, text, view=...)`). `health` counts consecutive failed polls and queues one alert DM at the threshold (and one
    recovery note). `embed_for(EmbedSpec)` (Phase J1) turns the formatted notices (peer_review/notices.py) into the channel's embed;
    the plain text stays the fallback. Never raises out of an iteration."""
    health = health if health is not None else PollHealth()
    await asyncio.sleep(initial_delay_seconds)          # let the Discord client become ready first
    while True:
        try:
            result = await asyncio.to_thread(watch.poll, clock.now())
            health.observe(failed=bool(result.problems), detail="; ".join(result.problems))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Community review poll failed; will retry next interval.")
            health.observe(failed=True, detail="the poll itself failed, see the bot log")
        try:
            if send is not None and owner_target and health.pending:
                extra = {"embed": embed_for(health_embed(health.pending, alert=health.pending_is_alert))} if embed_for is not None else {}
                if await send(owner_target, health.pending, **extra):
                    health.delivered()
            if send is not None and owner_target and incident_notices:
                for notice in await asyncio.to_thread(watch.pending_notifications):
                    extra = {"view": view_for(notice.incident_id)} if view_for is not None else {}
                    if embed_for is not None:
                        extra["embed"] = embed_for(incident_embed(notice))
                    sent = await send(owner_target, notice.text, **extra)
                    if sent:
                        await asyncio.to_thread(watch.mark_notified, notice.incident_id, clock.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Delivering community review messages failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)
