"""Core 2D geometry for sheet-metal parts.

Contours are stored as closed *bulge polylines* (the same representation DXF
LWPOLYLINE uses): a list of vertices ``(x, y, bulge)`` where ``bulge`` describes
the segment from this vertex to the next one. ``bulge = 0`` is a straight line,
otherwise ``bulge = tan(sweep / 4)`` and positive means counter-clockwise.

Keeping true arcs (instead of flattening everything to short lines) matters for
plasma and laser cutting: the exported DXF and G-code use real arcs (G2/G3),
which cut smoother and produce much smaller programs. Flattened polygons are
derived on demand only for the nesting maths.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from shapely.geometry import Polygon
from shapely.validation import make_valid

Vertex = Tuple[float, float, float]  # x, y, bulge
Point = Tuple[float, float]

EPS = 1e-9


# ---------------------------------------------------------------------------
# Arc helpers
# ---------------------------------------------------------------------------

def bulge_to_arc(p0: Point, p1: Point, bulge: float):
    """Return (center, radius, start_angle, sweep) for a bulge segment."""
    sweep = 4.0 * math.atan(bulge)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    if chord < EPS or abs(bulge) < EPS:
        return None
    radius = chord / (2.0 * math.sin(abs(sweep) / 2.0))
    # distance from chord midpoint to center (signed)
    mx, my = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
    h = radius * math.cos(sweep / 2.0)
    # unit normal to the left of the chord
    nx, ny = -dy / chord, dx / chord
    s = 1.0 if bulge > 0 else -1.0
    cx, cy = mx + s * h * nx, my + s * h * ny
    # with the formula above, for |sweep| > pi, h becomes negative which flips
    # the center to the correct side automatically.
    start = math.atan2(p0[1] - cy, p0[0] - cx)
    return (cx, cy), radius, start, sweep


def arc_points(p0: Point, p1: Point, bulge: float, tol: float, outside: bool = False) -> List[Point]:
    """Points along a bulge segment (excluding p0, including p1).

    Normally the points lie on the arc (chords cut inside it). With
    ``outside=True`` the polyline is made of tangent lines instead, so it
    encloses the true arc - used for nesting so that flattened parts are never
    smaller than the real ones.
    """
    if abs(bulge) < EPS:
        return [p1]
    arc = bulge_to_arc(p0, p1, bulge)
    if arc is None:
        return [p1]
    (cx, cy), r, a0, sweep = arc
    if r <= tol:
        n = 4
    else:
        step = 2.0 * math.acos(max(-1.0, 1.0 - tol / r))
        n = max(2, int(math.ceil(abs(sweep) / max(step, 1e-6))))
    n = min(n, 720)
    pts = []
    if outside:
        h = sweep / n
        rr = r / math.cos(h / 2.0)
        for i in range(1, n + 1):
            a = a0 + h * (i - 0.5)
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    else:
        for i in range(1, n):
            a = a0 + sweep * i / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(p1)
    return pts


def three_point_bulge(p0: Point, pm: Point, p1: Point) -> float:
    """Bulge of the arc p0 -> p1 passing through pm."""
    ax, ay = p0
    bx, by = pm
    cx, cy = p1
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < EPS:
        return 0.0
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)

    def ccw_delta(a, b):
        return (b - a) % (2 * math.pi)

    # sweep going CCW from a0 to a1; is the mid point on that path?
    sweep_ccw = ccw_delta(a0, a1)
    if ccw_delta(a0, am) <= sweep_ccw:
        sweep = sweep_ccw
    else:
        sweep = -(2 * math.pi - sweep_ccw)
    return math.tan(sweep / 4.0)


# ---------------------------------------------------------------------------
# Contours
# ---------------------------------------------------------------------------

@dataclass
class Contour:
    """A closed (or open, for marking) bulge polyline."""

    vertices: List[Vertex]
    closed: bool = True

    # -- basic properties -------------------------------------------------
    def __len__(self):
        return len(self.vertices)

    def segments(self):
        """Yield (p0, p1, bulge) for every segment."""
        v = self.vertices
        n = len(v)
        last = n if self.closed else n - 1
        for i in range(last):
            a = v[i]
            b = v[(i + 1) % n]
            yield (a[0], a[1]), (b[0], b[1]), a[2]

    def to_points(self, tol: float = 0.05, enclose_left: bool = False) -> List[Point]:
        """Flatten to points. ``enclose_left``: arcs turning left (bulge > 0) are
        approximated from outside, so the polygon fully covers the region on the
        left of the travel direction (material side of a CCW outline / CW hole)."""
        if not self.vertices:
            return []
        pts: List[Point] = [(self.vertices[0][0], self.vertices[0][1])]
        for p0, p1, b in self.segments():
            pts.extend(arc_points(p0, p1, b, tol, enclose_left and b > 0))
        if self.closed and len(pts) > 1 and _close(pts[0], pts[-1]):
            pts.pop()
        return pts

    def signed_area(self) -> float:
        """Exact signed area including circular segments (CCW positive)."""
        if not self.closed:
            return 0.0
        area = 0.0
        for p0, p1, b in self.segments():
            area += (p0[0] * p1[1] - p1[0] * p0[1]) / 2.0
            if abs(b) > EPS:
                arc = bulge_to_arc(p0, p1, b)
                if arc:
                    _, r, _, sweep = arc
                    # circular segment area, signed with the sweep direction
                    seg = 0.5 * r * r * (abs(sweep) - math.sin(abs(sweep)))
                    area += seg if sweep > 0 else -seg
        return area

    def length(self) -> float:
        total = 0.0
        for p0, p1, b in self.segments():
            if abs(b) < EPS:
                total += math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            else:
                arc = bulge_to_arc(p0, p1, b)
                total += abs(arc[1] * arc[3]) if arc else math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        return total

    def is_ccw(self) -> bool:
        return self.signed_area() > 0

    def reversed(self) -> "Contour":
        v = self.vertices
        n = len(v)
        if self.closed:
            # vertex i keeps position; bulge of segment (i+1 -> i) = -bulge(i)
            out = []
            for i in range(n - 1, -1, -1):
                prev_bulge = v[i - 1][2] if i - 1 >= 0 else v[n - 1][2]
                out.append((v[i][0], v[i][1], -prev_bulge))
            return Contour(out, True)
        out = []
        for i in range(n - 1, -1, -1):
            b = -v[i - 1][2] if i > 0 else 0.0
            out.append((v[i][0], v[i][1], b))
        return Contour(out, False)

    def oriented(self, ccw: bool) -> "Contour":
        return self if self.is_ccw() == ccw else self.reversed()

    def rotated_start(self, index: int) -> "Contour":
        """Same closed loop, starting at a different vertex."""
        if not self.closed or index == 0:
            return self
        return Contour(self.vertices[index:] + self.vertices[:index], True)

    def transformed(self, angle_deg: float = 0.0, mirror: bool = False,
                    dx: float = 0.0, dy: float = 0.0) -> "Contour":
        """Mirror about the Y axis (x -> -x), rotate about origin, translate."""
        a = math.radians(angle_deg)
        c, s = math.cos(a), math.sin(a)
        out = []
        for x, y, b in self.vertices:
            if mirror:
                x, b = -x, -b
            out.append((c * x - s * y + dx, s * x + c * y + dy, b))
        return Contour(out, self.closed)

    def bounds(self, tol: float = 0.05):
        pts = self.to_points(tol)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def is_circle(self):
        """Return (center, radius) if this contour is a full circle."""
        if not self.closed or len(self.vertices) not in (2, 3, 4):
            return None
        segs = list(self.segments())
        if any(abs(b) < EPS for _, _, b in segs):
            return None
        arcs = [bulge_to_arc(p0, p1, b) for p0, p1, b in segs]
        if any(a is None for a in arcs):
            return None
        c0, r0 = arcs[0][0], arcs[0][1]
        for c, r, _, _ in arcs[1:]:
            if math.hypot(c[0] - c0[0], c[1] - c0[1]) > 1e-4 or abs(r - r0) > 1e-4:
                return None
        total = sum(a[3] for a in arcs)
        if abs(abs(total) - 2 * math.pi) > 1e-3:
            return None
        return c0, r0

    @staticmethod
    def circle(cx: float, cy: float, r: float) -> "Contour":
        return Contour([(cx + r, cy, 1.0), (cx - r, cy, 1.0)], True)

    @staticmethod
    def rectangle(x0, y0, x1, y1) -> "Contour":
        return Contour([(x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)], True)

    @staticmethod
    def from_points(points: Iterable[Sequence[float]], closed: bool = True) -> "Contour":
        verts = []
        for p in points:
            b = float(p[2]) if len(p) > 2 else 0.0
            verts.append((float(p[0]), float(p[1]), b))
        if closed and len(verts) > 1 and _close(verts[0], verts[-1]):
            verts.pop()
        return Contour(verts, closed)


def _close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


# ---------------------------------------------------------------------------
# Part geometry
# ---------------------------------------------------------------------------

@dataclass
class PartGeometry:
    """Outer contour + holes (cut) + optional marking geometry (etch/engrave)."""

    outer: Contour
    holes: List[Contour] = field(default_factory=list)
    marks: List[Contour] = field(default_factory=list)
    name: str = "part"
    source: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def normalized(self) -> "PartGeometry":
        """Outer CCW, holes CW, bounding box moved to the origin."""
        x0, y0, _, _ = self.outer.bounds(0.01)
        def mv(c: Contour, ccw=None):
            c = c.transformed(0, False, -x0, -y0)
            return c.oriented(ccw) if ccw is not None else c
        return PartGeometry(mv(self.outer, True), [mv(h, False) for h in self.holes],
                            [mv(m) for m in self.marks], self.name, self.source, list(self.warnings), dict(self.meta))

    def transformed(self, angle_deg=0.0, mirror=False, dx=0.0, dy=0.0) -> "PartGeometry":
        t = lambda c: c.transformed(angle_deg, mirror, dx, dy)
        return PartGeometry(t(self.outer), [t(h) for h in self.holes], [t(m) for m in self.marks],
                            self.name, self.source, self.warnings, self.meta)

    def polygon(self, tol: float = 0.05, conservative: bool = False) -> Polygon:
        """Flattened polygon. ``conservative=True`` guarantees the polygon covers
        all material (arcs approximated from outside the part)."""
        if conservative:
            shell = self.outer.oriented(True).to_points(tol, True)
            holes = [h.oriented(False).to_points(tol, True) for h in self.holes]
        else:
            shell = self.outer.to_points(tol)
            holes = [h.to_points(tol) for h in self.holes]
        poly = Polygon(shell, [h for h in holes if len(h) >= 3])
        if not poly.is_valid:
            poly = make_valid(poly)
            if poly.geom_type != "Polygon":
                poly = max(getattr(poly, "geoms", [poly]), key=lambda g: g.area)
                if poly.geom_type != "Polygon":
                    poly = Polygon(shell)
        return poly

    @property
    def area(self) -> float:
        return abs(self.outer.signed_area()) - sum(abs(h.signed_area()) for h in self.holes)

    @property
    def bbox(self):
        return self.outer.bounds(0.01)

    @property
    def size(self):
        x0, y0, x1, y1 = self.bbox
        return x1 - x0, y1 - y0

    @property
    def cut_length(self) -> float:
        return self.outer.length() + sum(h.length() for h in self.holes)

    @property
    def pierce_count(self) -> int:
        return 1 + len(self.holes)


def loops_to_parts(loops: List[Contour], name: str, tol: float = 0.05,
                   source: Optional[str] = None) -> List[PartGeometry]:
    """Classify closed loops by containment depth into parts with holes.

    Depth 0 loops are part outlines, depth 1 are their holes, depth 2 are
    separate parts sitting inside a hole (e.g. a drawing with a ring and its
    slug), and so on.
    """
    loops = [l for l in loops if l.closed and len(l.vertices) >= 2 and abs(l.signed_area()) > 1e-6]
    if not loops:
        return []
    polys = [Polygon(l.to_points(tol)).buffer(0) for l in loops]
    areas = [p.area for p in polys]
    order = sorted(range(len(loops)), key=lambda i: -areas[i])
    parent = {}
    for idx_pos, i in enumerate(order):
        rp = polys[i].representative_point()
        best = None
        for j in order[:idx_pos]:  # only larger loops can contain this one
            if areas[j] > areas[i] and polys[j].contains(rp):
                if best is None or areas[j] < areas[best]:
                    best = j
        parent[i] = best
    depth = {}
    def get_depth(i):
        if i in depth:
            return depth[i]
        d = 0 if parent[i] is None else get_depth(parent[i]) + 1
        depth[i] = d
        return d
    for i in range(len(loops)):
        get_depth(i)
    outers = [i for i in order if depth[i] % 2 == 0]
    parts = []
    for k, i in enumerate(outers):
        holes = [loops[j] for j in range(len(loops)) if parent[j] == i and depth[j] % 2 == 1]
        pname = name if len(outers) == 1 else f"{name}#{k + 1}"
        parts.append(PartGeometry(loops[i], holes, [], pname, source))
    return parts


def chain_segments(segments: List[Tuple[Point, Point, float]], tol: float = 0.01):
    """Join loose segments (line/arc with bulge) into closed and open contours.

    Segments may come in any order or direction (as they do in most DXF files).
    Returns (closed_contours, open_contours).
    """
    segs = [(tuple(a), tuple(b), float(bu)) for a, b, bu in segments
            if math.hypot(b[0] - a[0], b[1] - a[1]) > tol * 0.01 or abs(bu) > EPS]
    n = len(segs)
    used = [False] * n
    cell = max(tol * 2.0, 1e-6)
    grid = {}

    def key(p):
        return (int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell)))

    for i, (a, b, _) in enumerate(segs):
        for end, p in ((0, a), (1, b)):
            grid.setdefault(key(p), []).append((i, end))

    def find(p, exclude):
        kx, ky = key(p)
        best, bestd = None, tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i, end in grid.get((kx + dx, ky + dy), ()):
                    if used[i] or i == exclude:
                        continue
                    q = segs[i][end]
                    d = math.hypot(q[0] - p[0], q[1] - p[1])
                    if d <= bestd:
                        best, bestd = (i, end), d
        return best

    closed, opened = [], []
    for start in range(n):
        if used[start]:
            continue
        used[start] = True
        a, b, bu = segs[start]
        chain = [(a, b, bu)]
        # grow forward
        while True:
            end_pt = chain[-1][1]
            if len(chain) > 1 or abs(chain[0][2]) > EPS:
                if math.hypot(end_pt[0] - chain[0][0][0], end_pt[1] - chain[0][0][1]) <= tol:
                    break
            hit = find(end_pt, -1)
            if hit is None:
                break
            i, end = hit
            used[i] = True
            s0, s1, sb = segs[i]
            chain.append((s0, s1, sb) if end == 0 else (s1, s0, -sb))
        start_pt, end_pt = chain[0][0], chain[-1][1]
        is_closed = math.hypot(end_pt[0] - start_pt[0], end_pt[1] - start_pt[1]) <= tol
        if not is_closed:
            # grow backward
            while True:
                st = chain[0][0]
                hit = find(st, -1)
                if hit is None:
                    break
                i, end = hit
                used[i] = True
                s0, s1, sb = segs[i]
                chain.insert(0, (s0, s1, sb) if end == 1 else (s1, s0, -sb))
                if math.hypot(chain[0][0][0] - chain[-1][1][0], chain[0][0][1] - chain[-1][1][1]) <= tol:
                    break
            start_pt, end_pt = chain[0][0], chain[-1][1]
            is_closed = math.hypot(end_pt[0] - start_pt[0], end_pt[1] - start_pt[1]) <= tol
        verts = [(s[0][0], s[0][1], s[2]) for s in chain]
        if is_closed:
            closed.append(Contour(verts, True))
        else:
            verts.append((end_pt[0], end_pt[1], 0.0))
            opened.append(Contour(verts, False))
    return closed, opened


def simplify_contour(c: Contour, tol: float = 1e-4) -> Contour:
    """Drop duplicate vertices and merge collinear straight segments."""
    v = c.vertices
    if len(v) < 3:
        return c
    out: List[Vertex] = []
    n = len(v)
    for i in range(n):
        x, y, b = v[i]
        if out and abs(out[-1][0] - x) < tol and abs(out[-1][1] - y) < tol:
            # duplicate point: keep bulge of the later vertex
            out[-1] = (out[-1][0], out[-1][1], b)
            continue
        out.append((x, y, b))
    if c.closed and len(out) > 1 and abs(out[0][0] - out[-1][0]) < tol and abs(out[0][1] - out[-1][1]) < tol:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        m = len(out)
        for i in range(m):
            p_prev = out[i - 1]
            p = out[i]
            p_next = out[(i + 1) % m]
            if not c.closed and (i == 0 or i == m - 1):
                continue
            if abs(p_prev[2]) < EPS and abs(p[2]) < EPS:
                cross = (p[0] - p_prev[0]) * (p_next[1] - p_prev[1]) - (p[1] - p_prev[1]) * (p_next[0] - p_prev[0])
                seg = math.hypot(p_next[0] - p_prev[0], p_next[1] - p_prev[1])
                dot = (p[0] - p_prev[0]) * (p_next[0] - p_prev[0]) + (p[1] - p_prev[1]) * (p_next[1] - p_prev[1])
                if seg > EPS and abs(cross) / seg < tol and 0 < dot < seg * seg:
                    out.pop(i)
                    changed = True
                    break
    return Contour(out, c.closed)
