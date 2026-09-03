"""Mapping: the matrix is the whole pixel path, so it carries most of the risk."""
from __future__ import annotations

import numpy as np
import pytest

from ambiwled import config as config_mod
from ambiwled.mapping import Layout, Mapper


def zone_ramp(n, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(n, 3)).astype(np.float32)


def test_matrix_shape_and_unit_rows(cfg, layout):
    m = Mapper()
    w = m.build(cfg, layout)
    assert w.shape == (462, 21)
    assert not m.problems
    # Every output pixel is a convex combination of source zones: no pixel may
    # gain or lose energy purely from the mapping.
    assert np.allclose(w.sum(axis=1), 1.0, atol=1e-5)
    assert (w >= 0).all()


def test_every_pixel_is_covered_exactly_once(cfg, layout):
    """Edge spans tile the strip: the validator and the matrix must agree."""
    assert config_mod.validate(cfg) == []
    covered = np.zeros(cfg["led"]["count"], dtype=int)
    for e in cfg["mapping"]["edges"]:
        covered[e["pixel_start"] : e["pixel_start"] + e["pixel_count"]] += 1
    assert (covered == 1).all()


def test_edges_draw_from_their_own_source(cfg, layout):
    w = Mapper().build(cfg, layout)
    names = [f"{s}[{i}]" for s, c in layout.sides for i in range(c)]
    used = {}
    for e in cfg["mapping"]["edges"]:
        s, n = e["pixel_start"], e["pixel_count"]
        used[e["name"]] = {names[i] for i in np.where(w[s : s + n].sum(axis=0) > 1e-6)[0]}

    assert {n for n in used["tv_wall"] if n.startswith("top")} == {f"top[{i}]" for i in range(11)}
    assert {n for n in used["right_side"] if n.startswith("right")} == {f"right[{i}]" for i in range(5)}
    assert {n for n in used["left_side"] if n.startswith("left")} == {f"left[{i}]" for i in range(5)}
    # The synthesised rear uses only the two screen bottom corners.
    assert used["rear"] == {"right[4]", "left[0]"}


def test_corner_blend_reduces_the_seam(cfg, layout):
    zones = zone_ramp(21)

    blended = Mapper().build(cfg, layout) @ zones
    off_cfg = config_mod.default_config()
    off_cfg["mapping"]["corner_blend"] = False
    plain = Mapper().build(off_cfg, layout) @ zones

    seam = 133  # tv_wall -> right_side boundary
    blended_step = np.abs(blended[seam - 1] - blended[seam]).max()
    plain_step = np.abs(plain[seam - 1] - plain[seam]).max()

    # Blending must substantially reduce the discontinuity ...
    assert blended_step < plain_step / 2
    # ... to the point where the join is no sharper than an ordinary step
    # inside the neighbouring edges. A magic threshold would only encode the
    # smoothness of whatever test data was used.
    interior = np.abs(np.diff(blended[seam - 40 : seam + 40], axis=0)).max(axis=1)
    assert blended_step <= interior.max()


def test_loop_closes(cfg, layout):
    """Last pixel meets first pixel: the ceiling is a closed rectangle."""
    out = Mapper().build(cfg, layout) @ zone_ramp(21)
    closure = np.abs(out[-1] - out[0]).max()
    # The wrap-around join must be no sharper than a normal adjacent-pixel step
    # elsewhere on the strip, i.e. continuous rather than seamed.
    typical = np.abs(np.diff(out, axis=0)).max(axis=1).max()
    assert closure <= typical


def test_no_banding_within_an_edge(cfg, layout):
    """Interpolation, not block repeat: adjacent pixels must not jump."""
    out = Mapper().build(cfg, layout) @ zone_ramp(21, seed=3)
    steps = np.abs(np.diff(out, axis=0)).max(axis=1)
    # ~12 px per zone on the TV wall; a block repeat would show full-zone jumps.
    assert steps.max() < 40.0


