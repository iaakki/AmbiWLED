"""Source zones -> output pixels.

The whole mapping collapses into a single (led_count x zone_count) weight matrix,
so a mapped frame is one matmul.  Rebuild the matrix when the config or the
detected source layout changes; never per frame.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Layout:
    """The side names and zone counts the TV actually reported."""

    sides: list[tuple[str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(c for _, c in self.sides)

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.sides]

    def offset(self, side: str) -> int | None:
        pos = 0
        for name, count in self.sides:
            if name == side:
                return pos
            pos += count
        return None

    def count(self, side: str) -> int:
        for name, c in self.sides:
            if name == side:
                return c
        return 0

    def index(self, side: str, zone: int) -> int | None:
        """Global zone index for `side[zone]`; negative zone indexes from the end."""
        off = self.offset(side)
        if off is None:
            return None
        n = self.count(side)
        if n == 0:
            return None
        if zone < 0:
            zone += n
        if not (0 <= zone < n):
            return None
        return off + zone

    def as_dict(self) -> dict[str, int]:
        return {n: c for n, c in self.sides}

    def __bool__(self) -> bool:
        return bool(self.sides)


def _interp_rows(
    n: int, positions: np.ndarray, refs: Sequence[int], zone_count: int, blend: float = 1.0
) -> np.ndarray:
    """Rows for one edge: pixel centres blended from zone centres with a
    triangular (tent) kernel whose half-width is `blend` zone-spacings.

    `blend=1.0` is a plain 2-tap linear interpolation between the nearest
    zone on each side of a pixel - the original scheme, and still the
    default: at exactly one zone-spacing away, a neighbour's weight has
    fallen to exactly zero, so only the two bracketing zones ever
    contribute, with the classic `(1-t, t)` split. `blend` toward 0 narrows
    the kernel until neighbouring zones stop overlapping at all - each
    pixel then takes its single nearest zone verbatim, the raw, blocky
    zones as the TV actually sent them. `blend` above 1 widens the kernel,
    pulling in zones beyond the immediate neighbours for a softer blend
    than a hard 2-tap scheme can produce.

    Outside the first/last zone centre the end colour is held (clamped);
    callers add virtual neighbour zones beforehand when corner blending is
    on - which then also respects `blend`, unchanged, since they are just
    more positions in the same array.
    """
    rows = np.zeros((n, zone_count), dtype=np.float32)
    if n <= 0 or not len(refs):
        return rows
    ref_arr = np.asarray(refs, dtype=np.intp)
    if len(refs) == 1:
        rows[:, ref_arr[0]] = 1.0
        return rows

    positions = np.asarray(positions, dtype=np.float64)
    # Positions are always regularly spaced (real zone centres, plus any
    # virtual corner-blend neighbours at the same spacing), so the smallest
    # gap is *the* spacing regardless of how many zones this edge has.
    step = float(np.min(np.diff(positions)))
    width = max(blend, 0.0) * step

    x = np.arange(n, dtype=np.float64) + 0.5
    dist = np.abs(x[:, None] - positions[None, :])  # (n, z)

    if width <= 1e-9:
        # Degenerate: nearest zone only, hard edges - the "blocky" extreme.
        nearest = np.argmin(dist, axis=1)
        rows[np.arange(n), ref_arr[nearest]] = 1.0
        return rows

    w = np.clip(1.0 - dist / width, 0.0, None)
    total = w.sum(axis=1)
    starved = total <= 1e-9
    if starved.any():
        # The kernel is narrower than the gap to the nearest zone for some
        # pixels (a small `blend`, few zones): fall back to that zone alone
        # rather than divide by zero and leave the pixel unlit.
        idx = np.where(starved)[0]
        nearest = np.argmin(dist[idx], axis=1)
        w[idx] = 0.0
        w[idx, nearest] = 1.0
        total = w.sum(axis=1)

    w = (w / total[:, None]).astype(np.float32)
    for k in range(len(ref_arr)):
        rows[:, ref_arr[k]] += w[:, k]
    return rows


class Mapper:
    """Builds and holds the weight matrix."""

    def __init__(self) -> None:
        self.matrix: np.ndarray | None = None
        self.layout = Layout()
        self.problems: list[str] = []
        self.edge_spans: list[dict[str, Any]] = []

    # -- helpers ---------------------------------------------------------

    def _edge_refs(self, edge: dict[str, Any], layout: Layout) -> list[int]:
        """Global zone indices this edge consumes, in output order.

        Empty for edges with no real source.  `synth_gradient` reports its two
        endpoints so neighbouring edges can still blend into it.

        For a real physical side, `reversed` flips the order *here* — before
        corner blending — rather than after building the rows.  A neighbour's
        corner blend reads this edge's own zone order via `refs_by_edge`, so a
        flip has to be visible at this point or the blended pixel at the join
        ends up on the wrong physical corner.  See `build()` for the other
        edge kinds, where there is no corner blend to keep in step and the
        flip is applied to the finished rows instead.
        """
        src = edge.get("source")
        if src == "synth_gradient":
            a = self._resolve_ref(edge.get("synth_from"), layout)
            b = self._resolve_ref(edge.get("synth_to"), layout)
            return [r for r in (a, b) if r is not None]
        if src in ("mirror_top", "average", "off") or src is None:
            return []
        off = layout.offset(src)
        if off is None:
            return []
        refs = list(range(off, off + layout.count(src)))
        if edge.get("reversed"):
            refs.reverse()
        return refs

    @staticmethod
    def _resolve_ref(spec: Any, layout: Layout) -> int | None:
        if not isinstance(spec, (list, tuple)) or len(spec) != 2:
            return None
        try:
            return layout.index(str(spec[0]), int(spec[1]))
        except (TypeError, ValueError):
            return None

    # -- build -----------------------------------------------------------

    def build(self, cfg: dict[str, Any], layout: Layout) -> np.ndarray | None:
        """(Re)build the weight matrix.  Returns None if it cannot be built."""
        self.layout = layout
        self.problems = []
        self.edge_spans = []

        led_count = int(cfg["led"]["count"])
        zone_count = layout.total
        if zone_count == 0:
            self.matrix = None
            self.problems.append("no source layout detected yet")
            return None

        corner_blend = bool(cfg["mapping"].get("corner_blend", True))
        zone_blend = float(cfg["mapping"].get("zone_blend", 1.0))
        edges = sorted(cfg["mapping"]["edges"], key=lambda e: int(e["pixel_start"]))
        refs_by_edge = [self._edge_refs(e, layout) for e in edges]

        matrix = np.zeros((led_count, zone_count), dtype=np.float32)
        built: dict[str, np.ndarray] = {}
        deferred: list[tuple[int, dict[str, Any]]] = []

        for i, edge in enumerate(edges):
            src = edge.get("source")
            n = int(edge["pixel_count"])
            start = int(edge["pixel_start"])
            self.edge_spans.append(
                {"name": edge.get("name"), "start": start, "count": n, "source": src}
            )

            if src == "mirror_top":
                deferred.append((i, edge))
                continue

            rows = self._build_edge(edge, refs_by_edge, i, layout, corner_blend, zone_count, zone_blend)
            if rows is None:
                continue
            # Direct sides already had `reversed` applied to their zone order
            # in _edge_refs, ahead of corner blending. Every other kind has no
            # blend to stay consistent with, so it is simplest to flip the
            # finished rows here instead.
            if edge.get("reversed") and src not in layout.names:
                rows = rows[::-1]
            matrix[start : start + n] = rows
            built[str(edge.get("name"))] = rows

        for i, edge in deferred:
            n = int(edge["pixel_count"])
            start = int(edge["pixel_start"])
            target = edge.get("mirror_of") or (edges[0].get("name") if edges else None)
            source_rows = built.get(str(target))
            if source_rows is None:
                self.problems.append(
                    f"{edge.get('name')}: mirror_of '{target}' is not a mappable edge"
                )
                continue
            rows = _resample_rows(source_rows, n)[::-1]  # mirrored = copied, reversed
            if edge.get("reversed"):
                rows = rows[::-1]
            matrix[start : start + n] = rows

        self.matrix = matrix
        return matrix

    def _build_edge(
        self,
        edge: dict[str, Any],
        refs_by_edge: list[list[int]],
        i: int,
        layout: Layout,
        corner_blend: bool,
        zone_count: int,
        zone_blend: float = 1.0,
    ) -> np.ndarray | None:
        src = edge.get("source")
        n = int(edge["pixel_count"])

        if src == "off":
            return np.zeros((n, zone_count), dtype=np.float32)

        if src == "average":
            rows = np.full((n, zone_count), 1.0 / zone_count, dtype=np.float32)
            return rows

        if src == "synth_gradient":
            a = self._resolve_ref(edge.get("synth_from"), layout)
            b = self._resolve_ref(edge.get("synth_to"), layout)
            if a is None or b is None:
                self.problems.append(
                    f"{edge.get('name')}: synth_from/synth_to do not resolve against the detected layout"
                )
                return np.zeros((n, zone_count), dtype=np.float32)
            rows = np.zeros((n, zone_count), dtype=np.float32)
            t = (np.arange(n, dtype=np.float32) + 0.5) / n
            np.add.at(rows, (np.arange(n), a), 1.0 - t)
            np.add.at(rows, (np.arange(n), b), t)
            return rows

        refs = list(refs_by_edge[i])
        if not refs:
            self.problems.append(
                f"{edge.get('name')}: source '{src}' is not in the detected layout "
                f"({', '.join(layout.names) or 'none'})"
            )
            return np.zeros((n, zone_count), dtype=np.float32)

        z = len(refs)
        step = n / z
        positions = [(k + 0.5) * step for k in range(z)]

        if corner_blend:
            prev_refs = refs_by_edge[(i - 1) % len(refs_by_edge)]
            next_refs = refs_by_edge[(i + 1) % len(refs_by_edge)]
            if prev_refs:
                refs.insert(0, prev_refs[-1])
                positions.insert(0, positions[0] - step)
            if next_refs:
                refs.append(next_refs[0])
                positions.append(positions[-1] + step)

        return _interp_rows(n, np.asarray(positions, dtype=np.float64), refs, zone_count, zone_blend)


def _resample_rows(rows: np.ndarray, n: int) -> np.ndarray:
    """Stretch/squash an edge's rows to a different pixel count."""
    m = rows.shape[0]
    if m == n:
        return rows.copy()
    src = (np.arange(n, dtype=np.float64) + 0.5) * m / n - 0.5
    lo = np.clip(np.floor(src).astype(int), 0, m - 1)
    hi = np.clip(lo + 1, 0, m - 1)
    t = (src - lo).astype(np.float32)[:, None]
    return (rows[lo] * (1.0 - t) + rows[hi] * t).astype(np.float32)
