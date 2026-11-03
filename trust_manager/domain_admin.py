"""
trust_manager/domain_admin.py

The owner's guarded way to create a Trust domain (TI1: a domain is never created without consent). Reached only from the local CLI
(`python -m cli trust-domain create`); no model, conversation or periodic path calls it.

`create()` checks, in order: the owner (constant-time compare with OWNER_USER_ID), `--yes`, a plain domain id (`[a-z][a-z0-9_]{1,31}`), a
display name and description (1-200 characters), and that the domain does not exist yet. It then writes the consent record FIRST
(`consent_log`: target_type `trust_algorithm`, target_id `trust_domain:<id>`, action `approved`, explanation_shown = the exact text the
command printed) and creates the domain with that consent id, starting at the Trust Manager's default score (0.6) and confidence (0.15).
"""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso
from trust_manager.repository import DEFAULT_NEW_DOMAIN_CONFIDENCE, DEFAULT_NEW_DOMAIN_SCORE, TrustManager

__all__ = ["DomainAdminOutcome", "TrustDomainAdmin", "domain_explanation"]

_DOMAIN_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class DomainAdminOutcome(StrEnum):
    CREATED = "created"
    OWNER_NOT_CONFIGURED = "owner_not_configured"
    NOT_OWNER = "not_owner"
    NOT_CONFIRMED = "not_confirmed"
    INVALID_INPUT = "invalid_input"
    ALREADY_EXISTS = "already_exists"


def domain_explanation(domain_id: str, display_name: str, description: str) -> str:
    return (f"Create the Trust domain '{domain_id}' ({display_name}): {description}\n"
            f"It starts at score {DEFAULT_NEW_DOMAIN_SCORE} and confidence {DEFAULT_NEW_DOMAIN_CONFIDENCE}. Only CONFIRMED Incidents and "
            f"recorded evidence change it; an UNCONFIRMED Incident never does. The domain can later be deactivated with consent.")


class TrustDomainAdmin:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None, owner_user_id: str | None) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))
        self._trust = TrustManager(self._core.db_path, core=self._core)
        self._owner = owner_user_id.strip() if owner_user_id and owner_user_id.strip() else None

    def authorize(self, actor: str) -> DomainAdminOutcome | None:
        if self._owner is None:
            return DomainAdminOutcome.OWNER_NOT_CONFIGURED
        if not isinstance(actor, str) or not hmac.compare_digest(actor.encode("utf-8"), self._owner.encode("utf-8")):
            return DomainAdminOutcome.NOT_OWNER
        return None

    def list_domains(self) -> list[dict]:
        with self._core.transaction() as tx:
            return [dict(r) for r in tx.fetch_all(
                "SELECT d.domain_id, d.display_name, d.is_active, d.created_at, s.score, s.confidence FROM trust_domains d "
                "LEFT JOIN trust_domain_state s ON s.domain_id = d.domain_id ORDER BY d.created_at")]

    def create(self, *, actor: str, domain_id: str, display_name: str, description: str, confirmed: bool,
               now: datetime) -> DomainAdminOutcome:
        refusal = self.authorize(actor)
        if refusal is not None:
            return refusal
        if confirmed is not True:
            return DomainAdminOutcome.NOT_CONFIRMED
        if (not isinstance(domain_id, str) or not _DOMAIN_ID.match(domain_id)
                or not all(isinstance(v, str) and 0 < len(v.strip()) <= 200 for v in (display_name, description))):
            return DomainAdminOutcome.INVALID_INPUT
        if self._trust.get_domain_state(domain_id) is not None:
            return DomainAdminOutcome.ALREADY_EXISTS
        consent_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(
                "INSERT INTO consent_log (id, created_at, target_type, target_id, target_version, action, decision_result_id, "
                "explanation_shown, user_comment) VALUES (?, ?, 'trust_algorithm', ?, NULL, 'approved', NULL, ?, ?)",
                (consent_id, _iso(now), f"trust_domain:{domain_id}", domain_explanation(domain_id, display_name.strip(), description.strip()),
                 "owner cli: trust-domain create"))

        apply_transition(self._core, write=write)
        self._trust.create_domain(domain_id=domain_id, display_name=display_name.strip(), description=description.strip(),
                                  created_via_consent_id=consent_id, now=now)
        return DomainAdminOutcome.CREATED
