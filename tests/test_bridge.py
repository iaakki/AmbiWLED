"""End-to-end: fake TV in, real UDP packets out.  No hardware required."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from ambiwled import config as config_mod
from ambiwled.bridge import Bridge
from conftest import ambilight_payload
from test_source import FakeTV, _wait_for


def bridge_cfg(tv, sink_host, sink_port):
    cfg = config_mod.default_config()
    cfg["source"].update({"tv_ip": "127.0.0.1", "port": tv.port, "poll_hz": 50.0,
                          "fail_threshold": 2, "off_probe_interval_s": 0.2,
                          "request_timeout_s": 1.0})
    cfg["output"]["targets"] = [{"host": sink_host, "port": sink_port,
                                 "pixel_offset": 0, "pixel_count": 462, "enabled": True}]
    cfg["frames"].update({"output_fps": 60.0})
    return cfg


@pytest.fixture
async def wired(udp_sink):
    host, port, recv = udp_sink
    tv = await FakeTV().start()
    bridge = Bridge(bridge_cfg(tv, host, port))
    await bridge.start()
    try:
        yield bridge, tv, recv
    finally:
        await bridge.stop()
        await tv.stop()


async def test_frames_flow_from_tv_to_wire(wired):
    bridge, tv, recv = wired
    await _wait_for(lambda: bridge.poller.state == "streaming")
    await asyncio.sleep(0.3)

    packets = recv()
    assert packets, "nothing reached the controller"
    pkt = packets[-1]
    assert pkt[0] == 4 and pkt[1] == 2 and len(pkt) == 1390

    # The wire content must equal the mapped frame, not something else.
    expected = np.clip(bridge.last_frame, 0, 255).astype(np.uint8)
    on_wire = np.frombuffer(pkt[4:], dtype=np.uint8).reshape(462, 3)
    assert np.abs(on_wire.astype(int) - expected.astype(int)).max() <= 1


async def test_mapping_matches_the_source_zones(wired):
    """Spot-check the geometry the spec defines, through the whole pipeline."""
    bridge, tv, recv = wired
    tv.payload = ambilight_payload(value=lambda side, i: {
        "left": {"r": 255, "g": 0, "b": 0},
        "top": {"r": 0, "g": 255, "b": 0},
        "right": {"r": 0, "g": 0, "b": 255},
    }[side])
    await _wait_for(lambda: bridge.poller.state == "streaming")
    await _wait_for(lambda: bridge.last_frame[60].argmax() == 1, timeout=5)

    f = bridge.last_frame
    assert f[60].argmax() == 1, "middle of the front run should come from top (green)"
    assert f[182].argmax() == 0, "middle of the left run should come from left (red)"
    assert f[280].argmax() == 2, "middle of the right run should come from right (blue)"
    # The synthesised back blends the two bottom corners: blue to red, no green.
    back = f[329:462]
    assert back[:, 1].max() < 40
    assert back[0].argmax() == 2 and back[-1].argmax() == 0


async def test_emission_stops_when_the_tv_goes_off(wired):
    """No black frames: silence is what triggers WLED's fallback."""
    bridge, tv, recv = wired
    await _wait_for(lambda: bridge.poller.state == "streaming")
    tv.fail = True
    await _wait_for(lambda: bridge.poller.state == "tv_off", timeout=5)

    recv()                                  # drain anything already in flight
    await asyncio.sleep(0.4)
    assert recv() == [], "must go silent, not send black"


async def test_emission_resumes_when_the_tv_returns(wired):
    bridge, tv, recv = wired
    tv.fail = True
    await _wait_for(lambda: bridge.poller.state == "tv_off", timeout=5)
    tv.fail = False
    await _wait_for(lambda: bridge.poller.state == "streaming", timeout=5)
    recv()
    await asyncio.sleep(0.3)
    assert recv(), "streaming must resume on its own"


async def test_config_change_applies_without_restart(wired):
    bridge, tv, recv = wired
    await _wait_for(lambda: bridge.poller.state == "streaming")

    cfg = config_mod.default_config()
    cfg["source"].update(bridge.cfg["source"])
    cfg["output"]["targets"] = bridge.cfg["output"]["targets"]
    cfg["frames"] = dict(bridge.cfg["frames"])
    cfg["colour"]["brightness"] = 0.25
    bridge.apply_config(cfg)

    await asyncio.sleep(0.3)
    bright = bridge.last_frame.astype(int).sum()
    assert bright > 0
    assert bridge.colour.brightness == 0.25


async def test_identify_overrides_the_stream_then_releases_it(wired):
    bridge, tv, recv = wired
    await _wait_for(lambda: bridge.poller.state == "streaming")

    bridge.identify(span=(0, 133), seconds=0.4, colour=(255, 255, 255))
    await asyncio.sleep(0.15)
    f = bridge.last_frame
    assert f[0].min() > 200 and not f[200].any(), "only the named range is lit"

    await asyncio.sleep(0.5)
    assert bridge.identify_span is None
    await asyncio.sleep(0.2)
    assert bridge.last_frame[200].any() or bridge.poller.state != "streaming"


# -- dimming: a config change must reach the strip immediately -------------

def test_apply_config_updates_auto_level_immediately():
    """The output loop only re-samples the dimming schedule every 20s - fine
    for the sun moving, not for a config change. Disabling auto-dim (or
    changing night/day level) must snap the real output straight away,
    not leave it showing whatever the schedule said up to 20s ago."""
    bridge = Bridge(config_mod.default_config())
    bridge.colour.auto_level = 0.18  # simulate having settled on a dim night level

    cfg = config_mod.default_config()
    cfg["dimming"]["enabled"] = False   # disabled always means 1.0, no clock/location needed
    bridge.apply_config(cfg)

    assert bridge.colour.auto_level == 1.0


