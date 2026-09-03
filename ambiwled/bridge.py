"""Wires the source, mapping, colour, frame engine and output together."""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import resource
import time
from collections import deque
from typing import Any

import numpy as np

from . import config as config_mod
from .colour import ColourStage
from .daylight import DimmingSchedule
from .frames import FrameEngine
import aiohttp

from .health import TargetHealth
from .mqtt import MqttBridge
from .wled import WledClient
from .mapping import Layout, Mapper
from .output import Sender
from .source import SourcePoller

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.led_count = int(cfg["led"]["count"])

        self.mapper = Mapper()
        self.colour = ColourStage(cfg)
        self.dimming = DimmingSchedule(cfg)
        self.engine = FrameEngine(cfg, self.led_count)
        self.sender = Sender(cfg)
        self.health = TargetHealth(cfg)
        self.mqtt = MqttBridge(cfg, self.metrics, self._on_mqtt_command,
                               self._on_mqtt_action)
        self.poller = SourcePoller(cfg, self._on_zones, self._on_state)

        self.zones: np.ndarray | None = None
        self.mapped = np.zeros((self.led_count, 3), dtype=np.float32)
        self.last_frame = np.zeros((self.led_count, 3), dtype=np.uint8)

        self.identify_edge: str | None = None
        self.identify_span: tuple[int, int] | None = None
        self.identify_until = 0.0
        self.identify_colour = (255, 255, 255)

        self.edge_scale: np.ndarray | None = None
        self.output_fps_actual = 0.0
        self._dim_checked = 0.0
        # A resampled percentage, not a lifetime average - CPU spikes (a
        # scene cut driving adaptive alpha hard, an identify chase) show up
        # within a couple of seconds instead of being smoothed away by
        # everything that came before.
        self.cpu_percent = 0.0
        self._cpu_checked = 0.0
        self._cpu_last_wall = time.monotonic()
        self._cpu_last_proc = self._proc_cpu_seconds()
        self._emitting_since: float | None = None
        # Preset-mode arbiter: retries a selection until confirmed, so it
        # works whether the controller accepts it immediately or only once
        # the realtime stream releases - never assumed, always confirmed.
        self._preset_target: int | None = None
        self._preset_applied = False
        self._preset_pending = False
        self._preset_last_attempt = 0.0
        self._preset_last_error: str | None = None
        if self.cfg.get("mode") == "preset":
            self._preset_target = self.cfg.get("preset_slot")
        # Set by the server, so MQTT commands take the validated path.
        self.on_config_change: Any = None
        self._tick_times: deque[float] = deque(maxlen=60)
        self._out_task: asyncio.Task | None = None
        self._dirty = True

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self.health.start()
        await self.mqtt.start()
        await self.poller.start()
        self._out_task = asyncio.create_task(self._output_loop(), name="output-loop")

    async def stop(self) -> None:
        if self._out_task:
            self._out_task.cancel()
            try:
                await self._out_task
            except asyncio.CancelledError:
                pass
        await self.poller.stop()
        await self.mqtt.stop()
        await self.health.stop()
        self.sender.close()

    # -- source callbacks ------------------------------------------------

    def _on_zones(self, zones: np.ndarray, layout: Layout) -> None:
        if self._dirty or layout.sides != self.mapper.layout.sides or self.mapper.matrix is None:
            self._rebuild(layout)
        self.zones = zones
        matrix = self.mapper.matrix
        if matrix is None or matrix.shape[1] != zones.shape[0]:
            return
        mapped = matrix @ zones
        self.mapped = self.colour.apply(mapped, self.edge_scale)
        self.engine.push(self.mapped)

    @property
    def mode(self) -> str:
        return str(self.cfg.get("mode", "ambilight"))

    def should_emit(self) -> bool:
        """Whether a packet is worth sending at all.

        In `off` we stay silent so WLED's realtime timeout hands control back to
        its own preset — that is how the ceiling returns to your boot preset.
        In `ambilight` the TV must be streaming with its Ambilight actually on,
        and a controller must be reachable.
        """
        if self.mode in ("off", "preset"):
            return False
        if not self.health.any_online():
            return False
        return self.poller.state == "streaming" and self.poller.ambilight_on is not False

    def _output_conflict(self) -> str | None:
        """Whether our stream is actually reaching the strip.

        Two ways it may not be, and they look identical from here because UDP
        reports nothing back:
        - another realtime source is feeding the controller (flicker), or
        - the controller has been told locally to ignore realtime and run its
          own effect, which is a legitimate thing to do from the WLED app.

        Only judged after we have been emitting for a while, so a slow health
        poll right after startup is never mistaken for either.
        """
        if not self.should_emit() or self._emitting_since is None:
            return None
        if time.monotonic() - self._emitting_since < 30.0:
            return None
        for host, info in self.health.info.items():
            if not info:
                continue
            source = info.get("realtime_source")
            if info.get("realtime"):
                if source and source not in ("UDP", "WLED"):
                    return f"{host} is taking realtime data from {source}, not from us"
            else:
                return (f"{host} is ignoring our stream - it is running its own "
                        f"effect (realtime overridden locally)")
        return None

    def _on_state(self, state: str) -> None:
        if state in ("tv_off", "ambilight_off"):
            # Stop emitting entirely; WLED's realtime timeout restores its preset.
            self.engine.reset()

    def _on_mqtt_command(self, patch: dict[str, Any]) -> None:
        """Apply a command from the broker through the same path as the UI, so
        it is validated and persisted rather than poked into memory."""
        if self.on_config_change is None:
            log.warning("MQTT command arrived before the server was ready")
            return
        self.on_config_change(patch)

    def _on_mqtt_action(self, action: str, payload: dict[str, Any]) -> None:
        """Actions that are not settings, so they never touch the config."""
        if action == "identify":
            edge = payload.get("edge")
            known = {str(e.get("name")) for e in self.cfg["mapping"]["edges"]}
            if edge in known:
                self.identify(edge=edge, seconds=10.0)
            else:
                log.warning("identify asked for unknown edge %r", edge)

    async def _apply_preset(self, slot: int) -> None:
        self._preset_pending = True
        try:
            targets = [t for t in self.cfg["output"]["targets"] if t.get("enabled", True)]
            if not targets:
                self._preset_last_error = "no enabled output targets"
                return
            errors = []
            async with aiohttp.ClientSession() as session:
                client = WledClient(session)
                for t in targets:
                    res = await client.select_preset(
                        t["host"], int(t.get("http_port", 80)), slot)
                    if not res.get("ok"):
                        errors.append(res.get("error") or f"{t['host']}: not confirmed yet")
            if not errors:
                if slot == self._preset_target:      # nothing changed the target mid-flight
                    self._preset_applied = True
                    self._preset_last_error = None
            else:
                self._preset_last_error = "; ".join(errors)
        except Exception as exc:
            self._preset_last_error = str(exc)
        finally:
            self._preset_pending = False

    def _resolve_preset_name(self) -> str | None:
        slot = self.cfg.get("preset_slot")
        if not slot:
            return None
        for names in self.health.presets.values():
            if slot in names:
                return names[slot]
        return None

    def _rebuild(self, layout: Layout | None = None) -> None:
        layout = layout if layout is not None else self.mapper.layout
        self.led_count = int(self.cfg["led"]["count"])
        self.engine.resize(self.led_count)
        self.mapper.build(self.cfg, layout)
        self.edge_scale = self._build_edge_scale()
        if self.mapper.problems:
            log.warning("mapping problems: %s", "; ".join(self.mapper.problems))
        else:
            log.info(
                "mapping built: %d pixels from %d zones over %d edges",
                self.led_count, layout.total, len(self.cfg["mapping"]["edges"]),
            )
        self._dirty = False

    def _build_edge_scale(self) -> np.ndarray | None:
        """Per-pixel brightness multiplier from each edge's own `brightness`.

        None when every edge is 1.0, so the colour stage takes its cheaper
        scalar path in the common case rather than always multiplying an array.
        """
        scale = np.ones(self.led_count, dtype=np.float32)
        uniform = True
        for e in self.cfg["mapping"]["edges"]:
            start = int(e["pixel_start"])
            end = min(start + int(e["pixel_count"]), self.led_count)
            bri = float(e.get("brightness", 1.0))
            if end > start:
                scale[start:end] = bri
            if abs(bri - 1.0) > 1e-6:
                uniform = False
        return None if uniform else scale

    # -- config ----------------------------------------------------------

    def apply_config(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        slot = cfg.get("preset_slot") if cfg.get("mode") == "preset" else None
        if slot != self._preset_target:
            self._preset_target = slot
            self._preset_applied = False
            self._preset_last_attempt = 0.0
            self._preset_last_error = None
        self.colour.update(cfg)
        self.dimming.update(cfg)
        self.engine.update(cfg)
        self.sender.update(cfg)
        self.health.update(cfg)
        self.mqtt.update(cfg)
        self.poller.update(cfg)
        # The output loop only re-samples the dimming schedule every 20s -
        # fine for the sun moving, wrong for a config change. Night level,
        # day level, and the enabled switch itself all take visible
        # brightness with them; nothing about this change should wait up to
        # 20s to reach the strip.
        self.colour.auto_level = self.dimming.level()
        self._dim_checked = time.monotonic()
        self._dirty = True
        self._rebuild()

    # -- identify --------------------------------------------------------

    def identify(
        self,
        edge: str | None = None,
        seconds: float = 10.0,
        colour: tuple[int, int, int] = (255, 255, 255),
        span: tuple[int, int] | None = None,
    ) -> None:
        """Light one edge, or an explicit (start, count) range.

        The range form deliberately does not consult the stored config, so the
        UI can identify an edge it is still editing — that is exactly when you
        need to check which physical run you are pointing at.
        """
        self.identify_edge = edge
        self.identify_span = span
        self.identify_colour = colour
        self.identify_until = time.monotonic() + seconds if (edge or span) else 0.0

    # A moving comet, not a static fill: the whole point of identifying a
    # named edge is checking that the direction it appears to run in on the
    # real strip matches what `reversed` tells the rest of the pipeline -
    # exactly the thing a solid colour cannot show. The span form (used
    # while an edge is still being edited, before it has a name to look up)
    # has no `reversed` of its own to check, so it stays a plain fill.
    _CHASE_PERIOD_S = 1.6
    _CHASE_WIDTH_PX = 4.0

    def _identify_frame(self) -> np.ndarray | None:
        active = self.identify_edge or self.identify_span
        if not active or time.monotonic() > self.identify_until:
            if active:
                self.identify_edge = None
                self.identify_span = None
            return None

        frame = np.zeros((self.led_count, 3), dtype=np.float32)
        if self.identify_span:
            start, count = self.identify_span
            start = max(0, min(start, self.led_count))
            end = max(start, min(start + count, self.led_count))
            frame[start:end] = self.identify_colour
            return frame
        for e in self.cfg["mapping"]["edges"]:
            if e.get("name") == self.identify_edge:
                start = int(e["pixel_start"])
                n = min(int(e["pixel_count"]), self.led_count - start)
                if n > 0:
                    self._paint_chase(frame, start, n, bool(e.get("reversed")))
                return frame
        return frame

    def _paint_chase(self, frame: np.ndarray, start: int, n: int, reversed_: bool) -> None:
        """A short comet sweeping the edge's own pixel order - ascending
        unless `reversed`, so flipping that field visibly flips which way
        the comet runs the next time Identify is pressed."""
        t = (time.monotonic() % self._CHASE_PERIOD_S) / self._CHASE_PERIOD_S
        if reversed_:
            t = 1.0 - t
        head = t * n
        idx = np.arange(n, dtype=np.float32)
        dist = np.abs(idx - head)
        dist = np.minimum(dist, n - dist)  # wrap, so the tail doesn't clip at the loop point
        intensity = np.clip(1.0 - dist / self._CHASE_WIDTH_PX, 0.0, 1.0) ** 1.5
        colour = np.asarray(self.identify_colour, dtype=np.float32)
        frame[start : start + n] = intensity[:, None] * colour

    # -- output loop -----------------------------------------------------

    async def _output_loop(self) -> None:
        next_tick = time.monotonic()
        while True:
            fps = max(float(self.cfg["frames"].get("output_fps", 60.0)), 1.0)
            period = 1.0 / fps
            now = time.monotonic()

            if now - self._dim_checked > 20.0:
                self._dim_checked = now
                self.colour.auto_level = self.dimming.level()

            if now - self._cpu_checked > 2.0:
                proc_now = self._proc_cpu_seconds()
                wall_dt = now - self._cpu_last_wall
                if wall_dt > 0:
                    self.cpu_percent = round(100.0 * (proc_now - self._cpu_last_proc) / wall_dt, 1)
                self._cpu_last_wall, self._cpu_last_proc, self._cpu_checked = now, proc_now, now

            if (self._preset_target is not None and not self._preset_applied
                    and not self._preset_pending and now - self._preset_last_attempt > 2.0):
                self._preset_last_attempt = now
                asyncio.create_task(self._apply_preset(self._preset_target))

            ident = self._identify_frame()
            if ident is not None:
                frame = ident
                emit = True
            else:
                frame = self.engine.tick(now)
                emit = self.should_emit()

            self.last_frame = np.clip(frame + 0.5, 0, 255).astype(np.uint8)
            if emit:
                if self._emitting_since is None:
                    self._emitting_since = now
                self.sender.send(frame)
            else:
                self._emitting_since = None

            self._tick_times.append(now)
            if len(self._tick_times) > 1:
                span = self._tick_times[-1] - self._tick_times[0]
                self.output_fps_actual = (len(self._tick_times) - 1) / span if span > 0 else 0.0

            # Absolute schedule, so sleep overhead does not accumulate into drift.
            next_tick += period
            slack = next_tick - time.monotonic()
            if slack < -period:
                next_tick = time.monotonic() + period   # fell far behind, resync
                slack = period
            await asyncio.sleep(max(slack, 0.0))

    # -- introspection ---------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        return {
            "source_fps": round(self.poller.effective_hz, 2),
            "output_fps": round(self.output_fps_actual, 1),
            "source_latency_ms": round(self.poller.median_latency_ms, 1),
            "tv_state": self.poller.state,
            "api_version": self.poller.api_version,
            "failed_polls": self.poller.failed_polls,
            "total_polls": self.poller.total_polls,
            "packets_sent": self.sender.packets_sent,
            "send_errors": self.sender.send_errors,
            "emitting": self.should_emit(),
            "ambilight_power": self.poller.ambilight_on,
            "ambilight_mode": self.poller.ambilight_mode,
            "targets_online": self.health.online_hosts(),
            "targets": {h: st.get("online") for h, st in self.health.status.items()},
            "controllers": self.health.info,
            "output_conflict": self._output_conflict(),
            "layout": self.mapper.layout.as_dict() or self.poller.layout.as_dict(),
            "mapping_problems": self.mapper.problems,
            "identify": self.identify_edge,
            "mode": self.mode,
            "preset_slot": self.cfg.get("preset_slot"),
            "preset_applied": self._preset_applied if self.mode == "preset" else None,
            "preset_error": self._preset_last_error,
            "preset_name": self._resolve_preset_name(),
            "presets": self.health.presets,
            "mqtt": self.mqtt.status(),
            "brightness_level": round(self.colour.brightness, 3),
            "brightness_applied": round(self.colour.effective_brightness, 4),
            "dimming": self.dimming.describe(),
            "source_interval_ms": round(self.engine.source_interval * 1000.0, 1),
            "cpu_percent": self.cpu_percent,
            "cpu_count": self._cpu_count(),
            # The host's load average, not a per-container figure - cgroups
            # don't get their own; still the honest answer to "how busy is
            # the machine this is running on".
            "load_avg": os.getloadavg(),
        }

    @staticmethod
    def _proc_cpu_seconds() -> float:
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime

    @staticmethod
    def _cpu_count() -> int:
        # Reflects a cgroup CPU-set limit if one is applied to the
        # container; os.cpu_count() would report the host's full count
        # regardless, which is misleading for "how many cores do I have".
        try:
            return len(os.sched_getaffinity(0))
        except AttributeError:
            return os.cpu_count() or 1

    def preview(self) -> list[int]:
        return self.last_frame.reshape(-1).tolist()
