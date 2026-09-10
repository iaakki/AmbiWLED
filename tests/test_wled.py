"""Segment derivation and the command channel to the controller."""
from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from ambiwled import config as config_mod
from ambiwled.wled import WledClient, segments_from_edges


def cfg_with(edges):
    cfg = config_mod.default_config()
    cfg["mapping"]["edges"] = edges
    return cfg


FOUR = [
    {"name": "Left", "pixel_start": 0, "pixel_count": 98, "source": "left"},
    {"name": "Front", "pixel_start": 98, "pixel_count": 133, "source": "top"},
    {"name": "Right", "pixel_start": 231, "pixel_count": 98, "source": "right"},
    {"name": "Rear", "pixel_start": 329, "pixel_count": 133, "source": "synth_gradient"},
]


def test_one_segment_per_edge_in_pixel_order():
    segs = segments_from_edges(cfg_with(FOUR))
    assert [s["n"] for s in segs] == ["Left", "Front", "Right", "Rear"]
    assert [(s["start"], s["stop"]) for s in segs] == [(0, 98), (98, 231), (231, 329), (329, 462)]
    assert [s["id"] for s in segs] == [0, 1, 2, 3]


def test_stop_is_exclusive_and_tiles_the_strip():
    """WLED's stop is exclusive; an off-by-one here leaves a dark pixel."""
    segs = segments_from_edges(cfg_with(FOUR))
    assert segs[0]["stop"] == segs[1]["start"]
    assert segs[-1]["stop"] == 462
    covered = sum(s["stop"] - s["start"] for s in segs)
    assert covered == 462


def test_edges_are_sorted_regardless_of_config_order():
    shuffled = [FOUR[2], FOUR[0], FOUR[3], FOUR[1]]
    segs = segments_from_edges(cfg_with(shuffled))
    assert [s["start"] for s in segs] == [0, 98, 231, 329]


def test_unnamed_edge_still_gets_a_name():
    segs = segments_from_edges(cfg_with([
        {"name": "", "pixel_start": 0, "pixel_count": 462, "source": "top"}]))
    assert segs[0]["n"] == "edge_1"


# -- the command channel -----------------------------------------------------

class FakeWledApi:
    def __init__(self, existing_segments=1, fail=False):
        self.existing = existing_segments
        self.fail = fail
        self.received = None

    async def start(self):
        self.calls = []
        app = web.Application()
        app.router.add_get("/json/state", self.get_state)
        app.router.add_post("/json/state", self.post_state)
        app.router.add_post("/upload", self.upload)
        app.router.add_get("/json/info", self.info)
        app.router.add_get("/presets.json", self.presets)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        await self.runner.cleanup()

    async def get_state(self, request):
        return web.json_response({"seg": [
            {"id": i, "start": i * 100, "stop": (i + 1) * 100} for i in range(self.existing)]})

    async def post_state(self, request):
        body = await request.json()
        self.calls.append("ledmap" if "ledmap" in body else "seg")
        if "ledmap" not in body:
            self.received = body
        if self.fail:
            return web.Response(status=500, text="nope")
        return web.json_response({"success": True})

    async def upload(self, request):
        self.calls.append("upload")
        self.uploaded = await request.read()
        return web.json_response({"ok": True})

    async def info(self, request):
        return web.json_response({"ver": "16.0.1", "leds": {"count": 462}})

    async def presets(self, request):
        return web.json_response({"2": {"n": "Front to back"}})


async def test_segments_are_pushed_to_the_controller():
    api = await FakeWledApi(existing_segments=1).start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).apply_segments(
            "127.0.0.1", api.port, segments_from_edges(cfg_with(FOUR)))
    assert res["ok"] is True
    assert res["segments"] == 4
    sent = api.received["seg"]
    assert [s["n"] for s in sent] == ["Left", "Front", "Right", "Rear"]
    await api.stop()


async def test_extra_existing_segments_are_removed():
    """Going from 6 segments to 4 must not leave two stale ones behind."""
    api = await FakeWledApi(existing_segments=6).start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).apply_segments(
            "127.0.0.1", api.port, segments_from_edges(cfg_with(FOUR)))
    assert res["removed"] == 2
    tail = api.received["seg"][4:]
    assert tail == [{"id": 4, "start": 0, "stop": 0}, {"id": 5, "start": 0, "stop": 0}]
    await api.stop()


