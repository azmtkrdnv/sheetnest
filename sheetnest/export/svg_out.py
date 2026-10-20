"""SVG previews of nested sheets (true arcs, part colours, labels, cut order)."""
from __future__ import annotations

import html
import math
from typing import Optional

from ..geometry import Contour, EPS, bulge_to_arc
from ..model import NestResult, SheetLayout

PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#B279A2", "#E45756", "#72B7B2", "#EECA3B", "#9D755D",
           "#FF9DA6", "#79706E", "#2E86AB", "#A23B72", "#3B8B5A", "#C97B2B", "#5B5F97", "#8C8C3A"]


def part_color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def contour_path(c: Contour, digits: int = 2) -> str:
    f = lambda v: f"{v:.{digits}f}".rstrip("0").rstrip(".")
    v0 = c.vertices[0]
    d = [f"M{f(v0[0])} {f(v0[1])}"]
    for p0, p1, b in c.segments():
        if abs(b) < EPS:
            d.append(f"L{f(p1[0])} {f(p1[1])}")
        else:
            arc = bulge_to_arc(p0, p1, b)
            if arc is None:
                d.append(f"L{f(p1[0])} {f(p1[1])}")
                continue
            r, sweep = arc[1], arc[3]
            large = 1 if abs(sweep) > math.pi else 0
            sw = 1 if sweep > 0 else 0
            d.append(f"A{f(r)} {f(r)} 0 {large} {sw} {f(p1[0])} {f(p1[1])}")
    if c.closed:
        d.append("Z")
    return "".join(d)


def sheet_svg(result: NestResult, layout: SheetLayout, width_px: Optional[int] = None, labels: bool = True,
              remnant=None, cut_paths=None) -> str:
    sheet = result.sheets[layout.sheet]
    W, H = sheet.width, sheet.height
    pad = max(W, H) * 0.01
    size = f' width="{width_px}" height="{width_px * (H + 2 * pad) / (W + 2 * pad):.0f}"' if width_px else ""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad:.1f} {-pad:.1f} {W + 2 * pad:.1f} {H + 2 * pad:.1f}"{size} '
           f'font-family="Inter, Segoe UI, Arial, sans-serif">']
    stroke = max(W, H) / 1500.0
    out.append(f'<g transform="translate(0,{H:.3f}) scale(1,-1)">')
    sp = contour_path(sheet.geometry.outer) + "".join(contour_path(h) for h in sheet.geometry.holes)
    out.append(f'<path d="{sp}" fill="#E9ECEF" stroke="#6C757D" stroke-width="{stroke * 1.5:.2f}" fill-rule="evenodd"/>')
    if remnant:
        x0, y0, x1, y1 = remnant
        out.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" fill="none" '
                   f'stroke="#B279A2" stroke-width="{stroke:.2f}" stroke-dasharray="{stroke * 8:.1f} {stroke * 5:.1f}"/>')
    for pl, g in result.placed_geometry(layout):
        d = contour_path(g.outer) + "".join(contour_path(h) for h in g.holes)
        col = part_color(pl.part)
        out.append(f'<path d="{d}" fill="{col}" fill-opacity="0.78" stroke="#1F2328" '
                   f'stroke-width="{stroke:.2f}" fill-rule="evenodd"><title>{html.escape(result.parts[pl.part].name)} '
                   f'#{pl.instance}  rot {pl.rotation:g}°</title></path>')
        for m in g.marks:
            out.append(f'<path d="{contour_path(m)}" fill="none" stroke="#FFFFFF" stroke-width="{stroke:.2f}"/>')
    if cut_paths:
        for cp in cut_paths:
            if cp.lead is not None:
                s = cp.contour.vertices[0]
                out.append(f'<line x1="{cp.pierce[0]:.1f}" y1="{cp.pierce[1]:.1f}" x2="{s[0]:.1f}" y2="{s[1]:.1f}" '
                           f'stroke="#D62728" stroke-width="{stroke * 1.2:.2f}"/>')
            out.append(f'<circle cx="{cp.pierce[0]:.1f}" cy="{cp.pierce[1]:.1f}" r="{stroke * 2.5:.2f}" fill="#D62728"/>')
    out.append("</g>")
    if labels:
        for pl, g in result.placed_geometry(layout):
            poly = g.polygon(0.5)
            p = poly.representative_point()
            w, h = g.size
            fs = max(W, H) / 120.0
            fs = min(fs, min(w, h) / 3.5)
            if fs < max(W, H) / 400.0:
                continue
            name = html.escape(result.parts[pl.part].name)
            out.append(f'<text x="{p.x:.1f}" y="{H - p.y:.1f}" font-size="{fs:.1f}" text-anchor="middle" '
                       f'dominant-baseline="middle" font-weight="600" fill="#1F2328" fill-opacity="0.85">{name}</text>')
    out.append("</svg>")
    return "\n".join(out)
