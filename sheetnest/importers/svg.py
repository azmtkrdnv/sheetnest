"""SVG import (laser-cutting workflows, Inkscape, Illustrator exports).

Lines stay lines and circular arcs stay true arcs; Bézier curves and
elliptical arcs are flattened within the curve tolerance.

Units: physical units in the SVG (mm, cm, in, pt) are converted to mm. Plain
user units / px are treated as 1 px = 1/96 in unless ``px_to_mm`` is given.
"""
from __future__ import annotations

import math
import os
from typing import List, Optional

from ..geometry import PartGeometry, chain_segments, loops_to_parts, simplify_contour


def read_svg(path: str, name: Optional[str] = None, *, tol: float = 0.05, join_tol: float = 0.05,
             px_to_mm: Optional[float] = None) -> List[PartGeometry]:
    try:
        import svgelements as se
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("SVG import needs 'svgelements' (pip install svgelements)") from exc
    ppi = 25.4 / px_to_mm if px_to_mm else 96.0
    svg = se.SVG.parse(path, ppi=ppi)
    k = 25.4 / ppi  # output units -> mm

    def P(pt):
        return (pt.x * k, -pt.y * k)  # flip Y: SVG y points down

    segs = []
    for el in svg.elements():
        if not isinstance(el, se.Shape):
            continue
        try:
            p = el if isinstance(el, se.Path) else se.Path(el)
            p = p.reify() if hasattr(p, "reify") else p
        except Exception:
            continue
        for seg in p:
            if isinstance(seg, se.Move):
                continue
            if seg.start is None or seg.end is None:
                continue
            a, b = P(seg.start), P(seg.end)
            if isinstance(seg, (se.Line, se.Close)):
                if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9:
                    segs.append((a, b, 0.0))
            elif isinstance(seg, se.Arc) and abs(seg.rx - seg.ry) < 1e-6 * max(1.0, seg.rx):
                sweep = seg.sweep  # radians, sign in SVG (y-down) space
                segs.append((a, b, -math.tan(sweep / 4.0)))
            else:
                length = seg.length(error=1e-4) * k
                n = max(4, min(2000, int(length / max(tol * 10, 0.5))))
                pts = [P(seg.point(i / n)) for i in range(n + 1)]
                for i in range(n):
                    segs.append((pts[i], pts[i + 1], 0.0))
    closed, opened = chain_segments(segs, join_tol)
    closed = [simplify_contour(c) for c in closed]
    name = name or os.path.splitext(os.path.basename(path))[0]
    parts = loops_to_parts(closed, name, tol, source=path)
    if not parts:
        raise ValueError(f"{path}: no closed outline found")
    if opened:
        for p in parts:
            p.warnings.append(f"{len(opened)} open path(s) ignored")
    return parts
