"""Generate sample sheet-metal parts (DXF, SVG, STEP) for trying the engine.

The DXFs deliberately use loose LINE/ARC/CIRCLE entities in shuffled order
and mixed directions, the way many CAD flat-pattern exports look.
"""
import math
import os
import random
import sys

import ezdxf

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "parts")
os.makedirs(OUT, exist_ok=True)
rng = random.Random(7)


class Sketch:
    def __init__(self):
        self.items = []  # ("line", p0, p1) | ("arc", c, r, a0, a1) | ("circle", c, r)

    def line(self, p0, p1):
        self.items.append(("line", p0, p1))

    def arc(self, c, r, a0, a1):
        self.items.append(("arc", c, r, a0, a1))

    def circle(self, c, r):
        self.items.append(("circle", c, r))

    def rounded_rect(self, x0, y0, x1, y1, r):
        self.line((x0 + r, y0), (x1 - r, y0))
        self.arc((x1 - r, y0 + r), r, 270, 360)
        self.line((x1, y0 + r), (x1, y1 - r))
        self.arc((x1 - r, y1 - r), r, 0, 90)
        self.line((x1 - r, y1), (x0 + r, y1))
        self.arc((x0 + r, y1 - r), r, 90, 180)
        self.line((x0, y1 - r), (x0, y0 + r))
        self.arc((x0 + r, y0 + r), r, 180, 270)

    def slot(self, cx, cy, length, width, horizontal=True):
        r = width / 2
        h = length / 2 - r
        if horizontal:
            self.line((cx - h, cy - r), (cx + h, cy - r))
            self.arc((cx + h, cy), r, 270, 90)
            self.line((cx + h, cy + r), (cx - h, cy + r))
            self.arc((cx - h, cy), r, 90, 270)
        else:
            self.line((cx + r, cy - h), (cx + r, cy + h))
            self.arc((cx, cy + h), r, 0, 180)
            self.line((cx - r, cy + h), (cx - r, cy - h))
            self.arc((cx, cy - h), r, 180, 360)

    def polygon(self, pts):
        for i in range(len(pts)):
            self.line(pts[i], pts[(i + 1) % len(pts)])

    def save(self, name, layer="0", extra=None):
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        items = list(self.items)
        rng.shuffle(items)
        for it in items:
            if it[0] == "line":
                p0, p1 = (it[1], it[2]) if rng.random() < 0.5 else (it[2], it[1])
                msp.add_line(p0, p1, dxfattribs={"layer": layer})
            elif it[0] == "arc":
                msp.add_arc(it[1], it[2], it[3], it[4], dxfattribs={"layer": layer})
            else:
                msp.add_circle(it[1], it[2], dxfattribs={"layer": layer})
        if extra:
            extra(doc, msp)
        doc.saveas(os.path.join(OUT, name))


def flange():
    s = Sketch()
    s.circle((0, 0), 160)
    s.circle((0, 0), 85)
    for i in range(8):
        a = math.radians(22.5 + 45 * i)
        s.circle((122 * math.cos(a), 122 * math.sin(a)), 11)
    s.save("flange_Q4.dxf")


def base_plate():
    s = Sketch()
    s.rounded_rect(0, 0, 420, 260, 20)
    for x, y in ((35, 35), (385, 35), (385, 225), (35, 225)):
        s.circle((x, y), 11)
    s.rounded_rect(150, 80, 270, 180, 8)

    def bend(doc, msp):  # bend line + etch that should be handled by layer rules
        msp.add_line((0, 130), (420, 130), dxfattribs={"layer": "BEND_LINES"})
        msp.add_text("BP-01", dxfattribs={"layer": "TEXT", "height": 10}).set_placement((300, 230))
        msp.add_line((60, 120), (100, 120), dxfattribs={"layer": "ETCH"})
    s.save("base_plate_Q3.dxf", extra=bend)


def l_bracket():
    s = Sketch()
    # L shape with inside fillet R15 and outside corner R5
    s.line((5, 0), (300, 0))
    s.line((300, 0), (300, 70))
    s.line((300, 70), (85, 70))
    s.arc((85, 85), 15, 180, 270)
    s.line((70, 85), (70, 220))
    s.line((70, 220), (0, 220))
    s.line((0, 220), (0, 5))
    s.arc((5, 5), 5, 180, 270)
    s.slot(190, 35, 60, 14)
    s.slot(35, 150, 60, 14, horizontal=False)
    s.save("l_bracket_Q10.dxf")


def gusset():
    s = Sketch()
    s.polygon([(0, 0), (200, 0), (200, 25), (25, 200), (0, 200)])
    s.circle((60, 60), 20)
    s.save("gusset_Q16.dxf")


def disc():
    s = Sketch()
    s.circle((0, 0), 55)
    s.circle((0, 0), 13)
    s.save("disc_Q12.dxf")


def strap():
    s = Sketch()
    s.rounded_rect(0, 0, 640, 70, 35)
    for x in (100, 320, 540):
        s.slot(x, 35, 50, 16)
    s.save("strap_Q6.dxf")


def tab_hook():
    # irregular part with concave arc - tests non-convex NFP
    s = Sketch()
    s.line((0, 0), (180, 0))
    s.line((180, 0), (180, 40))
    s.arc((130, 40), 50, 0, 90)     # convex quarter
    s.line((130, 90), (110, 90))
    s.arc((80, 90), 30, 0, 180)     # concave? (bulge outward) - creates a hump
    s.line((50, 90), (0, 90))
    s.line((0, 90), (0, 0))
    s.circle((40, 40), 10)
    s.circle((140, 30), 8)
    s.save("tab_hook_Q8.dxf")


def svg_part():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="150mm" height="100mm" viewBox="0 0 150 100">
  <path d="M10 10 H140 V60 A30 30 0 0 1 110 90 H10 Z" fill="none" stroke="black"/>
  <circle cx="40" cy="45" r="15" fill="none" stroke="black"/>
  <rect x="80" y="30" width="30" height="20" rx="4" fill="none" stroke="black"/>
</svg>"""
    with open(os.path.join(OUT, "cover_Q5.svg"), "w") as f:
        f.write(svg)


def step_part():
    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    except ImportError:
        print("OCP not installed - skipping STEP sample")
        return
    shape = BRepPrimAPI_MakeBox(250.0, 140.0, 8.0).Shape()
    for x, y, r in ((30, 30, 9), (220, 30, 9), (220, 110, 9), (30, 110, 9), (125, 70, 30)):
        cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), r, 10.0).Shape()
        shape = BRepAlgoAPI_Cut(shape, cyl).Shape()
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(os.path.join(OUT, "mount_plate_Q4.step"))


def remnant():
    # an irregular offcut left over from an earlier job (goes in ../stock/)
    stock = os.path.join(os.path.dirname(OUT.rstrip("/")), "stock")
    os.makedirs(stock, exist_ok=True)
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1400, 0), (1400, 500), (900, 500), (900, 1250), (0, 1250)], close=True)
    msp.add_circle((300, 900), 60)   # a defect / old hole: not usable
    doc.saveas(os.path.join(stock, "remnant_R17.dxf"))


if __name__ == "__main__":
    for f in (flange, base_plate, l_bracket, gusset, disc, strap, tab_hook, svg_part, step_part, remnant):
        f()
    print("samples written to", OUT)
