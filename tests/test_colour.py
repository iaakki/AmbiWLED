"""Colour stage: order of operations matters and is easy to regress."""
from __future__ import annotations

import numpy as np
import pytest

from ambiwled.colour import ColourStage


def stage(**overrides):
    # Default to the linear curve so the classic tests keep testing one thing.
    cfg = {"colour": {"saturation": 1.0, "brightness": 1.0, "gamma": 1.0,
                      "black_floor": 0, "perceptual_brightness": False}}
    cfg["colour"].update(overrides)
    return ColourStage(cfg)


def perceptual(**overrides):
    cfg = {"colour": {"saturation": 1.0, "brightness": 1.0, "gamma": 1.0,
                      "black_floor": 0, "perceptual_brightness": True,
                      "perceptual_gamma": 2.2}}
    cfg["colour"].update(overrides)
    return ColourStage(cfg)


def frame(*pixels):
    return np.array(pixels, dtype=np.float32)


def test_identity_by_default():
    f = frame([10, 120, 250], [0, 0, 0], [255, 255, 255])
    assert np.allclose(stage().apply(f), f)


def test_brightness_scales_linearly():
    out = stage(brightness=0.5).apply(frame([100, 200, 40]))
    assert np.allclose(out, [[50, 100, 20]])


def test_brightness_clamps_at_255():
    out = stage(brightness=4.0).apply(frame([100, 200, 40]))
    assert out.max() <= 255.0
    assert np.allclose(out[0], [255, 255, 160])   # 100 and 200 clip, 40*4 does not


def test_saturation_boost_pushes_away_from_grey():
    base = frame([120, 100, 80])
    out = stage(saturation=2.0).apply(base)
    grey = float(base[0] @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))
    # Each channel moves further from the luma axis, direction preserved.
    for i in range(3):
        assert abs(out[0][i] - grey) > abs(base[0][i] - grey)
        assert np.sign(out[0][i] - grey) == np.sign(base[0][i] - grey)


def test_saturation_zero_is_greyscale():
    out = stage(saturation=0.0).apply(frame([200, 60, 10]))
    assert np.allclose(out[0], out[0][0], atol=0.5)


def test_grey_is_unchanged_by_saturation():
    out = stage(saturation=2.5).apply(frame([128, 128, 128]))
    assert np.allclose(out, [[128, 128, 128]], atol=1.0)


def test_gamma_darkens_midtones_and_keeps_endpoints():
    out = stage(gamma=2.2).apply(frame([0, 128, 255]))
    assert out[0][0] == 0.0
    assert out[0][2] == 255.0
    assert out[0][1] < 128.0


def test_black_floor_snaps_dim_pixels_to_zero():
    out = stage(black_floor=30).apply(frame([10, 20, 25], [10, 20, 40], [0, 0, 0]))
    assert np.allclose(out[0], 0.0)      # brightest channel 25 < 30
    assert np.allclose(out[1], [10, 20, 40])   # brightest channel 40 survives
    assert np.allclose(out[2], 0.0)


def test_black_floor_is_per_pixel_not_per_channel():
    """Per-channel cutting would shift hue before the pixel goes dark."""
    out = stage(black_floor=30).apply(frame([5, 10, 200]))
    assert np.allclose(out[0], [5, 10, 200])   # dim channels preserved


def test_order_is_saturation_then_brightness_then_gamma():
    """Documented order; changing it silently changes the look."""
    f = frame([200, 100, 50])
    combined = stage(saturation=1.5, brightness=0.8, gamma=2.2).apply(f)

    step = stage(saturation=1.5).apply(f)
    step = stage(brightness=0.8).apply(step)
    step = stage(gamma=2.2).apply(step)
    assert np.allclose(combined, step, atol=1.0)


def test_input_is_not_mutated():
    f = frame([100, 150, 200])
    original = f.copy()
    stage(saturation=2.0, brightness=0.5, black_floor=10).apply(f)
    assert np.allclose(f, original)


