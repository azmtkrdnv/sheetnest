import math
import os
import re
import subprocess
import sys

import ezdxf
import pytest

from sheetnest import Contour, NestSettings, Part, PartGeometry, Sheet, load_parts, nest, validate
from sheetnest.export import CutSettings, OutputOptions, export_all, plan_sheet
from sheetnest.geometry import chain_segments
from sheetnest.job import job_from_dict, qty_from_name

HERE = os.path.dirname(__file__)
EXAMPLES = os.path.join(HERE, "..", "examples")
PARTS = os.path.join(EXAMPLES, "parts")


@pytest.fixture(scope="session", autouse=True)
def samples():
    if not os.path.exists(os.path.join(PARTS, "flange_Q4.dxf")):
        subprocess.check_call([sys.executable, os.path.join(EXAMPLES, "make_samples.py"), PARTS])


def fast(**kw):
    kw.setdefault("time_limit", 0)
    kw.setdefault("workers", 1)
    return NestSettings(**kw)


# -- geometry -----------------------------------------------------------------

def test_circle_area_and_length():
    c = Contour.circle(10, 20, 50)
    assert abs(c.signed_area() - math.pi * 2500) < 1e-6
    assert abs(c.length() - 2 * math.pi * 50) < 1e-6
    assert c.is_circle()
    assert abs(c.reversed().signed_area() + math.pi * 2500) < 1e-6


def test_mirror_flips_orientation_and_keeps_area():
    c = Contour([(0, 0, 0), (100, 0, 0.4), (100, 50, 0), (0, 50, 0)])
    m = c.transformed(30, True, 5, 5)
    assert abs(abs(m.signed_area()) - abs(c.signed_area())) < 1e-6
    assert (m.signed_area() > 0) != (c.signed_area() > 0)


def test_conservative_polygon_covers_true_shape():
    g = PartGeometry(Contour.circle(0, 0, 40), [Contour.circle(0, 0, 10)])
    outer = g.polygon(0.2, conservative=True)
    true_area = math.pi * (40 ** 2 - 10 ** 2)
    assert outer.area >= true_area
    assert g.polygon(0.2).area <= true_area


def test_chaining_shuffled_reversed_segments():
    segs = [((0, 0), (10, 0), 0), ((10, 10), (10, 0), 0), ((10, 10), (0, 10), 0), ((0, 0), (0, 10), 0)]
    closed, opened = chain_segments(segs, 0.01)
    assert len(closed) == 1 and not opened
    assert abs(abs(closed[0].signed_area()) - 100) < 1e-9


# -- import -------------------------------------------------------------------

def test_dxf_import_flange():
    (p,) = load_parts(os.path.join(PARTS, "flange_Q4.dxf"))
    assert len(p.holes) == 9
    assert abs(p.area - (math.pi * (160 ** 2 - 85 ** 2) - 8 * math.pi * 11 ** 2)) < 0.01
    assert all(h.is_circle() for h in p.holes)


def test_dxf_layers_marks_and_text():
    (p,) = load_parts(os.path.join(PARTS, "base_plate_Q3.dxf"))
    assert len(p.holes) == 5          # bend line ignored, cutout kept
    assert len(p.marks) == 1          # etch layer
    assert any("TEXT" in w for w in p.warnings)


def test_svg_import_keeps_arcs():
    (p,) = load_parts(os.path.join(PARTS, "cover_Q5.svg"))
    assert any(abs(v[2]) > 0.1 for v in p.outer.vertices)
    assert sum(1 for h in p.holes if h.is_circle()) == 1


def test_step_import_detects_thickness():
    pytest.importorskip("OCP")
    path = os.path.join(PARTS, "mount_plate_Q4.step")
    if not os.path.exists(path):
        pytest.skip("STEP sample not generated")
    (p,) = load_parts(path)
    assert p.meta["thickness"] == pytest.approx(8.0)
    assert len(p.holes) == 5
    assert p.size == pytest.approx((250, 140), abs=1e-3)


