"""Colour processing, applied after mapping and before smoothing."""
from __future__ import annotations

from typing import Any

import numpy as np

# Rec.709 luma, used as the grey axis for saturation.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


class ColourStage:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.auto_level = 1.0     # set by the dimming schedule
        self.update(cfg)

    @property
    def effective_brightness(self) -> float:
        """The linear scale actually applied.

        The eye's response is roughly a power law, so a slider that is linear in
        PWM spends most of its travel looking equally bright and only dims near
        the bottom.  Raising the level to ~2.2 makes the control proportional to
        what you see.  Values above 1.0 stay linear: that is gain, not level.
        """
        level = self.brightness * self.auto_level
        if not self.perceptual or level > 1.0:
            return level
        return max(level, 0.0) ** self.perceptual_gamma

    def update(self, cfg: dict[str, Any]) -> None:
        c = cfg.get("colour", {})
        self.saturation = float(c.get("saturation", 1.0))
        self.brightness = float(c.get("brightness", 1.0))
        self.perceptual = bool(c.get("perceptual_brightness", True))
        self.perceptual_gamma = float(c.get("perceptual_gamma", 2.2))
        self.gamma = float(c.get("gamma", 1.0))
        self.black_floor = float(c.get("black_floor", 0))
        self._gamma_lut = None
        if abs(self.gamma - 1.0) > 1e-6:
            x = np.linspace(0.0, 1.0, 1024, dtype=np.float32)
            self._gamma_lut = (np.power(x, self.gamma) * 255.0).astype(np.float32)

    def apply(self, frame: np.ndarray, edge_scale: np.ndarray | None = None) -> np.ndarray:
        """frame: (n, 3) float32 in 0..255.  Returns a new array.

        `edge_scale`, if given, is a per-pixel multiplier (shape (n,)) applied
        alongside the global brightness level — the run behind the viewer
        typically wants less than the one facing them. It comes from each
        edge's own `brightness`, so it follows the mapping rather than being
        a second thing to keep in sync.
        """
        out = frame

        if abs(self.saturation - 1.0) > 1e-6:
            grey = out @ _LUMA
            out = grey[:, None] + (out - grey[:, None]) * self.saturation

        scale = self.effective_brightness
        if edge_scale is not None:
            out = out * (scale * edge_scale)[:, None]
        elif abs(scale - 1.0) > 1e-6:
            out = out * scale

        out = np.clip(out, 0.0, 255.0)

        if self._gamma_lut is not None:
            idx = (out * (len(self._gamma_lut) - 1) / 255.0).astype(np.intp)
            out = self._gamma_lut[idx]

        if self.black_floor > 0:
            # Per pixel, so a dim colour goes dark without shifting hue first.
            dark = out.max(axis=1) < self.black_floor
            if dark.any():
                out = out.copy()
                out[dark] = 0.0

        return out
