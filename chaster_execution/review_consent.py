"""
chaster_execution/review_consent.py

The wearer's consent to community photo review (v0.39 Phase I3, philosophy 2.5 and 2.6). Community review (`all_members`) lets any
logged-in Chaster member see the lock's verification photos and vote on them, so it is never switched on without the wearer having
seen the fixed notice below and approved it for that lock.

Simplified (owner decision): the notice is informational. A wearer who proceeds after it agrees; nobody has to accept it, and there is no
re-acceptance when its wording changes (its version is kept only as a record of which text was shown). What remains is the wearer's right to
say no: an explicit decline or revocation stands until the wearer changes it (philosophy 2.5, revocable), and it is the one state that refuses.

Consent is stored append-only in `consent_log` of the database the lock belongs to: `target_type = integration`,
`target_id = community_photo_review:<lock id>`, `target_version = <notice version>`, `explanation_shown = <the exact notice>`,
`action = approved | rejected | revoked`. The latest record for the lock decides; it is specific to one lock and revocable at any time.

This module only reads and writes those records. It never talks to Chaster.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

__all__ = ["REPOSITORY_URL", "NOTICE_TEXT", "NOTICE_VERSION", "CONSENT_TARGET_PREFIX", "OWNER_ATTESTATION", "ConsentDecision", "ConsentState",
           "ReviewConsent", "ReviewConsentLedger", "consent_target"]

REPOSITORY_URL = "https://github.com/JohnTheSwitch/AI-Coach-Keyholder"
NOTICE_TEXT = (
    "Community photo review on your lock\n"
    "\n"
    "If you agree, the review of your verification pictures and of the photos you send to prove tasks is switched to "
    "community review on Chaster (visibility \"all members\"):\n"
    "- Any logged-in Chaster member can see those photos and vote whether they are valid. Your keyholder is no longer the "
    "only one who sees them.\n"
    "- Chaster itself no longer adds time or any other penalty when a photo is rejected.\n"
    "- Here, a rejected photo becomes at most an UNCONFIRMED incident that the owner reviews. Nothing is decided "
    "automatically from one rejected photo.\n"
    "- The setting is visible on your lock's page on Chaster.\n"
    "\n"
    "Proceeding means you agree to these terms: " + REPOSITORY_URL + "\n"
    "You can revoke this at any time: Chaster then stops asking for photos on this lock (the verification picture extension is "
    "removed, tasks and temporary openings no longer need a photo, and task photos are seen by your keyholder only). If you decline, "
    "community review is not switched on (and if it already is, it is switched off the same way)."
)
NOTICE_VERSION = hashlib.sha256(NOTICE_TEXT.encode("utf-8")).hexdigest()[:12]
CONSENT_TARGET_PREFIX = "community_photo_review:"
OWNER_ATTESTATION = "the owner attests the wearer account is the owner's own"     # written into a record's comment (CLI attestation)
_TARGET_TYPE = "integration"


def consent_target(lock_id: str) -> str:
    return f"{CONSENT_TARGET_PREFIX}{lock_id}"


class ConsentDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class ConsentState(StrEnum):
    APPROVED = "approved"      # the latest record approves (whichever notice text it saw)
    DECLINED = "declined"
    REVOKED = "revoked"
    NONE = "none"              # no record: the notice covers it


@dataclass(frozen=True, kw_only=True)
class ReviewConsent:
    lock_id: str
    state: ConsentState
    notice_version: str | None = None
    decided_at: str | None = None

    @property
    def approved(self) -> bool:
        return self.state is ConsentState.APPROVED

    @property
    def refused(self) -> bool:
        """An explicit no. It stands until the wearer changes it: the notice's implied agreement never overrides it."""
        return self.state in (ConsentState.DECLINED, ConsentState.REVOKED)


_STATES = {"rejected": ConsentState.DECLINED, "revoked": ConsentState.REVOKED}


class ReviewConsentLedger:
    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))

    def state(self, lock_id: str) -> ReviewConsent:
        with self._core.transaction() as tx:
            row = tx.fetch_one(
                "SELECT action, target_version, created_at FROM consent_log WHERE target_type = ? AND target_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (_TARGET_TYPE, consent_target(lock_id)))
        if row is None:
            return ReviewConsent(lock_id=lock_id, state=ConsentState.NONE)
        state = ConsentState.APPROVED if row["action"] == "approved" else _STATES.get(row["action"], ConsentState.NONE)
        return ReviewConsent(lock_id=lock_id, state=state, notice_version=row["target_version"], decided_at=row["created_at"])

    def owner_attested(self, lock_id: str) -> bool:
        """True once any record for this lock carries the owner's attestation that the lock's wearer account is the owner's own."""
        with self._core.transaction() as tx:
            return tx.fetch_one("SELECT 1 FROM consent_log WHERE target_type = ? AND target_id = ? AND user_comment LIKE ?",
                                (_TARGET_TYPE, consent_target(lock_id), f"%{OWNER_ATTESTATION}%")) is not None

    def record(self, *, lock_id: str, decision: ConsentDecision, comment: str, now: datetime) -> str:
        record_id = str(uuid.uuid4())

        def write(tx: Transaction, _s: object) -> None:
            tx.execute(
                "INSERT INTO consent_log (id, created_at, target_type, target_id, target_version, action, decision_result_id, "
                "explanation_shown, user_comment) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (record_id, _iso(now), _TARGET_TYPE, consent_target(lock_id), NOTICE_VERSION, decision.value, NOTICE_TEXT, comment[:200]))

        apply_transition(self._core, write=write)
        return record_id
