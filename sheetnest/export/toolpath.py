"""Cut sequencing shared by the G-code post-processors and the reports.

Rules applied (standard plasma / laser practice):
* inner contours (holes) of a part are cut before its outline;
* a part nested inside another part's hole is cut before that hole, so the
  slug carrying it doesn't drop or tip before it is finished;
* outlines run clockwise and holes counter-clockwise by default, which keeps
  the good side of a plasma arc on the part;
* each contour gets a lead-in on the scrap side, shortened (or moved to the
  centre of small round holes) when there isn't room;
* parts and holes are visited nearest-neighbour to keep rapids short.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..geometry import Contour, EPS
from ..model import NestResult, SheetLayout


@dataclass
class CutSettings:
    outer_cw: bool = True
    hole_ccw: bool = True
    lead_in: float = 5.0            # mm
    lead_type: str = "arc"          # "arc" (tangential quarter arc) or "line"
    overcut: float = 0.0            # mm of overlap past the start on straight first segments
    min_lead: float = 0.5
    cut_marks: bool = False         # also run marking geometry (etch) before cutting


@dataclass
class CutPath:
    contour: Contour                 # oriented, starting at the cut start point
    pierce: Tuple[float, float]
    lead: Optional[tuple]            # ("line",) or ("arc", cx, cy, ccw) or None
    kind: str                        # "hole", "outer", "mark"
    part_label: str
    overcut_to: Optional[Tuple[float, float]] = None


def _start_tangent(c: Contour):
    (p0, p1, b) = next(c.segments())
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    if abs(b) > EPS:
        ang -= 2.0 * math.atan(b)  # tangent = chord rotated by -sweep/2
    return math.cos(ang), math.sin(ang)


def _nearest_vertex(c: Contour, pos) -> int:
    best, bi = None, 0
    for i, v in enumerate(c.vertices):
        d = (v[0] - pos[0]) ** 2 + (v[1] - pos[1]) ** 2
        if best is None or d < best:
            best, bi = d, i
    return bi


def _clear(path, start, length, region) -> bool:
    """Lead path stays in the scrap region (ignoring the last bit where it meets the contour)."""
    if region is None:
        return True
    if region.is_empty:
        return False
    tip = Point(start).buffer(min(0.6, length * 0.25))
    rest = path.difference(tip)
    return rest.is_empty or region.contains(rest)


def _make_lead(c: Contour, scrap_left: bool, length: float, lead_type: str, scrap_region):
    """Return (pierce_point, lead_spec) or (start, None) if no room."""
    x0, y0, _ = c.vertices[0]
    tx, ty = _start_tangent(c)
    nx, ny = (-ty, tx) if scrap_left else (ty, -tx)
    L = length
    while L >= 0.5 - 1e-9:
        if lead_type == "arc":
            r = L
            cx, cy = x0 + r * nx, y0 + r * ny
            px, py = cx - r * tx, cy - r * ty
            # sample the quarter arc to test it stays in scrap
            pts = []
            for k in range(0, 7):
                a = k / 6 * (math.pi / 2)
                # point rotating from Q to P around C
                qx, qy = px - cx, py - cy
                ca, sa = math.cos(a if scrap_left else -a), math.sin(a if scrap_left else -a)
                pts.append((cx + qx * ca - qy * sa, cy + qx * sa + qy * ca))
            if _clear(LineString(pts), (x0, y0), L, scrap_region):
                return (px, py), ("arc", cx, cy, scrap_left)
        else:
            px, py = x0 + L * nx, y0 + L * ny
            if _clear(LineString([(px, py), (x0, y0)]), (x0, y0), L, scrap_region):
                return (px, py), ("line",)
        L /= 2.0
    return (x0, y0), None


def plan_sheet(result: NestResult, layout: SheetLayout, cs: Optional[CutSettings] = None) -> List[CutPath]:
    cs = cs or CutSettings()
    tol = 0.05
    sheet = result.sheets[layout.sheet]
    sheet_poly = sheet.geometry.polygon(tol)
    placed = list(result.placed_geometry(layout))
    polys = [g.polygon(tol) for _, g in placed]
    labels = [f"{result.parts[pl.part].name}#{pl.instance}" for pl, _ in placed]
    n = len(placed)
    # host[i] = index of the part whose hole contains part i
    host = [None] * n
    for i in range(n):
        rp = polys[i].representative_point()
        for j in range(n):
            if i != j and any(Polygon(r).contains(rp) for r in polys[j].interiors):
                if host[i] is None or polys[j].area < polys[host[i]].area:
                    host[i] = j
    # outside-scrap region for outer lead-ins: sheet minus all parts (as solids)
    solids = unary_union([Polygon(p.exterior) for p in polys])
    outside_scrap = sheet_poly.difference(solids)

    pos = (0.0, 0.0)
    done = [False] * n
    paths: List[CutPath] = []
    for _ in range(n):
        ready = [i for i in range(n) if not done[i] and all(done[k] for k in range(n) if host[k] == i)]
        if not ready:  # cycle guard (should not happen)
            ready = [i for i in range(n) if not done[i]]
        i = min(ready, key=lambda k: polys[k].centroid.distance(Point(pos)))
        done[i] = True
        pl, g = placed[i]
        # marks first (no lead-in)
        if cs.cut_marks:
            for m in g.marks:
                paths.append(CutPath(m, (m.vertices[0][0], m.vertices[0][1]), None, "mark", labels[i]))
                pos = (m.vertices[-1][0], m.vertices[-1][1])
        holes = list(g.holes)
        while holes:
            h = min(holes, key=lambda c: min((v[0] - pos[0]) ** 2 + (v[1] - pos[1]) ** 2 for v in c.vertices))
            holes.remove(h)
            hc = h.oriented(cs.hole_ccw)
            hc = hc.rotated_start(_nearest_vertex(hc, pos))
            hole_raw = Polygon(hc.to_points(tol))
            hole_poly = hole_raw
            # don't lead in across a nested part sitting in this hole
            nested = [polys[k] for k in range(n) if host[k] == i
                      and hole_raw.contains(polys[k].representative_point())]
            if nested and not hole_poly.is_empty:
                hole_poly = hole_poly.difference(unary_union([Polygon(p.exterior) for p in nested]))
            scrap_left = cs.hole_ccw  # CCW travel -> hole interior on the left
            pierce, lead = _make_lead(hc, scrap_left, cs.lead_in, cs.lead_type, hole_poly)
            if lead is None:
                circ = hc.is_circle()
                if circ and not nested:
                    (cx, cy), r = circ
                    if r > cs.min_lead:
                        pierce, lead = (cx, cy), ("line",)
            paths.append(_finish(hc, pierce, lead, "hole", labels[i], cs))
            pos = (hc.vertices[0][0], hc.vertices[0][1])
        oc = g.outer.oriented(not cs.outer_cw)
        oc = oc.rotated_start(_nearest_vertex(oc, pos))
        scrap_left = cs.outer_cw  # CW travel around a part -> outside is on the left
        region = outside_scrap
        if host[i] is not None:
            # part inside another part's hole: scrap is that hole minus the part
            hj = host[i]
            rp = polys[i].representative_point()
            ring = next(r for r in polys[hj].interiors if Polygon(r).contains(rp))
            siblings = [Polygon(polys[k].exterior) for k in range(n) if host[k] == hj]
            region = Polygon(ring).difference(unary_union(siblings))
        pierce, lead = _make_lead(oc, scrap_left, cs.lead_in, cs.lead_type, region)
        paths.append(_finish(oc, pierce, lead, "outer", labels[i], cs))
        pos = (oc.vertices[0][0], oc.vertices[0][1])
    return paths


def _finish(c: Contour, pierce, lead, kind, label, cs: CutSettings) -> CutPath:
    over = None
    if cs.overcut > 0:
        p0, p1, b = next(c.segments())
        seg = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if abs(b) < EPS and seg > EPS:
            t = min(cs.overcut, seg) / seg
            over = (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)
    return CutPath(c, pierce, lead, kind, label, over)


def path_stats(paths: List[CutPath]):
    cut = sum(p.contour.length() for p in paths if p.kind != "mark")
    mark = sum(p.contour.length() for p in paths if p.kind == "mark")
    pierces = sum(1 for p in paths if p.kind != "mark")
    rapid = 0.0
    pos = (0.0, 0.0)
    for p in paths:
        rapid += math.hypot(p.pierce[0] - pos[0], p.pierce[1] - pos[1])
        last = p.contour.vertices[0] if p.contour.closed else p.contour.vertices[-1]
        pos = (last[0], last[1])
    return {"cut_length": cut, "mark_length": mark, "pierces": pierces, "rapid_length": rapid}
