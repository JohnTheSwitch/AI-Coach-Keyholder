"""
chaster/extension_plan.py

The review-settings plan (v0.39 Phase I2): a PURE function from a freshly read `LockConfiguration` to the complete body for
`POST /locks/{lockId}/extensions` and an exact list of what changes. Nothing here talks to Chaster.

The body is the COMPLETE extension list, because Chaster replaces the lock's extensions with what it receives: every extension is sent
back with the `slug`, `config`, `mode` and `regularity` it was read with. Inside `config.peerVerification` of the two review extensions
(`tasks`, `verification-picture`) exactly three fields are set: `enabled` = true, `visibility` = `all_members` (community review) and
`punishments` = [] (no Chaster-side penalty; a rejected check is handled only by this engine). Every other field, including any other
key inside `peerVerification`, is sent back as read. Peer verification has to be on for community review to happen at all, so a check
that is switched off is switched on (the dry run shows it).

The plan refuses (and nothing may be sent) when the body could not be rebuilt faithfully: an extension without a valid `mode` or
`regularity`, a partner extension (its `partnerConfigurationToken` is not readable), or a missing review extension (I2 never adds
extensions).

`plan_id` is the first 16 hex characters of the SHA-256 of the canonical body. The write path recomputes it from a fresh read and only
sends when it equals the id the owner saw in the dry run, so what was reviewed is exactly what is sent, and a lock that changed in between
is refused.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from chaster.extension_config import REVIEW_EXTENSIONS, TARGET_VISIBILITY, LockConfiguration, Punishment, _peer_verification

__all__ = ["EXTENSION_MODES", "FieldChange", "ReviewPlan", "plan_review_update", "canonical_digest"]

EXTENSION_MODES = ("cumulative", "non_cumulative", "turn", "unlimited")      # LockExtensionConfigDto.mode
PLAN_ID_LENGTH = 16


@dataclass(frozen=True, kw_only=True)
class FieldChange:
    slug: str
    field: str            # e.g. "peerVerification.visibility"
    before: str
    after: str


@dataclass(frozen=True, kw_only=True)
class ReviewPlan:
    lock_id: str
    plan_id: str
    changes: tuple[FieldChange, ...]
    unchanged: tuple[str, ...]                      # slugs sent back exactly as read
    refusal: str | None                             # e.g. "partner_extension:foo"; nothing may be sent when set
    body: dict = field(repr=False, compare=False)   # {"extensions": [...]}; may contain task texts, never logged

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


def canonical_digest(body: dict) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:PLAN_ID_LENGTH]


def _punishments_text(punishments: tuple[Punishment, ...]) -> str:
    return "[" + ", ".join(p.label() for p in punishments) + "]"


def _enabled_text(value: bool | None) -> str:
    return {True: "on", False: "off"}.get(value, "unset")


def plan_review_update(config: LockConfiguration) -> ReviewPlan:
    entries: list[dict] = []
    changes: list[FieldChange] = []
    unchanged: list[str] = []
    refusal: str | None = None
    present = {e.slug for e in config.extensions}
    for slug in REVIEW_EXTENSIONS:
        if slug not in present and refusal is None:
            refusal = f"missing_extension:{slug}"
    for ext in config.extensions:
        if refusal is None and ext.is_partner is True:
            refusal = f"partner_extension:{ext.slug}"
        if refusal is None and (ext.mode not in EXTENSION_MODES or ext.regularity is None or ext.regularity < 0):
            refusal = f"incomplete_extension:{ext.slug}"
        cfg = copy.deepcopy(ext.config)
        if ext.slug in REVIEW_EXTENSIONS:
            before = ext.peer_verification
            pv = dict(cfg.get("peerVerification")) if isinstance(cfg.get("peerVerification"), dict) else {}
            pv.update({"enabled": True, "visibility": TARGET_VISIBILITY, "punishments": []})
            cfg["peerVerification"] = pv
            after = _peer_verification(pv)
            for name, old, new in (
                    ("peerVerification.enabled", _enabled_text(before.enabled if before else None), _enabled_text(after.enabled)),
                    ("peerVerification.visibility", (before.visibility if before else None) or "unset", after.visibility),
                    ("peerVerification.punishments", _punishments_text(before.punishments if before else ()), _punishments_text(after.punishments))):
                if old != new:
                    changes.append(FieldChange(slug=ext.slug, field=name, before=old, after=new))
        else:
            unchanged.append(ext.slug)
        entries.append({"slug": ext.slug, "config": cfg, "mode": ext.mode,
                        "regularity": int(ext.regularity) if isinstance(ext.regularity, float) and ext.regularity.is_integer() else ext.regularity})
    body = {"extensions": entries}
    return ReviewPlan(lock_id=config.lock_id, plan_id=canonical_digest(body), changes=tuple(changes), unchanged=tuple(unchanged),
                      refusal=refusal, body=body)