async def test_fewer_existing_segments_needs_no_deletions():
    api = await FakeWledApi(existing_segments=1).start()
    async with aiohttp.ClientSession() as session:
        await WledClient(session).apply_segments(
            "127.0.0.1", api.port, segments_from_edges(cfg_with(FOUR)))
    assert len(api.received["seg"]) == 4
    await api.stop()


async def test_controller_error_is_reported_not_raised():
    api = await FakeWledApi(fail=True).start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).apply_segments(
            "127.0.0.1", api.port, segments_from_edges(cfg_with(FOUR)))
    assert res["ok"] is False
    await api.stop()


async def test_unreachable_controller_is_reported_not_raised():
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).apply_segments("127.0.0.1", 1, [])
    assert res["ok"] is False
    assert "error" in res


# -- ledmap ------------------------------------------------------------------

from ambiwled.wled import ledmap_from_edges, sweep_segment   # noqa: E402


def test_ledmap_is_a_permutation():
    m = ledmap_from_edges(cfg_with(FOUR))
    assert len(m) == 462
    assert sorted(m) == list(range(462)), "every physical LED used exactly once"


def test_ledmap_starts_at_the_front_centre():
    m = ledmap_from_edges(cfg_with(FOUR))
    front = FOUR[1]
    centre = front["pixel_start"] + front["pixel_count"] // 2
    assert m[0] == centre
    assert m[-1] == centre + 1, "the mirror partner sits next to it"


