"""
toy_control/session.py

The bot runs the Deep Diver during a task (owner, 2026-11-17: option 1, "build the first one"). He presses "It's in" on a task that uses
the Deep Diver; the bot then plays a random pattern for the task's own time and always stops at the end, on "Stop", on an error or when
the bot shuts down. It never starts on its own, and it is only ever reached from that button. Owner, 2026-11-17 ("more intelligently
done"): a session is shaped (warm-up, build, teasing, finish), fits the task (`profile_for`) and is built around his own level, which his
answer after each session moves (`store.py`).

Safety in code: one session at a time; at most MAX_MINUTES; levels inside 0.05-1.0 (not alone: at most 0.25); every session ends with
StopDeviceCmd in a `finally`. The pattern is drawn up front, so tests check it without a toy. The session runs on its own thread with its own
event loop, so a slow or missing Intiface never blocks the bot.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Callable

from infrastructure.decision_log import toy_log
from toy_control.client import DEFAULT_URL, IntifaceClient, ToyError, pick_device

__all__ = ["ToySessions", "plan_pattern", "profile_for", "MAX_MINUTES", "LEVELS", "PROFILES"]

logger = logging.getLogger("ai_coach_keyholder.toy_control")

MAX_MINUTES = 90
DISCREET_MAX = 0.25                       # not alone: quiet enough not to be heard
FLOOR = 0.05                              # the "almost nothing" of a tease
PROFILES = ("default", "prostate", "wear", "discreet")
LEVELS = {"alone": (FLOOR, 1.0), "discreet": (FLOOR, DISCREET_MAX)}
SCAN_SECONDS = 10.0
# Owner, 2026-11-23: "Pause" and "Continue" on a running session; the time left waits while paused. A session left paused this long ends
# (the toy is already off), so a forgotten pause never holds the one session slot for days.
MAX_PAUSED_SECONDS = 12 * 3600
# Live, 2026-11-23: in a 3.7-minute quiet step nothing was sent, Intiface's WebSocket pings went unanswered ("No pongs received") and it dropped
# the bot. The connection is now read all the time (`_aiohttp_connect`), and the current command is sent again this often, so neither the
# link nor the toy (the owner: it turns itself off after about 5 idle minutes) ever sits idle.
KEEPALIVE_SECONDS = 30.0
# Owner, 2026-11-23: "at the start of each session I would like some confirmation that it works right. Something like a short beep."
CHECK_BUZZ = (0.4, 0.6)                   # level, seconds; then off for a moment before the session itself


def profile_for(catalog_key: str, mode: str, minutes: float) -> str:
    """Owner, 2026-11-17: the session fits the task. Not alone: gentle waves; prostate training: slow build, long holds; a long wear while he
    does something else: mostly off, surprise bursts; anything else: warm-up, build, teasing, finish."""
    if mode != "alone":
        return "discreet"
    if catalog_key == "prostate_training":
        return "prostate"
    if catalog_key.startswith("wear_") or minutes >= 60:
        return "wear"
    return "default"


class _Steps:
    def __init__(self, total: float, cap: float) -> None:
        self.steps: list[tuple[float, float]] = []
        self.left, self.cap = total, cap

    def add(self, level: float, seconds: float) -> None:
        seconds = min(float(seconds), self.left)
        if seconds <= 0:
            return
        level = 0.0 if level <= 0 else round(max(FLOOR, min(self.cap, level)), 2)
        self.steps.append((level, seconds))
        self.left -= seconds


# "Steps" (owner, 2026-11-17: "yesterday I really liked the steps" in the Satisfyer app): four bursts, each stronger, a gap after each, then
# back to the lowest. Measured from his screen recording of the app: one burst and its gap take 1 s (about 0.5 s on, 0.5 s off), the peaks at
# about 40, 60, 80 and 100 % of the top.
STEP_ON, STEP_OFF = 0.5, 0.5
STEP_LEVELS = (0.4, 0.6, 0.8, 1.0)


def _steps(out: _Steps, rng: random.Random, top: float, *, sets: int | None = None) -> None:
    """The app's "Steps", repeated: one set is 4 s."""
    for _ in range(sets or rng.randint(4, 8)):
        for f in STEP_LEVELS:
            out.add(top * f, STEP_ON)
            out.add(0.0, STEP_OFF)


