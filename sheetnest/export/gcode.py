"""Template-based G-code post-processor for plasma and laser tables.

Arcs are written as true G2/G3 moves (I/J incremental from the arc start).
Each preset is a dict of template strings; ``{name}`` placeholders are filled
from the post settings (feed, pierce_delay, kerf, power, ...). Add your own
preset by passing a dict to ``write_gcode(..., post=my_dict)`` or in the job
file under ``output.gcode.post``.

Kerf: ``kerf_mode="controller"`` emits the preset's cutter-compensation
codes (G41/G40 by default) so the controller offsets by half the kerf;
``"none"`` cuts on the drawn line (parts will be about one kerf width smaller
on outlines and larger on holes).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..geometry import EPS, bulge_to_arc
from .toolpath import CutPath, path_stats

POSTS: Dict[str, dict] = {
    "generic_plasma": {
        "ext": "nc",
        "header": "(sheetnest - {sheet_name})\nG21 (mm)\nG90 (absolute)\nG17\nG40\nG49\nF{feed}\n",
        "footer": "M5\nG40\nG0 X0 Y0\nM30\n",
        "cut_on": "M3\nG4 P{pierce_delay}\n",
        "cut_off": "M5\n",
        "mark_on": "M3\n", "mark_off": "M5\n",
        "comp_on": "G41", "comp_off": "G40",
        "comment": "({text})",
    },
    "linuxcnc_qtplasmac": {
        "ext": "ngc",
        "header": "(sheetnest - {sheet_name})\nG21 G40 G49 G64 P0.1\nG90 G94\nF#<_hal[plasmac.cut-feed-rate]>\n",
        "footer": "M5 $0\nG40\nG0 X0 Y0\nM2\n",
        "cut_on": "M3 $0 S1\n",
        "cut_off": "M5 $0\n",
        "mark_on": "M3 $2 S1\n", "mark_off": "M5 $2\n",
        "comp_on": "G41.1 D{kerf}", "comp_off": "G40",
        "comment": "({text})",
        "feed_word": False,
    },
    "mach3_plasma": {
        "ext": "tap",
        "header": "(sheetnest - {sheet_name})\nG21\nG90\nG40\nF{feed}\n",
        "footer": "M5\nG40\nG0 X0 Y0\nM30\n",
        "cut_on": "M3\nG4 P{pierce_delay}\n",
        "cut_off": "M5\n",
        "mark_on": "M3\n", "mark_off": "M5\n",
        "comp_on": "G41", "comp_off": "G40",
        "comment": "({text})",
    },
    "grbl_laser": {
        "ext": "gcode",
        "header": "; sheetnest - {sheet_name}\nG21\nG90\nM5\n",
        "footer": "M5\nG0 X0 Y0\n",
        "cut_on": "M4 S{power}\nG4 P{pierce_delay}\n",
        "cut_off": "M5\n",
        "mark_on": "M4 S{mark_power}\n", "mark_off": "M5\n",
        "comp_on": "", "comp_off": "",
        "comment": "; {text}",
    },
}


def _fmt(v: float, digits: int) -> str:
    s = f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def write_gcode(paths: List[CutPath], path: Optional[str] = None, *, post="generic_plasma", feed: float = 2000.0,
                mark_feed: Optional[float] = None, pierce_delay: float = 0.5, kerf: float = 1.5,
                kerf_mode: str = "none", power: float = 1000, mark_power: float = 200, digits: int = 3,
                sheet_name: str = "sheet", comments: bool = True, rapid_speed: float = 15000.0) -> dict:
    """Write G-code for one sheet. Returns stats incl. an estimated run time (s)."""
    tpl = dict(POSTS[post]) if isinstance(post, str) else dict(post)
    vars_ = dict(feed=_fmt(feed, 1), pierce_delay=_fmt(pierce_delay, 2), kerf=_fmt(kerf, 3),
                 power=_fmt(power, 0), mark_power=_fmt(mark_power, 0), sheet_name=sheet_name)
    f = lambda x: _fmt(x, digits)
    out: List[str] = [tpl["header"].format(**vars_)]
    feed_word = tpl.get("feed_word", True)
    use_comp = kerf_mode == "controller" and tpl.get("comp_on")

    def comment(text):
        if comments and tpl.get("comment"):
            out.append(tpl["comment"].format(text=text.replace("(", "[").replace(")", "]")) + "\n")

    for cp in paths:
        c = cp.contour
        is_mark = cp.kind == "mark"
        comment(f"{cp.part_label} {cp.kind}")
        out.append(f"G0 X{f(cp.pierce[0])} Y{f(cp.pierce[1])}\n")
        out.append(tpl["mark_on" if is_mark else "cut_on"].format(**vars_))
        fw = f" F{_fmt(mark_feed or feed, 1)}" if feed_word else ""
        start = (c.vertices[0][0], c.vertices[0][1])
        if use_comp and not is_mark:
            out.append(tpl["comp_on"].format(**vars_) + "\n")
        if cp.lead is not None:
            if cp.lead[0] == "line":
                out.append(f"G1 X{f(start[0])} Y{f(start[1])}{fw}\n")
            else:
                _, cx, cy, ccw = cp.lead
                out.append(f"{'G3' if ccw else 'G2'} X{f(start[0])} Y{f(start[1])} "
                           f"I{f(cx - cp.pierce[0])} J{f(cy - cp.pierce[1])}{fw}\n")
            fw = ""
        first = True
        for p0, p1, b in c.segments():
            extra = fw if first else ""
            first = False
            if abs(b) < EPS:
                out.append(f"G1 X{f(p1[0])} Y{f(p1[1])}{extra}\n")
            else:
                arc = bulge_to_arc(p0, p1, b)
                (cx, cy) = arc[0]
                out.append(f"{'G3' if b > 0 else 'G2'} X{f(p1[0])} Y{f(p1[1])} I{f(cx - p0[0])} J{f(cy - p0[1])}{extra}\n")
        if cp.overcut_to:
            out.append(f"G1 X{f(cp.overcut_to[0])} Y{f(cp.overcut_to[1])}\n")
        if use_comp and not is_mark:
            out.append(tpl["comp_off"].format(**vars_) + "\n")
        out.append(tpl["mark_off" if is_mark else "cut_off"].format(**vars_))
    out.append(tpl["footer"].format(**vars_))
    text = "".join(out)
    if path:
        with open(path, "w", newline="\n") as fh:
            fh.write(text)
    st = path_stats(paths)
    st["est_time_s"] = (st["cut_length"] / feed * 60.0 + st["mark_length"] / (mark_feed or feed) * 60.0
                        + st["pierces"] * pierce_delay + st["rapid_length"] / rapid_speed * 60.0)
    st["lines"] = text.count("\n")
    st["extension"] = tpl.get("ext", "nc")
    return st