def test_ledmap_midpoint_is_the_rear_centre():
    m = ledmap_from_edges(cfg_with(FOUR))
    rear = FOUR[3]
    mid = m[len(m) // 2]
    assert rear["pixel_start"] <= mid < rear["pixel_start"] + rear["pixel_count"]


def test_mirror_partners_are_physically_symmetric():
    """logical i and (last - i) must be equidistant from the front centre,
    or a mirrored segment sweeps the two sides at different speeds."""
    m = ledmap_from_edges(cfg_with(FOUR))
    n = len(m)
    centre = m[0]
    for i in (1, 50, 120, 200, 230):
        a = (centre - m[i]) % n            # distance one way round
        b = (m[n - 1 - i] - centre) % n    # distance the other way
        assert abs(a - b) <= 1, f"logical {i}: {a} vs {b}"


def test_ledmap_walks_the_loop_without_jumps():
    """Consecutive logical positions must be physically adjacent, otherwise a
    sweep would tear."""
    m = ledmap_from_edges(cfg_with(FOUR))
    n = len(m)
    for half in (m[: n // 2], m[n // 2:]):
        steps = {(half[i + 1] - half[i]) % n for i in range(len(half) - 1)}
        assert steps <= {1, n - 1}, f"non-adjacent step in the path: {steps}"


def test_ledmap_falls_back_to_the_longest_run_when_unnamed():
    edges = [dict(e) for e in FOUR]
    for e in edges:
        e["name"] = e["name"].lower().replace("front", "wall")
    m = ledmap_from_edges(cfg_with(edges))
    assert sorted(m) == list(range(462))


def test_sweep_layout_is_one_mirrored_segment():
    segs = sweep_segment(cfg_with(FOUR))
    assert len(segs) == 1
    assert (segs[0]["start"], segs[0]["stop"]) == (0, 462)
    assert segs[0]["mi"] is True


async def test_sweep_upload_then_select_order():
    """The map must be uploaded before the slot is selected, or the controller
    is briefly pointed at a file that does not exist."""
    api = await FakeWledApi().start()
    async with aiohttp.ClientSession() as session:
        client = WledClient(session)
        cfg = cfg_with(FOUR)
        assert (await client.upload_ledmap("127.0.0.1", api.port, ledmap_from_edges(cfg)))["ok"]
        await client.apply_segments("127.0.0.1", api.port, sweep_segment(cfg))
        await client.set_ledmap("127.0.0.1", api.port, 1)
    assert api.calls == ["upload", "seg", "ledmap"]
    assert b"AmbiWled front-to-rear" in api.uploaded
    await api.stop()


async def test_ledmap_upload_failure_is_reported():
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).upload_ledmap("127.0.0.1", 1, [0, 1, 2])
    assert res["ok"] is False


# -- ledmap registration -----------------------------------------------------

class MapAwareWled(FakeWledApi):
    """Models the behaviour that caused the bug: uploaded maps are only
    registered at boot."""

    def __init__(self, registered=(0,), **kw):
        super().__init__(**kw)
        self.registered = list(registered)
        self.pending = None
        self.reboots = 0

    async def info(self, request):
        return web.json_response(
            {"ver": "16.0.1", "leds": {"count": 462},
             "maps": [{"id": i} for i in self.registered]})

    async def upload(self, request):
        self.calls.append("upload")
        self.uploaded = await request.read()
        self.pending = 1                       # on the filesystem, not registered
        return web.json_response({"ok": True})

    async def post_state(self, request):
        body = await request.json()
        if body.get("rb"):
            self.calls.append("reboot")
            self.reboots += 1
            if self.pending is not None:       # boot scan picks it up
                self.registered.append(self.pending)
                self.pending = None
            return web.json_response({"success": True})
        return await super().post_state(request)


async def test_registered_maps_is_read_from_the_maps_field():
    api = await MapAwareWled(registered=(0, 1)).start()
    async with aiohttp.ClientSession() as session:
        assert await WledClient(session).registered_maps("127.0.0.1", api.port) == [0, 1]
    await api.stop()


async def test_registered_maps_handles_the_legacy_bitfield():
    class Legacy(FakeWledApi):
        async def info(self, request):
            return web.json_response({"ledmaps": 0b0011})
    api = await Legacy().start()
    async with aiohttp.ClientSession() as session:
        assert await WledClient(session).registered_maps("127.0.0.1", api.port) == [0, 1]
    await api.stop()


async def test_a_freshly_uploaded_map_is_not_yet_registered():
    """The exact trap: the file exists, the slot can be selected, and the map
    is silently ignored until the controller restarts."""
    api = await MapAwareWled(registered=(0,)).start()
    async with aiohttp.ClientSession() as session:
        client = WledClient(session)
        await client.upload_ledmap("127.0.0.1", api.port, list(range(462)))
        assert 1 not in await client.registered_maps("127.0.0.1", api.port)
        assert (await client.reboot("127.0.0.1", api.port, wait_s=5))["ok"]
        assert 1 in await client.registered_maps("127.0.0.1", api.port)
    assert api.reboots == 1
    await api.stop()


async def test_reboot_reports_failure_if_it_never_returns():
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).reboot("127.0.0.1", 1, wait_s=3)
    assert res["ok"] is False


# -- front-to-back layout ----------------------------------------------------

from ambiwled.wled import front_centre_pixel, front_to_back_segment   # noqa: E402


def test_front_centre_is_the_middle_of_the_front_run():
    assert front_centre_pixel(cfg_with(FOUR)) == 98 + 133 // 2 == 164


def test_front_centre_found_by_source_when_the_name_differs():
    edges = [dict(e) for e in FOUR]
    edges[1]["name"] = "wall"            # still source 'top'
    assert front_centre_pixel(cfg_with(edges)) == 164


def test_front_centre_falls_back_to_the_longest_run():
    edges = [{"name": "a", "pixel_start": 0, "pixel_count": 100, "source": "left"},
             {"name": "b", "pixel_start": 100, "pixel_count": 362, "source": "left"}]
    assert front_centre_pixel(cfg_with(edges)) == 100 + 362 // 2


def test_front_to_back_is_one_mirrored_offset_segment():
    seg = front_to_back_segment(cfg_with(FOUR))
    assert len(seg) == 1
    s = seg[0]
    assert (s["start"], s["stop"]) == (0, 462)
    assert s["mi"] is True and s["rev"] is False
    assert s["of"] == 164


def test_offset_places_the_mirror_axis_at_the_front_centre():
    """WLED applies the offset after mirroring, wrapping inside the segment:
    effect index 0 lands on `of` and `of - 1`, the two front-centre pixels."""
    n = 462
    of = front_to_back_segment(cfg_with(FOUR))[0]["of"]
    start_a = of % n
    start_b = (n - 1 + of) % n           # the mirrored partner of index 0
    assert {start_a, start_b} == {164, 163}

    last = 230                            # final effect index with mirror on
    end_a = (last + of) % n
    end_b = (n - 1 - last + of) % n
    assert {end_a, end_b} == {394, 395}, "the effect must meet at the rear centre"


def test_offset_follows_a_changed_layout():
    edges = [dict(e) for e in FOUR]
    edges[1]["pixel_count"] = 101         # shorter front run
    edges[2]["pixel_start"] = 199
    assert front_centre_pixel(cfg_with(edges)) == 98 + 50


async def test_preset_save_is_sent_correctly():
    api = await FakeWledApi().start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).save_preset("127.0.0.1", api.port, 2, "Front to back")
    assert res["ok"] is True and res["preset"] == 2
    assert api.received["psave"] == 2
    assert api.received["n"] == "Front to back"
    assert api.received["sb"] is True, "segment bounds must be included or the layout is lost"
    await api.stop()