def _burst(out: _Steps, rng: random.Random, base: float, seconds: float) -> None:
    """Owner, 2026-11-23: "the bursts would be more unpredictable if they weren't the same intensity the whole time". Each burst takes a
    shape he can't guess (a climb, a drop, his steps, pulses, a wave or random jumps) and changes level every 1-4 s, around his own level."""
    shape = rng.choice(("climb", "drop", "steps", "pulse", "wave", "jumps"))
    lo, hi = base * 0.5, base * 1.3
    end = max(0.0, out.left - seconds)                     # never past the session's end (that looped forever)
    if shape == "steps":
        while out.left > end:
            for f in STEP_LEVELS:
                out.add(hi * f, min(STEP_ON, out.left - end))
                out.add(0.0, min(STEP_OFF, out.left - end))
        return
    if shape == "pulse":
        while out.left > end:                               # each pulse its own strength
            out.add(rng.uniform(lo, hi), min(1.0, out.left - end))
            out.add(0.0, min(1.0, out.left - end))
        return
    k = 0
    while out.left > end:
        part = min(float(rng.randint(1, 4)), out.left - end)
        done = 1 - (out.left - end) / seconds
        if shape == "climb":
            level = lo + (hi - lo) * done
        elif shape == "drop":
            level = hi - (hi - lo) * done
        elif shape == "wave":
            level = (lo, (lo + hi) / 2, hi, (lo + hi) / 2)[k % 4]
        else:
            level = rng.uniform(lo, hi)
        out.add(level, part)
        k += 1


def _surprises(steps: list[tuple[float, float]], rng: random.Random, base: float, cap: float) -> list[tuple[float, float]]:
    """Owner, 2026-11-17 (the app): "once a few minutes it changed, stop for a few seconds, then turned on for a few seconds ... so I don't get
    used to one thing". Every 2-4 minutes: off for 3-8 s, then 2-5 s at a level he can't predict, then on as planned. The total stays the same:
    the inserted seconds are taken off the end."""
    total = sum(s for _, s in steps)
    out: list[tuple[float, float]] = []
    elapsed, nxt = 0.0, float(rng.randint(120, 240))
    for level, seconds in steps:
        if elapsed >= nxt and elapsed < total - 60:              # never in the last minute: the finish stays the finish
            out.append((0.0, float(rng.randint(3, 8))))
            out.append((round(max(FLOOR, min(cap, base * rng.uniform(0.4, 1.2))), 2), float(rng.randint(2, 5))))
            nxt = elapsed + rng.randint(120, 240)
        out.append((level, seconds))
        elapsed += seconds
    trimmed, left = [], total
    for level, seconds in out:
        if left <= 0:
            break
        trimmed.append((level, min(seconds, left)))
        left -= seconds
    return trimmed


def plan_pattern(rng: random.Random, minutes: float, mode: str, *, profile: str | None = None, base: float = 0.5) -> list[tuple[float, float]]:
    """(level, seconds) steps filling `minutes` (at most MAX_MINUTES); level 0 is off. `base` is his own level (toy_control/store.py), the
    strength the session is built around."""
    total = max(0.0, min(float(minutes), MAX_MINUTES)) * 60
    profile = profile or ("discreet" if mode != "alone" else "default")
    cap = DISCREET_MAX if mode != "alone" or profile == "discreet" else 1.0
    base = max(0.1, min(1.0, float(base)))
    out = _Steps(total, cap)
    low = max(FLOOR, base * 0.3)
    if profile == "discreet":
        top = min(DISCREET_MAX, max(0.1, base * 0.4))
        while out.left > 0:                                       # gentle waves: up over three steps, down over two
            for f in (0.4, 0.7, 1.0, 0.7, 0.4):
                out.add(top * f, rng.randint(30, 60))
    elif profile == "wear":
        while out.left > 0:                                       # mostly off; now and then a burst he can't predict
            out.add(0.0, rng.randint(60, 240))                     # owner, 2026-11-23: 1-4 minutes quiet (was 3-10)
            _burst(out, rng, base, rng.randint(10, 40))
    elif profile == "prostate":
        warm, finish = total * 0.2, total * 0.15
        while out.left > total - warm:                            # slow warm-up
            done = 1 - (out.left - (total - warm)) / warm
            out.add(low + (base * 0.7 - low) * done, 45)
        while out.left > finish:                                  # long, steady holds, now and then a dip
            out.add(base * rng.choice((0.8, 0.9, 1.0)), rng.randint(60, 120))
            if rng.random() < 0.25:
                out.add(low, 30)
            elif rng.random() < 0.3:
                _steps(out, rng, base)
        while out.left > 0:
            out.add(base * 1.1, 60)
    else:
        warm, build, tease = total * 0.1, total * 0.4, total * 0.3
        while out.left > total - warm:                            # warm-up: low and slow
            done = 1 - (out.left - (total - warm)) / warm
            out.add(low + (base * 0.6 - low) * done, 30)
        k = 0
        while out.left > total - warm - build:                    # build: steps (his favourite), each set a little higher, a short rest
            done = 1 - (out.left - (total - warm - build)) / build
            top = base * (0.6 + 0.4 * done)
            motif = rng.choice(("steps", "steps", "waves", "pulse"))   # never the same thing for long (owner, 2026-11-17)
            if motif == "steps":
                _steps(out, rng, top)
            elif motif == "waves":
                for f in (0.5, 0.75, 1.0, 0.75, 0.5):
                    out.add(top * f, rng.randint(4, 8))
            else:
                for _ in range(rng.randint(6, 12)):
                    out.add(top, 1.0)
                    out.add(0.0, 1.0)
            if k % 2:
                out.add(base * 0.5, rng.randint(10, 20))
            k += 1
        while out.left > total - warm - build - tease:            # teasing: steps up past his level, then almost nothing, and wait
            _steps(out, rng, base * 1.15, sets=rng.randint(4, 6))
            out.add(FLOOR, rng.randint(20, 60))
        while out.left > 0:                                       # finish: the strongest stretch
            out.add(base * 1.1 + rng.choice((-0.05, 0.0, 0.05)), 30)
    return _surprises(out.steps, rng, base, cap) if profile in ("default", "prostate") else out.steps


