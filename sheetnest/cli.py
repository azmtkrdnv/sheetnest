"""Command-line interface.

    sheetnest run job.yaml -o out/
    sheetnest quick parts/*.dxf flange.dxf:12 --sheet 3000x1500x10 --spacing 6 --margin 10 -o out/
    sheetnest inspect part.dxf part.step
    sheetnest posts
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

from . import __version__
from .export.gcode import POSTS
from .export.report import OutputOptions, export_all
from .export.toolpath import CutSettings
from .importers import load_parts, supported_formats
from .job import Job, load_job, parts_from_file
from .model import NestSettings, Sheet
from .optimize import nest
from .validate import validate


def _parse_sheet(s: str) -> Sheet:
    """'3000x1500', '3000x1500x10' (qty), '3000x1500x10:S235:5' (material:thickness)."""
    dims, *rest = s.split(":")
    vals = [float(v) for v in dims.lower().replace("*", "x").split("x")]
    if len(vals) < 2:
        raise argparse.ArgumentTypeError(f"bad sheet '{s}', use WIDTHxHEIGHT[xQTY]")
    qty = int(vals[2]) if len(vals) > 2 else None
    mat = rest[0] if rest and rest[0] else None
    th = float(rest[1]) if len(rest) > 1 and rest[1] else None
    return Sheet(vals[0], vals[1], quantity=qty, material=mat, thickness=th)


def _expand_inputs(items):
    exts = tuple("." + e for e in supported_formats())
    for item in items:
        qty = None
        path = item
        if ":" in item and not os.path.exists(item):
            head, tail = item.rsplit(":", 1)
            if tail.isdigit():
                path, qty = head, int(tail)
        if os.path.isdir(path):
            for f in sorted(os.listdir(path)):
                if f.lower().endswith(exts):
                    yield os.path.join(path, f), qty
        elif any(ch in path for ch in "*?["):
            for f in sorted(glob.glob(path)):
                yield f, qty
        else:
            yield path, qty


def _run(job: Job, outdir: str, quiet: bool = False) -> int:
    for w in job.warnings:
        print("warning:", w, file=sys.stderr)
    if not job.parts:
        print("error: no parts to nest", file=sys.stderr)
        return 2
    if not job.sheets:
        print("error: no sheets defined", file=sys.stderr)
        return 2
    n_inst = sum(p.quantity for p in job.parts)
    if not quiet:
        print(f"nesting {n_inst} parts ({len(job.parts)} types) on {len(job.sheets)} sheet type(s), "
              f"optimising for {job.settings.time_limit:g}s ...")
    t0 = time.time()
    result = nest(job.parts, job.sheets, job.settings)
    problems = validate(result)
    export_all(result, outdir, job.output, job.name)
    if not quiet:
        print(result.summary())
        print(f"outputs written to {os.path.abspath(outdir)} ({time.time() - t0:.1f}s)")
    if problems:
        print("VALIDATION FAILED:", *problems[:20], sep="\n  ", file=sys.stderr)
        return 3
    return 1 if result.unplaced else 0


def cmd_run(a):
    job = load_job(a.job)
    if a.time is not None:
        job.settings.time_limit = a.time
    if a.workers is not None:
        job.settings.workers = a.workers
    out = a.output or os.path.join(os.path.dirname(os.path.abspath(a.job)), f"{job.name}_out")
    return _run(job, out, a.quiet)


def cmd_quick(a):
    imp = {"join_tol": a.join_tol}
    if a.units:
        imp["units"] = a.units
    job = Job(name=a.name)
    for path, qty in _expand_inputs(a.files):
        kw = {}
        if a.material:
            kw["material"] = a.material
        if a.thickness:
            kw["thickness"] = a.thickness
        try:
            job.parts.extend(parts_from_file(path, qty if qty is not None else a.qty, imp, **kw))
        except Exception as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 2
    for p in job.parts:
        for w in p.geometry.warnings:
            job.warnings.append(f"{p.name}: {w}")
    job.sheets = [_parse_sheet(s) for s in (a.sheet or ["3000x1500"])]
    job.settings = NestSettings(spacing=a.spacing, margin=a.margin,
                                rotations=[float(r) for r in a.rotations.split(",")],
                                allow_mirror=a.mirror, part_in_part=not a.no_part_in_part,
                                time_limit=a.time, gravity=a.gravity, workers=a.workers or 0,
                                curve_tolerance=a.curve_tol)
    gcode = None
    if a.gcode:
        gcode = {"post": a.gcode, "feed": a.feed, "kerf": a.kerf, "pierce_delay": a.pierce_delay,
                 "kerf_mode": "controller" if a.kerf_comp else "none"}
    job.output = OutputOptions(gcode=gcode, cut=CutSettings(lead_in=a.lead_in))
    return _run(job, a.output, a.quiet)


def cmd_inspect(a):
    rc = 0
    for path, _ in _expand_inputs(a.files):
        try:
            parts = load_parts(path, join_tol=a.join_tol)
        except Exception as exc:
            print(f"{path}: ERROR {exc}")
            rc = 2
            continue
        for p in parts:
            w, h = p.size
            circ = sum(1 for c in p.holes if c.is_circle())
            print(f"{path}\n  part {p.name}: {w:.2f} x {h:.2f} mm, area {p.area:.1f} mm², "
                  f"{len(p.holes)} hole(s) ({circ} round), cut length {p.cut_length:.1f} mm, "
                  f"{len(p.marks)} mark(s)" + (f", thickness {p.meta['thickness']} mm" if "thickness" in p.meta else ""))
            for wmsg in p.warnings:
                print("  warning:", wmsg)
    return rc


def cmd_posts(a):
    for k, v in POSTS.items():
        print(f"{k:22s} .{v['ext']}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheetnest", description="True-shape nesting for plasma / laser cutting")
    ap.add_argument("--version", action="version", version=f"sheetnest {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a job file (YAML/JSON)")
    r.add_argument("job")
    r.add_argument("-o", "--output")
    r.add_argument("-t", "--time", type=float, help="override optimisation time (s)")
    r.add_argument("-w", "--workers", type=int)
    r.add_argument("-q", "--quiet", action="store_true")
    r.set_defaults(func=cmd_run)

    q = sub.add_parser("quick", help="nest files directly from the command line")
    q.add_argument("files", nargs="+", help="part files, folders or globs; append :N for quantity (part.dxf:12)")
    q.add_argument("-s", "--sheet", action="append", help="WIDTHxHEIGHT[xQTY][:MATERIAL[:THICKNESS]], repeatable")
    q.add_argument("-o", "--output", default="nest_out")
    q.add_argument("--name", default="nest")
    q.add_argument("--qty", type=int, default=None, help="default quantity (else from filename _Q12, else 1)")
    q.add_argument("--spacing", type=float, default=5.0)
    q.add_argument("--margin", type=float, default=10.0)
    q.add_argument("--rotations", default="0,90,180,270")
    q.add_argument("--mirror", action="store_true", help="allow mirrored parts")
    q.add_argument("--no-part-in-part", action="store_true")
    q.add_argument("--gravity", choices=["left", "bottom"], default="left")
    q.add_argument("-t", "--time", type=float, default=10.0, help="optimisation time in seconds")
    q.add_argument("-w", "--workers", type=int)
    q.add_argument("--curve-tol", type=float, default=0.1)
    q.add_argument("--join-tol", type=float, default=0.05)
    q.add_argument("--units", choices=["mm", "cm", "m", "in"])
    q.add_argument("--material")
    q.add_argument("--thickness", type=float)
    q.add_argument("--gcode", choices=sorted(POSTS), help="write G-code with this post-processor")
    q.add_argument("--feed", type=float, default=2000.0)
    q.add_argument("--kerf", type=float, default=1.5)
    q.add_argument("--kerf-comp", action="store_true", help="emit controller kerf compensation (G41)")
    q.add_argument("--pierce-delay", type=float, default=0.5)
    q.add_argument("--lead-in", type=float, default=5.0)
    q.add_argument("-q", "--quiet", action="store_true")
    q.set_defaults(func=cmd_quick)

    i = sub.add_parser("inspect", help="check how part files are read")
    i.add_argument("files", nargs="+")
    i.add_argument("--join-tol", type=float, default=0.05)
    i.set_defaults(func=cmd_inspect)

    p = sub.add_parser("posts", help="list G-code post-processors")
    p.set_defaults(func=cmd_posts)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