async def test_preset_save_failure_is_reported():
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).save_preset("127.0.0.1", 1, 2, "x")
    assert res["ok"] is False


async def test_preset_save_is_verified_by_reading_it_back():
    """WLED answers success even when it silently drops the save."""
    class Liar(FakeWledApi):
        async def post_state(self, request):
            body = await request.json()
            if "psave" in body:
                return web.json_response({"success": True})   # but stores nothing
            return await super().post_state(request)
        async def presets(self, request):
            return web.json_response({"1": {}})

    api = await Liar().start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).save_preset("127.0.0.1", api.port, 2, "Front to back")
    assert res["ok"] is False
    assert "did not store" in res["error"]
    await api.stop()


async def test_preset_save_blames_the_stream_when_one_is_running():
    class Streaming(FakeWledApi):
        async def info(self, request):
            return web.json_response({"ver": "16.0.1", "live": True, "lm": "UDP"})
        async def post_state(self, request):
            body = await request.json()
            if "psave" in body:
                return web.json_response({"success": True})
            return await super().post_state(request)

    api = await Streaming().start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).save_preset("127.0.0.1", api.port, 2, "x")
    assert res["ok"] is False
    assert "realtime stream is active" in res["error"]
    await api.stop()


async def test_preset_save_succeeds_when_it_is_actually_stored():
    class Honest(FakeWledApi):
        def __init__(self):
            super().__init__()
            self.store = {}
        async def post_state(self, request):
            body = await request.json()
            if "psave" in body:
                self.store[str(body["psave"])] = {"n": body.get("n")}
                return web.json_response({"success": True})
            return await super().post_state(request)
        async def presets(self, request):
            return web.json_response(self.store)

    api = await Honest().start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).save_preset("127.0.0.1", api.port, 2, "Front to back")
    assert res["ok"] is True and res["preset"] == 2
    await api.stop()


# -- preset selection ---------------------------------------------------------

class PresetWledApi(FakeWledApi):
    """Models the same drop-until-released behaviour the preset SAVE has, so
    select_preset's retry-until-confirmed design is exercised against it too,
    even though it is not yet known whether selection has the same quirk."""

    def __init__(self, apply_after_attempts=1, presets=None):
        super().__init__()
        self.ps = -1
        self.attempts = 0
        self.apply_after = apply_after_attempts
        self._presets = presets or {"1": {"n": "Solid"}, "2": {"n": "Front to back"}}

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
        return web.json_response(self._presets)


def register_get_state(api_cls):
    return api_cls


async def test_select_preset_confirms_by_reading_ps_back():
    api = await PresetWledApi(apply_after_attempts=1).start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).select_preset("127.0.0.1", api.port, 2)
    assert res == {"host": "127.0.0.1", "ok": True, "preset": 2, "reported_ps": 2}
    await api.stop()


async def test_select_preset_reports_not_yet_applied_rather_than_lying():
    """If the write did not take (mirrors the preset-save quirk), this must
    say so rather than trust the {"success": true} response."""
    api = await PresetWledApi(apply_after_attempts=99).start()
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).select_preset("127.0.0.1", api.port, 2)
    assert res["ok"] is False
    await api.stop()


async def test_select_preset_on_an_unreachable_controller():
    async with aiohttp.ClientSession() as session:
        res = await WledClient(session).select_preset("127.0.0.1", 1, 2)
    assert res["ok"] is False
    assert "error" in res


async def test_list_presets_names_and_filters_empty_slots():
    api = await PresetWledApi(presets={
        "0": {}, "1": {"n": "Solid"}, "2": {"n": "Front to back"}, "3": {}}).start()
    async with aiohttp.ClientSession() as session:
        names = await WledClient(session).list_presets("127.0.0.1", api.port)
    assert names == {1: "Solid", 2: "Front to back"}
    await api.stop()


async def test_list_presets_falls_back_to_a_generated_name():
    api = await PresetWledApi(presets={"5": {"seg": []}}).start()   # no "n"
    async with aiohttp.ClientSession() as session:
        names = await WledClient(session).list_presets("127.0.0.1", api.port)
    assert names == {5: "Preset 5"}
    await api.stop()


async def test_list_presets_on_an_unreachable_controller_is_empty_not_raised():
    async with aiohttp.ClientSession() as session:
        assert await WledClient(session).list_presets("127.0.0.1", 1) == {}
