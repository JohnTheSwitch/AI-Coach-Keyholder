"""
supervision/photo_check_ins.py

Photo check-ins (owner, 2026-11-13): some check-ins ask for a verification picture in Chaster instead of a text answer. A text answer only shows
that the wearer was at their phone; a picture with Chaster's code shows the lock is on.

- The picture never comes to this bot: it is taken in Chaster, reviewed by the Chaster community as before, and the bot only reads WHEN pictures
  were submitted (`chaster/peer_reviews.py`, `PeerReviewReader.picture_times`). A rejection is handled by the community review watch as before.
- Only with the wearer's own consent (`photo check-ins on`), revocable at any time (`photo check-ins off`); revoking closes open ones as `skipped`,
  never a miss.
- How many per week follows the Trust level (`PHOTO_CHECK_INS_PER_WEEK`): earning trust means fewer pictures. Only in the discretionary window,
  with PHOTO_RESPONSE_TIMEOUT to answer, and the whole answer time also inside the discretionary window.
- A picture submitted between the prompt and the deadline answers it. None by the deadline is a missed check-in, with the same consequence as a
  missed text check-in (Trust and warnings only). When the pictures cannot be read, it is never a miss: after PHOTO_UNKNOWN_LIMIT it is `skipped`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.outbox import DomainEvent, write_event
from infrastructure.time_format import iso as _iso
from supervision.models import TrustLevel

__all__ = ["PHOTO_CHECK_INS_PER_WEEK", "PHOTO_RESPONSE_TIMEOUT", "PHOTO_UNKNOWN_LIMIT", "PHOTO_POLL", "PHOTO_SLOT", "PHOTO_CLOCK_SKEW",
           "PhotoCheckInConsent", "PhotoCheckInSwitch", "photo_prompt", "PHOTO_RECEIVED"]

# Owner, 2026-11-13: "around 1-2 a week normally, more while supervision mode is on".
PHOTO_CHECK_INS_PER_WEEK = {TrustLevel.LEVEL_4: 1, TrustLevel.LEVEL_3: 2, TrustLevel.LEVEL_2: 3, TrustLevel.LEVEL_1: 4}
PHOTO_RESPONSE_TIMEOUT = timedelta(hours=2)       # owner, 2026-11-13: a picture needs privacy, so 2 hours instead of 30 minutes
PHOTO_UNKNOWN_LIMIT = timedelta(hours=2)          # past the deadline with unreadable pictures this long: skipped, never a miss
PHOTO_POLL = timedelta(minutes=5)                 # how often an open photo check-in reads the pictures
PHOTO_CLOCK_SKEW = timedelta(minutes=2)           # Chaster's clock and ours
PHOTO_SLOT = 50                                   # between the scheduled slots and RELEASE_SLOT: at most one photo check-in a day

PHOTO_RECEIVED = "Got your verification picture. Thanks."


def photo_prompt(answer_by_local: str) -> str:
    return (f"Photo check-in. Take a verification picture in Chaster now, with the code it shows you. You have until {answer_by_local}. "
            "The picture stays on Chaster; I only see that it arrived.")


class PhotoCheckInConsent:
    """The wearer's own opt-in. Unknown or unreadable means off (fail closed: no photo demands without consent)."""

    def __init__(self, db_path: str | Path, *, core: CoreDatabase | None = None) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))

    def is_on(self, user_id: str) -> bool:
        try:
            with self._core.transaction() as tx:
                row = tx.fetch_one("SELECT consented_at, revoked_at FROM photo_check_in_consent WHERE user_id = ?", (user_id,))
        except Exception:
            return False
        return row is not None and row["consented_at"] is not None and row["revoked_at"] is None

    def set(self, user_id: str, on: bool, *, now: datetime) -> bool:
        """Returns True when the state changed. Revoking also closes open photo check-ins as `skipped` (never a miss)."""
        def write(tx: Transaction, _s: object) -> bool:
            row = tx.fetch_one("SELECT consented_at, revoked_at FROM photo_check_in_consent WHERE user_id = ?", (user_id,))
            was_on = row is not None and row["consented_at"] is not None and row["revoked_at"] is None
            if was_on == on:
                return False
            if row is None:
                tx.execute("INSERT INTO photo_check_in_consent (user_id, consented_at, revoked_at, updated_at) VALUES (?, ?, NULL, ?)",
                           (user_id, _iso(now) if on else None, _iso(now)))
            elif on:
                tx.execute("UPDATE photo_check_in_consent SET consented_at = ?, revoked_at = NULL, updated_at = ? WHERE user_id = ?",
                           (_iso(now), _iso(now), user_id))
            else:
                tx.execute("UPDATE photo_check_in_consent SET revoked_at = ?, updated_at = ? WHERE user_id = ?", (_iso(now), _iso(now), user_id))
                tx.execute("UPDATE check_ins SET status = 'skipped', updated_at = ? WHERE user_id = ? AND kind = 'photo' AND status IN "
                           "('scheduled', 'sent')", (_iso(now), user_id))
            write_event(tx, DomainEvent(event_type="supervision.photo_check_ins_consented" if on else "supervision.photo_check_ins_revoked",
                                        source_module="supervision", payload={"user_id": user_id}, occurred_at=now))
            return True

        return apply_transition(self._core, write=write)

    def drawn_on(self, user_id: str, local_date: str) -> bool:
        with self._core.transaction() as tx:
            row = tx.fetch_one("SELECT last_drawn_date FROM photo_check_in_consent WHERE user_id = ?", (user_id,))
        return row is not None and row["last_drawn_date"] == local_date

    def mark_drawn(self, tx: Transaction, user_id: str, local_date: str, now: datetime) -> None:
        """Inside the caller's transaction, together with the photo row it may have inserted."""
        tx.execute("UPDATE photo_check_in_consent SET last_drawn_date = ?, updated_at = ? WHERE user_id = ?", (local_date, _iso(now), user_id))


@dataclass(frozen=True)
class PhotoCheckInSwitch:
    """What the DM commands need: the consent store and the one wearer the check-ins are for."""
    consent: PhotoCheckInConsent
    user_id: str
