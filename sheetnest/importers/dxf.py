"""DXF (and DWG via the ODA File Converter) import.

Handles the entity types that show up in sheet-metal flat patterns exported
from SolidWorks, Inventor, Fusion, Solid Edge, AutoCAD etc.: LINE, ARC,
CIRCLE, LWPOLYLINE, POLYLINE, SPLINE, ELLIPSE and block INSERTs (exploded).
Arcs and circles are kept as true arcs. Splines and ellipses are flattened.
"""
from __future__ import annotations

import fnmatch
import math
import os
from typing import List, Optional, Sequence

import ezdxf
from ezdxf import disassemble

from ..geometry import Contour, PartGeometry, chain_segments, loops_to_parts, simplify_contour

# Layers that typically carry non-cut information in flat-pattern exports.
DEFAULT_IGNORE_LAYERS = ["*BEND*", "*DIM*", "*TEXT*", "*ANNOT*", "DEFPOINTS", "*CENTER*",
                         "*HIDDEN*", "*TITLE*", "*BORDER*", "*FORM*", "*SKETCH*"]
DEFAULT_MARK_LAYERS = ["*ETCH*", "*MARK*", "*ENGRAV*", "*SCRIBE*"]

# $INSUNITS -> millimetres
_UNIT_TO_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4}


def _match(layer: str, patterns: Sequence[str]) -> bool:
    lay = layer.upper()
    return any(fnmatch.fnmatchcase(lay, p.upper()) for p in patterns)


def _flatten(entity, tol) -> List:
    pts = [(v.x, v.y) for v in entity.flattening(tol)]
    return [(pts[i], pts[i + 1], 0.0) for i in range(len(pts) - 1)]


def entity_segments(e, tol: float = 0.05):
    """Convert one DXF entity into segments (p0, p1, bulge) and/or closed contours."""
    t = e.dxftype()
    segs, loops = [], []
    if t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        segs.append(((s.x, s.y), (en.x, en.y), 0.0))
    elif t == "ARC":
        if _is_planar_xy(e):
            c, r = e.dxf.center, e.dxf.radius
            a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
            sweep = (a1 - a0) % (2 * math.pi)
            if sweep < 1e-9:
                sweep = 2 * math.pi
            p0 = (c.x + r * math.cos(a0), c.y + r * math.sin(a0))
            p1 = (c.x + r * math.cos(a1), c.y + r * math.sin(a1))
            if sweep > math.pi * 1.999:
                loops.append(Contour.circle(c.x, c.y, r))
            else:
                segs.append((p0, p1, math.tan(sweep / 4.0)))
        else:
            segs.extend(_flatten(e, tol))
    elif t == "CIRCLE":
        c = e.dxf.center
        if _is_planar_xy(e):
            loops.append(Contour.circle(c.x, c.y, e.dxf.radius))
        else:
            segs.extend(_flatten(e, tol))
    elif t == "LWPOLYLINE":
        pts = list(e.get_points("xyb"))
        if _is_planar_xy(e) and len(pts) >= 2:
            if e.closed:
                for i in range(len(pts)):
                    a, b = pts[i], pts[(i + 1) % len(pts)]
                    segs.append(((a[0], a[1]), (b[0], b[1]), a[2]))
            else:
                for i in range(len(pts) - 1):
                    a, b = pts[i], pts[i + 1]
                    segs.append(((a[0], a[1]), (b[0], b[1]), a[2]))
        else:
            segs.extend(_flatten_path(e, tol))
    elif t == "POLYLINE":
        if e.is_2d_polyline:
            vs = [(v.dxf.location.x, v.dxf.location.y, v.dxf.bulge) for v in e.vertices]
            n = len(vs)
            rng = range(n) if e.is_closed else range(n - 1)
            for i in rng:
                a, b = vs[i], vs[(i + 1) % n]
                segs.append(((a[0], a[1]), (b[0], b[1]), a[2]))
        elif e.is_3d_polyline:
            vs = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            n = len(vs)
            rng = range(n) if e.is_closed else range(n - 1)
            for i in rng:
                segs.append((vs[i], vs[(i + 1) % n], 0.0))
    elif t in ("SPLINE", "ELLIPSE"):
        segs.extend(_flatten(e, tol))
    return segs, loops


def _flatten_path(e, tol):
    from ezdxf import path as ezpath
    p = ezpath.make_path(e)
    pts = [(v.x, v.y) for v in p.flattening(tol)]
    return [(pts[i], pts[i + 1], 0.0) for i in range(len(pts) - 1)]


def _is_planar_xy(e) -> bool:
    try:
        ext = e.dxf.extrusion
    except Exception:
        return True
    # (0,0,-1) extrusion mirrors OCS -> handle by flattening in WCS
    return abs(ext.z - 1.0) < 1e-9


