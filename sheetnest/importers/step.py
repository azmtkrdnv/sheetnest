"""STEP / IGES import for *flat* sheet-metal parts (optional, needs OCP).

Export the flat pattern from your CAD system (SolidWorks: "Flatten", then
save as STEP; or use the Sheet Metal flat-pattern export). The importer finds
the largest planar face, projects its boundary into 2D and keeps circular
edges as true arcs. The plate thickness is detected from the opposite face.

Install the backend with:  pip install cadquery-ocp
"""
from __future__ import annotations

import os
from typing import List, Optional

from ..geometry import Contour, PartGeometry, loops_to_parts, simplify_contour, three_point_bulge


def _require_ocp():
    try:
        import OCP  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("STEP/IGES import needs OpenCascade bindings: pip install cadquery-ocp") from exc


def read_step(path: str, name: Optional[str] = None, *, tol: float = 0.05, **_) -> List[PartGeometry]:
    _require_ocp()
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line, GeomAbs_Plane
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    _face = getattr(TopoDS, 'Face_s', None) or TopoDS.Face
    _wire = getattr(TopoDS, 'Wire_s', None) or TopoDS.Wire

    ext = path.lower().rsplit(".", 1)[-1]
    if ext in ("igs", "iges"):
        from OCP.IGESControl import IGESControl_Reader as Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as Reader
    reader = Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise ValueError(f"{path}: could not read file")
    reader.TransferRoots()
    shape = reader.OneShape()

    faces = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        f = _face(exp.Current())
        surf = BRepAdaptor_Surface(f)
        if surf.GetType() == GeomAbs_Plane:
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(f, props)
            faces.append((props.Mass(), f, surf.Plane()))
        exp.Next()
    if not faces:
        raise ValueError(f"{path}: no planar faces found - is this a flat part?")
    faces.sort(key=lambda t: -t[0])
    area, face, plane = faces[0]
    pos = plane.Position()
    o, xd, yd, zd = pos.Location(), pos.XDirection(), pos.YDirection(), pos.Direction()

    def to2d(p):
        v = (p.X() - o.X(), p.Y() - o.Y(), p.Z() - o.Z())
        return (v[0] * xd.X() + v[1] * xd.Y() + v[2] * xd.Z(),
                v[0] * yd.X() + v[1] * yd.Y() + v[2] * yd.Z())

    warnings = []
    # thickness: distance to the largest parallel planar face
    thickness = None
    for a, _f, pl in faces[1:]:
        d = pl.Position().Direction()
        if abs(abs(d.X() * zd.X() + d.Y() * zd.Y() + d.Z() * zd.Z()) - 1.0) < 1e-6 and a > 0.5 * area:
            lo = pl.Location()
            thickness = abs((lo.X() - o.X()) * zd.X() + (lo.Y() - o.Y()) * zd.Y() + (lo.Z() - o.Z()) * zd.Z())
            break
    loops = []
    wexp = TopExp_Explorer(face, TopAbs_WIRE)
    while wexp.More():
        wire = _wire(wexp.Current())
        verts = []
        we = BRepTools_WireExplorer(wire, face)
        while we.More():
            edge = we.Current()
            reversed_ = we.Orientation() == TopAbs_REVERSED
            c = BRepAdaptor_Curve(edge)
            t0, t1 = c.FirstParameter(), c.LastParameter()
            if reversed_:
                t0, t1 = t1, t0
            kind = c.GetType()
            if kind == GeomAbs_Line:
                verts.append((*to2d(c.Value(t0)), 0.0))
            elif kind == GeomAbs_Circle:
                # split into two halves so full circles work too
                tm = (t0 + t1) / 2.0
                for a_, b_ in ((t0, tm), (tm, t1)):
                    p0, pm, p1 = to2d(c.Value(a_)), to2d(c.Value((a_ + b_) / 2)), to2d(c.Value(b_))
                    verts.append((p0[0], p0[1], three_point_bulge(p0, pm, p1)))
            else:
                length = GCPnts_AbscissaPoint.Length_s(c, min(t0, t1), max(t0, t1))
                n = max(8, min(2000, int(length / max(tol * 4, 0.2))))
                for i in range(n):
                    p = to2d(c.Value(t0 + (t1 - t0) * i / n))
                    verts.append((p[0], p[1], 0.0))
            we.Next()
        if len(verts) >= 2:
            loops.append(simplify_contour(Contour(verts, True)))
        wexp.Next()

    name = name or os.path.splitext(os.path.basename(path))[0]
    parts = loops_to_parts(loops, name, tol, source=path)
    if not parts:
        raise ValueError(f"{path}: could not build an outline from the largest planar face")
    if len(faces) > 2 and thickness is None:
        warnings.append("no parallel back face found - make sure the STEP is a flat pattern")
    for p in parts:
        if thickness:
            p.meta["thickness"] = round(thickness, 4)
        p.warnings.extend(warnings)
    return parts
