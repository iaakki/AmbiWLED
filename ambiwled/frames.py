"""Output frame generation: decoupled from the source poll rate."""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Any

import numpy as np


class FrameEngine:
    """Holds the current target frame and produces output frames on demand."""

    def __init__(self, cfg: dict[str, Any], led_count: int) -> None:
        self.led_count = led_count
        self.out = np.zeros((led_count, 3), dtype=np.float32)
        self.target = np.zeros((led_count, 3), dtype=np.float32)
        self.prev_target = np.zeros((led_count, 3), dtype=np.float32)
        self.target_time: float | None = None
        self.prev_target_time: float | None = None
        self._intervals: deque[float] = deque(maxlen=30)
        self.update(cfg)

    # -- config ----------------------------------------------------------

    def update(self, cfg: dict[str, Any]) -> None:
        f = cfg.get("frames", {})
        self.mode = f.get("mode", "smoothing")
        self.output_fps = float(f.get("output_fps", 60.0))
        self.tau = max(float(f.get("tau_ms", 80.0)), 1.0) / 1000.0
        self.adaptive_alpha = bool(f.get("adaptive_alpha", True))
        self.cut_threshold = max(float(f.get("cut_threshold", 100.0)), 1.0)

    def resize(self, led_count: int) -> None:
        if led_count == self.led_count:
            return
        self.led_count = led_count
        self.out = np.zeros((led_count, 3), dtype=np.float32)
        self.target = np.zeros((led_count, 3), dtype=np.float32)
        self.prev_target = np.zeros((led_count, 3), dtype=np.float32)

    # -- input -----------------------------------------------------------

    def push(self, frame: np.ndarray, now: float | None = None) -> None:
        """A newly mapped source frame arrived."""
        now = time.monotonic() if now is None else now
        if frame.shape != self.target.shape:
            self.resize(frame.shape[0])
        if self.target_time is not None:
            self._intervals.append(now - self.target_time)
        self.prev_target = self.target
        self.prev_target_time = self.target_time
        self.target = frame.astype(np.float32, copy=False)
        self.target_time = now

    def reset(self) -> None:
        """Forget state, e.g. after the TV goes off."""
        self.out[:] = 0.0
        self.target[:] = 0.0
        self.prev_target[:] = 0.0
        self.target_time = None
        self.prev_target_time = None
        self._intervals.clear()

    @property
    def source_interval(self) -> float:
        """Rolling median of observed source arrival intervals."""
        if not self._intervals:
            return 1.0 / 20.0
        return float(np.median(np.fromiter(self._intervals, dtype=np.float64)))

    # -- output ----------------------------------------------------------

    def tick(self, dt: float, now: float | None = None) -> np.ndarray:
        now = time.monotonic() if now is None else now

        if self.mode == "passthrough":
            self.out = self.target.copy()
            return self.out

        if self.mode == "interpolation":
            interval = self.source_interval
            if self.prev_target_time is not None and interval > 0:
                t = (now - self.target_time) / interval
                t = min(max(t, 0.0), 1.0)
                # Interpolate between the last two source frames: always one
                # full source frame behind, by construction.
                self.out = self.prev_target + (self.target - self.prev_target) * t
            else:
                self.out = self.target.copy()
            return self.out

        # smoothing
        alpha = 1.0 - math.exp(-dt / self.tau)
        delta = self.target - self.out
        if self.adaptive_alpha:
            boost = np.clip(np.abs(delta).max(axis=1) / self.cut_threshold, 0.0, 1.0)
            a = alpha + (1.0 - alpha) * boost
            self.out = self.out + delta * a[:, None]
        else:
            self.out = self.out + delta * alpha
        return self.out
