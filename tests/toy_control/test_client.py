"""The Intiface client and the owner's toy test, against a fake Buttplug server (no network, no toy)."""

from __future__ import annotations

import asyncio
import json

from toy_control.client import TEST_MAX_LEVEL, TEST_MAX_SECONDS, IntifaceClient, pick_device, run_test, Device


class FakeServer:
    def __init__(self, devices):
        self.devices, self.sent, self.queue = devices, [], asyncio.Queue()

    async def send(self, text):
        (kind, body), = json.loads(text)[0].items()
        self.sent.append((kind, body))
        mid = body["Id"]
        if kind == "RequestServerInfo":
            reply = {"ServerInfo": {"Id": mid, "ServerName": "Intiface Central", "MessageVersion": 3, "MaxPingTime": 0}}
        elif kind == "RequestDeviceList":
            reply = {"DeviceList": {"Id": mid, "Devices": self.devices}}
        else:
            reply = {"Ok": {"Id": mid}}
        await self.queue.put(json.dumps([reply]))

    async def recv(self):
        return await self.queue.get()


DIVER = {"DeviceIndex": 0, "DeviceName": "Satisfyer Deep Diver", "DeviceMessages": {"ScalarCmd": [{"StepCount": 100, "ActuatorType": "Vibrate"}]}}


def test_one_capped_buzz_and_always_a_stop(monkeypatch) -> None:
    async def no_sleep(_s):
        return None

    monkeypatch.setattr("toy_control.client.asyncio.sleep", no_sleep)
    server = FakeServer([DIVER])
    said = []
    assert asyncio.run(run_test(IntifaceClient(server.send, server.recv), level=0.9, seconds=60, say=said.append)) is True
    kinds = [k for k, _ in server.sent]
    assert kinds == ["RequestServerInfo", "RequestDeviceList", "ScalarCmd", "StopDeviceCmd"]
    scalar = dict(server.sent)["ScalarCmd"]["Scalars"][0]
    assert scalar == {"Index": 0, "Scalar": TEST_MAX_LEVEL, "ActuatorType": "Vibrate"}
    assert f"{TEST_MAX_SECONDS:g} s" in said[1] and said[-1] == "Stopped."


def test_nothing_runs_without_a_clear_toy(monkeypatch) -> None:
    async def no_sleep(_s):
        return None

    monkeypatch.setattr("toy_control.client.asyncio.sleep", no_sleep)
    server = FakeServer([])
    assert asyncio.run(run_test(IntifaceClient(server.send, server.recv), level=0.2, seconds=1, say=lambda t: None)) is False
    assert "ScalarCmd" not in [k for k, _ in server.sent]
    assert pick_device([Device(0, "A", 1), Device(1, "B", 1)]) is None
    assert pick_device([Device(0, "A", 1), Device(1, "SF Deep Diver", 1)]).name == "SF Deep Diver"


def test_the_cli_needs_yes() -> None:
    import io
    from cli.main import main
    out = io.StringIO()
    assert main(["toy-test"], out=out) == 2 and "--yes" in out.getvalue()
