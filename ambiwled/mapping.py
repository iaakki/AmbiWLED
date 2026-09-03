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


def _interp_rows(n: int, positions: np.ndarray, refs: Sequence[int], zone_count: int) -> np.ndarray:
    """Rows for one edge: pixel centres linearly interpolated between zone centres.

    Outside the first/last zone centre the end colour is held (clamped); callers
    add virtual neighbour zones beforehand when corner blending is on.
    """
    rows = np.zeros((n, zone_count), dtype=np.float32)
    if n <= 0 or not len(refs):
        return rows
    ref_arr = np.asarray(refs, dtype=np.intp)
    if len(refs) == 1:
        rows[:, ref_arr[0]] = 1.0
        return rows

    x = np.arange(n, dtype=np.float64) + 0.5
    seg = np.clip(np.searchsorted(positions, x) - 1, 0, len(positions) - 2)
    p0 = positions[seg]
    p1 = positions[seg + 1]
    t = np.clip((x - p0) / np.maximum(p1 - p0, 1e-9), 0.0, 1.0)

    idx = np.arange(n, dtype=np.intp)
    np.add.at(rows, (idx, ref_arr[seg]), (1.0 - t).astype(np.float32))
    np.add.at(rows, (idx, ref_arr[seg + 1]), t.astype(np.float32))
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
        if edge.get("source_reversed"):
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

            rows = self._build_edge(edge, refs_by_edge, i, layout, corner_blend, zone_count)
            if rows is None:
                continue
            if edge.get("output_reversed"):
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
            if edge.get("output_reversed"):
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

        return _interp_rows(n, np.asarray(positions, dtype=np.float64), refs, zone_count)


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
