"""Commands sent to the controller over its JSON API.

The realtime stream is one-way and stateless; this is the other channel, used
for the things that outlive a frame — segment layout, brightness, power.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


def segments_from_edges(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One WLED segment per mapped ceiling run.

    Edges are the single definition of the geometry; segments are derived from
    them, so there is nothing to keep in sync by hand.  This only affects what
    WLED does with its *own* effects — realtime output ignores segments.
    """
    edges = sorted(cfg["mapping"]["edges"], key=lambda e: int(e["pixel_start"]))
    segments: list[dict[str, Any]] = []
    for i, e in enumerate(edges):
        start = int(e["pixel_start"])
        segments.append({
            "id": i,
            "start": start,
            "stop": start + int(e["pixel_count"]),   # WLED's stop is exclusive
            "n": str(e.get("name") or f"edge_{i + 1}"),
            "on": True,
            "sel": True,
        })
    return segments


def front_centre_pixel(cfg: dict[str, Any]) -> int:
    """The pixel halfway along the run nearest the TV."""
    edges = sorted(cfg["mapping"]["edges"], key=lambda e: int(e["pixel_start"]))
    by_name = {str(e.get("name", "")).lower(): e for e in edges}
    front = by_name.get("front") or by_name.get("tv_wall")
    if front is None:
        front = next((e for e in edges if e.get("source") == "top"), None)
    if front is None:
        front = max(edges, key=lambda e: int(e["pixel_count"]))
    return int(front["pixel_start"]) + int(front["pixel_count"]) // 2


