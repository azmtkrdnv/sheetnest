"""Write every output for a nesting result into a folder.

    out/
      report.html          visual report: all sheets, utilisation, part list
      result.json          machine-readable placements (for a SolidWorks add-in, ERP, ...)
      parts.csv / sheets.csv
      sheet_01.dxf ...     nested sheet drawings (true arcs, CAM layers)
      sheet_01.svg ...     previews
      sheet_01.nc  ...     G-code (if enabled)
"""
from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..model import NestResult
from .dxf_out import remnant_rect, write_sheet_dxf
from .gcode import write_gcode
from .svg_out import part_color, sheet_svg
from .toolpath import CutSettings, path_stats, plan_sheet


@dataclass
class OutputOptions:
    dxf: bool = True
    svg: bool = True
    html: bool = True
    json: bool = True
    csv: bool = True
    labels: bool = True
    remnant_min: Optional[float] = 150.0     # suggest offcuts at least this wide (None = off)
    gcode: Optional[dict] = None             # e.g. {"post": "generic_plasma", "feed": 2500, "kerf": 1.6}
    cut: CutSettings = field(default_factory=CutSettings)


def export_all(result: NestResult, outdir: str, opts: Optional[OutputOptions] = None, job_name: str = "nest") -> dict:
    opts = opts or OutputOptions()
    os.makedirs(outdir, exist_ok=True)
    sheets_info = []
    for n, layout in enumerate(result.layouts, 1):
        base = os.path.join(outdir, f"sheet_{n:02d}")
        sheet = result.sheets[layout.sheet]
        paths = plan_sheet(result, layout, opts.cut)
        stats = path_stats(paths)
        rem = remnant_rect(result, layout, opts.remnant_min) if opts.remnant_min is not None else None
        info = {
            "index": n, "sheet": sheet.name, "width": sheet.width, "height": sheet.height,
            "material": sheet.material, "thickness": sheet.thickness, "group": layout.group,
            "parts": layout.count, "utilization": round(layout.utilization, 4),
            "used_length": round(layout.used_length, 1),
            "cut_length": round(stats["cut_length"], 1), "pierces": stats["pierces"],
            "rapid_length": round(stats["rapid_length"], 1),
            "remnant": [round(v, 1) for v in rem] if rem else None,
            "placements": [dict(part=result.parts[p.part].name, part_index=p.part, instance=p.instance,
                                x=round(p.x, 4), y=round(p.y, 4), rotation=p.rotation, mirror=p.mirror)
                           for p in layout.placements],
            "files": {},
        }
        if opts.dxf:
            write_sheet_dxf(result, layout, base + ".dxf", opts.labels, opts.remnant_min)
            info["files"]["dxf"] = os.path.basename(base + ".dxf")
        if opts.gcode:
            g = dict(opts.gcode)
            post = g.pop("post", "generic_plasma")
            from .gcode import POSTS
            ext = (POSTS[post]["ext"] if isinstance(post, str) else post.get("ext", "nc"))
            gst = write_gcode(paths, base + "." + ext, post=post, sheet_name=f"sheet {n} {sheet.name}", **g)
            info["est_time_min"] = round(gst["est_time_s"] / 60.0, 1)
            info["files"]["gcode"] = os.path.basename(base + "." + ext)
        svg = sheet_svg(result, layout, labels=opts.labels, remnant=rem)
        if opts.svg:
            with open(base + ".svg", "w") as f:
                f.write(svg)
            info["files"]["svg"] = os.path.basename(base + ".svg")
        info["_svg"] = sheet_svg(result, layout, labels=opts.labels, remnant=rem, cut_paths=paths)
        sheets_info.append(info)

    parts_info = []
    placed_count = {}
    for l in result.layouts:
        for p in l.placements:
            placed_count[p.part] = placed_count.get(p.part, 0) + 1
    for i, p in enumerate(result.parts):
        w, h = p.geometry.size
        parts_info.append({
            "name": p.name, "file": _rel(p.geometry.source, outdir), "quantity": p.quantity, "placed": placed_count.get(i, 0),
            "width": round(w, 2), "height": round(h, 2), "area": round(p.geometry.area, 1),
            "holes": len(p.geometry.holes), "cut_length": round(p.geometry.cut_length, 1),
            "material": p.material, "thickness": p.thickness, "warnings": p.geometry.warnings,
        })
    used_area = 0.0
    for l, info in zip(result.layouts, sheets_info):
        rem = info["remnant"]
        used_area += result.sheets[l.sheet].area - ((rem[2] - rem[0]) * (rem[3] - rem[1]) if rem else 0.0)
    part_area = sum(l.part_area for l in result.layouts)
    summary = {
        "job": job_name,
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "sheets_used": result.sheet_count,
        "utilization": round(result.utilization, 4),
        "utilization_excl_offcut": round(part_area / used_area, 4) if used_area else 0.0,
        "parts_requested": sum(p.quantity for p in result.parts),
        "parts_placed": sum(placed_count.values()),
        "elapsed_s": round(result.elapsed, 2), "layouts_evaluated": result.evaluations,
        "settings": asdict(result.settings),
        "messages": result.messages,
        "transform": "placed = rotate(mirror_x(part), rotation deg about origin) + (x, y); "
                     "part origin = lower-left corner of its bounding box as imported",
    }
    data = {"summary": summary, "sheets": [{k: v for k, v in s.items() if k != "_svg"} for s in sheets_info],
            "parts": parts_info}
    if opts.json:
        with open(os.path.join(outdir, "result.json"), "w") as f:
            json.dump(data, f, indent=2, default=str)
    if opts.csv:
        with open(os.path.join(outdir, "parts.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "quantity", "placed", "width", "height", "area_mm2", "holes", "cut_length", "material",
                        "thickness", "file"])
            for p in parts_info:
                w.writerow([p["name"], p["quantity"], p["placed"], p["width"], p["height"], p["area"], p["holes"],
                            p["cut_length"], p["material"] or "", p["thickness"] or "", p["file"] or ""])
        with open(os.path.join(outdir, "sheets.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["sheet", "stock", "width", "height", "parts", "utilization_pct", "used_length", "cut_length",
                        "pierces", "est_time_min", "remnant"])
            for s in sheets_info:
                rem = s["remnant"]
                w.writerow([s["index"], s["sheet"], s["width"], s["height"], s["parts"],
                            round(s["utilization"] * 100, 1), s["used_length"], s["cut_length"], s["pierces"],
                            s.get("est_time_min", ""),
                            f"{rem[2] - rem[0]:.0f}x{rem[3] - rem[1]:.0f}" if rem else ""])
    if opts.html:
        with open(os.path.join(outdir, "report.html"), "w") as f:
            f.write(_html(data, sheets_info, result))
    return data


