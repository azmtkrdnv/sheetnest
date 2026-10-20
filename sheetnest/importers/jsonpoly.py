"""JSON polygon import: the simplest way to feed parts from other software.

Format (one part or a list of parts)::

    {"name": "bracket",
     "outer": [[0,0], [100,0], [100,50, 0.4142], [0,50]],   # [x, y, (bulge)]
     "holes": [[[20,20],[30,20],[30,30],[20,30]]],
     "circles": [[60, 25, 5]],                               # optional holes: cx, cy, r
     "quantity": 4}
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from ..geometry import Contour, PartGeometry


def part_from_dict(d: dict, default_name: str = "part") -> PartGeometry:
    outer = Contour.from_points(d["outer"])
    holes = [Contour.from_points(h) for h in d.get("holes", [])]
    holes += [Contour.circle(c[0], c[1], c[2]) for c in d.get("circles", [])]
    return PartGeometry(outer, holes, [], d.get("name", default_name))


def read_json(path: str, name: Optional[str] = None, **_) -> List[PartGeometry]:
    with open(path) as f:
        data = json.load(f)
    base = name or os.path.splitext(os.path.basename(path))[0]
    items = data if isinstance(data, list) else data.get("parts", [data])
    parts = []
    for i, d in enumerate(items):
        p = part_from_dict(d, base if len(items) == 1 else f"{base}#{i + 1}")
        p.source = path
        parts.append(p)
    return parts