@pytest.mark.parametrize("mode", ["synth_gradient", "mirror_top", "average", "off"])
def test_rear_synthesis_modes(cfg, layout, mode):
    rear = next(e for e in cfg["mapping"]["edges"] if e["name"] == "rear")
    rear["source"] = mode
    m = Mapper()
    w = m.build(cfg, layout)
    assert not m.problems
    rows = w[rear["pixel_start"] : rear["pixel_start"] + rear["pixel_count"]]
    out = rows @ zone_ramp(21, seed=7)

    if mode == "off":
        assert np.allclose(out, 0.0)
    elif mode == "average":
        assert np.allclose(out, out[0], atol=1e-4)          # flat
        assert np.allclose(rows.sum(axis=1), 1.0, atol=1e-5)
    elif mode == "synth_gradient":
        assert not np.allclose(out[0], out[-1])             # actually a gradient
        deltas = np.diff(out, axis=0)
        assert (np.sign(deltas).std(axis=0) < 1e-6).all()   # monotone
    elif mode == "mirror_top":
        assert not np.allclose(out, out[0])


def test_reversed_flips_a_direct_edges_zone_order(cfg, layout):
    """One `reversed` field, not two: on a plain physical side it mirrors
    the edge's output, same as the old source_reversed used to."""
    zones = zone_ramp(21, seed=11)
    normal = Mapper().build(cfg, layout) @ zones

    cfg["mapping"]["corner_blend"] = False
    for e in cfg["mapping"]["edges"]:
        if e["name"] == "tv_wall":
            e["reversed"] = True
    flipped = Mapper().build(cfg, layout) @ zones

    plain_cfg = config_mod.default_config()
    plain_cfg["mapping"]["corner_blend"] = False
    plain = Mapper().build(plain_cfg, layout) @ zones
    assert np.allclose(flipped[0:133], plain[0:133][::-1], atol=1e-3)
    assert not np.allclose(normal[0:133], flipped[0:133])


def test_reversed_flips_a_synth_edges_pixel_order(cfg, layout):
    """A synth/mirror edge has no source order of its own to reverse, so
    `reversed` flips its finished pixels instead - same visible result."""
    zones = zone_ramp(21, seed=13)
    cfg["mapping"]["corner_blend"] = False
    base = Mapper().build(cfg, layout) @ zones

    for e in cfg["mapping"]["edges"]:
        if e["name"] == "rear":  # synth_gradient in the fixture config
            e["reversed"] = True
    flipped = Mapper().build(cfg, layout) @ zones
    a, b = 231, 231 + 133
    assert np.allclose(flipped[a:b], base[a:b][::-1], atol=1e-3)


def test_reversed_keeps_corner_blend_attached_to_the_right_neighbour(cfg, layout):
    """The subtlety `reversed` has to hide: with corner blending on, flipping
    a direct edge must not relocate its blended pixel to the wrong end. A
    plain row-reversal (the naive "one field" implementation) would do
    exactly that, since the blend was computed for the un-flipped geometry.
    """
    plain = Mapper().build(cfg, layout)  # corner_blend defaults on
    left_zone = layout.offset("left")
    left_count = layout.count("left")
    # tv_wall borders left_side (its predecessor in edge order): pixel 0
    # should carry some weight from left_side's zones.
    assert plain[0, left_zone : left_zone + left_count].sum() > 0

    for e in cfg["mapping"]["edges"]:
        if e["name"] == "tv_wall":
            e["reversed"] = True
    reversed_built = Mapper().build(cfg, layout)
    # Still true after flipping: the blend belongs to the physical seam,
    # not to whichever TV content ends up displayed there.
    assert reversed_built[0, left_zone : left_zone + left_count].sum() > 0

    naive = plain[0:133][::-1]
    assert not np.allclose(reversed_built[0:133], naive, atol=1e-3)


def test_layout_change_is_handled(cfg, layout):
    """Zone counts may change with source or Ambilight style (spec 9.3)."""
    m = Mapper()
    m.build(cfg, layout)
    assert m.matrix.shape == (462, 21)

    wider = Layout([("left", 6), ("top", 14), ("right", 6)])
    w2 = m.build(cfg, wider)
    assert w2.shape == (462, 26)
    assert not m.problems
    assert np.allclose(w2.sum(axis=1), 1.0, atol=1e-5)


def test_unknown_source_is_reported_not_crashed(cfg, layout):
    cfg["mapping"]["edges"][0]["source"] = "bottom"   # this TV has no bottom
    m = Mapper()
    w = m.build(cfg, layout)
    assert w is not None
    assert any("bottom" in p for p in m.problems)


def test_empty_layout_is_survivable(cfg):
    m = Mapper()
    assert m.build(cfg, Layout([])) is None
    assert m.problems