# -- identify: a moving comet, so a flipped edge is visible on the real strip -

def test_identify_by_name_lights_only_part_of_its_own_edge(monkeypatch):
    """A comet, not a solid fill - and it must never spill past the edge's
    own pixel range into whatever is next to it."""
    import time as time_mod
    bridge = Bridge(config_mod.default_config())
    bridge.identify(edge="front", seconds=5.0)         # front: pixels 0..132
    monkeypatch.setattr(time_mod, "monotonic", lambda: 1000.2)
    frame = bridge._identify_frame()
    lit = np.where(frame.any(axis=1))[0]
    assert len(lit) > 0
    assert lit.min() >= 0 and lit.max() < 133
    assert len(lit) < 133


def test_identify_chase_moves_over_time(monkeypatch):
    import time as time_mod
    bridge = Bridge(config_mod.default_config())
    bridge.identify(edge="front", seconds=5.0)
    monkeypatch.setattr(time_mod, "monotonic", lambda: 1000.0)
    frame_a = bridge._identify_frame()
    monkeypatch.setattr(time_mod, "monotonic", lambda: 1000.8)
    frame_b = bridge._identify_frame()
    assert not np.allclose(frame_a, frame_b)


def test_identify_chase_direction_follows_reversed(monkeypatch):
    """The whole point: flipping `reversed` must flip which way the comet
    runs, so pressing Identify after tapping Flip is the confirmation."""
    import time as time_mod

    def make(is_reversed):
        cfg = config_mod.default_config()
        for e in cfg["mapping"]["edges"]:
            if e["name"] == "front":
                e["reversed"] = is_reversed
        b = Bridge(cfg)
        b.identify(edge="front", seconds=5.0)
        return b

    forward, backward = make(False), make(True)
    monkeypatch.setattr(time_mod, "monotonic", lambda: 1000.1)  # early in the cycle
    f_head = int(np.argmax(forward._identify_frame().sum(axis=1)))
    b_head = int(np.argmax(backward._identify_frame().sum(axis=1)))
    assert f_head < 133 // 2, "not reversed: early in the cycle, near the start"
    assert b_head > 133 // 2, "reversed: early in the cycle, near the end"


def test_identify_by_range_is_still_a_plain_fill():
    """The span form (identifying an edge still being edited, before it has
    a saved `reversed` to check) is unaffected - no direction to show yet."""
    bridge = Bridge(config_mod.default_config())
    bridge.identify(span=(10, 20), seconds=5.0)
    frame = bridge._identify_frame()
    block = frame[10:30]
    assert block.min() > 0
    assert block.min() == block.max()


async def test_metrics_report_reality(wired):
    bridge, tv, recv = wired
    await _wait_for(lambda: bridge.poller.state == "streaming")
    await asyncio.sleep(0.5)

    m = bridge.metrics()
    assert m["tv_state"] == "streaming" and m["emitting"] is True
    assert m["source_fps"] > 0 and m["output_fps"] > 0
    assert m["layout"] == {"left": 5, "top": 11, "right": 5}
    assert m["mapping_problems"] == []
    assert m["send_errors"] == 0


# -- per-edge brightness trim -------------------------------------------------

def edges_for(brightness_by_name: dict[str, float]) -> list[dict]:
    edges = [
        {"name": "front", "pixel_start": 0, "pixel_count": 133, "source": "top",
         "reversed": False},
        {"name": "left", "pixel_start": 133, "pixel_count": 98, "source": "left",
         "reversed": False},
        {"name": "right", "pixel_start": 231, "pixel_count": 98, "source": "right",
         "reversed": False},
        {"name": "back", "pixel_start": 329, "pixel_count": 133, "source": "synth_gradient",
         "synth_from": ["right", -1], "synth_to": ["left", 0],
         "reversed": False},
    ]
    for e in edges:
        e["brightness"] = brightness_by_name.get(e["name"], 1.0)
    return edges


async def test_default_mapping_has_no_edge_trim(wired):
    bridge, tv, recv = wired
    assert bridge.edge_scale is None, "no trim configured, no per-pixel array needed"


async def test_dimming_the_rear_edge_only_affects_those_pixels(wired):
    bridge, tv, recv = wired
    cfg = config_mod.default_config()
    cfg["source"] = bridge.cfg["source"]
    cfg["output"] = bridge.cfg["output"]
    cfg["frames"] = dict(bridge.cfg["frames"])
    cfg["mapping"]["edges"] = edges_for({"back": 0.3})
    bridge.apply_config(cfg)

    assert bridge.edge_scale is not None
    assert np.allclose(bridge.edge_scale[0:133], 1.0)      # front
    assert np.allclose(bridge.edge_scale[329:462], 0.3)    # back
    assert np.allclose(bridge.edge_scale[133:231], 1.0)    # left


async def test_the_trim_actually_dims_the_streamed_frame(wired):
    bridge, tv, recv = wired
    tv.payload = ambilight_payload(value=lambda side, i: {"r": 200, "g": 200, "b": 200})
    cfg = config_mod.default_config()
    cfg["source"] = bridge.cfg["source"]
    cfg["output"] = bridge.cfg["output"]
    cfg["frames"] = dict(bridge.cfg["frames"])
    cfg["mapping"]["edges"] = edges_for({"back": 0.25})
    bridge.apply_config(cfg)

    await _wait_for(lambda: bridge.last_frame[50].max() > 100, timeout=6)
    front = int(bridge.last_frame[50].max())      # front: undimmed
    rear = int(bridge.last_frame[400].max())      # back: dimmed to 25%
    assert front > 150
    assert 0 < rear < front // 2
