"""Emission gating: don't send when nobody is casting or nobody is listening."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from ambiwled import config as config_mod
from ambiwled.bridge import Bridge
from ambiwled.health import TargetHealth
from test_source import FakeTV, _wait_for


class FakeWLED:
    """Answers /json/info like a WLED controller, until told not to."""

    def __init__(self):
        self.up = True
        self.hits = 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/json/info", self.info)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        await self.runner.cleanup()

    async def info(self, request):
        self.hits += 1
        if not self.up:
            raise web.HTTPServiceUnavailable()
        return web.json_response({"ver": "0.15.1", "leds": {"count": 462}})


def health_cfg(http_port, **overrides):
    cfg = {"output": {"require_online": True, "presence_check_s": 0.2,
                      "presence_timeout_s": 1.0, "presence_fail_threshold": 2,
                      "targets": [{"host": "127.0.0.1", "port": 21324, "http_port": http_port,
                                   "pixel_offset": 0, "pixel_count": 462, "enabled": True}]}}
    cfg["output"].update(overrides)
    return cfg


# -- health monitor on its own ----------------------------------------------

async def test_reachable_controller_is_online():
    wled = await FakeWLED().start()
    h = TargetHealth(health_cfg(wled.port))
    await h.start()
    try:
        await _wait_for(lambda: h.any_online() and h.status[h.hosts[0]]["online"] is True)
    finally:
        await h.stop(); await wled.stop()


async def test_unreachable_controller_goes_offline():
    h = TargetHealth(health_cfg(1))     # nothing listening
    await h.start()
    try:
        await _wait_for(lambda: h.any_online() is False, timeout=6)
        assert h.online_hosts() == []
    finally:
        await h.stop()


async def test_a_single_blip_does_not_take_it_offline():
    """Threshold of 2: one failed probe must not stop the lights."""
    wled = await FakeWLED().start()
    h = TargetHealth(health_cfg(wled.port))
    await h.start()
    try:
        await _wait_for(lambda: h.status[h.hosts[0]]["online"] is True)
        wled.up = False
        await asyncio.sleep(0.25)                   # about one probe
        assert h.any_online() is True               # still considered up
        await _wait_for(lambda: h.any_online() is False, timeout=6)
    finally:
        await h.stop(); await wled.stop()


async def test_recovery_is_detected():
    wled = await FakeWLED().start()
    h = TargetHealth(health_cfg(wled.port))
    await h.start()
    try:
        wled.up = False
        await _wait_for(lambda: h.any_online() is False, timeout=6)
        wled.up = True
        await _wait_for(lambda: h.any_online() is True, timeout=6)
    finally:
        await h.stop(); await wled.stop()


async def test_unknown_counts_as_online_so_first_frames_are_not_withheld():
    h = TargetHealth(health_cfg(1))
    assert h.any_online() is True                   # nothing probed yet
    await h.stop()


async def test_check_can_be_disabled():
    h = TargetHealth(health_cfg(1, presence_check_s=0))
    assert h.enabled is False
    assert h.any_online() is True
    await h.stop()


async def test_require_online_false_always_emits():
    h = TargetHealth(health_cfg(1, require_online=False))
    await h.start()
    try:
        await asyncio.sleep(0.6)
        assert h.any_online() is True
    finally:
        await h.stop()


# -- the gate, end to end ----------------------------------------------------

@pytest.fixture
async def wired(udp_sink):
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await FakeWLED().start()
    cfg = config_mod.default_config()
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "fail_threshold": 2, "off_probe_interval_s": 0.2,
                          "request_timeout_s": 1.0, "ambilight_power_check_s": 0.2})
    cfg["output"].update({"presence_check_s": 0.2, "presence_fail_threshold": 2,
                          "require_online": True,
                          "targets": [{"host": host, "port": port,
                                       "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462,
                                       "enabled": True}]})
    cfg["frames"]["mode"] = "passthrough"
    bridge = Bridge(cfg)
    await bridge.start()
    try:
        yield bridge, tv, wled, recv
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_emits_when_tv_casts_and_controller_answers(wired):
    bridge, tv, wled, recv = wired
    await _wait_for(lambda: bridge.should_emit(), timeout=6)
    await asyncio.sleep(0.3)
    assert recv(), "should be streaming"
    assert bridge.metrics()["ambilight_power"] is True


async def test_stops_when_ambilight_is_switched_off_on_the_tv(wired):
    """TV on, picture on, Ambilight off: the zones read black and would
    otherwise be streamed as a dark frame forever."""
    bridge, tv, wled, recv = wired
    await _wait_for(lambda: bridge.should_emit(), timeout=6)

    tv.power = "Off"
    await _wait_for(lambda: bridge.poller.ambilight_on is False, timeout=6)
    await _wait_for(lambda: not bridge.should_emit(), timeout=6)
    recv()
    await asyncio.sleep(0.4)
    assert recv() == [], "must go silent so WLED falls back"
    assert bridge.poller.state == "ambilight_off"


async def test_resumes_when_ambilight_comes_back(wired):
    bridge, tv, wled, recv = wired
    tv.power = "Off"
    await _wait_for(lambda: not bridge.should_emit(), timeout=6)
    tv.power = "On"
    await _wait_for(lambda: bridge.should_emit(), timeout=6)
    recv()
    await asyncio.sleep(0.3)
    assert recv()


async def test_stops_when_the_controller_is_unreachable(wired):
    bridge, tv, wled, recv = wired
    await _wait_for(lambda: bridge.should_emit(), timeout=6)

    wled.up = False                       # controller powered off
    await _wait_for(lambda: not bridge.should_emit(), timeout=8)
    recv()
    await asyncio.sleep(0.4)
    assert recv() == [], "no point shouting at a controller that is gone"


async def test_resumes_when_the_controller_returns(wired):
    bridge, tv, wled, recv = wired
    wled.up = False
    await _wait_for(lambda: not bridge.should_emit(), timeout=8)
    wled.up = True
    await _wait_for(lambda: bridge.should_emit(), timeout=8)
    recv()
    await asyncio.sleep(0.3)
    assert recv()


async def test_metrics_expose_both_signals(wired):
    bridge, tv, wled, recv = wired
    await _wait_for(lambda: bridge.should_emit(), timeout=6)
    m = bridge.metrics()
    assert m["ambilight_power"] is True
    assert m["ambilight_mode"] == "internal"
    assert m["targets_online"]
    assert m["emitting"] is True


# -- controller telemetry ----------------------------------------------------

class TelemetryWLED(FakeWLED):
    """Reports the fields WLED 0.16 exposes about its own rendering."""

    def __init__(self, fps=120, pwr=672, maxpwr=0, live=True, lm="UDP"):
        super().__init__()
        self.fps, self.pwr, self.maxpwr, self.live, self.lm = fps, pwr, maxpwr, live, lm

    async def info(self, request):
        self.hits += 1
        if not self.up:
            raise web.HTTPServiceUnavailable()
        return web.json_response({
            "ver": "16.0.1", "name": "WLED", "live": self.live, "lm": self.lm,
            "uptime": 204, "freeheap": 113976,
            "leds": {"count": 462, "fps": self.fps, "pwr": self.pwr, "maxpwr": self.maxpwr},
        })


async def test_controller_telemetry_is_collected():
    wled = await TelemetryWLED().start()
    h = TargetHealth(health_cfg(wled.port))
    await h.start()
    try:
        await _wait_for(lambda: h.info.get(h.hosts[0], {}).get("fps") is not None, timeout=6)
        info = h.info[h.hosts[0]]
        assert info["version"] == "16.0.1"
        assert info["fps"] == 120
        assert info["power_ma"] == 672
        assert info["realtime_source"] == "UDP"
        assert info["brightness_limited"] is False
    finally:
        await h.stop(); await wled.stop()


async def test_abl_limiting_is_detected():
    """Draw at or above the cap means WLED is scaling brightness down."""
    wled = await TelemetryWLED(pwr=3927, maxpwr=3927).start()
    h = TargetHealth(health_cfg(wled.port))
    await h.start()
    try:
        await _wait_for(lambda: h.info.get(h.hosts[0], {}).get("power_ma") is not None, timeout=6)
        assert h.info[h.hosts[0]]["brightness_limited"] is True
    finally:
        await h.stop(); await wled.stop()


async def test_a_non_wled_responder_still_counts_as_present():
    """Presence must not depend on the response being parseable WLED JSON."""
    class Plain(FakeWLED):
        async def info(self, request):
            return web.Response(text="not json")
    plain = await Plain().start()
    h = TargetHealth(health_cfg(plain.port))
    await h.start()
    try:
        await _wait_for(lambda: h.any_online() is True, timeout=6)
    finally:
        await h.stop(); await plain.stop()


async def test_another_source_driving_the_strip_is_flagged(udp_sink):
    """If something else grabs realtime control, the symptom is flicker; say so."""
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await TelemetryWLED(lm="E1.31").start()
    cfg = config_mod.default_config()
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "ambilight_power_check_s": 0.2, "request_timeout_s": 1.0})
    cfg["output"].update({"presence_check_s": 0.2,
                          "targets": [{"host": host, "port": port, "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462, "enabled": True}]})
    bridge = Bridge(cfg)
    await bridge.start()
    try:
        await _wait_for(lambda: bridge._emitting_since is not None, timeout=8)
        bridge._emitting_since -= 31          # past the settling window
        await _wait_for(lambda: bridge.metrics()["output_conflict"] is not None, timeout=8)
        assert "E1.31" in bridge.metrics()["output_conflict"]
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_local_override_is_reported(udp_sink):
    """WLED can be told from its own app to ignore realtime and run an effect.
    That is legitimate, but the user should be able to see why the ceiling
    stopped following the TV."""
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await TelemetryWLED(live=False, lm=None).start()
    cfg = config_mod.default_config()
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "ambilight_power_check_s": 0.2, "request_timeout_s": 1.0})
    cfg["output"].update({"presence_check_s": 0.2,
                          "targets": [{"host": host, "port": port, "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462, "enabled": True}]})
    bridge = Bridge(cfg)
    await bridge.start()
    try:
        await _wait_for(lambda: bridge._emitting_since is not None, timeout=8)
        assert bridge.metrics()["output_conflict"] is None, "must not flag immediately"
        bridge._emitting_since -= 31          # pretend we have been emitting a while
        await _wait_for(lambda: bridge.metrics()["output_conflict"] is not None, timeout=6)
        assert "overridden locally" in bridge.metrics()["output_conflict"]
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_no_conflict_reported_during_the_settling_window(udp_sink):
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await TelemetryWLED(live=False, lm=None).start()
    cfg = config_mod.default_config()
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "ambilight_power_check_s": 0.2, "request_timeout_s": 1.0})
    cfg["output"].update({"presence_check_s": 0.2,
                          "targets": [{"host": host, "port": port, "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462, "enabled": True}]})
    bridge = Bridge(cfg)
    await bridge.start()
    try:
        await _wait_for(lambda: bridge.should_emit(), timeout=8)
        await asyncio.sleep(1.0)
        assert bridge.metrics()["output_conflict"] is None
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


# -- mode arbitration --------------------------------------------------------

async def test_off_mode_stays_silent(udp_sink):
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await FakeWLED().start()
    cfg = config_mod.default_config()
    cfg["mode"] = "off"
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "request_timeout_s": 1.0, "ambilight_power_check_s": 0.2})
    cfg["output"].update({"presence_check_s": 0.2,
                          "targets": [{"host": host, "port": port, "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462, "enabled": True}]})
    bridge = Bridge(cfg)
    await bridge.start()
    try:
        await _wait_for(lambda: bridge.poller.state == "streaming", timeout=6)
        recv()
        await asyncio.sleep(0.5)
        assert recv() == [], "off means off, even with a healthy TV"
        assert bridge.should_emit() is False
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_switching_mode_live_takes_effect(udp_sink):
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await FakeWLED().start()
    cfg = config_mod.default_config()
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "request_timeout_s": 1.0, "ambilight_power_check_s": 0.2})
    cfg["output"].update({"presence_check_s": 0.2,
                          "targets": [{"host": host, "port": port, "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462, "enabled": True}]})
    bridge = Bridge(cfg)
    await bridge.start()
    try:
        await _wait_for(lambda: bridge.should_emit(), timeout=6)
        new = config_mod.merge_defaults({**cfg, "mode": "off"})
        bridge.apply_config(new)
        await asyncio.sleep(0.3)
        assert bridge.should_emit() is False
        bridge.apply_config(config_mod.merge_defaults({**cfg, "mode": "ambilight"}))
        await _wait_for(lambda: bridge.should_emit(), timeout=6)
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


# -- preset mode arbitration --------------------------------------------------

from test_wled import FakeWledApi   # noqa: E402


class ArbiterWled(FakeWledApi):
    """Applies a preset selection only after a configurable number of writes,
    the way the retry loop must cope with regardless of the real cause."""

    def __init__(self, apply_after=1):
        super().__init__()
        self.ps = -1
        self.attempts = 0
        self.apply_after = apply_after

    async def post_state(self, request):
        body = await request.json()
        if "ps" in body:
            self.attempts += 1
            if self.attempts >= self.apply_after:
                self.ps = body["ps"]
            return web.json_response({"success": True})
        return await super().post_state(request)

    async def get_state(self, request):
        return web.json_response({"ps": self.ps, "seg": []})

    async def presets(self, request):
        return web.json_response({"1": {"n": "Solid"}, "2": {"n": "Music"}})


async def preset_wired(udp_sink, apply_after=1):
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    wled = await ArbiterWled(apply_after=apply_after).start()
    cfg = config_mod.default_config()
    cfg["mode"] = "preset"
    cfg["preset_slot"] = 2
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "request_timeout_s": 1.0, "ambilight_power_check_s": 0.2})
    cfg["output"].update({"presence_check_s": 0.2,
                          "targets": [{"host": host, "port": port, "http_port": wled.port,
                                       "pixel_offset": 0, "pixel_count": 462,
                                       "enabled": True}]})
    bridge = Bridge(cfg)
    await bridge.start()
    return bridge, tv, wled, recv


async def test_preset_mode_does_not_stream_pixels(udp_sink):
    from ambiwled.wled import WledClient
    bridge, tv, wled, recv = await preset_wired(udp_sink)
    try:
        await asyncio.sleep(1.5)
        assert recv() == [], "preset mode must never send DNRGB"
        assert bridge.should_emit() is False
        assert bridge.metrics()["mode"] == "preset"
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_preset_mode_ignores_the_tv_entirely(udp_sink):
    """Unlike ambilight mode, preset mode does not care whether the TV is on."""
    bridge, tv, wled, recv = await preset_wired(udp_sink)
    try:
        tv.fail = True
        await _wait_for(lambda: bridge.poller.state == "tv_off", timeout=6)
        await asyncio.sleep(0.5)
        assert bridge.should_emit() is False   # unchanged either way
        assert wled.ps == 2 or bridge.metrics()["preset_applied"] is False
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_preset_selection_is_confirmed_not_assumed(udp_sink):
    bridge, tv, wled, recv = await preset_wired(udp_sink, apply_after=1)
    try:
        await _wait_for(lambda: bridge.metrics()["preset_applied"] is True, timeout=8)
        assert wled.ps == 2
        assert bridge.metrics()["preset_error"] is None
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_preset_selection_retries_until_it_actually_takes(udp_sink):
    """Regression against exactly the failure mode preset SAVE has: a
    {"success": true} response that did not actually change anything."""
    bridge, tv, wled, recv = await preset_wired(udp_sink, apply_after=3)
    try:
        await asyncio.sleep(0.5)
        assert bridge.metrics()["preset_applied"] is False, "must not claim success early"
        await _wait_for(lambda: bridge.metrics()["preset_applied"] is True, timeout=10)
        assert wled.attempts >= 3
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_switching_the_target_preset_reapplies(udp_sink):
    bridge, tv, wled, recv = await preset_wired(udp_sink)
    try:
        await _wait_for(lambda: bridge.metrics()["preset_applied"] is True, timeout=8)
        bridge.apply_config(config_mod.merge_defaults({**bridge.cfg, "preset_slot": 1}))
        assert bridge.metrics()["preset_applied"] is False, "changing the target must reset"
        await _wait_for(lambda: wled.ps == 1, timeout=8)
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_leaving_preset_mode_stops_the_arbiter(udp_sink):
    bridge, tv, wled, recv = await preset_wired(udp_sink)
    try:
        await _wait_for(lambda: bridge.metrics()["preset_applied"] is True, timeout=8)
        bridge.apply_config(config_mod.merge_defaults({**bridge.cfg, "mode": "ambilight"}))
        assert bridge.metrics()["preset_applied"] is None
        assert bridge._preset_target is None
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()


async def test_preset_names_and_the_active_name_are_exposed(udp_sink):
    bridge, tv, wled, recv = await preset_wired(udp_sink)
    try:
        await _wait_for(lambda: any(bridge.health.presets.values()), timeout=6)
        m = bridge.metrics()
        assert 2 in next(iter(m["presets"].values()))
        await _wait_for(lambda: bridge.metrics()["preset_name"] == "Music", timeout=8)
    finally:
        await bridge.stop(); await wled.stop(); await tv.stop()