def _rel(path, outdir):
    """Source paths relative to the output folder, so reports can be moved or shared."""
    if not path:
        return path
    try:
        return os.path.relpath(path, outdir).replace(os.sep, "/")
    except ValueError:  # different drive on Windows
        return path


def _html(data, sheets_info, result) -> str:
    s = data["summary"]
    esc = html.escape
    total_cut = sum(x["cut_length"] for x in sheets_info) / 1000.0
    total_pierce = sum(x["pierces"] for x in sheets_info)
    est = [x.get("est_time_min") for x in sheets_info if x.get("est_time_min") is not None]
    tiles = [("Sheets", f"{s['sheets_used']}"), ("Utilisation", f"{s['utilization'] * 100:.1f}%"),
             ("Excl. offcut", f"{s['utilization_excl_offcut'] * 100:.1f}%"),
             ("Parts placed", f"{s['parts_placed']} / {s['parts_requested']}"),
             ("Cut length", f"{total_cut:.1f} m"), ("Pierces", f"{total_pierce}")]
    if est:
        tiles.append(("Est. cut time", f"{sum(est):.0f} min"))
    tile_html = "".join(f'<div class="tile"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>'
                        for k, v in tiles)
    msgs = "".join(f'<div class="msg">{esc(m)}</div>' for m in s["messages"])
    cards = []
    for x in sheets_info:
        rem = x["remnant"]
        meta = [f"{x['width']:g} × {x['height']:g} mm", f"{x['parts']} parts",
                f"{x['utilization'] * 100:.1f}% used", f"length used {x['used_length']:.0f} mm",
                f"cut {x['cut_length'] / 1000:.2f} m", f"{x['pierces']} pierces"]
        if x.get("est_time_min") is not None:
            meta.append(f"~{x['est_time_min']:.0f} min")
        if rem:
            meta.append(f"offcut {rem[2] - rem[0]:.0f} × {rem[3] - rem[1]:.0f}")
        files = " · ".join(f'<a href="{esc(v)}">{esc(k.upper())}</a>' for k, v in x["files"].items())
        group = f'<span class="grp">{esc(x["group"])}</span>' if x["group"] else ""
        cards.append(f'<section class="card"><header><h2>Sheet {x["index"]} <small>{esc(x["sheet"])}</small>{group}</h2>'
                     f'<div class="files">{files}</div></header><div class="meta">{" · ".join(esc(m) for m in meta)}</div>'
                     f'<div class="svg">{x["_svg"]}</div></section>')
    rows = []
    for i, p in enumerate(data["parts"]):
        ok = p["placed"] >= p["quantity"]
        warn = "<br>".join(esc(w) for w in p["warnings"])
        rows.append(f'<tr><td><span class="sw" style="background:{part_color(i)}"></span>{esc(p["name"])}</td>'
                    f'<td class="n">{p["quantity"]}</td><td class="n {"" if ok else "bad"}">{p["placed"]}</td>'
                    f'<td class="n">{p["width"]:.1f} × {p["height"]:.1f}</td><td class="n">{p["area"] / 1e6:.4f}</td>'
                    f'<td class="n">{p["holes"]}</td><td class="n">{p["cut_length"]:.0f}</td>'
                    f'<td>{esc(str(p["material"] or ""))} {esc(str(p["thickness"] or ""))}</td><td class="w">{warn}</td></tr>')
    st = s["settings"]
    settings_line = (f"spacing {st['spacing']} mm · margin {st['margin']} mm · rotations {', '.join(str(r) for r in st['rotations'])}"
                     f" · part-in-part {'on' if st['part_in_part'] else 'off'} · {s['layouts_evaluated']} layouts in {s['elapsed_s']} s")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Nesting report – {esc(s['job'])}</title>
