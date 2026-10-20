"""Load a nesting job from YAML/JSON and turn file lists into Part/Sheet objects.

See ``examples/job.yaml`` for the full format.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
from dataclasses import dataclass, field, fields
from typing import List, Optional, Tuple

from .export.report import OutputOptions
from .export.toolpath import CutSettings
from .importers import load_parts, supported_formats
from .model import NestSettings, Part, Sheet

QTY_RE = re.compile(r"^(?P<name>.*?)[ _\-.]+(?:q|qty|x)[ _\-]?(?P<q>\d+)$", re.IGNORECASE)


def qty_from_name(stem: str) -> Tuple[str, Optional[int]]:
    """'bracket_Q12' -> ('bracket', 12); 'plate x4' -> ('plate', 4); 'base' -> ('base', None)."""
    m = QTY_RE.match(stem)
    if m and m.group("name"):
        return m.group("name"), int(m.group("q"))
    return stem, None


@dataclass
class Job:
    name: str = "nest"
    parts: List[Part] = field(default_factory=list)
    sheets: List[Sheet] = field(default_factory=list)
    settings: NestSettings = field(default_factory=NestSettings)
    output: OutputOptions = field(default_factory=OutputOptions)
    warnings: List[str] = field(default_factory=list)


def _dc(cls, d: dict):
    names = {f.name for f in fields(cls)}
    unknown = set(d) - names
    if unknown:
        raise ValueError(f"unknown {cls.__name__} option(s): {', '.join(sorted(unknown))}")
    return cls(**d)


def parts_from_file(path: str, quantity: Optional[int] = None, import_opts: Optional[dict] = None,
                    **part_kw) -> List[Part]:
    stem = os.path.splitext(os.path.basename(path))[0]
    name, q = qty_from_name(stem)
    qty = quantity if quantity is not None else (q if q is not None else 1)
    geoms = load_parts(path, name=part_kw.pop("name", None) or name, **(import_opts or {}))
    return [Part(g, quantity=qty, **part_kw) for g in geoms]


def _part_kwargs(spec: dict) -> dict:
    kw = {}
    for k in ("material", "name", "priority"):  # "optional" is handled by the caller
        if spec.get(k) is not None:
            kw[k] = spec[k]
    if spec.get("thickness") is not None:
        kw["thickness"] = float(spec["thickness"])
    if spec.get("rotations") is not None:
        r = spec["rotations"]
        kw["rotations"] = [float(x) for x in (str(r).split(",") if isinstance(r, (str, int, float)) else r)]
    if spec.get("mirror") is not None:
        kw["allow_mirror"] = bool(spec["mirror"])
    return kw


def load_job(path: str) -> Job:
    with open(path) as f:
        text = f.read()
    if path.lower().endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return job_from_dict(data, os.path.dirname(os.path.abspath(path)))


def job_from_dict(data: dict, base_dir: str = ".") -> Job:
    job = Job(name=data.get("name", "nest"))
    rel = lambda p: p if os.path.isabs(p) else os.path.join(base_dir, p)
    imp = dict(data.get("import", {}))
    if data.get("units"):
        imp.setdefault("units", data["units"])
    job.settings = _dc(NestSettings, data.get("settings", {}) or {})
    if "rotations" in (data.get("settings") or {}):
        job.settings.rotations = [float(r) for r in job.settings.rotations]

    for spec in data.get("sheets", []):
        spec = dict(spec)
        kw = {k: spec[k] for k in ("quantity", "name", "material", "cost") if k in spec}
        if spec.get("thickness") is not None:
            kw["thickness"] = float(spec["thickness"])
        if "file" in spec:
            geoms = load_parts(rel(spec["file"]), **imp)
            geoms.sort(key=lambda g: -g.area)
            kw.setdefault("name", os.path.splitext(os.path.basename(spec["file"]))[0])
            job.sheets.append(Sheet(geometry=geoms[0], **kw))
        else:
            job.sheets.append(Sheet(width=float(spec["width"]), height=float(spec["height"]), **kw))

    exts = tuple("." + e for e in supported_formats())
    for spec in data.get("parts", []):
        spec = dict(spec)
        kw = _part_kwargs(spec)
        qty = spec.get("quantity")
        if "file" in spec:
            try:
                job.parts.extend(parts_from_file(rel(spec["file"]), qty, imp, **kw))
            except (RuntimeError, FileNotFoundError, OSError) as exc:
                # `optional: true` lets a job skip a file whose reader isn't installed (e.g. STEP)
                if not spec.get("optional"):
                    raise
                job.warnings.append(f"skipped {spec['file']}: {exc}")
        elif "folder" in spec:
            pattern = spec.get("pattern", "*")
            files = sorted(f for f in glob.glob(os.path.join(rel(spec["folder"]), pattern))
                           if f.lower().endswith(exts))
            if not files:
                job.warnings.append(f"no part files found in {spec['folder']} matching {pattern}")
            for f in files:
                job.parts.extend(parts_from_file(f, qty, imp, **dict(kw)))
        elif "bom" in spec:
            job.parts.extend(parts_from_bom(rel(spec["bom"]), imp, kw))
        elif "outer" in spec:
            from .importers.jsonpoly import part_from_dict
            g = part_from_dict(spec).normalized()
            job.parts.append(Part(g, quantity=int(qty or 1), **{k: v for k, v in kw.items() if k != "name"}))
        else:
            raise ValueError(f"part entry needs 'file', 'folder', 'bom' or 'outer': {spec}")

    out = dict(data.get("output", {}) or {})
    cut = out.pop("cut", None)
    job.output = _dc(OutputOptions, out)
    if cut:
        job.output.cut = _dc(CutSettings, cut)
    for p in job.parts:
        for w in p.geometry.warnings:
            job.warnings.append(f"{p.name}: {w}")
    return job


def parts_from_bom(path: str, import_opts: dict, defaults: dict) -> List[Part]:
    """CSV with columns: file, quantity[, name, material, thickness, rotations, mirror, priority]."""
    base = os.path.dirname(os.path.abspath(path))
    parts = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        for row in csv.DictReader(f, dialect=dialect):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("file"):
                continue
            spec = dict(defaults)
            spec.update({k: v for k, v in row.items() if v and k not in ("file", "quantity", "qty")})
            kw = _part_kwargs(spec)
            q = row.get("quantity") or row.get("qty")
            fpath = row["file"] if os.path.isabs(row["file"]) else os.path.join(base, row["file"])
            parts.extend(parts_from_file(fpath, int(float(q)) if q else None, import_opts, **kw))
    return parts
