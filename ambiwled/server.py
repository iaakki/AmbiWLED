"""aiohttp web server: static UI, config REST API, live preview WebSocket."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from . import config as config_mod
from .bridge import Bridge
from .wled import (WledClient, front_to_back_segment, segments_from_edges)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SAVE_DEBOUNCE_S = 0.75


class Server:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        bridge.on_config_change = self.apply_patch
        self.app = web.Application()
        self.clients: set[web.WebSocketResponse] = set()
        self._save_task: asyncio.Task | None = None
        self._save_pending = False
        self._preview_task: asyncio.Task | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        a = self.app
        a.router.add_get("/", self.index)
        a.router.add_get("/api/state", self.get_state)
        a.router.add_get("/api/config", self.get_config)
        a.router.add_put("/api/config", self.put_config)
        a.router.add_post("/api/config/reset", self.reset_config)
        a.router.add_post("/api/config/validate", self.validate_config)
        a.router.add_post("/api/wled/segments", self.push_segments)
        a.router.add_post("/api/resolve-link", self.resolve_link)
        a.router.add_get("/ws", self.websocket)
        a.router.add_static("/static/", STATIC_DIR, name="static")

    # -- pages -----------------------------------------------------------

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    # -- config API ------------------------------------------------------

    async def get_state(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "metrics": self.bridge.metrics(),
                "config": config_mod.redacted(self.bridge.cfg),
                "detected_layout": self.bridge.poller.layout.as_dict(),
                "validation": config_mod.validate(self.bridge.cfg),
            }
        )

    async def get_config(self, request: web.Request) -> web.Response:
        response = web.json_response(config_mod.redacted(self.bridge.cfg))
        if request.query.get("download"):
            response.headers["Content-Disposition"] = 'attachment; filename="ambiwled-config.json"'
        return response

    async def validate_config(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"errors": ["body is not valid JSON"]}, status=400)
        candidate = config_mod.merge_defaults(body)
        return web.json_response({"errors": config_mod.validate(candidate)})

    async def put_config(self, request: web.Request) -> web.Response:
        """Accepts a full config or a partial patch; applies live on success."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"errors": ["body is not valid JSON"]}, status=400)

        patch = body.get("patch") if isinstance(body, dict) and "patch" in body else body
        errors = self.apply_patch(patch)
        if errors:
            return web.json_response({"errors": errors}, status=400)
        return web.json_response({"errors": [], "config": config_mod.redacted(self.bridge.cfg)})

    async def reset_config(self, request: web.Request) -> web.Response:
        cfg = config_mod.default_config()
        self.bridge.apply_config(cfg)
        self._schedule_save()
        return web.json_response({"errors": [], "config": config_mod.redacted(cfg)})

    def apply_patch(self, patch: dict[str, Any]) -> list[str]:
        """Validate, apply and persist a partial config change.

        Shared by the HTTP API and MQTT so a command from Home Assistant is
        subject to exactly the same checks as one from the UI.
        """
        candidate = config_mod.merge_defaults(config_mod._merge(self.bridge.cfg, patch))
        candidate = config_mod.unredact(candidate, self.bridge.cfg)
        errors = config_mod.validate(candidate)
        if errors:
            log.warning("rejected config change %s: %s", patch, "; ".join(errors))
            return errors
        self.bridge.apply_config(candidate)
        self._schedule_save()
        return []

    def _schedule_save(self) -> None:
        """Write now, and again shortly after in case more edits follow.

        The write is a few kilobytes, so there is no reason to leave a window
        where an applied change exists only in memory: a restart in that window
        loses it silently, which is exactly what used to happen during a deploy.
        """
        self._save_pending = True
        self._flush_save()
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = asyncio.create_task(self._debounced_save())

    async def _debounced_save(self) -> None:
        """Catches anything that arrived while the immediate write was in flight."""
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(SAVE_DEBOUNCE_S)
            self._save_pending = True
            self._flush_save()

    def flush_config(self) -> None:
        """Public: write any outstanding change to disk right now."""
        self._save_pending = True
        self._flush_save()

    def _flush_save(self) -> None:
        """Write immediately.  Called by the debounce and again on shutdown, so
        a change made in the last moments before a stop is never lost."""
        if not self._save_pending:
            return
        try:
            config_mod.save(self.bridge.cfg)
            self._save_pending = False
            log.debug("config persisted")
        except Exception:
            log.exception("could not persist config")

    # -- websocket -------------------------------------------------------

    async def push_segments(self, request: web.Request) -> web.Response:
        """Write the edge layout to the controller as segments.

        Deliberately an explicit action, not something that happens on every
        config change: it rewrites state on someone else's device.
        """
        # 'edges'         - one segment per ceiling run, each with its own effect
        # 'front_to_back' - one mirrored segment, offset to the front centre, so a
        #                   single effect curls from the front round to the rear
        mode = request.query.get("mode", "edges")
        if mode not in ("edges", "front_to_back"):
            return web.json_response(
                {"errors": ["mode must be 'edges' or 'front_to_back'"]}, status=400)
        preset = request.query.get("preset")

        targets = [t for t in self.bridge.cfg["output"]["targets"] if t.get("enabled", True)]
        if not targets:
            return web.json_response({"errors": ["no enabled output targets"]}, status=400)

        cfg = self.bridge.cfg
        segments = (front_to_back_segment(cfg) if mode == "front_to_back"
                    else segments_from_edges(cfg))

        results = []
        async with aiohttp.ClientSession() as session:
            client = WledClient(session)
            for t in targets:
                host, port = t["host"], int(t.get("http_port", 80))
                step: dict[str, Any] = {"host": host}
                seg = await client.apply_segments(host, port, segments)
                step.update(seg)
                if seg.get("ok"):
                    # Neither layout uses a ledmap; make sure none is selected.
                    step["ledmap_select"] = await client.set_ledmap(host, port, 0)
                    if preset:
                        step["preset"] = await client.save_preset(
                            host, port, int(preset),
                            "Front to back" if mode == "front_to_back" else "Ceiling edges")
                        step["ok"] = step["preset"].get("ok", False)
                results.append(step)

        ok = all(r.get("ok") for r in results)
        return web.json_response(
            {"ok": ok, "mode": mode, "results": results, "segments": segments},
            status=200 if ok else 502)

    async def resolve_link(self, request: web.Request) -> web.Response:
        """Follow a redirect chain and report where it actually lands.

        For the auto-dim location picker: a shortened map link (Google's
        `maps.app.goo.gl` share links, the default from its mobile Share
        button) carries no coordinates itself — they only appear after the
        redirect. Browsers block client-side JS from reading a cross-origin
        redirect's destination, so this does it server-side, where that
        restriction does not apply. The browser's own `parseCoordinates()`
        stays the one place that understands URL formats; this only returns
        the resolved URL for it to run on.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body is not valid JSON"}, status=400)
        url = str(body.get("url", "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            return web.json_response({"error": "not a http(s) URL"}, status=400)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=6.0),
                ) as r:
                    resolved = str(r.url)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=502)
        return web.json_response({"resolved_url": resolved})

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_json({"type": "hello", "config": config_mod.redacted(self.bridge.cfg)})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "identify":
                    span = None
                    if data.get("start") is not None and data.get("count") is not None:
                        span = (int(data["start"]), int(data["count"]))
                    self.bridge.identify(
                        edge=data.get("edge"),
                        seconds=float(data.get("seconds", 10.0)),
                        colour=tuple(data.get("colour", (255, 255, 255))),
                        span=span,
                    )
        finally:
            self.clients.discard(ws)
        return ws

    async def _preview_loop(self) -> None:
        """Pixels as binary frames, metrics as JSON a few times a second."""
        counter = 0
        while True:
            fps = max(float(self.bridge.cfg["web"].get("preview_fps", 15.0)), 1.0)
            if self.clients:
                payload = self.bridge.last_frame.tobytes()
                counter += 1
                send_metrics = counter % max(int(fps / 3), 1) == 0
                metrics = json.dumps({"type": "metrics", **self.bridge.metrics()}) if send_metrics else None
                for ws in list(self.clients):
                    try:
                        await ws.send_bytes(payload)
                        if metrics:
                            await ws.send_str(metrics)
                    except Exception:
                        self.clients.discard(ws)
            await asyncio.sleep(1.0 / fps)

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> web.AppRunner:
        web_cfg = self.bridge.cfg.get("web", {})
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, web_cfg.get("host", "0.0.0.0"), int(web_cfg.get("port", 8080)))
        await site.start()
        self._preview_task = asyncio.create_task(self._preview_loop(), name="preview")
        log.info("web UI on http://%s:%s", web_cfg.get("host"), web_cfg.get("port"))
        return runner

    async def stop(self, runner: web.AppRunner) -> None:
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        self._flush_save()
        if self._preview_task:
            self._preview_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._preview_task
        for ws in list(self.clients):
            with contextlib.suppress(Exception):
                await ws.close()
        await runner.cleanup()
