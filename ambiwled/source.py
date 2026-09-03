"""Philips JointSPACE Ambilight poller."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable

import aiohttp
import numpy as np

from .mapping import Layout

log = logging.getLogger(__name__)


class SourcePoller:
    """Polls the TV, emits mapped-ready zone arrays, and doubles as the TV
    presence detector: consecutive failures mean the TV is off."""

    def __init__(
        self,
        cfg: dict[str, Any],
        on_frame: Callable[[np.ndarray, Layout], None],
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.on_frame = on_frame
        self.on_state = on_state
        self.session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

        self.layout = Layout()
        # starting | unconfigured | streaming | ambilight_off | tv_off | error
        self.state = "starting"
        self.ambilight_on: bool | None = None
        self.ambilight_mode: str | None = None
        self._power_checked = 0.0
        self.api_version: int | None = None
        self.consecutive_failures = 0
        self.failed_polls = 0
        self.total_polls = 0
        self.effective_hz = 0.0
        self._latencies: deque[float] = deque(maxlen=40)
        self._frame_times: deque[float] = deque(maxlen=40)
        self._backoff = 1.0
        self.update(cfg)

    # -- config ----------------------------------------------------------

    def update(self, cfg: dict[str, Any]) -> None:
        s = cfg.get("source", {})
        new_ip = s.get("tv_ip", "")
        if getattr(self, "tv_ip", None) not in (None, new_ip):
            # Address changed: re-detect the API version against the new host.
            self.api_version = None
            self.layout = Layout()
        self.tv_ip = new_ip
        self.port = int(s.get("port", 1925))
        self.configured_api = s.get("api_version")
        self.endpoint = s.get("endpoint", "processed")
        self.poll_hz = float(s.get("poll_hz", 20.0))
        self.request_timeout = float(s.get("request_timeout_s", 1.0))
        self.fail_threshold = int(s.get("fail_threshold", 3))
        self.off_probe_interval = float(s.get("off_probe_interval_s", 5.0))
        self.adaptive_backoff = bool(s.get("adaptive_backoff", True))
        self.power_check_interval = float(s.get("ambilight_power_check_s", 3.0))
        if self.configured_api in (1, 6):
            self.api_version = int(self.configured_api)

    # -- metrics ---------------------------------------------------------

    @property
    def median_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        return float(np.median(np.fromiter(self._latencies, dtype=np.float64)) * 1000.0)

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(limit=1, force_close=False, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.request_timeout + 2.0),
            headers={"Connection": "keep-alive"},
        )
        self._task = asyncio.create_task(self._run(), name="source-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            log.info("TV state: %s -> %s", self.state, state)
            self.state = state
            if self.on_state:
                self.on_state(state)

    # -- polling ---------------------------------------------------------

    async def _detect_api_version(self) -> None:
        """GET /6/system; api_version.Major == 1 means use the /1/ paths."""
        if self.configured_api in (1, 6):
            self.api_version = int(self.configured_api)
            return
        for probe in (6, 1):
            url = f"http://{self.tv_ip}:{self.port}/{probe}/system"
            try:
                assert self.session is not None
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                    major = int(data.get("api_version", {}).get("Major", probe))
                    self.api_version = 1 if major == 1 else 6
                    log.info(
                        "TV %s reports API v%s (%s), using /%s/ endpoints",
                        self.tv_ip, major, data.get("name", "?"), self.api_version,
                    )
                    return
            except Exception:
                continue
        log.warning("could not detect TV API version, assuming 6")
        self.api_version = 6

    @staticmethod
    def _parse(payload: dict[str, Any]) -> tuple[Layout, np.ndarray] | None:
        """Flatten the Ambilight JSON into a Layout and an (n, 3) float array.

        Side names and zone counts are read from the payload, never assumed.
        """
        if not isinstance(payload, dict) or not payload:
            return None
        layer = payload.get("layer1")
        if not isinstance(layer, dict):
            # Fall back to the first layer-shaped value present.
            layer = next(
                (v for v in payload.values() if isinstance(v, dict) and v), None
            )
        if not isinstance(layer, dict):
            return None

        sides: list[tuple[str, int]] = []
        values: list[tuple[float, float, float]] = []
        for side, zones in layer.items():
            if not isinstance(zones, dict) or not zones:
                continue
            keys = sorted(zones.keys(), key=lambda k: int(k) if str(k).lstrip("-").isdigit() else 0)
            sides.append((side, len(keys)))
            for k in keys:
                z = zones[k] or {}
                values.append((float(z.get("r", 0)), float(z.get("g", 0)), float(z.get("b", 0))))

        if not sides:
            return None
        return Layout(sides), np.asarray(values, dtype=np.float32)

    async def _check_ambilight_power(self) -> None:
        """The TV can be on with Ambilight switched off; the zones then read
        black, which is indistinguishable from a dark scene.  Ask directly."""
        if self.power_check_interval <= 0:
            self.ambilight_on = None
            return
        now = time.monotonic()
        if now - self._power_checked < self.power_check_interval:
            return
        self._power_checked = now
        base = f"http://{self.tv_ip}:{self.port}/{self.api_version}/ambilight"
        assert self.session is not None
        try:
            async with self.session.get(
                f"{base}/power", timeout=aiohttp.ClientTimeout(total=self.request_timeout)
            ) as r:
                data = await r.json(content_type=None)
            power = data.get("power") if isinstance(data, dict) else None
            if power is None:
                # This TV does not answer /ambilight/power. Unknown, not off:
                # never withhold output on the strength of a missing endpoint.
                self.ambilight_on = None
                return
            on = str(power).lower() == "on"
            if on != self.ambilight_on:
                log.info("TV Ambilight power: %s", "on" if on else "off")
            self.ambilight_on = on
            async with self.session.get(
                f"{base}/mode", timeout=aiohttp.ClientTimeout(total=self.request_timeout)
            ) as r:
                self.ambilight_mode = (await r.json(content_type=None)).get("current")
        except Exception:
            # Not fatal: fall back to treating the stream itself as the signal.
            self.ambilight_on = None

    async def _poll_once(self) -> bool:
        url = f"http://{self.tv_ip}:{self.port}/{self.api_version}/ambilight/{self.endpoint}"
        started = time.monotonic()
        assert self.session is not None
        async with self.session.get(
            url, timeout=aiohttp.ClientTimeout(total=self.request_timeout)
        ) as r:
            if r.status != 200:
                raise aiohttp.ClientResponseError(
                    r.request_info, r.history, status=r.status, message=f"HTTP {r.status}"
                )
            payload = await r.json(content_type=None)
        elapsed = time.monotonic() - started
        self._latencies.append(elapsed)

        parsed = self._parse(payload)
        if parsed is None:
            return False
        layout, zones = parsed

        if layout.sides != self.layout.sides:
            log.info(
                "source layout %s: %s (%d zones)",
                "detected" if not self.layout else "changed",
                ", ".join(f"{n}={c}" for n, c in layout.sides),
                layout.total,
            )
            self.layout = layout

        now = time.monotonic()
        self._frame_times.append(now)
        if len(self._frame_times) > 1:
            span = self._frame_times[-1] - self._frame_times[0]
            self.effective_hz = (len(self._frame_times) - 1) / span if span > 0 else 0.0

        self.on_frame(zones, layout)
        return True

    async def _run(self) -> None:
        while True:
            if not self.tv_ip:
                # Nothing to poll yet; a fresh install sits here until the TV
                # address is entered, and that is not a fault condition.
                self._set_state("unconfigured")
                await asyncio.sleep(2.0)
                continue
            if self.api_version is None:
                await self._detect_api_version()

            interval = (1.0 / max(self.poll_hz, 0.1)) * self._backoff
            tick = time.monotonic()
            self.total_polls += 1
            try:
                ok = await self._poll_once()
                if ok:
                    self.consecutive_failures = 0
                    await self._check_ambilight_power()
                    self._set_state("ambilight_off" if self.ambilight_on is False else "streaming")
                else:
                    raise ValueError("unrecognised Ambilight payload")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.consecutive_failures += 1
                self.failed_polls += 1
                if self.consecutive_failures == self.fail_threshold:
                    log.info("TV unreachable after %d failures (%s)", self.fail_threshold, exc)
                if self.consecutive_failures >= self.fail_threshold:
                    self._set_state("tv_off")
                    self.effective_hz = 0.0
                    self._frame_times.clear()
                    # Keep probing slowly; the poll loop is the wake-up detector.
                    await asyncio.sleep(self.off_probe_interval)
                    continue

            # Adaptive backoff: never queue requests behind a slow TV.
            if self.adaptive_backoff and self._latencies:
                median = float(np.median(np.fromiter(self._latencies, dtype=np.float64)))
                base = 1.0 / max(self.poll_hz, 0.1)
                if median > base * self._backoff:
                    self._backoff = min(self._backoff * 1.25, 10.0)
                elif median < base * self._backoff * 0.6:
                    self._backoff = max(self._backoff / 1.1, 1.0)

            sleep_for = interval - (time.monotonic() - tick)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
