"""HTTP API and persistence, exercised through the real server."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from ambiwled import config as config_mod
from ambiwled.bridge import Bridge
from ambiwled.server import Server


@pytest.fixture
async def running(tmp_config):
    """A real Server on an ephemeral port.  The poller is never started, so no
    TV is contacted; the output loop is not running either."""
    cfg = config_mod.default_config()
    cfg["web"] = {"host": "127.0.0.1", "port": 0, "preview_fps": 15.0}
    config_mod.save(cfg)

    bridge = Bridge(cfg)
    server = Server(bridge)
    runner = await server.start()
    host, port = runner.addresses[0][:2]
    base = f"http://{host}:{port}"
    async with aiohttp.ClientSession() as session:
        yield server, bridge, base, session, tmp_config
    await server.stop(runner)


async def test_state_reports_config_and_validation(running):
    _, _, base, session, _ = running
    async with session.get(f"{base}/api/state") as r:
        assert r.status == 200
        data = await r.json()
    assert data["validation"] == []
    assert data["config"]["led"]["count"] == 462
    assert data["metrics"]["tv_state"] in ("starting", "tv_off", "error")


async def test_ui_is_served_even_with_no_tv(running):
    """Spec: must come up and serve the UI when the TV is unreachable."""
    _, _, base, session, _ = running
    async with session.get(f"{base}/") as r:
        assert r.status == 200
        assert "AmbiWled" in await r.text()
    async with session.get(f"{base}/static/app.js") as r:
        assert r.status == 200


async def test_patch_applies_live_and_persists(running):
    _, bridge, base, session, path = running
    async with session.put(f"{base}/api/config",
                           json={"patch": {"colour": {"saturation": 1.6}}}) as r:
        assert r.status == 200
    assert bridge.cfg["colour"]["saturation"] == 1.6
    assert bridge.colour.saturation == 1.6          # applied to the live stage
    await asyncio.sleep(1.0)                        # past the debounce
    assert json.loads(path.read_text())["colour"]["saturation"] == 1.6


async def test_patch_leaves_untouched_keys_alone(running):
    """A scoped patch must not clobber settings it does not mention."""
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config", json={"patch": {"source": {"tv_ip": "10.0.0.5"}}})
    async with session.put(f"{base}/api/config",
                           json={"patch": {"frames": {"output_fps": 40.0}}}) as r:
        assert r.status == 200
    assert bridge.cfg["source"]["tv_ip"] == "10.0.0.5"    # survived the second patch
    assert bridge.cfg["frames"]["output_fps"] == 40.0


async def test_invalid_patch_is_rejected_and_changes_nothing(running):
    _, bridge, base, session, _ = running
    before = json.loads(json.dumps(bridge.cfg))
    async with session.put(f"{base}/api/config", json={"patch": {"mapping": {"edges": [
            {"name": "a", "pixel_start": 0, "pixel_count": 200, "source": "top"},
            {"name": "b", "pixel_start": 100, "pixel_count": 100, "source": "left"}]}}}) as r:
        assert r.status == 400
        errors = (await r.json())["errors"]
    assert any("overlap" in e for e in errors)
    assert bridge.cfg == before, "a rejected update must not partially apply"


async def test_half_typed_ip_is_rejected_by_the_api(running):
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config", json={"patch": {"source": {"tv_ip": "192.0.2.10"}}})
    async with session.put(f"{base}/api/config",
                           json={"patch": {"source": {"tv_ip": "192.0.2."}}}) as r:
        assert r.status == 400
    assert bridge.cfg["source"]["tv_ip"] == "192.0.2.10", "a rejected edit must not apply"


async def test_validate_endpoint_is_a_dry_run(running):
    _, bridge, base, session, _ = running
    bad = config_mod.default_config()
    bad["led"]["count"] = 500
    async with session.post(f"{base}/api/config/validate", json=bad) as r:
        assert r.status == 200
        assert (await r.json())["errors"]
    assert bridge.cfg["led"]["count"] == 462, "validate must not apply anything"


async def test_reset_restores_defaults(running):
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config", json={"patch": {"colour": {"brightness": 0.3}}})
    async with session.post(f"{base}/api/config/reset") as r:
        assert r.status == 200
    assert bridge.cfg["colour"]["brightness"] == 1.0


async def test_malformed_json_is_a_400_not_a_500(running):
    _, _, base, session, _ = running
    async with session.put(f"{base}/api/config", data="{{{") as r:
        assert r.status == 400


async def test_identify_by_range_works_without_a_valid_config(running):
    """Identify must work on the edge being edited, even mid-edit."""
    _, bridge, base, session, _ = running
    async with session.ws_connect(f"{base}/ws") as ws:
        await ws.send_json({"type": "identify", "start": 133, "count": 98,
                            "seconds": 5, "colour": [255, 0, 0]})
        await asyncio.sleep(0.2)
        assert bridge.identify_span == (133, 98)

    frame = bridge._identify_frame()
    lit = [i for i in range(462) if frame[i].any()]
    assert lit[0] == 133 and lit[-1] == 230
    assert tuple(frame[133]) == (255, 0, 0)
    assert not frame[132].any() and not frame[231].any()


async def test_identify_by_name_still_works(running):
    """Routing only - the chase pattern itself is covered in test_bridge.py."""
    _, bridge, base, session, _ = running
    async with session.ws_connect(f"{base}/ws") as ws:
        await ws.send_json({"type": "identify", "edge": "back", "seconds": 5})
        await asyncio.sleep(0.2)
    frame = bridge._identify_frame()
    lit = [i for i in range(462) if frame[i].any()]
    assert lit, "identifying 'back' produced no light at all"
    assert 329 <= lit[0] and lit[-1] <= 461, "must not spill outside back's own range"


async def test_identify_expires(running):
    _, bridge, _, _, _ = running
    bridge.identify(span=(0, 10), seconds=0.05)
    assert bridge._identify_frame() is not None
    await asyncio.sleep(0.1)
    assert bridge._identify_frame() is None


async def test_websocket_sends_pixel_frames(running):
    _, bridge, base, session, _ = running
    async with session.ws_connect(f"{base}/ws") as ws:
        for _ in range(30):
            msg = await asyncio.wait_for(ws.receive(), timeout=3)
            if msg.type == aiohttp.WSMsgType.BINARY:
                assert len(msg.data) == 462 * 3
                return
    pytest.fail("no binary preview frame arrived")


async def test_config_survives_an_immediate_restart(tmp_config):
    """Regression: the debounced save used to be dropped on shutdown, losing
    the last change made before a stop."""
    cfg = config_mod.default_config()
    cfg["web"] = {"host": "127.0.0.1", "port": 0, "preview_fps": 15.0}
    config_mod.save(cfg)

    bridge = Bridge(cfg)
    server = Server(bridge)
    runner = await server.start()
    host, port = runner.addresses[0][:2]

    async with aiohttp.ClientSession() as session:
        await session.put(f"http://{host}:{port}/api/config",
                          json={"patch": {"source": {"tv_ip": "192.0.2.10"},
                                          "output": {"targets": [{"host": "192.0.2.20",
                                                                  "port": 21324,
                                                                  "pixel_offset": 0,
                                                                  "pixel_count": 462,
                                                                  "enabled": True}]}}})
    await server.stop(runner)          # no delay at all: inside the debounce window

    saved = json.loads(tmp_config.read_text())
    assert saved["source"]["tv_ip"] == "192.0.2.10"
    assert saved["output"]["targets"][0]["host"] == "192.0.2.20"


async def test_segment_push_endpoint_reports_failure_without_a_controller(running):
    """No reachable controller must be a clean 502, not a traceback."""
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config", json={"patch": {"output": {"targets": [
        {"host": "127.0.0.1", "port": 21324, "http_port": 1,
         "pixel_offset": 0, "pixel_count": 462, "enabled": True}]}}})
    async with session.post(f"{base}/api/wled/segments") as r:
        assert r.status == 502
        data = await r.json()
    assert data["ok"] is False
    assert data["results"][0]["ok"] is False
    # The derived segments are still returned, so the UI can show what it tried.
    assert [s["n"] for s in data["segments"]] == ["front", "left", "right", "back"]


async def test_segment_push_needs_an_enabled_target(running):
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config", json={"patch": {"output": {"targets": [
        {"host": "192.0.2.20", "port": 21324, "http_port": 80,
         "pixel_offset": 0, "pixel_count": 462, "enabled": False}]}}})
    async with session.post(f"{base}/api/wled/segments") as r:
        assert r.status == 400


async def test_mqtt_commands_take_the_same_validated_path_as_the_ui(running):
    """A command from the broker must be validated and persisted, not poked
    straight into memory."""
    server, bridge, base, session, path = running
    assert bridge.on_config_change is not None, "the bridge must be wired to the server"

    assert bridge.on_config_change({"mode": "off"}) == []
    assert bridge.cfg["mode"] == "off"
    assert bridge.should_emit() is False

    errors = bridge.on_config_change({"mode": "nonsense"})
    assert errors and bridge.cfg["mode"] == "off", "a bad command must change nothing"

    await asyncio.sleep(1.0)
    assert json.loads(path.read_text())["mode"] == "off", "commands must persist"


async def test_the_api_never_returns_the_broker_password(running):
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config",
                      json={"patch": {"mqtt": {"password": "hunter2"}}})
    assert bridge.cfg["mqtt"]["password"] == "hunter2"

    async with session.get(f"{base}/api/config") as r:
        assert (await r.json())["mqtt"]["password"] == config_mod.REDACTED
    async with session.get(f"{base}/api/state") as r:
        assert (await r.json())["config"]["mqtt"]["password"] == config_mod.REDACTED


async def test_saving_the_page_back_does_not_wipe_the_password(running):
    _, bridge, base, session, _ = running
    await session.put(f"{base}/api/config", json={"patch": {"mqtt": {"password": "hunter2"}}})
    async with session.get(f"{base}/api/config") as r:
        seen = await r.json()
    await session.put(f"{base}/api/config", json={"patch": seen})
    assert bridge.cfg["mqtt"]["password"] == "hunter2"


async def test_a_change_is_on_disk_immediately_not_after_a_delay(running):
    """Regression: settings applied shortly before a restart were lost, because
    the write waited out a debounce and then ran last during shutdown."""
    _, bridge, base, session, path = running
    async with session.put(f"{base}/api/config",
                           json={"patch": {"mqtt": {"enabled": True,
                                                    "host": "192.0.2.30"}}}) as r:
        assert r.status == 200
    # No sleep at all: the write must already have happened.
    assert json.loads(path.read_text())["mqtt"]["enabled"] is True


async def test_rapid_edits_all_survive(running):
    _, bridge, base, session, path = running
    for value in (0.4, 0.6, 0.8):
        await session.put(f"{base}/api/config",
                          json={"patch": {"colour": {"brightness": value}}})
    assert json.loads(path.read_text())["colour"]["brightness"] == 0.8
    await asyncio.sleep(1.0)
    assert json.loads(path.read_text())["colour"]["brightness"] == 0.8


async def test_flush_config_is_callable_before_teardown(running):
    server, bridge, base, session, path = running
    await session.put(f"{base}/api/config", json={"patch": {"mode": "off"}})
    server.flush_config()
    assert json.loads(path.read_text())["mode"] == "off"


# -- link resolution (for pasted short map links) -----------------------------

async def test_resolve_link_follows_a_redirect(running):
    """Models a shortened share link: the coordinates only exist on the page
    it redirects to, which client-side JS cannot read cross-origin."""
    server, bridge, base, session, _ = running
    target_app = web.Application()

    async def shortlink(request):
        raise web.HTTPFound("/landed?data=!3d40.6892494!4d-74.0445004")

    async def landed(request):
        return web.Response(text="ok")

    target_app.router.add_get("/short", shortlink)
    target_app.router.add_get("/landed", landed)
    runner = web.AppRunner(target_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with session.post(f"{base}/api/resolve-link",
                                json={"url": f"http://127.0.0.1:{port}/short"}) as r:
            assert r.status == 200
            data = await r.json()
        assert "landed" in data["resolved_url"]
        assert "!3d40.6892494!4d-74.0445004" in data["resolved_url"]
    finally:
        await runner.cleanup()


async def test_resolve_link_rejects_non_http_schemes(running):
    _, _, base, session, _ = running
    async with session.post(f"{base}/api/resolve-link",
                            json={"url": "javascript:alert(1)"}) as r:
        assert r.status == 400


async def test_resolve_link_reports_an_unreachable_host(running):
    _, _, base, session, _ = running
    async with session.post(f"{base}/api/resolve-link",
                            json={"url": "http://127.0.0.1:1/nope"}) as r:
        assert r.status == 502
        data = await r.json()
    assert "error" in data


async def test_resolve_link_rejects_malformed_json(running):
    _, _, base, session, _ = running
    async with session.post(f"{base}/api/resolve-link", data="{{{") as r:
        assert r.status == 400


# -- MQTT connection test (item 7) --------------------------------------------

async def test_mqtt_test_reports_the_result_from_mqtt_mod(running, monkeypatch):
    _, _, base, session, _ = running
    monkeypatch.setattr("ambiwled.server.mqtt_mod.test_connection",
                        lambda *a, **k: _resolved({"ok": False, "reason": "auth"}))
    async with session.post(f"{base}/api/mqtt/test",
                            json={"host": "192.0.2.30", "port": 1883,
                                  "username": "u", "password": "wrong"}) as r:
        assert r.status == 200
        data = await r.json()
    assert data == {"ok": False, "reason": "auth"}


async def test_mqtt_test_rejects_a_bad_host_without_attempting_a_connection(running, monkeypatch):
    _, _, base, session, _ = running
    called = []
    monkeypatch.setattr("ambiwled.server.mqtt_mod.test_connection",
                        lambda *a, **k: called.append(1) or _resolved({"ok": True}))
    async with session.post(f"{base}/api/mqtt/test",
                            json={"host": "", "port": 1883}) as r:
        data = await r.json()
    assert data["ok"] is False
    assert not called


async def test_mqtt_test_falls_back_to_the_stored_password(running, monkeypatch):
    """The wizard may be testing after only changing the host - it should not
    have to know or resend an already-saved password."""
    _, bridge, base, session, _ = running
    bridge.cfg["mqtt"]["password"] = "already-stored"
    seen = {}

    def fake(host, port, username, password, **k):
        seen["password"] = password
        return _resolved({"ok": True})

    monkeypatch.setattr("ambiwled.server.mqtt_mod.test_connection", fake)
    async with session.post(f"{base}/api/mqtt/test",
                            json={"host": "192.0.2.30", "port": 1883,
                                  "username": "u", "password": config_mod.REDACTED}) as r:
        assert r.status == 200
    assert seen["password"] == "already-stored"


def _resolved(value):
    async def _coro():
        return value
    return _coro()


# -- import (schema-versioned, item: "Export/Import settings are stubs") -----

async def test_import_rejects_a_mismatched_schema_version(running):
    _, bridge, base, session, _ = running
    old = config_mod.default_config()
    old["version"] = 1
    async with session.post(f"{base}/api/config/import", json=old) as r:
        assert r.status == 400
        data = await r.json()
    assert "version" in data["errors"][0]
    assert bridge.cfg["version"] == config_mod.DEFAULT_CONFIG["version"]


async def test_import_replaces_the_config_when_the_version_matches(running):
    _, bridge, base, session, tmp_config = running
    fresh = config_mod.default_config()
    fresh["led"]["count"] = 100
    fresh["mapping"]["edges"] = [
        {"name": "only", "pixel_start": 0, "pixel_count": 100, "source": "top",
         "reversed": False, "brightness": 1.0}
    ]
    async with session.post(f"{base}/api/config/import", json=fresh) as r:
        assert r.status == 200
    assert bridge.cfg["led"]["count"] == 100
    assert len(bridge.cfg["mapping"]["edges"]) == 1


async def test_import_rejects_malformed_json(running):
    _, _, base, session, _ = running
    async with session.post(f"{base}/api/config/import", data="not json") as r:
        assert r.status == 400
