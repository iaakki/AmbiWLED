"""Frame generation: smoothing must not overshoot, interpolation must lag by one."""
from __future__ import annotations

import numpy as np

from ambiwled.frames import FrameEngine


def engine(**overrides):
    cfg = {"frames": {"mode": "smoothing", "output_fps": 60.0, "tau_ms": 80.0,
                      "adaptive_alpha": False, "cut_threshold": 100.0}}
    cfg["frames"].update(overrides)
    return FrameEngine(cfg, led_count=4)


def solid(value):
    return np.full((4, 3), value, dtype=np.float32)


def test_passthrough_is_identity():
    e = engine(mode="passthrough")
    e.push(solid(200))
    assert np.allclose(e.tick(1 / 60), 200)


def test_smoothing_converges_without_overshoot():
    e = engine()
    e.push(solid(255))
    prev = 0.0
    for _ in range(200):
        out = float(e.tick(1 / 60)[0][0])
        assert out >= prev - 1e-6      # monotone
        assert out <= 255.0 + 1e-6     # never overshoots the target
        prev = out
    assert out > 254.0                 # and does get there


def test_smoothing_respects_tau():
    """After one tau, an exponential filter covers ~63% of the distance."""
    e = engine(tau_ms=100.0)
    e.push(solid(100))
    out = e.tick(0.1)                  # dt == tau
    assert 60.0 < float(out[0][0]) < 66.0


def test_smoothing_is_slower_with_a_larger_tau():
    fast, slow = engine(tau_ms=20.0), engine(tau_ms=500.0)
    fast.push(solid(255)); slow.push(solid(255))
    assert float(fast.tick(1 / 60)[0][0]) > float(slow.tick(1 / 60)[0][0])


def test_adaptive_alpha_makes_cuts_snappier():
    plain, adaptive = engine(adaptive_alpha=False), engine(adaptive_alpha=True)
    plain.push(solid(255)); adaptive.push(solid(255))
    assert float(adaptive.tick(1 / 60)[0][0]) > float(plain.tick(1 / 60)[0][0])


def test_adaptive_alpha_leaves_small_changes_smooth():
    """A gentle pan must not be sharpened; only cuts should be."""
    plain, adaptive = engine(adaptive_alpha=False), engine(adaptive_alpha=True)
    for e in (plain, adaptive):
        e.out = solid(100).copy()
        e.push(solid(102))             # delta of 2, far below cut_threshold
    a = float(adaptive.tick(1 / 60)[0][0])
    p = float(plain.tick(1 / 60)[0][0])
    assert abs(a - p) < 0.05


def test_adaptive_alpha_is_per_pixel():
    e = engine(adaptive_alpha=True)
    e.out = np.zeros((4, 3), dtype=np.float32)
    target = np.zeros((4, 3), dtype=np.float32)
    target[0] = 255                     # one pixel cuts, the rest hold
    e.push(target)
    out = e.tick(1 / 60)
    assert out[0][0] > out[1][0]


def test_interpolation_lags_by_one_source_frame():
    e = engine(mode="interpolation")
    e.push(solid(0), now=0.0)
    e.push(solid(100), now=0.05)        # 50 ms apart -> 20 fps source
    # Right after the newer frame arrives we are still showing the older one.
    assert float(e.tick(1 / 60, now=0.05)[0][0]) == 0.0
    # Half an interval later, half way between them.
    assert 45.0 < float(e.tick(1 / 60, now=0.075)[0][0]) < 55.0
    # A full interval later, fully arrived at the newer frame.
    assert float(e.tick(1 / 60, now=0.10)[0][0]) == 100.0


def test_interpolation_does_not_extrapolate_past_the_target():
    e = engine(mode="interpolation")
    e.push(solid(0), now=0.0)
    e.push(solid(100), now=0.05)
    assert float(e.tick(1 / 60, now=5.0)[0][0]) == 100.0   # stale, but clamped


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
    e.tick(1 / 60)
    e.reset()
    assert np.allclose(e.out, 0.0)
    assert np.allclose(e.target, 0.0)


def test_resize_follows_a_layout_change():
    e = engine()
    e.push(np.full((8, 3), 50, dtype=np.float32))
    assert e.out.shape == (8, 3)
    assert np.allclose(e.tick(1.0), 50, atol=1.0)
