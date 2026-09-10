"""Output target presence.

UDP is fire-and-forget, so a dead controller looks exactly like a live one from
the sender's side.  Poll each target's HTTP API so the bridge knows whether
anything is actually listening, and can stop sending when nothing is.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .wled import WledClient

import aiohttp

log = logging.getLogger(__name__)


class TargetHealth:
    """Tracks reachability of each configured output target."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self.status: dict[str, dict[str, Any]] = {}
        self.presets: dict[str, dict[int, str]] = {}
        self._wled: WledClient | None = None
        self.info: dict[str, dict[str, Any]] = {}
        self.update(cfg)

    def update(self, cfg: dict[str, Any]) -> None:
        o = cfg.get("output", {})
        self.interval = float(o.get("presence_check_s", 10.0))
        self.require_online = bool(o.get("require_online", True))
        self.timeout = float(o.get("presence_timeout_s", 2.0))
        self.fail_threshold = int(o.get("presence_fail_threshold", 2))
        self.hosts = [t["host"] for t in o.get("targets", [])
                      if t.get("enabled", True) and t.get("host")]
        # The controller's HTTP port, which is not the realtime UDP port.
        self.http_ports = {t["host"]: int(t.get("http_port", 80))
                           for t in o.get("targets", [])
                           if t.get("enabled", True) and t.get("host")}
        for host in list(self.status):
            if host not in self.hosts:
                del self.status[host]
                self.presets.pop(host, None)
                self.info.pop(host, None)
        for host in self.hosts:
            self.status.setdefault(host, {"online": None, "failures": 0, "checked": 0.0})

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def any_online(self) -> bool:
        """True when emission should proceed.

        Unknown (not yet checked) counts as online, so a slow first check never
        withholds the very first frames.
        """
        if not self.require_online or not self.enabled or not self.hosts:
            return True
        return any(self.status.get(h, {}).get("online") is not False for h in self.hosts)

    def online_hosts(self) -> list[str]:
        return [h for h in self.hosts if self.status.get(h, {}).get("online") is not False]

    async def start(self) -> None:
        if not self.enabled:
            return
        self.session = aiohttp.ClientSession()
        self._wled = WledClient(self.session)
        self._task = asyncio.create_task(self._run(), name="target-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()
            self.session = None

    async def check_once(self, host: str) -> bool:
        """A WLED controller answers /json/info; anything else answering counts too.

        The same response carries the controller's own telemetry, so reachability
        and "is it keeping up" cost one request rather than two.
        """
        assert self.session is not None
        try:
            port = self.http_ports.get(host, 80)
            async with self.session.get(
                f"http://{host}:{port}/json/info",
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                if r.status >= 500:
                    return False
                try:
                    self._record_info(host, await r.json(content_type=None))
                except Exception:
                    pass          # reachable but not WLED; presence still counts
                if self._wled is not None:
                    self.presets[host] = await self._wled.list_presets(host, port)
                return True
        except Exception:
            return False

    def _record_info(self, host: str, info: dict[str, Any]) -> None:
        leds = info.get("leds") or {}
        self.info[host] = {
            "version": info.get("ver"),
            "name": info.get("name"),
            "fps": leds.get("fps"),
            "power_ma": leds.get("pwr"),
            "led_count": leds.get("count"),
            "brightness_limited": bool(leds.get("maxpwr")) and bool(leds.get("pwr"))
                                  and leds.get("pwr", 0) >= leds.get("maxpwr", 0) > 0,
            "realtime": info.get("live"),
            "realtime_source": info.get("lm"),
            "uptime_s": info.get("uptime"),
            "free_heap": info.get("freeheap"),
        }

    def _record(self, host: str, ok: bool) -> None:
        state = self.status.setdefault(host, {"online": None, "failures": 0, "checked": 0.0})
        was = state["online"]
        state["checked"] = time.time()
        if ok:
            state["failures"] = 0
            state["online"] = True
        else:
            state["failures"] += 1
            if state["failures"] >= self.fail_threshold:
                state["online"] = False
        if state["online"] != was:
            log.info("output target %s is %s", host,
                     "reachable" if state["online"] else "unreachable")

    async def _run(self) -> None:
        while True:
            if self.enabled and self.hosts:
                results = await asyncio.gather(
                    *(self.check_once(h) for h in self.hosts), return_exceptions=True
                )
                for host, ok in zip(self.hosts, results):
                    self._record(host, ok is True)
            # Re-probe an unreachable target sooner than a healthy one.
            offline = any(s.get("online") is False for s in self.status.values())
            await asyncio.sleep(max(self.interval / 3 if offline else self.interval, 1.0))