def front_to_back_segment(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One segment whose effect starts at the front centre and meets at the rear.

    WLED applies a segment's offset *after* mirroring and wraps it inside the
    segment, so the offset chooses where the mirror axis sits.  With mirror on
    and no offset the axis lands wherever the strip happens to start — for a
    ceiling loop that is an arbitrary corner.  Setting the offset to the front
    centre puts the axis where the viewer actually is.

    The result is a single effect instance, so both halves stay in phase and
    there is no restart at the corners — which four independent segments cannot
    give you.  The offset is derived from the mapping, so it follows the edges
    rather than being a number to remember.
    """
    return [{
        "id": 0,
        "start": 0,
        "stop": int(cfg["led"]["count"]),
        "n": "Front to back",
        "on": True,
        "sel": True,
        "mi": True,
        "rev": False,
        "of": front_centre_pixel(cfg),
    }]


def ledmap_from_edges(cfg: dict[str, Any]) -> list[int]:
    """Reorder the strip into one continuous path for WLED's own effects.

    Segments render independently, so a wipe restarts at every corner.  A
    ledmap instead renumbers the logical sequence: logical 0 sits at the FRONT
    CENTRE and the order runs away from it in both directions, meeting at the
    REAR CENTRE.  Logical i and (last - i) are then physically symmetric, so a
    single segment with mirror enabled sweeps the whole room as one motion.

    MEASURED ON WLED 16.0.1 (2026-09-02): this does NOT work on a 1D strip.
    The map uploads, `info.maps` lists it, and `ledmap: 1` selects it, but
    effect rendering ignores it entirely -- a solid block written to logical
    0..9 lit physical 0..9, unmapped.  Ledmaps appear to apply only to 2D
    matrix setups on this firmware.  Kept for reference and for the CLI tool;
    not wired to any button, because a control that silently does nothing is
    worse than no control.

    Separately confirmed and still true: ledmaps never affect realtime data,
    so nothing here can disturb the Ambilight stream.
    """
    led_count = int(cfg["led"]["count"])
    edges = sorted(cfg["mapping"]["edges"], key=lambda e: int(e["pixel_start"]))
    by_name = {str(e.get("name", "")).lower(): e for e in edges}

    front = by_name.get("front") or by_name.get("tv_wall")
    if front is None:
        # Fall back to the longest run: on a ceiling rectangle that is the
        # wall the TV is on.
        front = max(edges, key=lambda e: int(e["pixel_count"]))
    centre = int(front["pixel_start"]) + int(front["pixel_count"]) // 2

    first_half = led_count // 2 + led_count % 2
    mapping = [0] * led_count
    for i in range(first_half):
        mapping[i] = (centre - i) % led_count
    for j in range(led_count - first_half):
        mapping[led_count - 1 - j] = (centre + 1 + j) % led_count

    if sorted(mapping) != list(range(led_count)):
        raise ValueError("generated ledmap is not a permutation")
    return mapping


def sweep_segment(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One segment over the whole strip, mirrored: effects sweep front to rear."""
    return [{"id": 0, "start": 0, "stop": int(cfg["led"]["count"]),
             "n": "Ceiling sweep", "on": True, "sel": True, "mi": True, "rev": False}]


def _deletions(existing: int, wanted: int) -> list[dict[str, Any]]:
    """A segment is removed by giving it a zero length."""
    return [{"id": i, "start": 0, "stop": 0} for i in range(wanted, existing)]


class WledClient:
    def __init__(self, session: aiohttp.ClientSession, timeout: float = 5.0) -> None:
        self.session = session
        self.timeout = timeout

    def _base(self, host: str, http_port: int) -> str:
        return f"http://{host}:{http_port}"

    async def state(self, host: str, http_port: int = 80) -> dict[str, Any]:
        async with self.session.get(
            f"{self._base(host, http_port)}/json/state",
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as r:
            return await r.json(content_type=None)

    async def upload_ledmap(self, host: str, http_port: int, mapping: list[int],
                            slot: int = 1, name: str = "AmbiWled front-to-rear") -> dict[str, Any]:
        """Write the map to the controller's filesystem.

        Slot 1 rather than 0, so the controller's default (no map) stays intact
        and rollback is a single `ledmap: 0`.
        """
        body = json.dumps({"n": name, "map": mapping}).encode()
        form = aiohttp.FormData()
        form.add_field("data", body, filename=f"/ledmap{slot}.json",
                       content_type="application/json")
        try:
            async with self.session.post(
                f"{self._base(host, http_port)}/upload", data=form,
                timeout=aiohttp.ClientTimeout(total=30.0),
            ) as r:
                if r.status >= 300:
                    return {"host": host, "ok": False,
                            "error": f"upload returned HTTP {r.status}"}
        except Exception as exc:
            return {"host": host, "ok": False, "error": str(exc)}
        return {"host": host, "ok": True, "slot": slot, "entries": len(mapping)}

    async def save_preset(self, host: str, http_port: int, slot: int,
                          name: str) -> dict[str, Any]:
        """Persist the current state.  Segments, mirror, offset and effect are
        runtime-only; a power cut loses them unless they live in a preset.

        MEASURED on WLED 16.0.1: a preset save is silently dropped while a
        realtime stream is active, and `psave` still answers {"success":true}.
        So the result is confirmed by reading the preset back, never by the
        status code, and an active stream is reported as the likely cause.
        """
        base = self._base(host, http_port)
        streaming = False
        try:
            async with self.session.get(
                f"{base}/json/info", timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as r:
                streaming = bool((await r.json(content_type=None)).get("live"))
        except Exception:
            pass

        try:
            async with self.session.post(
                f"{base}/json/state",
                json={"psave": int(slot), "n": name, "ib": True, "sb": True},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                if r.status >= 300:
                    return {"host": host, "ok": False, "preset": slot,
                            "error": f"HTTP {r.status}"}
        except Exception as exc:
            return {"host": host, "ok": False, "preset": slot, "error": str(exc)}

        await asyncio.sleep(1.5)          # the controller writes it lazily
        try:
            async with self.session.get(
                f"{base}/presets.json", timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as r:
                presets = await r.json(content_type=None)
        except Exception as exc:
            return {"host": host, "ok": False, "preset": slot,
                    "error": f"saved but could not verify: {exc}"}

        stored = presets.get(str(slot)) or {}
        saved = bool(stored) and stored.get("n") == name
        if saved:
            return {"host": host, "ok": True, "preset": slot, "name": name}
        return {
            "host": host, "ok": False, "preset": slot,
            "error": ("the controller reported success but did not store the preset"
                      + (" - it drops preset saves while a realtime stream is active;"
                         " stop the stream (or turn the TV off) and try again"
                         if streaming else "")),
        }

    async def list_presets(self, host: str, http_port: int) -> dict[int, str]:
        """Named presets on the controller, for a picker in the UI/MQTT.

        Presets are user content on the controller, not something this project
        defines, so nothing here assumes what they contain.
        """
        try:
            async with self.session.get(
                f"{self._base(host, http_port)}/presets.json",
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                data = await r.json(content_type=None)
        except Exception:
            return {}
        out: dict[int, str] = {}
        for k, v in (data or {}).items():
            if k.isdigit() and isinstance(v, dict) and v:
                out[int(k)] = str(v.get("n") or f"Preset {k}")
        return out

    async def select_preset(self, host: str, http_port: int, slot: int) -> dict[str, Any]:
        """Ask the controller to switch to a stored preset.

        MEASURED: unlike a preset *save*, which WLED silently drops while a
        realtime stream is active, *selecting* one is a plain state write —
        `psave` is the special case, `ps` is not. Still verified by reading
        `ps` back rather than trusting the response, since this project has
        been burned twice by a device answering success without doing the
        thing (see the preset-save and ledmap findings).
        """
        base = self._base(host, http_port)
        try:
            async with self.session.post(
                f"{base}/json/state", json={"ps": int(slot)},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                if r.status >= 300:
                    return {"host": host, "ok": False, "error": f"HTTP {r.status}"}
        except Exception as exc:
            return {"host": host, "ok": False, "error": str(exc)}

        try:
            async with self.session.get(
                f"{base}/json/state", timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as r:
                current = (await r.json(content_type=None)).get("ps")
        except Exception as exc:
            return {"host": host, "ok": False, "error": f"could not verify: {exc}"}

        return {"host": host, "ok": current == int(slot), "preset": slot, "reported_ps": current}

    async def registered_maps(self, host: str, http_port: int) -> list[int]:
        """Slots the controller has actually loaded.

        WLED scans the filesystem for ledmaps only at boot, so a freshly
        uploaded map exists as a file but is not usable until it restarts.
        This is the difference between the map working and being silently
        ignored, which looks identical from the API.
        """
        try:
            async with self.session.get(
                f"{self._base(host, http_port)}/json/info",
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                info = await r.json(content_type=None)
        except Exception:
            return []
        # 16.x reports `maps` as a list of objects; older builds use a bitfield.
        maps = info.get("maps")
        if isinstance(maps, list):
            return [int(m.get("id", -1)) for m in maps if isinstance(m, dict)]
        if isinstance(maps, int):
            return [i for i in range(10) if maps >> i & 1]
        legacy = info.get("ledmaps")
        if isinstance(legacy, int):
            return [i for i in range(10) if legacy >> i & 1]
        return []

    async def reboot(self, host: str, http_port: int, wait_s: float = 40.0) -> dict[str, Any]:
        """Restart and wait for it to answer again."""
        try:
            async with self.session.post(
                f"{self._base(host, http_port)}/json/state", json={"rb": True},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ):
                pass
        except Exception:
            pass          # the controller often drops the connection mid-reply
        await asyncio.sleep(2.0)

        deadline = asyncio.get_running_loop().time() + wait_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with self.session.get(
                    f"{self._base(host, http_port)}/json/info",
                    timeout=aiohttp.ClientTimeout(total=3.0),
                ) as r:
                    if r.status == 200:
                        await asyncio.sleep(1.5)   # let it finish booting
                        return {"host": host, "ok": True}
            except Exception:
                pass
            await asyncio.sleep(1.5)
        return {"host": host, "ok": False, "error": "did not come back after reboot"}

    async def set_ledmap(self, host: str, http_port: int, slot: int) -> dict[str, Any]:
        try:
            async with self.session.post(
                f"{self._base(host, http_port)}/json/state", json={"ledmap": slot},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                return {"host": host, "ok": r.status < 300, "ledmap": slot}
        except Exception as exc:
            return {"host": host, "ok": False, "error": str(exc)}

    async def apply_segments(self, host: str, http_port: int,
                             segments: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace the segment layout, removing any extras that remain."""
        try:
            current = await self.state(host, http_port)
        except Exception as exc:
            return {"host": host, "ok": False, "error": f"could not read state: {exc}"}

        existing = len([s for s in current.get("seg", []) if s.get("stop", 0) > s.get("start", 0)])
        payload = {"seg": segments + _deletions(existing, len(segments))}
        try:
            async with self.session.post(
                f"{self._base(host, http_port)}/json/state", json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                body = await r.text()
                ok = r.status < 300
        except Exception as exc:
            return {"host": host, "ok": False, "error": str(exc)}

        log.info("pushed %d segments to %s (%s)", len(segments), host,
                 "ok" if ok else body[:80])
        return {"host": host, "ok": ok, "segments": len(segments),
                "removed": max(existing - len(segments), 0), "response": body[:120]}