<style>
:root{{--bg:#F6F7F9;--card:#fff;--ink:#1F2328;--mute:#5F6B7A;--line:#DDE1E6;--accent:#2E6BD0}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Inter,"Segoe UI",system-ui,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mute);margin-bottom:20px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.tile .v{{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums}}.tile .k{{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.msg{{background:#FFF4E5;border:1px solid #F5C77E;border-radius:8px;padding:8px 12px;margin-bottom:8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}}
.card header{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.card h2{{font-size:16px;margin:0}}.card h2 small{{color:var(--mute);font-weight:500;margin-left:6px}}
.grp{{margin-left:8px;font-size:12px;background:#EEF2FF;color:#3346A8;border-radius:999px;padding:2px 8px}}
.files a{{color:var(--accent);text-decoration:none;font-weight:600;font-size:12px}}
.meta{{color:var(--mute);margin:6px 0 10px;font-variant-numeric:tabular-nums}}
.svg svg{{width:100%;height:auto;display:block}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{font-size:12px;color:var(--mute);font-weight:600;background:#FAFBFC}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}td.bad{{color:#C62828;font-weight:700}}td.w{{color:#8A5A00;font-size:12px}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:8px}}
.wrap{{overflow-x:auto}}footer{{color:var(--mute);font-size:12px;margin-top:20px}}
</style></head><body><main>
<h1>Nesting report – {esc(s['job'])}</h1><div class="sub">{esc(s['created'])} · {esc(settings_line)}</div>
<div class="tiles">{tile_html}</div>{msgs}
{''.join(cards)}
<h2 style="font-size:16px;margin:28px 0 10px">Parts</h2>
<div class="wrap"><table><thead><tr><th>Part</th><th>Qty</th><th>Placed</th><th>Size (mm)</th><th>Area (m²)</th><th>Holes</th><th>Cut (mm)</th><th>Material</th><th>Notes</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<footer>Red dots and lines on the sheets show pierce points and lead-ins. Generated by sheetnest.</footer>
</main></body></html>"""