def read_dxf(path: str, name: Optional[str] = None, *, tol: float = 0.05, join_tol: float = 0.05,
             ignore_layers: Optional[Sequence[str]] = None, mark_layers: Optional[Sequence[str]] = None,
             cut_layers: Optional[Sequence[str]] = None, units: Optional[str] = None,
             split: bool = True) -> List[PartGeometry]:
    """Read a DXF/DWG file and return one or more parts.

    ``cut_layers`` (if given) restricts cutting geometry to those layers.
    ``units``: force source units ("mm", "in", "cm", "m"); default reads $INSUNITS.
    ``split``: if the drawing contains several separate outlines, return each as
    its own part (default). Otherwise only the largest one is used.
    """
    ignore_layers = DEFAULT_IGNORE_LAYERS if ignore_layers is None else ignore_layers
    mark_layers = DEFAULT_MARK_LAYERS if mark_layers is None else mark_layers
    if path.lower().endswith(".dwg"):
        doc = _read_dwg(path)
    else:
        try:
            doc = ezdxf.readfile(path)
        except ezdxf.DXFStructureError:
            from ezdxf import recover
            doc, _ = recover.readfile(path)
    scale = _units_scale(doc, units)
    msp = doc.modelspace()
    name = name or os.path.splitext(os.path.basename(path))[0]
    cut_segs, cut_loops, mark_segs, mark_loops = [], [], [], []
    warnings = []
    skipped = {}
    for e in disassemble.recursive_decompose(msp):
        t = e.dxftype()
        layer = e.dxf.get("layer", "0")
        if t in ("TEXT", "MTEXT", "DIMENSION", "HATCH", "POINT", "SOLID", "LEADER", "MLEADER", "ATTRIB", "ATTDEF"):
            skipped[t] = skipped.get(t, 0) + 1
            continue
        is_mark = _match(layer, mark_layers)
        if not is_mark:
            if cut_layers is not None and not _match(layer, cut_layers):
                continue
            if cut_layers is None and _match(layer, ignore_layers):
                continue
        try:
            segs, loops = entity_segments(e, tol)
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"could not read {t} on layer {layer}: {exc}")
            continue
        (mark_segs if is_mark else cut_segs).extend(segs)
        (mark_loops if is_mark else cut_loops).extend(loops)
    if skipped:
        warnings.append("ignored " + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items())))
    closed, opened = chain_segments(cut_segs, join_tol)
    closed = [simplify_contour(c) for c in closed + cut_loops]
    if scale != 1.0:
        closed = [_scale(c, scale) for c in closed]
    parts = loops_to_parts(closed, name, tol, source=path)
    if opened:
        gap = sum(c.length() for c in opened) * scale
        warnings.append(f"{len(opened)} open contour(s) ({gap:.1f} mm total) could not be closed and were ignored; "
                        f"check for gaps larger than join tolerance {join_tol}")
    if not parts:
        raise ValueError(f"{path}: no closed outline found. " + "; ".join(warnings))
    if not split and len(parts) > 1:
        parts.sort(key=lambda p: -abs(p.outer.signed_area()))
        warnings.append(f"{len(parts) - 1} extra outline(s) ignored (split=False)")
        parts = [parts[0]]
        parts[0].name = name
    mclosed, mopen = chain_segments(mark_segs, join_tol)
    marks = [_scale(c, scale) for c in mclosed + mopen + mark_loops]
    for p in parts:
        p.warnings.extend(warnings)
    if marks:
        # attach marking geometry to the part whose outline contains it
        from shapely.geometry import Point as SPoint
        polys = [p.polygon(tol) for p in parts]
        for m in marks:
            v = m.vertices[0]
            for p, poly in zip(parts, polys):
                if poly.buffer(0.01).contains(SPoint(v[0], v[1])):
                    p.marks.append(m)
                    break
    return parts


def _units_scale(doc, units: Optional[str]) -> float:
    if units:
        u = units.lower()
        return {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, "ft": 304.8}[u]
    code = doc.header.get("$INSUNITS", 0)
    return _UNIT_TO_MM.get(code, 1.0)


def _scale(c: Contour, s: float) -> Contour:
    if s == 1.0:
        return c
    return Contour([(x * s, y * s, b) for x, y, b in c.vertices], c.closed)


def _read_dwg(path: str):
    try:
        from ezdxf.addons import odafc
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("DWG support needs ezdxf's odafc add-on") from exc
    if not odafc.is_installed():
        raise RuntimeError("Reading DWG requires the free ODA File Converter "
                           "(https://www.opendesign.com/guestfiles/oda_file_converter). "
                           "Install it or export DXF instead.")
    return odafc.readfile(path)