def test_output_stays_in_range_for_extremes():
    f = np.random.default_rng(0).integers(0, 256, size=(462, 3)).astype(np.float32)
    out = stage(saturation=3.0, brightness=2.0, gamma=0.4, black_floor=20).apply(f)
    assert out.min() >= 0.0 and out.max() <= 255.0


# -- perceptual brightness ---------------------------------------------------

def test_perceptual_curve_is_identity_at_full():
    assert perceptual(brightness=1.0).effective_brightness == pytest.approx(1.0)


def test_perceptual_half_is_dimmer_than_linear_half():
    """A linear 0.5 looks much brighter than half; the curve fixes that."""
    assert perceptual(brightness=0.5).effective_brightness == pytest.approx(0.5 ** 2.2, rel=1e-6)
    assert perceptual(brightness=0.5).effective_brightness < 0.5


def test_perceptual_curve_is_monotone():
    levels = [perceptual(brightness=b).effective_brightness for b in (0.1, 0.25, 0.5, 0.75, 1.0)]
    assert levels == sorted(levels)


def test_gain_above_one_stays_linear():
    """Above full level it is gain, not perceived level; do not curve it."""
    assert perceptual(brightness=1.5).effective_brightness == pytest.approx(1.5)


def test_linear_mode_is_still_available():
    assert stage(brightness=0.5).effective_brightness == pytest.approx(0.5)


def test_auto_level_multiplies_before_the_curve():
    c = perceptual(brightness=1.0)
    c.auto_level = 0.5
    assert c.effective_brightness == pytest.approx(0.5 ** 2.2, rel=1e-6)

    c = perceptual(brightness=0.5)
    c.auto_level = 0.5
    assert c.effective_brightness == pytest.approx(0.25 ** 2.2, rel=1e-6)


def test_auto_level_actually_dims_the_frame():
    c = perceptual(brightness=1.0)
    full = c.apply(frame([200, 200, 200]))[0][0]
    c.auto_level = 0.4
    dimmed = c.apply(frame([200, 200, 200]))[0][0]
    assert dimmed < full / 2


def test_zero_level_is_black():
    c = perceptual(brightness=0.0)
    assert np.allclose(c.apply(frame([255, 255, 255])), 0.0)


# -- per-edge brightness trim -------------------------------------------------

def test_edge_scale_none_is_unchanged_from_before():
    f = frame([200, 100, 50], [10, 20, 30])
    assert np.allclose(stage().apply(f, edge_scale=None), stage().apply(f))


def test_edge_scale_dims_only_the_pixels_it_covers():
    f = frame([200, 200, 200], [200, 200, 200], [200, 200, 200])
    out = stage().apply(f, edge_scale=np.array([1.0, 0.5, 1.0], dtype=np.float32))
    assert np.allclose(out[0], 200) and np.allclose(out[2], 200)
    assert np.allclose(out[1], 100)


def test_edge_scale_combines_with_global_brightness():
    f = frame([200, 200, 200])
    out = stage(brightness=0.5, perceptual_brightness=False).apply(
        f, edge_scale=np.array([0.5], dtype=np.float32))
    assert np.allclose(out, 50.0)      # 0.5 global * 0.5 edge = 0.25 of 200


def test_edge_scale_zero_is_black_for_that_pixel_only():
    f = frame([200, 200, 200], [200, 200, 200])
    out = stage().apply(f, edge_scale=np.array([0.0, 1.0], dtype=np.float32))
    assert np.allclose(out[0], 0.0)
    assert np.allclose(out[1], 200.0)


def test_edge_scale_above_one_is_gain_not_clipped_early():
    f = frame([50, 50, 50])
    out = stage().apply(f, edge_scale=np.array([2.0], dtype=np.float32))
    assert np.allclose(out, 100.0)


def test_edge_scale_still_respects_the_255_ceiling():
    f = frame([200, 200, 200])
    out = stage().apply(f, edge_scale=np.array([2.0], dtype=np.float32))
    assert out.max() <= 255.0
