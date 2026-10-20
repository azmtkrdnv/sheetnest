"""Independent check of a nesting result against the real (un-inflated) geometry."""
from __future__ import annotations

from typing import List

from shapely.strtree import STRtree

from .model import NestResult


def validate(result: NestResult, tol: float = 0.02) -> List[str]:
    """Return a list of problems (empty list = layout is valid).

    Checks every sheet for parts closer than ``spacing - tol`` to each other
    (a part inside another part's hole counts as clear if it keeps the
    spacing to the hole edge), and parts closer than ``margin - tol`` to the
    sheet edge or outside the sheet.
    """
    s = result.settings
    problems = []
    fine = min(0.02, s.curve_tolerance)
    for n, layout in enumerate(result.layouts, 1):
        sheet = result.sheets[layout.sheet]
        sheet_poly = sheet.geometry.polygon(fine)
        items = []
        for pl, geom in result.placed_geometry(layout):
            items.append((pl, geom.polygon(fine)))
        polys = [p for _, p in items]
        tree = STRtree(polys)
        for i, (pl, poly) in enumerate(items):
            name = f"{result.parts[pl.part].name}#{pl.instance}"
            if not sheet_poly.buffer(tol).contains(poly):
                problems.append(f"sheet {n}: {name} extends outside the sheet")
            else:
                d = sheet_poly.exterior.distance(poly)
                for ring in sheet_poly.interiors:
                    d = min(d, ring.distance(poly))
                if d < s.margin - tol:
                    problems.append(f"sheet {n}: {name} is {d:.2f} mm from the sheet edge (margin {s.margin})")
            for j in tree.query(poly.buffer(s.spacing)):
                j = int(j)
                if j <= i:
                    continue
                other = polys[j]
                if poly.buffer(-tol / 2).intersects(other.buffer(-tol / 2)):
                    problems.append(f"sheet {n}: {name} overlaps "
                                    f"{result.parts[items[j][0].part].name}#{items[j][0].instance}")
                    continue
                d = poly.distance(other)
                if d < s.spacing - tol:
                    problems.append(f"sheet {n}: {name} is {d:.2f} mm from "
                                    f"{result.parts[items[j][0].part].name}#{items[j][0].instance} "
                                    f"(spacing {s.spacing})")
    return problems