def test_quantity_from_filename():
    assert qty_from_name("flange_Q4") == ("flange", 4)
    assert qty_from_name("plate x12") == ("plate", 12)
    assert qty_from_name("bolt_M10x20") == ("bolt_M10x20", None)


# -- nesting ------------------------------------------------------------------

def test_exact_fit_with_zero_spacing():
    parts = [Part(PartGeometry(Contour.rectangle(0, 0, 100, 50)), 4)]
    r = nest(parts, [Sheet(200, 100)], fast(spacing=0, margin=0, time_limit=2))
    assert r.sheet_count == 1 and not r.unplaced
    assert validate(r) == []


def test_spacing_and_margin_respected():
    parts = [Part(g, 6) for g in load_parts(os.path.join(PARTS, "tab_hook_Q8.dxf"))]
    r = nest(parts, [Sheet(600, 400)], fast(spacing=8, margin=12))
    assert not r.unplaced
    assert validate(r) == []


def test_part_in_part():
    ring = PartGeometry(Contour.circle(0, 0, 100), [Contour.circle(0, 0, 60)], name="ring")
    disc = PartGeometry(Contour.circle(0, 0, 50), name="disc")
    r = nest([Part(ring, 1), Part(disc, 1)], [Sheet(222, 222)], fast(spacing=5, margin=5))
    assert r.sheet_count == 1 and not r.unplaced
    assert validate(r) == []
    r2 = nest([Part(ring, 1), Part(disc, 1)], [Sheet(222, 222)], fast(spacing=5, margin=5, part_in_part=False))
    assert r2.sheet_count == 2


def test_irregular_remnant_with_defect():
    sheet = PartGeometry(Contour([(0, 0, 0), (800, 0, 0), (800, 300, 0), (400, 300, 0), (400, 600, 0), (0, 600, 0)]),
                         [Contour.circle(200, 450, 50)])
    parts = [Part(PartGeometry(Contour.rectangle(0, 0, 90, 90)), 30)]
    r = nest(parts, [Sheet(geometry=sheet, quantity=1)], fast(spacing=4, margin=5))
    assert validate(r) == []
    assert r.layouts[0].count >= 18


def test_material_groups_and_stock_limits():
    sq = PartGeometry(Contour.rectangle(0, 0, 100, 100))
    parts = [Part(sq, 4, material="S235", thickness=5), Part(sq, 4, material="AL", thickness=3)]
    sheets = [Sheet(250, 250, quantity=1, material="S235", thickness=5), Sheet(250, 250, quantity=5, material="AL", thickness=3)]
    r = nest(parts, sheets, fast(spacing=5, margin=5))
    assert r.sheet_count == 2 and not r.unplaced
    for l in r.layouts:
        mats = {r.parts[p.part].material for p in l.placements}
        assert mats == {r.sheets[l.sheet].material}


def test_oversized_part_reported():
    parts = [Part(PartGeometry(Contour.rectangle(0, 0, 500, 500), name="big"), 1)]
    r = nest(parts, [Sheet(300, 300)], fast())
    assert r.unplaced == {0: 1}
    assert any("larger" in m for m in r.messages)


def test_optimiser_runs_parallel_and_is_valid():
    parts = []
    for f in ("gusset_Q16.dxf", "disc_Q12.dxf", "l_bracket_Q10.dxf"):
        q = int(re.search(r"_Q(\d+)", f).group(1))
        parts += [Part(g, q) for g in load_parts(os.path.join(PARTS, f))]
    r = nest(parts, [Sheet(1500, 1000)], NestSettings(spacing=5, margin=10, time_limit=3, workers=2))
    assert not r.unplaced
    assert validate(r) == []


# -- outputs ------------------------------------------------------------------

