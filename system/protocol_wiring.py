"""
system/protocol_wiring.py

Builds the protocol ladder (protocol/) for the owner (owner, 2026-11-16; autonomous queue A2-A4) and the address fragment for the voice.
"""

from __future__ import annotations

import logging
from datetime import timedelta

logger = logging.getLogger("ai_coach_keyholder.protocol")


def window_left_hours_for(core):
    from penalty_engine.models import PenaltyWindowStatus
    from penalty_engine.window import remaining_active_hours
    from system.penalty_policy import make_penalty_engine
    penalties = make_penalty_engine(core)

    def left(now):
        window = penalties.get_active_or_frozen_penalty_window()
        if window is None or window.status is not PenaltyWindowStatus.ACTIVE:
            return None
        return remaining_active_hours(window, now)

    return left


def build_address_provider(core):
    from conversation_engine.context_providers.address_provider import AddressContextProvider
    from obedience.service import get_verified_task_ids_since
    from protocol.service import get_protocol_state
    from infrastructure.time_format import iso
    left = window_left_hours_for(core)

    def title(user_id):
        state = get_protocol_state(core, user_id)
        return state["title"] if state and int(state["rung"]) >= 4 else None

    return AddressContextProvider(window_active=lambda now: left(now) is not None,
                                  praise_earned=lambda user_id, now: bool(get_verified_task_ids_since(core, user_id, iso(now - timedelta(hours=24)))),
                                  title=title)


def build_protocol(core, *, user_id, check_ins, window_cfg, paused, todays_task_open):
    from lines.service import LineService
    from obedience.service import get_verified_task_ids_since
    from protocol.service import ProtocolLadder, Ports
    from supervision.checkins import get_duty_misses
    lines = LineService(core, user_id=user_id)
    return ProtocolLadder(core, user_id=user_id, ports=Ports(
        local=window_cfg.local, to_utc=window_cfg.to_utc, quiet_end=window_cfg.quiet_end, paused=paused,
        open_lines=lambda: lines.open_lines(user_id), todays_task_open=todays_task_open, window_left_hours=window_left_hours_for(core),
        verified_since=lambda since: get_verified_task_ids_since(core, user_id, since),
        open_duty=check_ins.open_duty, answer_duties=check_ins.answer_duties,
        duty_misses=lambda since: get_duty_misses(core, user_id, since)))


async def protocol_loop(ladder, clock, *, interval_seconds: float = 120.0) -> None:
    import asyncio
    while True:
        try:
            await asyncio.to_thread(ladder.tick, clock.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("The protocol pass failed; it continues.")
        await asyncio.sleep(interval_seconds)
