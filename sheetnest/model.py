"""Job description and result data structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .geometry import Contour, PartGeometry


@dataclass
class Part:
    geometry: PartGeometry
    quantity: int = 1
    name: Optional[str] = None
    material: Optional[str] = None
    thickness: Optional[float] = None
    rotations: Optional[Sequence[float]] = None   # None -> NestSettings.rotations
    allow_mirror: Optional[bool] = None           # None -> NestSettings.allow_mirror
    priority: int = 0                             # higher = nested first

    def __post_init__(self):
        if self.name is None:
            self.name = self.geometry.name
        if self.thickness is None and "thickness" in self.geometry.meta:
            self.thickness = self.geometry.meta["thickness"]


@dataclass
class Sheet:
    """A stock sheet or a remnant.

    Give ``width``/``height`` for a rectangular sheet, or ``geometry`` for an
    irregular remnant (holes in the remnant geometry are treated as unusable).
    ``quantity=None`` means unlimited stock.
    """
    width: float = 0.0
    height: float = 0.0
    quantity: Optional[int] = None
    name: Optional[str] = None
    material: Optional[str] = None
    thickness: Optional[float] = None
    geometry: Optional[PartGeometry] = None
    cost: Optional[float] = None

    def __post_init__(self):
        if self.geometry is None:
            if self.width <= 0 or self.height <= 0:
                raise ValueError("sheet needs width and height, or geometry")
            self.geometry = PartGeometry(Contour.rectangle(0, 0, self.width, self.height), [], [], "sheet")
        else:
            self.geometry = self.geometry.normalized()
            self.width, self.height = self.geometry.size
        if self.name is None:
            self.name = f"{self.width:g}x{self.height:g}"

    @property
    def is_rectangle(self) -> bool:
        o = self.geometry.outer
        return (not self.geometry.holes and len(o.vertices) == 4 and all(abs(v[2]) < 1e-12 for v in o.vertices)
                and abs(abs(o.signed_area()) - self.width * self.height) < 1e-6)

    @property
    def area(self) -> float:
        return self.geometry.area

    def accepts(self, part: Part) -> bool:
        if self.material and part.material and self.material.lower() != part.material.lower():
            return False
        if self.thickness and part.thickness and abs(self.thickness - part.thickness) > 1e-6:
            return False
        return True


@dataclass
class NestSettings:
    spacing: float = 5.0            # min distance between cut edges of neighbouring parts (incl. kerf)
    margin: float = 10.0            # min distance from parts to the sheet edge
    rotations: Sequence[float] = (0, 90, 180, 270)
    allow_mirror: bool = False
    part_in_part: bool = True       # place small parts inside holes of large ones
    curve_tolerance: float = 0.1    # chord error when approximating arcs for nesting (mm)
    time_limit: float = 10.0        # seconds of order optimisation (0 = single greedy pass)
    gravity: str = "left"           # "left": fill along X, leaving a remnant on the right; "bottom": along Y
    sheet_selection: str = "in_order"   # "in_order" or "best_fit"
    workers: int = 0                # optimisation processes (0 = auto)
    seed: int = 1


@dataclass
class Placement:
    part: int            # index into the job's part list
    instance: int        # 1-based copy number of that part
    x: float
    y: float
    rotation: float
    mirror: bool = False

    def transform(self):
        return self.rotation, self.mirror, self.x, self.y


@dataclass
class SheetLayout:
    sheet: int                       # index into the job's sheet list
    placements: List[Placement] = field(default_factory=list)
    used_length: float = 0.0         # extent along the gravity axis (incl. margin)
    utilization: float = 0.0         # part area / sheet area
    part_area: float = 0.0
    group: str = ""

    @property
    def count(self):
        return len(self.placements)


@dataclass
class NestResult:
    parts: List[Part]
    sheets: List[Sheet]
    settings: NestSettings
    layouts: List[SheetLayout] = field(default_factory=list)
    unplaced: Dict[int, int] = field(default_factory=dict)   # part index -> missing copies
    messages: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    evaluations: int = 0

    @property
    def sheet_count(self) -> int:
        return len(self.layouts)

    @property
    def utilization(self) -> float:
        a = sum(self.sheets[l.sheet].area for l in self.layouts)
        return sum(l.part_area for l in self.layouts) / a if a else 0.0

    def placed_geometry(self, layout: SheetLayout):
        """Yield (placement, transformed PartGeometry) for one sheet."""
        for pl in layout.placements:
            yield pl, self.parts[pl.part].geometry.transformed(*pl.transform())

    def summary(self) -> str:
        lines = [f"{self.sheet_count} sheet(s), overall utilisation {self.utilization * 100:.1f}%, "
                 f"{self.evaluations} layouts evaluated in {self.elapsed:.1f}s"]
        for i, l in enumerate(self.layouts, 1):
            s = self.sheets[l.sheet]
            lines.append(f"  sheet {i}: {s.name} ({s.width:g} x {s.height:g}) {l.count} parts, "
                         f"util {l.utilization * 100:.1f}%, used length {l.used_length:.0f} mm"
                         + (f"  [{l.group}]" if l.group else ""))
        for p, n in self.unplaced.items():
            lines.append(f"  NOT PLACED: {self.parts[p].name} x{n}")
        lines.extend("  " + m for m in self.messages)
        return "\n".join(lines)
