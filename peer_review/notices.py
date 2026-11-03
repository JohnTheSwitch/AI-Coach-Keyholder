"""
peer_review/notices.py

Phase J1: the proactive messages of the community review watch as channel-agnostic embeds (application/models.EmbedSpec); the bot turns
them into Discord embeds. The plain `text` of each message stays the fallback for channels without embeds. No Trust value, no task text,
no verification code, no voter names: lock id, what kind of photo, vote counts, incident id.
"""

from __future__ import annotations

from application.models import EmbedField, EmbedSpec
from peer_review.watch import PendingNotice

__all__ = ["incident_embed", "health_embed"]

_SOURCE_TEXT = {"tasks": "task photo", "verification-picture": "verification picture"}


def _votes(verified, rejected) -> str:
    return f"{'?' if verified is None else verified} verified / {'?' if rejected is None else rejected} rejected"


def incident_embed(notice: PendingNotice) -> EmbedSpec:
    what = _SOURCE_TEXT.get(notice.source, notice.source)
    return EmbedSpec(
        title="Photo review rejected",
        description=(f"The community voted against a {what} on your lock. Nothing was deducted and no time was added: it is recorded as "
                     f"an UNCONFIRMED incident and waits for your decision."),
        color="red",
        fields=(
            EmbedField(name="Lock", value=notice.lock_id),
            EmbedField(name="Votes", value=_votes(notice.verified_votes, notice.rejected_votes)),
            EmbedField(name="Incident", value=notice.incident_id),
            EmbedField(name="Decide", value=("Use the buttons below. Fallback on the bot's machine: "
                                             f"`python -m cli peer-reviews confirm|dismiss {notice.incident_id} --owner-id <your id> --yes`")),
        ),
    )


def health_embed(text: str, *, alert: bool) -> EmbedSpec:
    if alert:
        return EmbedSpec(title="Photo review watch needs attention", description=text, color="red")
    return EmbedSpec(title="Photo review watch is working again", description=text, color="green")
