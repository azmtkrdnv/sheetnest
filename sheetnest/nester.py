"""Greedy NFP placement on one or more sheets.

Given an ordered list of part instances, ``Packer.run`` fills sheets one at a
time. For every part it tries each allowed rotation (and mirror), computes the
region of valid reference-point positions as

    free = IFP(sheet, part) - union( NFP(placed_i, part) )

and picks the vertex of ``free`` with the best gravity score. Parts that do
not fit stay in the queue and are tried on the next sheet, so small parts
fill gaps (and holes) left by large ones.

Translation-invariant NFPs are cached per (placed variant, moving variant).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .model import NestSettings, Part, Placement, Sheet, SheetLayout
from .nfp import compute_ifp, compute_nfp, polygon_vertices


def _clean_polygon(geom) -> Polygon:
    if geom.geom_type == "Polygon":
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    return max(polys, key=lambda g: g.area) if polys else Polygon()


@dataclass
class Variant:
    key: Tuple[int, float, bool]
    part: int
    rotation: float
    mirror: bool
    poly: Polygon            # inflated nesting polygon (with holes)
    bounds: Tuple[float, float, float, float]
    area: float


class NestContext:
    """Per-job caches: inflated part variants, sheet regions, NFPs and IFPs."""

    def __init__(self, parts: Sequence[Part], sheets: Sequence[Sheet], settings: NestSettings):
        self.parts = parts
        self.sheets = sheets
        self.s = settings
        # Part polygons are flattened from outside (they always cover the real
        # part), so inflating by exactly half the spacing keeps the true
        # contours at least `spacing` apart and lets straight edges fit exactly.
        self.inflate = settings.spacing / 2.0
        self.variants: Dict[int, List[Variant]] = {}
        self.true_area = [p.geometry.area for p in parts]
        self._nfp: Dict[tuple, Polygon] = {}
        self._ifp: Dict[tuple, Polygon] = {}
        self.sheet_regions = [self._sheet_region(sh) for sh in sheets]
        for i, p in enumerate(parts):
            self.variants[i] = self._make_variants(i, p)

    # -- setup -------------------------------------------------------------
    def _make_variants(self, idx: int, part: Part) -> List[Variant]:
        tol = self.s.curve_tolerance
        base = part.geometry.polygon(tol, conservative=True)
        rots = part.rotations if part.rotations is not None else self.s.rotations
        mirror_ok = part.allow_mirror if part.allow_mirror is not None else self.s.allow_mirror
        mirrors = (False, True) if mirror_ok else (False,)
        out, seen = [], []
        for m in mirrors:
            for r in rots:
                g = base
                if m:
                    g = affinity.scale(g, -1, 1, origin=(0, 0))
                g = affinity.rotate(g, r, origin=(0, 0))
                if self.inflate > 0:
                    g = _clean_polygon(g.buffer(self.inflate, join_style=2, mitre_limit=2.0))
                # skip variants identical to one we already have (symmetric parts)
                # skip variants that are the same shape as one we have (symmetric parts),
                # comparing with both moved to their bounding-box corner
                gn = affinity.translate(g, -g.bounds[0], -g.bounds[1])
                dup = False
                for o in seen:
                    if abs(o.area - gn.area) < gn.length * tol and o.symmetric_difference(gn).area < gn.length * tol:
                        dup = True
                        break
                if dup:
                    continue
                seen.append(gn)
                out.append(Variant((idx, float(r), m), idx, float(r) % 360, m, g, g.bounds, g.area))
        return out

    def _sheet_region(self, sheet: Sheet):
        shrink = self.s.margin - self.s.spacing / 2.0
        if any(abs(v[2]) > 1e-12 for c in [sheet.geometry.outer] + sheet.geometry.holes for v in c.vertices):
            shrink += self.s.curve_tolerance  # chords of a curved remnant edge may lie outside it
        if sheet.is_rectangle:
            rect = (shrink, shrink, sheet.width - shrink, sheet.height - shrink)
            return Polygon([(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]), rect
        poly = sheet.geometry.polygon(self.s.curve_tolerance)
        poly = _clean_polygon(poly.buffer(-shrink, join_style=2))
        return poly, None

    # -- cached geometry ---------------------------------------------------
    def nfp(self, a: Variant, b: Variant) -> Polygon:
        k = (a.key, b.key)
        g = self._nfp.get(k)
        if g is None:
            g = compute_nfp(a.poly, b.poly, self.s.part_in_part)
            self._nfp[k] = g
        return g

    def ifp(self, sheet_idx: int, b: Variant) -> Polygon:
        k = (sheet_idx, b.key)
        g = self._ifp.get(k)
        if g is None:
            region, rect = self.sheet_regions[sheet_idx]
            g = compute_ifp(region, b.poly, rect)
            self._ifp[k] = g
        return g


class SheetState:
    def __init__(self, ctx: NestContext, sheet_idx: int, cross_weight: float = 0.05):
        self.ctx = ctx
        self.cross_weight = cross_weight
        self.sheet_idx = sheet_idx
        self.sheet = ctx.sheets[sheet_idx]
        self.placed: List[Tuple[Variant, float, float]] = []
        self.placements: List[Placement] = []
        self.unions: Dict[tuple, Tuple[object, int]] = {}
        self.failed: set = set()
        self.extent = [0.0, 0.0]   # max x, max y of placed (inflated) parts
        region, _ = ctx.sheet_regions[sheet_idx]
        self.free_area = region.area
        self.part_area = 0.0
        self.axis = 0 if ctx.s.gravity == "left" else 1

    def forbidden(self, v: Variant):
        geom, n = self.unions.get(v.key, (None, 0))
        if n < len(self.placed):
            new = []
            for pv, x, y in self.placed[n:]:
                g = self.ctx.nfp(pv, v)
                if not g.is_empty:
                    new.append(affinity.translate(g, x, y))
            if geom is not None:
                new.append(geom)
            geom = unary_union(new) if new else None
            self.unions[v.key] = (geom, len(self.placed))
        return geom

    def best_position(self, v: Variant):
        if v.area > self.free_area + 1e-6:
            return None
        ifp = self.ctx.ifp(self.sheet_idx, v)
        if ifp.is_empty:
            return None
        forb = self.forbidden(v)
        free = ifp if forb is None else ifp.difference(forb)
        if free.is_empty:
            return None
        pts = polygon_vertices(free)
        if not pts:
            return None
        arr = np.asarray(pts)
        bx0, by0, bx1, by1 = v.bounds
        W, H = self.sheet.width, self.sheet.height
        wl, wx, wy = 8.0, 1.0, self.cross_weight
        if self.axis == 0:
            # 1) don't grow the used length along X, 2) stay left, 3) then low
            lead = np.maximum(self.extent[0], arr[:, 0] + bx1)
            score = lead / W * wl + (arr[:, 0] + bx0) / W * wx + (arr[:, 1] + by0) / H * wy
        else:
            lead = np.maximum(self.extent[1], arr[:, 1] + by1)
            score = lead / H * wl + (arr[:, 1] + by0) / H * wx + (arr[:, 0] + bx0) / W * wy
        i = int(np.argmin(score))
        return float(score[i]), float(arr[i, 0]), float(arr[i, 1])

    def try_place(self, part_idx: int, instance: int, forced: Optional[int] = None) -> bool:
        if part_idx in self.failed:
            return False
        best = None
        variants = self.ctx.variants[part_idx]
        if forced is not None and forced < len(variants):
            variants = [variants[forced]]
        for v in variants:
            r = self.best_position(v)
            if r is not None and (best is None or r[0] < best[0] - 1e-9):
                best = (r[0], r[1], r[2], v)
        if best is None:
            self.failed.add(part_idx)
            return False
        _, x, y, v = best
        self.placed.append((v, x, y))
        self.placements.append(Placement(part_idx, instance, x, y, v.rotation, v.mirror))
        self.extent[0] = max(self.extent[0], x + v.bounds[2])
        self.extent[1] = max(self.extent[1], y + v.bounds[3])
        self.free_area -= v.area
        self.part_area += self.ctx.true_area[part_idx]
        return True

    def layout(self) -> SheetLayout:
        s = self.ctx.s
        ext = self.extent[self.axis] - self.ctx.inflate + s.margin if self.placed else 0.0
        full = self.sheet.width if self.axis == 0 else self.sheet.height
        return SheetLayout(self.sheet_idx, list(self.placements), min(ext, full),
                           self.part_area / self.sheet.area if self.sheet.area else 0.0, self.part_area)


class Packer:
    """Fill sheets with an ordered list of part instances."""

    def __init__(self, ctx: NestContext, sheet_ids: Sequence[int]):
        self.ctx = ctx
        self.sheet_ids = list(sheet_ids)
        self.cross_weight = 0.05
        self.forced: Dict[int, int] = {}

    def fill_sheet(self, sheet_idx: int, queue: List[Tuple[int, int]]):
        st = SheetState(self.ctx, sheet_idx, self.cross_weight)
        remaining = []
        for part_idx, inst in queue:
            if (not self.ctx.sheets[sheet_idx].accepts(self.ctx.parts[part_idx])
                    or not st.try_place(part_idx, inst, self.forced.get(part_idx))):
                remaining.append((part_idx, inst))
        return st, remaining

    def run(self, order: Sequence[int], cross_weight: float = 0.05,
            forced: Optional[Dict[int, int]] = None) -> Tuple[List[SheetLayout], List[Tuple[int, int]]]:
        """``forced`` optionally pins a part to one of its rotation variants."""
        self.cross_weight = cross_weight
        self.forced = forced or {}
        counter: Dict[int, int] = {}
        queue = []
        for p in order:
            counter[p] = counter.get(p, 0) + 1
            queue.append((p, counter[p]))
        stock = {i: self.ctx.sheets[i].quantity for i in self.sheet_ids}
        layouts: List[SheetLayout] = []
        mode = self.ctx.s.sheet_selection
        while queue:
            available = [i for i in self.sheet_ids if stock[i] is None or stock[i] > 0]
            if not available:
                break
            if mode == "best_fit" and len(available) > 1:
                best = None
                for sid in available:
                    st, rem = self.fill_sheet(sid, queue)
                    if not st.placed:
                        continue
                    # prefer the sheet that is used best; a smaller sheet that takes everything wins
                    key = (st.part_area / self.ctx.sheets[sid].area, -len(rem))
                    if best is None or key > best[0]:
                        best = (key, sid, st, rem)
                if best is None:
                    break
                _, sid, st, rem = best
            else:
                st, rem, sid = None, queue, None
                for cand in available:
                    st, rem = self.fill_sheet(cand, queue)
                    if st.placed:
                        sid = cand
                        break
                if sid is None:
                    break
            if stock[sid] is not None:
                stock[sid] -= 1
            layouts.append(st.layout())
            queue = rem
        return layouts, queue


def layout_cost(ctx: NestContext, layouts: List[SheetLayout], unplaced) -> float:
    """Lower is better: area of all full sheets + used part of the last one."""
    cost = 0.0
    for i, l in enumerate(layouts):
        sh = ctx.sheets[l.sheet]
        weight = sh.cost if sh.cost is not None else sh.area
        if i < len(layouts) - 1:
            cost += weight
        else:
            full = sh.width if ctx.s.gravity == "left" else sh.height
            cost += weight * (l.used_length / full if full else 1.0)
    for p, _ in unplaced:
        cost += ctx.true_area[p] * 1000.0
    return cost
