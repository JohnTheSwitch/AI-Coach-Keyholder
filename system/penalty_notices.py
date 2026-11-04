"""
system/penalty_notices.py

Tells the wearer what the Penalty Engine decided. These are outbox consumers, so the notice is queued in the same transaction that consumes the event: a
consequence can never happen unannounced (philosophy 3.8: explainable), and never twice. The texts say what happened and what can be done; they never carry a
Trust value, a level or a release time.

A freeze for an exemption is announced by the exemption itself, and an Emergency Override needs no message from this system at all.
"""

from __future__ import annotations

from infrastructure.database import Transaction
from notifications.queue import enqueue_in_transaction

__all__ = ["penalty_notice_handlers", "WARNING_TEXT", "WINDOW_STARTED_TEXT", "WINDOW_RESUMED_TEXT", "WINDOW_ENDED_TEXT"]

WARNING_TEXT = "A confirmed rule problem was recorded as a Warning. No Penalty Window started and your lock is unchanged."
WINDOW_STARTED_TEXT = ("A Penalty Window has started because of a confirmed rule problem. Your tokens can't be redeemed until it ends. Health and safety always "
                       "come first, and you can ask for an exemption at any time if life needs it. Ask me about your penalty window for how long is left.")
WINDOW_RESUMED_TEXT = "Your Penalty Window is running again from where it paused."
WINDOW_ENDED_TEXT = "Your Penalty Window has ended."


def _extended_text(hours: object) -> str:
    return f"Your Penalty Window was extended by {float(hours):g} h because of a new confirmed rule problem."


def penalty_notice_handlers(user_id: str) -> dict:
    """event type -> handler for `build_consumer_registry`. One wearer per database, as everywhere in the Penalty Engine."""

    def notify(tx: Transaction, event, kind: str, text: str) -> None:
        enqueue_in_transaction(tx, user_id=user_id, kind=kind, dedupe_key=f"penalty:{event.id}", text=text, now=event.occurred_at)

    def on_extended(tx: Transaction, event) -> None:
        if not event.payload.get("with_start"):                 # the incident that opens a window also runs through Extension: that is one event for the wearer
            notify(tx, event, "penalty_window", _extended_text(event.payload.get("assigned_hours", 0)))

    return {
        "penalty_warning.recorded": lambda tx, event: notify(tx, event, "penalty_warning", WARNING_TEXT),
        "penalty_window.started": lambda tx, event: notify(tx, event, "penalty_window", WINDOW_STARTED_TEXT),
        "penalty_window.extended": on_extended,
        "penalty_window.resumed": lambda tx, event: notify(tx, event, "penalty_window", WINDOW_RESUMED_TEXT),
        "penalty_window.completed": lambda tx, event: notify(tx, event, "penalty_window", WINDOW_ENDED_TEXT),
    }
