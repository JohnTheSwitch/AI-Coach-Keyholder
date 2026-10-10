"""
system/penalty_policy.py

The composition-root pieces of the dynamic Penalty Window policy (v0.39): same-transaction READ-ONLY readers of the
operating mode and of a domain's Trust score, and the factory that builds the Penalty Engine used by the event
consumers. Reading another module's table inside the consuming transaction is the documented consumer pattern
(a consumer cannot call a transaction-opening public API); nothing is written here.
"""

from __future__ import annotations

from infrastructure.database import Database as CoreDatabase
from infrastructure.database import Transaction
from penalty_engine.duration import Mode
from penalty_engine.policy import DynamicWindowPolicy
from penalty_engine.repository import PenaltyEngine

__all__ = ["make_penalty_engine", "read_mode", "read_trust"]


def read_mode(tx: Transaction) -> Mode:
    row = tx.fetch_one("SELECT current_mode FROM operating_mode_state WHERE id = 1")
    return Mode.ADVANCED if row is not None and row["current_mode"] == "advanced" else Mode.STANDARD


def read_trust(tx: Transaction, domain_id: str) -> float | None:
    row = tx.fetch_one("SELECT score FROM trust_domain_state WHERE domain_id = ?", (domain_id,))
    return None if row is None else float(row["score"])


def make_penalty_engine(core: CoreDatabase) -> PenaltyEngine:
    return PenaltyEngine(core.db_path, core=core,
                         window_policy=DynamicWindowPolicy(read_mode=read_mode, read_trust=read_trust))
