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
`regularity`, a partner extension (its `partnerConfigurationToken` is not readable), or (forward plan only) a missing review extension (I2 never adds
extensions).

`plan_review_revert()` (Phase I3, widened 2026-11-03) makes Chaster stop asking for photos: it removes `verification-picture`, switches
`temporary-opening`'s picture requirements off and the tasks' peer verification and `verificationRequired` / `codeRequired` off; every
other extension is sent back as read (see its docstring). Used when the wearer declines or revokes consent to community review, and by
`extension-review --revert`.

`plan_id` is the first 16 hex characters of the SHA-256 of the canonical body. The write path recomputes it from a fresh read and only
sends when it equals the id the owner saw in the dry run, so what was reviewed is exactly what is sent, and a lock that changed in between
is refused.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from chaster.extension_config import (PRIVATE_VISIBILITY, REVIEW_EXTENSIONS, TARGET_VISIBILITY, LockConfiguration, Punishment,
                                      _peer_verification)

__all__ = ["EXTENSION_MODES", "PICTURE_EXTENSION", "PICTURE_DEPENDENT_FLAGS", "FieldChange", "ReviewPlan", "plan_review_update",
           "plan_review_revert", "canonical_digest"]

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
    """Community review: peer verification on, visibility `all_members`, no Chaster punishments."""
    target = {"enabled": True, "visibility": TARGET_VISIBILITY, "punishments": []}

    def forward(ext, cfg: dict) -> list[FieldChange]:
        if ext.slug not in REVIEW_EXTENSIONS:
            return []
        before = ext.peer_verification
        pv = dict(cfg.get("peerVerification")) if isinstance(cfg.get("peerVerification"), dict) else {}
        pv.update(copy.deepcopy(target))
        cfg["peerVerification"] = pv
        after = _peer_verification(pv)
        changes = []
        for name, old, new in (
                ("peerVerification.enabled", _enabled_text(before.enabled if before else None), _enabled_text(after.enabled)),
                ("peerVerification.visibility", (before.visibility if before else None) or "unset", after.visibility or "unset"),
                ("peerVerification.punishments", _punishments_text(before.punishments if before else ()), _punishments_text(after.punishments))):
            if old != new:
                changes.append(FieldChange(slug=ext.slug, field=name, before=old, after=new))
        return changes

    return _plan(config, forward)


PICTURE_EXTENSION = "verification-picture"
PICTURE_DEPENDENT_FLAGS = {"temporary-opening": ("requireVerificationPictureBefore", "requireVerificationPictureAfter")}


def plan_review_revert(config: LockConfiguration) -> ReviewPlan:
    """A declined or revoked consent (or `extension-review --revert`): Chaster stops asking for photos on this lock, and nobody but the
    keyholder sees the task photos that already exist. Exactly these changes, each only when it is not already in that state:

    - `verification-picture` is REMOVED from the extension list: its photo requests come from the extension itself (its config has no
      on/off switch), so removing it is the only way to stop them;
    - every other extension's own "verification picture" requirement is switched off explicitly rather than trusting Chaster to default it
      (`temporary-opening`: `requireVerificationPictureBefore` / `requireVerificationPictureAfter` -> false);
    - `tasks`: `peerVerification.enabled` -> false and `peerVerification.visibility` -> `keyholder` (`punishments` are left as they are),
      and every task item's `verificationRequired` and `codeRequired` -> false (TaskItem, OpenAPI 0.35.0); task text, points and duration
      stay exactly as read.

    Every other extension (`pillory`, `link`, ...) and every other field is sent back exactly as read; nothing is added. A lock that never
    had `verification-picture` is fine (the revert does not require it)."""

    def revert(ext, cfg: dict) -> list[FieldChange]:
        changes: list[FieldChange] = []
        for flag in PICTURE_DEPENDENT_FLAGS.get(ext.slug, ()):
            if cfg.get(flag) is True:
                cfg[flag] = False
                changes.append(FieldChange(slug=ext.slug, field=flag, before="on", after="off"))
        if ext.slug != "tasks":
            return changes
        pv = cfg.get("peerVerification")
        if isinstance(pv, dict):
            pv = dict(pv)
            if pv.get("enabled") is True:
                pv["enabled"] = False
                changes.append(FieldChange(slug="tasks", field="peerVerification.enabled", before="on", after="off"))
            if pv.get("visibility") is not None and pv.get("visibility") != PRIVATE_VISIBILITY:
                changes.append(FieldChange(slug="tasks", field="peerVerification.visibility", before=str(pv["visibility"]),
                                           after=PRIVATE_VISIBILITY))
                pv["visibility"] = PRIVATE_VISIBILITY
            cfg["peerVerification"] = pv
        if isinstance(cfg.get("tasks"), list):
            items = cfg["tasks"]
            for flag in ("verificationRequired", "codeRequired"):
                on = sum(1 for t in items if isinstance(t, dict) and t.get(flag) is True)
                if on:
                    changes.append(FieldChange(slug="tasks", field=f"tasks[].{flag}", before=f"on for {on} of {len(items)} tasks",
                                               after="off"))
            cfg["tasks"] = [({**t, **{f: False for f in ("verificationRequired", "codeRequired") if t.get(f) is True}}
                             if isinstance(t, dict) else t) for t in items]
        return changes

    return _plan(config, revert, remove=(PICTURE_EXTENSION,), require_review_extensions=False)


def _plan(config: LockConfiguration, transform, *, remove: tuple[str, ...] = (), require_review_extensions: bool = True) -> ReviewPlan:
    """The complete body: every extension in its read order, each config deep-copied and changed only by `transform(ext, cfg)`, which
    mutates `cfg` and returns the FieldChanges it made. An extension whose config ends up equal to what was read is `unchanged`; a slug in
    `remove` is left out of the body (recorded as a change). The forward plan requires both review extensions; the revert does not."""
    entries: list[dict] = []
    changes: list[FieldChange] = []
    unchanged: list[str] = []
    refusal: str | None = None
    present = {e.slug for e in config.extensions}
    for slug in REVIEW_EXTENSIONS if require_review_extensions else ():
        if slug not in present and refusal is None:
            refusal = f"missing_extension:{slug}"
    for ext in config.extensions:
        if refusal is None and ext.is_partner is True:
            refusal = f"partner_extension:{ext.slug}"
        if refusal is None and (ext.mode not in EXTENSION_MODES or ext.regularity is None or ext.regularity < 0):
            refusal = f"incomplete_extension:{ext.slug}"
        if ext.slug in remove:
            changes.append(FieldChange(slug=ext.slug, field="extension", before="present", after="removed"))
            continue
        cfg = copy.deepcopy(ext.config)
        changes += transform(ext, cfg)
        if cfg == ext.config:
            unchanged.append(ext.slug)
        entries.append({"slug": ext.slug, "config": cfg, "mode": ext.mode,
                        "regularity": int(ext.regularity) if isinstance(ext.regularity, float) and ext.regularity.is_integer() else ext.regularity})
    body = {"extensions": entries}
    return ReviewPlan(lock_id=config.lock_id, plan_id=canonical_digest(body), changes=tuple(changes), unchanged=tuple(unchanged),
                      refusal=refusal, body=body)
