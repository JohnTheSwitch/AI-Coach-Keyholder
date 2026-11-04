"""
notifications/queue.py

The durable outbound queue. Every proactive message (incident cards, alerts, consequences, exemption decisions) is a row first and a DM second, so a closed
DM, an offline bot or a crash delays a notice and never loses it. `enqueue_in_transaction` writes the row in the caller's own transaction, the same outbox
discipline `infrastructure/outbox.py` applies to events: the fact and the notice about it commit together or not at all.

`dedupe_key` makes every enqueue idempotent. Delivery is at least once: a crash between the send and the mark can repeat one message, which is harmless, and a
lost notice about a penalty is not. No channel type lives here (notifications/dispatcher.py delivers through an injected port).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from application.models import ActionSpec, EmbedField, EmbedSpec
from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction, apply_transition
from infrastructure.time_format import iso as _iso

__all__ = ["Notification", "NotificationQueue", "DeliveryResult", "enqueue_in_transaction", "RETRY_DELAYS", "MAX_ATTEMPTS", "STALE_CLAIM"]

# Rising delays, then once an hour: a long outage is ridden out, and a dead channel stops after about a day.
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=30))
LATER_DELAY = timedelta(hours=1)
MAX_ATTEMPTS = 30
STALE_CLAIM = timedelta(minutes=5)              # a row left `sending` by a crash goes back in the queue
MAX_TEXT = 3900


class DeliveryResult(StrEnum):
    DELIVERED = "delivered"
    UNDELIVERABLE = "undeliverable"             # provably not sent: DMs closed, unknown user, no identity
    UNKNOWN = "unknown"                         # timeout or a server error: it may or may not have arrived


@dataclass(frozen=True, kw_only=True)
class Notification:
    id: str
    user_id: str
    kind: str
    text: str
    embed: EmbedSpec | None
    actions: tuple[ActionSpec, ...]
    immediate: bool
    attempts: int


def _encode_embed(embed: EmbedSpec | None) -> str | None:
    return None if embed is None else json.dumps(asdict(embed))


def _decode_embed(raw: str | None) -> EmbedSpec | None:
    if not raw:
        return None
    data = json.loads(raw)
    return EmbedSpec(title=data["title"], description=data.get("description"), color=data.get("color", "blue"),
                     fields=tuple(EmbedField(**f) for f in data.get("fields") or ()))


def _decode_actions(raw: str | None) -> tuple[ActionSpec, ...]:
    return tuple(ActionSpec(**a) for a in json.loads(raw)) if raw else ()


def enqueue_in_transaction(tx: Transaction, *, user_id: str, kind: str, dedupe_key: str, text: str, now: datetime, embed: EmbedSpec | None = None,
                           actions: tuple[ActionSpec, ...] = (), immediate: bool = False) -> bool:
    """True when a new row was written, False when `dedupe_key` was already queued. `immediate` skips quiet hours: only for what the user is waiting for."""
    cursor = tx.execute(
        "INSERT OR IGNORE INTO notifications (id, user_id, kind, dedupe_key, text, embed_json, actions_json, immediate, status, next_attempt_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (str(uuid4()), user_id, kind, dedupe_key, text[:MAX_TEXT], _encode_embed(embed),
         json.dumps([asdict(a) for a in actions]) if actions else None, 1 if immediate else 0, _iso(now), _iso(now)))
    return cursor.rowcount == 1


class NotificationQueue:
    def __init__(self, db_path: str | Path | None = None, *, core: CoreDatabase | None = None) -> None:
        self._core = core if core is not None else CoreDatabase(Path(db_path))

    def enqueue(self, *, user_id: str, kind: str, dedupe_key: str, text: str, now: datetime, embed: EmbedSpec | None = None,
                actions: tuple[ActionSpec, ...] = (), immediate: bool = False) -> bool:
        def write(tx: Transaction, _s: object) -> bool:
            return enqueue_in_transaction(tx, user_id=user_id, kind=kind, dedupe_key=dedupe_key, text=text, now=now, embed=embed, actions=actions,
                                          immediate=immediate)

        return apply_transition(self._core, write=write)

    def claim_due(self, now: datetime, *, quiet: bool, limit: int = 20) -> list[Notification]:
        """Oldest first. During quiet hours only `immediate` rows leave the queue; the others simply wait, which costs them no attempt."""
        def write(tx: Transaction, _s: object) -> list:
            tx.execute("UPDATE notifications SET status = 'pending', claimed_at = NULL WHERE status = 'sending' AND claimed_at <= ?", (_iso(now - STALE_CLAIM),))
            gate = "AND immediate = 1" if quiet else ""
            rows = tx.fetch_all(f"SELECT * FROM notifications WHERE status = 'pending' AND next_attempt_at <= ? {gate} ORDER BY created_at, rowid LIMIT ?",
                                (_iso(now), limit))
            for row in rows:
                tx.execute("UPDATE notifications SET status = 'sending', claimed_at = ? WHERE id = ?", (_iso(now), row["id"]))
            return rows

        return [Notification(id=r["id"], user_id=r["user_id"], kind=r["kind"], text=r["text"], embed=_decode_embed(r["embed_json"]),
                             actions=_decode_actions(r["actions_json"]), immediate=bool(r["immediate"]), attempts=r["attempts"])
                for r in apply_transition(self._core, write=write)]

    def record(self, notification: Notification, result: DeliveryResult, now: datetime) -> None:
        """`DELIVERED` is final. Anything else is retried with a rising delay until MAX_ATTEMPTS, then kept as `failed`: never deleted, for the audit."""
        attempts = notification.attempts + 1

        def write(tx: Transaction, _s: object) -> None:
            if result is DeliveryResult.DELIVERED:
                tx.execute("UPDATE notifications SET status = 'delivered', attempts = ?, delivered_at = ?, last_outcome = ?, claimed_at = NULL WHERE id = ?",
                           (attempts, _iso(now), result.value, notification.id))
            elif attempts >= MAX_ATTEMPTS:
                tx.execute("UPDATE notifications SET status = 'failed', attempts = ?, last_outcome = ?, claimed_at = NULL WHERE id = ?",
                           (attempts, result.value, notification.id))
            else:
                delay = RETRY_DELAYS[attempts - 1] if attempts <= len(RETRY_DELAYS) else LATER_DELAY
                tx.execute("UPDATE notifications SET status = 'pending', attempts = ?, next_attempt_at = ?, last_outcome = ?, claimed_at = NULL WHERE id = ?",
                           (attempts, _iso(now + delay), result.value, notification.id))

        apply_transition(self._core, write=write)

    def counts(self) -> dict[str, int]:
        with self._core.transaction() as tx:
            return {r["status"]: r["n"] for r in tx.fetch_all("SELECT status, COUNT(*) AS n FROM notifications GROUP BY status")}
