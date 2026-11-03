"""
chaster/extension_config.py

Read-only extension configuration of a lock (v0.39 Phase I1, the first step of Autonomous Extension Management).

`LockConfigurationReader.read(user_id, lock_ids)` fetches each lock with `GET /locks/{lockId}` through the account's connection and
`parse_lock_configuration()` maps it to typed values: the account's role on that lock, who may edit the extensions
(`lock.extensions.edit` in the lock's permission contract), and every extension with its `slug`, `_id`, `mode`, `regularity` and
`config`. For the two review extensions (`tasks`, `verification-picture`) the peer-verification settings are parsed: `enabled`,
`visibility`, `punishments`. `review_gaps()` lists how a lock differs from the Phase I target (community review, no Chaster-side
punishments). Nothing here writes anything.

Verified live 2026-11-03 (wearer and keyholder alike): `role`, `permissions.grants[]` (`resource`, `category`, `subjects` {wearer: [verbs],
keyholder: [verbs]}) and `extensions[]` (`_id`, `slug`, `mode`, `regularity`, `config`, `userData`, ...); `config.peerVerification` is
{`enabled`, `punishments`, `visibility`}. The extension config itself is an untyped `object` in the OpenAPI document; the visibility
values come from `PeerVerificationVisibilityEnum` (`all_members`, `keyholder`) and the punishments from the `*LockActionModel` schemas
({`name`, `params`}, e.g. `add_time` with seconds).

SECRETS AND PERSONAL TEXT: the lock's `combination`, `offerToken`, `sharedLinkToken` and every extension's `userData` (which holds the
current verification code) are never copied. The extension `config` is kept as a deep copy because a later write has to send the
COMPLETE extension list back (`POST /locks/{lockId}/extensions`); it may contain task texts, so it is excluded from `repr` and never
logged. Logs and problem codes carry lock ids, slugs and enum values only.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from chaster.lock_client import ChasterLockApiError, ChasterLockClient
from chaster.managed_locks import LockRole
from chaster.token_manager import (
    ChasterNotConnectedError, ChasterReauthorizationRequiredError, ChasterRefreshUnavailableError, ChasterTokenManager,
)

logger = logging.getLogger("ai_coach_keyholder.chaster.extension_config")

__all__ = [
    "REVIEW_EXTENSIONS", "EXTENSIONS_EDIT_PERMISSION", "TARGET_VISIBILITY", "VISIBILITY_VALUES",
    "Punishment", "PeerVerificationSettings", "ExtensionSnapshot", "LockConfiguration", "ConfigurationReport",
    "parse_lock_configuration", "review_gaps", "describe_review", "LockConfigurationReader",
]

REVIEW_EXTENSIONS = ("tasks", "verification-picture")
EXTENSIONS_EDIT_PERMISSION = "lock.extensions.edit"
VISIBILITY_VALUES = ("all_members", "keyholder")            # PeerVerificationVisibilityEnum; there is no value named "community"
TARGET_VISIBILITY = "all_members"                         # community review: every Chaster member may vote


@dataclass(frozen=True, kw_only=True)
class Punishment:
    name: str | None
    seconds: int | None                                    # numeric `params` (add_time / remove_time); None for other shapes

    def label(self) -> str:
        return f"{self.name or '?'}({self.seconds})" if self.seconds is not None else (self.name or "?")


@dataclass(frozen=True, kw_only=True)
class PeerVerificationSettings:
    enabled: bool | None
    visibility: str | None
    punishments: tuple[Punishment, ...]


@dataclass(frozen=True, kw_only=True)
class ExtensionSnapshot:
    slug: str
    extension_id: str | None
    mode: str | None
    regularity: float | None
    is_partner: bool | None
    peer_verification: PeerVerificationSettings | None
    config: dict = field(repr=False, compare=False)        # deep copy for the later full-list write; never logged


@dataclass(frozen=True, kw_only=True)
class LockConfiguration:
    lock_id: str
    role: LockRole | None
    status: str | None
    is_test_lock: bool | None
    edit_subjects: frozenset[str]                          # subjects holding "edit" on lock.extensions.edit
    extensions: tuple[ExtensionSnapshot, ...]

    @property
    def can_edit_extensions(self) -> bool:
        return self.role is not None and self.role.value in self.edit_subjects

    def extension(self, slug: str) -> ExtensionSnapshot | None:
        return next((e for e in self.extensions if e.slug == slug), None)


@dataclass
class ConfigurationReport:
    configurations: list[LockConfiguration] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)      # e.g. "6aca95cd...: api_error:403"


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _punishment(raw: Any) -> Punishment | None:
    if not isinstance(raw, dict):
        return None
    params = _number(raw.get("params"))
    return Punishment(name=_str(raw.get("name")), seconds=int(params) if params is not None else None)


def _peer_verification(raw: Any) -> PeerVerificationSettings | None:
    if not isinstance(raw, dict):
        return None
    punishments = raw.get("punishments")
    parsed = tuple(p for p in (_punishment(x) for x in punishments) if p is not None) if isinstance(punishments, list) else ()
    return PeerVerificationSettings(enabled=_bool(raw.get("enabled")), visibility=_str(raw.get("visibility")), punishments=parsed)


def _extension(raw: Any) -> ExtensionSnapshot | None:
    if not isinstance(raw, dict) or _str(raw.get("slug")) is None:
        return None
    config = raw.get("config")
    config = copy.deepcopy(config) if isinstance(config, dict) else {}
    return ExtensionSnapshot(slug=raw["slug"], extension_id=_str(raw.get("_id")), mode=_str(raw.get("mode")),
                             regularity=_number(raw.get("regularity")), is_partner=_bool(raw.get("isPartner")), config=config,
                             peer_verification=_peer_verification(config.get("peerVerification")))


def _edit_subjects(permissions: Any) -> frozenset[str]:
    grants = permissions.get("grants") if isinstance(permissions, dict) else None
    for grant in grants if isinstance(grants, list) else ():
        if isinstance(grant, dict) and grant.get("resource") == EXTENSIONS_EDIT_PERMISSION and isinstance(grant.get("subjects"), dict):
            return frozenset(subject for subject, verbs in grant["subjects"].items()
                             if isinstance(subject, str) and isinstance(verbs, list) and "edit" in verbs)
    return frozenset()


def parse_lock_configuration(raw: Any) -> LockConfiguration | None:
    """None for anything that is not a lock with an id; ill-typed fields become None or empty (never raises, nothing is guessed)."""
    if not isinstance(raw, dict) or _str(raw.get("_id")) is None:
        return None
    try:
        role = LockRole(raw.get("role"))
    except ValueError:
        role = None
    extensions = raw.get("extensions")
    parsed = tuple(e for e in (_extension(x) for x in extensions) if e is not None) if isinstance(extensions, list) else ()
    return LockConfiguration(lock_id=raw["_id"], role=role, status=_str(raw.get("status")), is_test_lock=_bool(raw.get("isTestLock")),
                             edit_subjects=_edit_subjects(raw.get("permissions")), extensions=parsed)


def review_gaps(config: LockConfiguration) -> tuple[str, ...]:
    """How the lock differs from the Phase I target for each review extension: present, peer verification on, visibility
    `all_members`, no Chaster-side punishments. Empty when the lock already matches."""
    gaps: list[str] = []
    for slug in REVIEW_EXTENSIONS:
        ext = config.extension(slug)
        pv = ext.peer_verification if ext else None
        if ext is None:
            gaps.append(f"{slug}: extension missing")
            continue
        if pv is None or pv.enabled is not True:
            gaps.append(f"{slug}: peer verification off")
        if pv is None or pv.visibility != TARGET_VISIBILITY:
            gaps.append(f"{slug}: visibility {pv.visibility if pv and pv.visibility else 'unset'} (target {TARGET_VISIBILITY})")
        if pv is not None and pv.punishments:
            gaps.append(f"{slug}: punishments {', '.join(p.label() for p in pv.punishments)} (target none)")
    return tuple(gaps)


def describe_review(ext: ExtensionSnapshot | None) -> str:
    """One line of enum values only, for logs and the CLI."""
    if ext is None:
        return "not on the lock"
    pv = ext.peer_verification
    if pv is None:
        return "no peerVerification settings"
    state = {True: "on", False: "off"}.get(pv.enabled, "unknown")
    punishments = ", ".join(p.label() for p in pv.punishments) or "none"
    return f"peer verification {state}, visibility {pv.visibility or 'unset'}, punishments {punishments}"


class LockConfigurationReader:
    """READ-ONLY: one `GET /locks/{lockId}` per lock through the account's connection (401 refresh and retry by the token manager)."""

    def __init__(self, *, token_manager: ChasterTokenManager, lock_client: ChasterLockClient) -> None:
        self._tokens = token_manager
        self._client = lock_client

    def read(self, *, user_id: str, lock_ids: Iterable[str], now: datetime) -> ConfigurationReport:
        report = ConfigurationReport()
        for lock_id in dict.fromkeys(lock_ids):            # each lock once, in discovery order
            problem = self._read_one(user_id, lock_id, now, report)
            if problem:
                report.problems.append(f"{lock_id}: {problem}")
        return report

    def _read_one(self, user_id, lock_id, now, report) -> str | None:
        try:
            raw = self._tokens.call_with_token(user_id, now=now, operation=lambda token: self._client.get_lock(access_token=token, lock_id=lock_id))
        except ValueError:
            return "invalid_lock_id"
        except ChasterNotConnectedError:
            return "not_connected"
        except (ChasterReauthorizationRequiredError, ChasterRefreshUnavailableError):
            return "reauthorization_required"
        except ChasterLockApiError as exc:
            return "api_error" + (f":{exc.status_code}" if exc.status_code else "")
        except Exception:
            logger.exception("Unexpected failure while reading the configuration of lock %s.", lock_id)
            return "failed"
        config = parse_lock_configuration(raw)
        if config is None:
            return "unparseable"
        report.configurations.append(config)
        logger.info("Lock %s (role %s, may edit extensions: %s).", lock_id, config.role.value if config.role else "unknown",
                    "yes" if config.can_edit_extensions else "no")
        for slug in REVIEW_EXTENSIONS:
            logger.info("Lock %s %s: %s.", lock_id, slug, describe_review(config.extension(slug)))
        return None
