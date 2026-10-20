"""File importers. ``load_parts(path)`` picks one by file extension.

Register additional formats with ``register_importer(".ext", func)`` where
``func(path, name=None, **options) -> list[PartGeometry]``.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from ..geometry import PartGeometry

_IMPORTERS: Dict[str, Callable[..., List[PartGeometry]]] = {}


def register_importer(ext: str, func: Callable[..., List[PartGeometry]]) -> None:
    _IMPORTERS[ext.lower().lstrip(".")] = func


def supported_formats() -> List[str]:
    return sorted(_IMPORTERS)


def load_parts(path: str, name: Optional[str] = None, **options) -> List[PartGeometry]:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in _IMPORTERS:
        raise ValueError(f"unsupported file type '.{ext}' ({path}); supported: {', '.join(supported_formats())}")
    func = _IMPORTERS[ext]
    import inspect
    params = inspect.signature(func).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        options = {k: v for k, v in options.items() if k in params}
    parts = func(path, name, **options)
    return [p.normalized() for p in parts]


def _dxf(path, name=None, **kw):
    import inspect
    from .dxf import read_dxf
    allowed = inspect.signature(read_dxf).parameters
    return read_dxf(path, name, **{k: v for k, v in kw.items() if k in allowed})


def _svg(path, name=None, tol=0.05, join_tol=0.05, px_to_mm=None, **_):
    from .svg import read_svg
    return read_svg(path, name, tol=tol, join_tol=join_tol, px_to_mm=px_to_mm)


def _json(path, name=None, **kw):
    from .jsonpoly import read_json
    return read_json(path, name)


def _step(path, name=None, tol=0.05, **_):
    from .step import read_step
    return read_step(path, name, tol=tol)


for _e in ("dxf", "dwg"):
    register_importer(_e, _dxf)
register_importer("svg", _svg)
register_importer("json", _json)
for _e in ("step", "stp", "igs", "iges"):
    register_importer(_e, _step)
