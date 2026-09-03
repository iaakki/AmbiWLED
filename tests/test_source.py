"""Source polling against a fake TV.  No real TV required."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from aiohttp import web

from ambiwled.source import SourcePoller
from conftest import ambilight_payload


class FakeTV:
    """Serves the JointSPACE endpoints the bridge actually uses."""

    def __init__(self, api_major=6, payload=None, fail=False, power="On",
                 serve_power=True):
        self.api_major = api_major
        self.payload = payload or ambilight_payload()
        self.fail = fail
        self.power = power
        self.serve_power = serve_power
        self.requests = 0
        self.paths = []

    async def start(self):
        app = web.Application()
        app.router.add_get("/{ver}/system", self.system)
        app.router.add_get("/{ver}/ambilight/{endpoint}", self.ambilight)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        await self.runner.cleanup()

    async def system(self, request):
        if str(request.match_info["ver"]) != str(self.api_major):
            return web.Response(status=404)
        return web.json_response({"name": "FakeTV", "api_version": {"Major": self.api_major}})

    async def ambilight(self, request):
        endpoint = request.match_info["endpoint"]
        if endpoint in ("power", "mode"):
            if not self.serve_power:
                return web.Response(status=404)
            if self.fail:
                return web.Response(status=500)
            return web.json_response(
                {"power": self.power} if endpoint == "power" else {"current": "internal"})
        self.requests += 1
        self.paths.append(request.path)
        if self.fail:
            return web.Response(status=500)
        return web.json_response(self.payload)


# -- payload parsing (pure, no I/O) -----------------------------------------

def test_parse_reads_layout_from_the_payload_not_assumptions():
    parsed = SourcePoller._parse(ambilight_payload())
    layout, zones = parsed
    assert layout.sides == [("left", 5), ("top", 11), ("right", 5)]
    assert layout.total == 21
    assert zones.shape == (21, 3)


def test_parse_handles_a_different_zone_count():
    """Zone counts may vary by source or Ambilight style (spec 9.3)."""
    layout, zones = SourcePoller._parse(
        ambilight_payload(sides=(("left", 8), ("top", 16), ("right", 8))))
    assert layout.total == 32
    assert zones.shape == (32, 3)


def test_parse_handles_a_tv_that_does_report_bottom():
    layout, _ = SourcePoller._parse(
        ambilight_payload(sides=(("left", 5), ("top", 11), ("right", 5), ("bottom", 11))))
    assert dict(layout.sides)["bottom"] == 11


def test_parse_orders_zones_numerically_not_lexically():
    """Keys are strings: '10' must not sort before '2'."""
    payload = ambilight_payload(
        sides=(("top", 11),),
        value=lambda side, i: {"r": i, "g": 0, "b": 0})
    _, zones = SourcePoller._parse(payload)
    assert [int(v) for v in zones[:, 0]] == list(range(11))


def test_parse_rejects_junk():
    assert SourcePoller._parse({}) is None
    assert SourcePoller._parse({"layer1": {}}) is None
    assert SourcePoller._parse("not a dict") is None


def test_parse_falls_back_to_another_layer_key():
    payload = {"layer2": ambilight_payload()["layer1"]}
    layout, _ = SourcePoller._parse(payload)
    assert layout.total == 21


# -- live polling ------------------------------------------------------------

async def poller_for(tv, **overrides):
    cfg = {"source": {"tv_ip": "127.0.0.1", "port": tv.port, "api_version": None,
                      "endpoint": "processed", "poll_hz": 50.0, "request_timeout_s": 1.0,
                      "fail_threshold": 2, "off_probe_interval_s": 0.2,
                      "adaptive_backoff": False}}
    cfg["source"].update(overrides)
    frames = []
    return SourcePoller(cfg, lambda z, l: frames.append((z, l))), frames


@pytest.fixture
async def tv():
    t = await FakeTV().start()
    yield t
    await t.stop()


async def test_detects_api_v6_and_streams(tv):
    p, frames = await poller_for(tv)
    await p.start()
    await _wait_for(lambda: len(frames) >= 3)
    await p.stop()

    assert p.state == "streaming"
    assert p.layout.sides == [("left", 5), ("top", 11), ("right", 5)]
    assert frames[0][0].shape == (21, 3)
    assert p.effective_hz > 0
    assert all("/6/ambilight/processed" in path for path in tv.paths)


async def test_api_v1_uses_the_v1_paths():
    t = await FakeTV(api_major=1).start()
    try:
        p, frames = await poller_for(t)
        await p.start()
        await _wait_for(lambda: len(frames) >= 1, timeout=5)
        await p.stop()
        assert p.api_version == 1
        assert all("/1/ambilight/" in path for path in t.paths)
    finally:
        await t.stop()


async def test_failures_flip_to_tv_off_and_stop_emitting():
    t = await FakeTV(fail=True).start()
    try:
        p, frames = await poller_for(t)
        await p.start()
        await _wait_for(lambda: p.state == "tv_off", timeout=5)
        await p.stop()
        assert frames == []
        assert p.consecutive_failures >= 2
        assert p.effective_hz == 0.0
    finally:
        await t.stop()


async def test_recovers_when_the_tv_comes_back():
    t = await FakeTV(fail=True).start()
    try:
        p, frames = await poller_for(t)
        await p.start()
        await _wait_for(lambda: p.state == "tv_off", timeout=5)
        t.fail = False                              # TV wakes up
        await _wait_for(lambda: p.state == "streaming", timeout=5)
        await p.stop()
        assert frames, "must resume delivering frames on its own"
    finally:
        await t.stop()


async def test_mid_stream_layout_change_is_picked_up(tv):
    p, frames = await poller_for(tv)
    await p.start()
    await _wait_for(lambda: len(frames) >= 2)
    tv.payload = ambilight_payload(sides=(("left", 6), ("top", 14), ("right", 6)))
    await _wait_for(lambda: p.layout.total == 26, timeout=5)
    await p.stop()
    assert frames[-1][0].shape == (26, 3)


# -- helpers -----------------------------------------------------------------

async def _wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within %.1fs" % timeout)