class ToySessions:
    """`connect(url)` -> async context manager yielding (send, recv); default aiohttp. `on_problem(text)` is told when the toy could not be
    reached (called on the session's thread)."""

    def __init__(self, *, url: str = DEFAULT_URL, connect=None, rng: random.Random | None = None,
                 start_intiface: Callable[[], bool] | None = None) -> None:
        """`start_intiface()` (owner, 2026-11-25): starts Intiface when nothing answers; True when its server is up. None: never started."""
        self._url = url
        self._start_intiface = start_intiface
        self._reached = False
        self._connect = connect or _aiohttp_connect
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._pause: threading.Event | None = None
        self._played = 0.0                      # seconds of the pattern played in the current / last session (not paused)
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, minutes: float, mode: str, *, profile: str | None = None, base: float = 0.5,
              on_problem: Callable[[str], None] = lambda text: None, on_done: Callable[[], None] = lambda: None,
              on_started: Callable[[], None] = lambda: None) -> bool:
        """False when a session is already running. Returns at once; the session runs on its own thread. `on_done()` after a session that
        reached the toy (also when stopped), `on_problem(text)` when it could not."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            stop, pause = threading.Event(), threading.Event()
            pattern = plan_pattern(self._rng, minutes, mode, profile=profile, base=base)
            toy_log("Session planned.", minutes=minutes, mode=mode, profile=profile, base=base, steps=len(pattern),
                    top=max((lv for lv, _ in pattern), default=0.0))
            thread = threading.Thread(target=self._run, args=(pattern, stop, on_problem, on_done, pause, on_started), name="toy-session",
                                      daemon=True)
            self._stop, self._pause, self._thread = stop, pause, thread
        thread.start()
        return True

    def stop(self) -> bool:
        with self._lock:
            stop, thread = self._stop, self._thread
        if stop is None or thread is None or not thread.is_alive():
            return False
        stop.set()
        toy_log("Stop pressed.")
        thread.join(timeout=10)
        return True

    def pause(self) -> bool:
        """Owner, 2026-11-23: the toy goes off and the session's time stops. False when nothing is running or it is already paused."""
        with self._lock:
            pause, thread = self._pause, self._thread
            if pause is None or thread is None or not thread.is_alive() or pause.is_set():
                return False
            pause.set()
        toy_log("Pause pressed.")
        return True

    def resume(self) -> bool:
        """The session goes on where it stopped. False when nothing is paused."""
        with self._lock:
            pause, thread = self._pause, self._thread
            if pause is None or thread is None or not thread.is_alive() or not pause.is_set():
                return False
            pause.clear()
        toy_log("Continue pressed.")
        return True

    def played_seconds(self) -> float:
        """Live, 2026-11-23: how much of the session ran, so "Try again" goes on with only what was left."""
        return self._played

    def paused(self) -> bool:
        with self._lock:
            return self._pause is not None and self._thread is not None and self._thread.is_alive() and self._pause.is_set()

    def _run(self, pattern, stop: threading.Event, on_problem, on_done, pause: threading.Event | None = None,
             on_started: Callable[[], None] = lambda: None) -> None:
        try:
            self._reached = False
            try:
                asyncio.run(self._session(pattern, stop, pause or threading.Event(), on_started))
            except OSError:
                # Owner, 2026-11-25: Intiface not running -> the bot starts it and tries once more. Only before the toy was reached, so a link
                # lost mid-session never starts the pattern over.
                if self._reached or self._start_intiface is None or stop.is_set() or not self._start_intiface():
                    raise
                asyncio.run(self._session(pattern, stop, pause or threading.Event(), on_started))
            on_done()
        except (ToyError, OSError, LookupError) as exc:
            logger.warning("Toy session could not run: %s", exc)
            toy_log("Session could not run.", error=str(exc))
            on_problem(str(exc))
        except Exception as exc:                                    # aiohttp errors and the like: never take the bot down
            logger.exception("Toy session failed.")
            toy_log("Session failed.", error=exc.__class__.__name__)
            on_problem(exc.__class__.__name__)

    async def _session(self, pattern, stop: threading.Event, pause: threading.Event, on_started: Callable[[], None] = lambda: None) -> None:
        async with self._connect(self._url) as (send, recv):
            client = IntifaceClient(send, recv)
            info = await client.handshake()
            self._reached = True
            ping_every = float(info.get("MaxPingTime") or 0) / 1000 / 2 if isinstance(info, dict) else 0.0
            devices = await client.device_list() or await client.scan(SCAN_SECONDS)
            device = pick_device(devices)
            if device is None:
                raise LookupError("the toy was not found")
            logger.info("Toy session started: %d steps.", len(pattern))
            toy_log("Session started.", device=device.name)
            self._played = 0.0
            paused_for, began = 0.0, time.monotonic()
            last_sent = last_ping = time.monotonic()

            async def keep_alive(level: float, *, off: bool) -> None:
                nonlocal last_sent, last_ping
                now_ = time.monotonic()
                if ping_every > 0 and now_ - last_ping >= ping_every:
                    await client.ping()
                    last_ping = now_
                if now_ - last_sent >= KEEPALIVE_SECONDS:
                    if off or level <= 0:
                        await client.stop(device)
                    else:
                        await client.vibrate(device, level)
                    last_sent = now_

            try:
                level, seconds = CHECK_BUZZ                               # the check buzz: he feels it is connected
                await client.vibrate(device, level)
                await asyncio.sleep(seconds)
                await client.stop(device)
                toy_log("Check buzz.", level=level, seconds=seconds)
                await asyncio.sleep(1.0)
                on_started()
            except (ToyError, OSError):
                raise
            except Exception:
                logger.exception("The start confirmation failed; the session goes on.")
            try:
                for level, seconds in pattern:
                    left, sent = float(seconds), False
                    while left > 0 and not stop.is_set():
                        if pause.is_set():                                   # owner, 2026-11-23: off, and the step's time waits
                            if sent:
                                await client.stop(device)
                                toy_log("Off (paused).")
                                sent = False
                            if paused_for >= MAX_PAUSED_SECONDS:
                                toy_log("Paused too long: session ended.", paused_seconds=round(paused_for))
                                stop.set()
                                break
                            await keep_alive(0.0, off=True)
                            await asyncio.sleep(0.25)
                            paused_for += 0.25
                            continue
                        if not sent:
                            if level > 0:
                                await client.vibrate(device, level)
                            else:
                                await client.stop(device)
                            toy_log(f"Level {level:.2f}." if level > 0 else "Off.", seconds=round(left, 1))
                            sent = True
                            last_sent = time.monotonic()
                        await keep_alive(level, off=False)
                        started = time.monotonic()
                        await asyncio.sleep(min(0.5, left))
                        ran = time.monotonic() - started
                        left -= ran
                        self._played += ran
                    if stop.is_set():
                        break
            finally:
                await client.stop(device)
                logger.info("Toy session ended.")
                toy_log("Session ended, toy stopped.", stopped_early=stop.is_set(), paused_seconds=round(paused_for),
                        wall_seconds=round(time.monotonic() - began))


class _aiohttp_connect:
    def __init__(self, url: str) -> None:
        self._url = url

    async def __aenter__(self):
        import aiohttp
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url, autoping=True, heartbeat=10.0)
        queue: asyncio.Queue = asyncio.Queue()

        async def reader() -> None:
            # Live, 2026-11-23: aiohttp answers the server's pings only inside receive(), so the socket is read all the time, not only
            # while waiting for an answer.
            try:
                while True:
                    msg = await self._ws.receive()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await queue.put(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        toy_log("Connection to Intiface closed.", kind=str(msg.type))
                        await queue.put(None)
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                toy_log("Connection to Intiface failed.", error=str(exc))
                await queue.put(None)

        self._reader = asyncio.create_task(reader())

        async def recv() -> str:
            data = await queue.get()
            if data is None:
                await queue.put(None)                   # every later read fails too
                raise ToyError("the connection closed")
            return data

        return self._ws.send_str, recv

    async def __aexit__(self, *exc) -> None:
        self._reader.cancel()
        await self._ws.close()
        await self._session.close()