def _gcode_arcs_ok(text):
    x = y = 0.0
    for line in text.splitlines():
        m = re.match(r"(G\d+)\b", line)
        if not m:
            continue
        w = dict(re.findall(r"([XYIJ])([-\d.]+)", line))
        nx, ny = float(w.get("X", x)), float(w.get("Y", y))
        if m.group(1) in ("G2", "G3"):
            cx, cy = x + float(w["I"]), y + float(w["J"])
            assert abs(math.hypot(x - cx, y - cy) - math.hypot(nx - cx, ny - cy)) < 0.002, line
        x, y = nx, ny


def test_exports(tmp_path):
    parts = [Part(g, 2) for f in ("flange_Q4.dxf", "disc_Q12.dxf", "base_plate_Q3.dxf")
             for g in load_parts(os.path.join(PARTS, f))]
    r = nest(parts, [Sheet(1500, 1000)], fast(spacing=6, margin=10))
    opts = OutputOptions(gcode={"post": "generic_plasma", "feed": 2500})
    data = export_all(r, str(tmp_path), opts, "t")
    for name in ("report.html", "result.json", "parts.csv", "sheets.csv", "sheet_01.dxf", "sheet_01.svg", "sheet_01.nc"):
        assert (tmp_path / name).exists(), name
    doc = ezdxf.readfile(str(tmp_path / "sheet_01.dxf"))
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert {"SHEET", "CUT_OUTER", "CUT_INNER"} <= layers
    text = (tmp_path / "sheet_01.nc").read_text()
    _gcode_arcs_ok(text)
    pierces = text.count("\nM3\n")
    assert pierces == sum(1 + len(r.parts[p.part].geometry.holes) for p in r.layouts[0].placements)
    assert data["summary"]["parts_placed"] == 6


def test_holes_before_outline_and_nested_parts_first():
    ring = PartGeometry(Contour.circle(0, 0, 100), [Contour.circle(0, 0, 60)], name="ring")
    disc = PartGeometry(Contour.circle(0, 0, 50), name="disc")
    r = nest([Part(ring, 1), Part(disc, 1)], [Sheet(222, 222)], fast(spacing=5, margin=5))
    paths = plan_sheet(r, r.layouts[0], CutSettings())
    kinds = [(p.part_label.split("#")[0], p.kind) for p in paths]
    assert kinds == [("disc", "outer"), ("ring", "hole"), ("ring", "outer")]
    # outlines clockwise, holes counter-clockwise
    assert not paths[0].contour.is_ccw() and paths[1].contour.is_ccw() and not paths[2].contour.is_ccw()


def test_job_from_dict(tmp_path):
    job = job_from_dict({
        "name": "j",
        "settings": {"spacing": 4, "margin": 8, "time_limit": 0, "workers": 1},
        "sheets": [{"width": 1000, "height": 500, "quantity": 3}],
        "parts": [{"folder": PARTS, "pattern": "disc*.dxf"},
                  {"outer": [[0, 0], [50, 0], [50, 30, 1], [0, 30]], "quantity": 3, "name": "tag"}],
        "output": {"gcode": {"post": "grbl_laser"}, "cut": {"lead_in": 3}},
    })
    assert sum(p.quantity for p in job.parts) == 12 + 3
    r = nest(job.parts, job.sheets, job.settings)
    export_all(r, str(tmp_path), job.output)
    assert (tmp_path / "sheet_01.gcode").exists()
    assert validate(r) == []


def test_cli_quick(tmp_path):
    out = tmp_path / "o"
    rc = subprocess.call([sys.executable, "-m", "sheetnest", "quick", os.path.join(PARTS, "gusset_Q16.dxf"),
                          os.path.join(PARTS, "disc_Q12.dxf") + ":3", "--sheet", "1200x800", "-t", "1",
                          "-w", "1", "-o", str(out), "-q"], cwd=os.path.join(HERE, ".."))
    assert rc == 0
    assert (out / "report.html").exists()
