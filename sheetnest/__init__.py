"""sheetnest - true-shape nesting engine for sheet metal (plasma / laser / waterjet).

Quick start::

    from sheetnest import load_parts, Part, Sheet, NestSettings, nest

    parts = [Part(g, quantity=10) for g in load_parts("bracket.dxf")]
    result = nest(parts, [Sheet(3000, 1500, quantity=5)], NestSettings(spacing=6, margin=10))
    print(result.summary())
"""
from .geometry import Contour, PartGeometry
from .importers import load_parts, register_importer, supported_formats
from .model import NestResult, NestSettings, Part, Placement, Sheet, SheetLayout
from .optimize import nest
from .validate import validate

__version__ = "0.1.0"

__all__ = ["Contour", "PartGeometry", "load_parts", "register_importer", "supported_formats",
           "NestResult", "NestSettings", "Part", "Placement", "Sheet", "SheetLayout", "nest", "validate"]
