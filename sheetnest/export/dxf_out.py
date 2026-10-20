"""Write nested sheets to DXF (one file per sheet) with true arcs and circles.

Layers: SHEET (stock outline), CUT_OUTER, CUT_INNER, MARK (etch), LABELS,
REMNANT (suggested reusable offcut). Most CAM packages (SheetCam, ProNest,
Lantek, LightBurn, ...) can map these layers to operations directly.
"""
from __future__ import annotations

from typing import Optional

import ezdxf

from ..geometry import Contour
from ..model import NestResult, SheetLayout

LAYERS = {"SHEET": 8, "CUT_OUTER": 1, "CUT_INNER": 5, "MARK": 3, "LABELS": 2, "REMNANT": 6}


def _add_contour(msp, c: Contour, layer: str):
    circ = c.is_circle()
    if circ:
        (cx, cy), r = circ
        msp.add_circle((cx, cy), r, dxfattribs={"layer": layer})
        return
    msp.add_lwpolyline([(x, y, 0, 0, b) for x, y, b in c.vertices], format="xyseb",
                       close=c.closed, dxfattribs={"layer": layer})


def remnant_rect(result: NestResult, layout: SheetLayout, min_size: float = 100.0):
    """Rectangular offcut left after the used length, or None."""
    sheet = result.sheets[layout.sheet]
    if not sheet.is_rectangle or not layout.placements:
        return None
    # used_length already includes one margin beyond the last part
    if result.settings.gravity == "left":
        x = layout.used_length
        if sheet.width - x < min_size:
            return None
        return (x, 0.0, sheet.width, sheet.height)
    y = layout.used_length
    if sheet.height - y < min_size:
        return None
    return (0.0, y, sheet.width, sheet.height)


def write_sheet_dxf(result: NestResult, layout: SheetLayout, path: str, labels: bool = True,
                    remnant_min: Optional[float] = 100.0, version: str = "R2010") -> None:
    doc = ezdxf.new(version)
    doc.header["$INSUNITS"] = 4  # mm
    for name, color in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    msp = doc.modelspace()
    sheet = result.sheets[layout.sheet]
    _add_contour(msp, sheet.geometry.outer, "SHEET")
    for h in sheet.geometry.holes:
        _add_contour(msp, h, "SHEET")
    for pl, g in result.placed_geometry(layout):
        _add_contour(msp, g.outer, "CUT_OUTER")
        for h in g.holes:
            _add_contour(msp, h, "CUT_INNER")
        for m in g.marks:
            _add_contour(msp, m, "MARK")
        if labels:
            poly = g.polygon(0.5)
            p = poly.representative_point()
            w, h = g.size
            height = max(2.0, min(12.0, min(w, h) / 8.0))
            msp.add_text(f"{result.parts[pl.part].name}", height=height,
                         dxfattribs={"layer": "LABELS"}).set_placement(
                (p.x, p.y), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
    if remnant_min is not None:
        r = remnant_rect(result, layout, remnant_min)
        if r:
            _add_contour(msp, Contour.rectangle(*r), "REMNANT")
    doc.saveas(path)
