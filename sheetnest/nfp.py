"""No-fit polygons (NFP) and inner-fit polygons (IFP).

For a fixed part A and a moving part B (both already inflated by half the
spacing), the NFP is the set of translations of B that make it overlap A.
It is the Minkowski sum A ⊕ (-B). We compute it with Clipper's integer
Minkowski sum (robust for non-convex shapes) as

    sweep(boundary(A), -B)  ∪  (A - b0)  ∪  (a0 - B)

where a0/b0 are any vertex of A/B. The last two terms cover the cases where
one shape lies completely inside the other.

Holes of A are carved back out of the NFP where B fits entirely inside the
hole ("part-in-part"): positions p with B+p inside hole H are
(H - b0) minus sweep(boundary(H), -B).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import pyclipper
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

SCALE = 1000.0          # Clipper works in integers: 1 unit = 1 micron
EPS = 0.002             # touching tolerance (mm)


def _to_int(coords) -> List[List[int]]:
    return [[int(round(x * SCALE)), int(round(y * SCALE))] for x, y in coords]


def _paths_to_geom(paths) -> Polygon:
    """Clipper result paths (outers CCW, holes CW) -> shapely geometry."""
    outers, holes = [], []
    for p in paths:
        if len(p) < 3:
            continue
        pts = [(x / SCALE, y / SCALE) for x, y in p]
        (outers if pyclipper.Orientation(p) else holes).append(pts)
    polys = [Polygon(o) for o in outers]
    hole_polys = [Polygon(h) for h in holes]
    assigned = [[] for _ in polys]
    for h, hp in zip(holes, hole_polys):
        rp = hp.representative_point()
        best = None
        for i, op in enumerate(polys):
            if op.contains(rp) and (best is None or op.area < polys[best].area):
                best = i
        if best is not None:
            assigned[best].append(h)
    result = [Polygon(o, assigned[i]).buffer(0) for i, o in enumerate(outers)]
    return unary_union(result) if result else Polygon()


def _ring_coords(ring) -> list:
    c = list(ring.coords)
    if len(c) > 1 and c[0] == c[-1]:
        c = c[:-1]
    return c


def fill_slivers(geom, min_width: float = 0.05):
    """Remove numerical slivers: interior holes thinner than ``min_width``.

    Integer rounding in the Minkowski sum leaves hair-thin gaps between the
    swept pieces. Left in place they would look like valid (free) positions.
    """
    if geom.is_empty:
        return geom
    polys = geom.geoms if hasattr(geom, "geoms") else [geom]
    out = []
    for p in polys:
        if p.geom_type != "Polygon":
            continue
        keep = [r for r in p.interiors if not Polygon(r).buffer(-min_width, join_style=2).is_empty]
        out.append(Polygon(p.exterior, keep))
    return unary_union(out) if len(out) > 1 else (out[0] if out else Polygon())


def drop_thin(geom, min_width: float = 0.05):
    """Remove pieces of a region that are thinner than ``min_width``."""
    if geom.is_empty:
        return geom
    polys = geom.geoms if hasattr(geom, "geoms") else [geom]
    keep = [p for p in polys if p.geom_type == "Polygon" and not p.buffer(-min_width / 2, join_style=2).is_empty]
    return unary_union(keep) if keep else Polygon()


def sweep(ring_coords, neg_b_int) -> Polygon:
    """Area swept by -B when its reference point travels along a closed ring."""
    res = pyclipper.MinkowskiSum(neg_b_int, _to_int(ring_coords), True)
    return _paths_to_geom(res)


def negate_outer(poly: Polygon):
    """Outer ring of -B as integer path, plus one vertex b0 of B."""
    ext = _ring_coords(poly.exterior)
    neg = [(-x, -y) for x, y in ext]
    return _to_int(neg), ext[0]


def compute_nfp(a: Polygon, b: Polygon, part_in_part: bool = True) -> Polygon:
    """Forbidden region for B's origin when A sits at the origin.

    ``a`` may have holes; only the outer outline of ``b`` is used.
    The result is shrunk by EPS so touching positions remain allowed.
    """
    neg_b, b0 = negate_outer(b)
    a_ext = _ring_coords(a.exterior)
    a0 = a_ext[0]
    b_outer = Polygon(b.exterior)
    pieces = [sweep(a_ext, neg_b),
              affinity.translate(Polygon(a.exterior), -b0[0], -b0[1]),
              affinity.translate(affinity.scale(b_outer, -1, -1, origin=(0, 0)), a0[0], a0[1])]
    forbidden = fill_slivers(unary_union(pieces))
    if part_in_part and a.interiors:
        bx0, by0, bx1, by1 = b_outer.bounds
        allowed = []
        for ring in a.interiors:
            hole = Polygon(ring)
            hx0, hy0, hx1, hy1 = hole.bounds
            # quick reject: B's bounding box must fit (in this orientation) inside the hole's
            if (bx1 - bx0) >= (hx1 - hx0) or (by1 - by0) >= (hy1 - hy0) or b_outer.area >= hole.area:
                continue
            region = affinity.translate(hole, -b0[0], -b0[1]).difference(sweep(_ring_coords(ring), neg_b))
            region = drop_thin(region)
            if not region.is_empty:
                allowed.append(region)
        if allowed:
            forbidden = forbidden.difference(unary_union(allowed).buffer(EPS, join_style=2))
    return forbidden.buffer(-EPS, join_style=2)


def compute_ifp(sheet: Polygon, b: Polygon, rect: Optional[Sequence[float]] = None) -> Polygon:
    """Allowed region for B's origin so that B stays inside the usable sheet area."""
    bx0, by0, bx1, by1 = b.exterior.bounds
    if rect is not None:
        sx0, sy0, sx1, sy1 = rect
        x0, x1 = sx0 - bx0, sx1 - bx1
        y0, y1 = sy0 - by0, sy1 - by1
        if x1 < x0 - EPS or y1 < y0 - EPS:
            return Polygon()
        return box(x0 - EPS, y0 - EPS, max(x1, x0) + EPS, max(y1, y0) + EPS)
    neg_b, b0 = negate_outer(b)
    b_outer = Polygon(b.exterior)
    region = affinity.translate(Polygon(sheet.exterior), -b0[0], -b0[1])
    cut = [sweep(_ring_coords(sheet.exterior), neg_b)]
    for ring in sheet.interiors:  # defects / holes in a remnant
        cut.append(sweep(_ring_coords(ring), neg_b))
        d0 = _ring_coords(ring)[0]
        cut.append(affinity.translate(affinity.scale(b_outer, -1, -1, origin=(0, 0)), d0[0], d0[1]))
        cut.append(affinity.translate(Polygon(ring), -b0[0], -b0[1]))
    region = region.difference(fill_slivers(unary_union(cut)).buffer(-EPS, join_style=2))
    return drop_thin(region, 0.05)


def polygon_vertices(geom):
    """All vertices of a (Multi)Polygon as a list of (x, y)."""
    if geom.is_empty:
        return []
    polys = geom.geoms if hasattr(geom, "geoms") else [geom]
    pts = []
    for p in polys:
        if p.geom_type != "Polygon":
            continue
        pts.extend(p.exterior.coords[:-1])
        for r in p.interiors:
            pts.extend(r.coords[:-1])
    return pts
