"""Frame generation: one algorithm - linear interpolation between the last
two real samples, timed against their own observed arrival interval."""
from __future__ import annotations

import numpy as np

from ambiwled.frames import FrameEngine


def engine(**overrides):
    cfg = {"frames": {"output_fps": 60.0}}
    cfg["frames"].update(overrides)
    return FrameEngine(cfg, led_count=4)


def solid(value):
    return np.full((4, 3), value, dtype=np.float32)


def test_first_frame_is_shown_immediately():
    """Nothing to interpolate from yet, so the first push is the output."""
    e = engine()
    e.push(solid(200))
    assert np.allclose(e.tick(), 200)


def test_interpolation_lags_by_one_source_frame():
    e = engine()
    e.push(solid(0), now=0.0)
    e.push(solid(100), now=0.05)        # 50 ms apart -> 20 fps source
    # Right after the newer frame arrives we are still showing the older one.
    assert float(e.tick(now=0.05)[0][0]) == 0.0
    # Half an interval later, half way between them.
    assert 45.0 < float(e.tick(now=0.075)[0][0]) < 55.0
    # A full interval later, fully arrived at the newer frame.
    assert float(e.tick(now=0.10)[0][0]) == 100.0


def test_interpolation_does_not_extrapolate_past_the_target():
    e = engine()
    e.push(solid(0), now=0.0)
    e.push(solid(100), now=0.05)
    assert float(e.tick(now=5.0)[0][0]) == 100.0   # stale, but clamped


def test_output_fps_equal_to_poll_rate_is_still_just_interpolation():
    """`output_fps == poll_hz` means "no interpolation" in the UI's terms,
    but the engine itself does not need a separate mode for it - ticking at
    the same rate the source arrives just rarely has anything to blend."""
    e = engine(output_fps=20.0)
    e.push(solid(0), now=0.0)
    e.push(solid(100), now=0.05)
    assert float(e.tick(now=0.05)[0][0]) == 0.0
    assert float(e.tick(now=0.10)[0][0]) == 100.0


def test_source_interval_uses_observed_arrivals_not_configured_rate():
    e = engine()
    t = 0.0
    for _ in range(10):
        t += 0.1                        # a 10 fps source
        e.push(solid(1), now=t)
    assert 0.09 < e.source_interval < 0.11


def test_reset_clears_state():
    e = engine()
    e.push(solid(255))
    e.tick()
    e.reset()
    assert np.allclose(e.out, 0.0)
    assert np.allclose(e.target, 0.0)


def test_resize_follows_a_layout_change():
    e = engine()
    e.push(np.full((8, 3), 50, dtype=np.float32))
    assert e.out.shape == (8, 3)
    # First frame after a resize: nothing to interpolate from, shown as-is.
    assert np.allclose(e.tick(), 50)
