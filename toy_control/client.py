"""
toy_control/client.py

A minimal Buttplug v3 client for Intiface Central's local WebSocket (owner, 2026-11-16; P6 groundwork). See README.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

__all__ = ["IntifaceClient", "Device", "ToyError", "TEST_MAX_LEVEL", "TEST_MAX_SECONDS", "DEFAULT_URL", "run_test"]

logger = logging.getLogger("ai_coach_keyholder.toy_control")

DEFAULT_URL = "ws://127.0.0.1:12345"
TEST_MAX_LEVEL = 0.5
TEST_MAX_SECONDS = 10.0
MESSAGE_VERSION = 3


class ToyError(Exception):
    pass


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    vibrators: int


def _devices(raw: list) -> list[Device]:
    out = []
    for d in raw or []:
        scalars = (d.get("DeviceMessages") or {}).get("ScalarCmd") or []
        vib = sum(1 for s in scalars if s.get("ActuatorType") == "Vibrate")
        out.append(Device(index=int(d["DeviceIndex"]), name=str(d.get("DeviceName") or "?"), vibrators=vib))
    return out


class IntifaceClient:
    """`send(text)` and `recv() -> text` are the WebSocket; injected so tests need no server."""

    def __init__(self, send: Callable[[str], Awaitable[None]], recv: Callable[[], Awaitable[str]], *, client_name: str = "AI Coach") -> None:
        self._send, self._recv, self._name = send, recv, client_name
        self._id = 0
        self.devices: dict[int, Device] = {}

    async def _call(self, kind: str, body: dict | None = None, *, timeout: float = 5.0) -> dict:
        self._id += 1
        msg_id = self._id
        await self._send(json.dumps([{kind: {"Id": msg_id, **(body or {})}}]))
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            left = end - loop.time()
            if left <= 0:
                raise ToyError(f"no answer to {kind}")
            for item in json.loads(await asyncio.wait_for(self._recv(), timeout=left)):
                (name, payload), = item.items()
                if name == "DeviceAdded":
                    dev = _devices([payload])[0]
                    self.devices[dev.index] = dev
                elif name == "DeviceRemoved":
                    self.devices.pop(int(payload.get("DeviceIndex", -1)), None)
                if payload.get("Id") == msg_id:
                    if name == "Error":
                        raise ToyError(str(payload.get("ErrorMessage") or "error"))
                    return {name: payload}

    async def handshake(self) -> dict:
        answer = await self._call("RequestServerInfo", {"ClientName": self._name, "MessageVersion": MESSAGE_VERSION})
        return answer.get("ServerInfo", {})

    async def device_list(self) -> list[Device]:
        answer = await self._call("RequestDeviceList")
        for dev in _devices(answer.get("DeviceList", {}).get("Devices", [])):
            self.devices[dev.index] = dev
        return list(self.devices.values())

    async def scan(self, seconds: float = 5.0) -> list[Device]:
        await self._call("StartScanning")
        await asyncio.sleep(seconds)
        await self._call("StopScanning")
        return await self.device_list()

    async def vibrate(self, device: Device, level: float) -> None:
        level = max(0.0, min(float(level), 1.0))
        scalars = [{"Index": i, "Scalar": level, "ActuatorType": "Vibrate"} for i in range(max(device.vibrators, 1))]
        await self._call("ScalarCmd", {"DeviceIndex": device.index, "Scalars": scalars})

    async def stop(self, device: Device) -> None:
        await self._call("StopDeviceCmd", {"DeviceIndex": device.index})


def pick_device(devices: list[Device]) -> Device | None:
    """The Satisfyer if there is one, else the only vibrating device; None when unclear (nothing runs)."""
    vibrating = [d for d in devices if d.vibrators > 0]
    for d in vibrating:
        if any(w in d.name.lower() for w in ("satisfyer", "deep diver", "sf ")):
            return d
    return vibrating[0] if len(vibrating) == 1 else None


async def run_test(client: IntifaceClient, *, level: float, seconds: float, scan_seconds: float = 5.0,
                   say: Callable[[str], None] = print) -> bool:
    """The owner's test: handshake, find the toy, one capped buzz, always stopped. True when it ran."""
    level, seconds = min(level, TEST_MAX_LEVEL), min(seconds, TEST_MAX_SECONDS)
    info = await client.handshake()
    say(f"Connected to {info.get('ServerName') or 'Intiface'}.")
    devices = await client.device_list() or await client.scan(scan_seconds)
    device = pick_device(devices)
    if device is None:
        say("No single vibrating toy found: " + (", ".join(d.name for d in devices) or "none") + ". Nothing ran.")
        return False
    say(f"Found {device.name}. Buzzing at {round(level * 100)}% for {seconds:g} s.")
    try:
        await client.vibrate(device, level)
        await asyncio.sleep(seconds)
    finally:
        await client.stop(device)
        say("Stopped.")
    return True
