# Contributing

Thanks for helping improve sheetnest. Bug reports with a sample file are the most useful contribution of all.

## Reporting a problem

Open an issue and include:
- the `sheetnest --version` output and your OS / Python version;
- the command or job file you ran;
- if a part is read wrongly: the DXF/SVG/STEP file (or a stripped-down copy) and the output of
  `sheetnest inspect yourfile.dxf`;
- if a layout or G-code looks wrong: `result.json` and a screenshot of `report.html`.

**Never run untested G-code on a machine.** If you find a G-code problem, please say which
controller and post-processor you use.

## Development setup

```bash
git clone https://github.com/<you>/sheetnest.git
cd sheetnest
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # add ,step for STEP/IGES support
python examples/make_samples.py  # regenerate sample parts (incl. STEP if OCP is installed)
pytest -q
ruff check .
```

## Where things live

| Module | Responsibility |
|---|---|
| `geometry.py` | Bulge-polyline contours, arc maths, loop chaining, hole classification |
| `importers/` | One module per file format; register new ones with `register_importer` |
| `nfp.py` | No-fit / inner-fit polygons (Clipper Minkowski sums) |
| `nester.py` | Greedy placement on a sheet, NFP caching |
| `optimize.py` | Order / rotation search, material grouping, `nest()` entry point |
| `validate.py` | Independent overlap / spacing / margin check |
| `export/` | DXF, SVG, HTML/JSON/CSV report, cut sequencing, G-code posts |
| `job.py`, `cli.py` | Job files and command line |

## Pull requests

- Keep changes focused; one feature or fix per PR.
- Add or update a test in `tests/` for behaviour changes. Every nesting test should end with
  `assert validate(result) == []`.
- Run `pytest -q` and `ruff check .` before pushing; CI runs both on Linux, Windows and macOS.
- New G-code post-processors are welcome: add a template dict to `POSTS` in
  `export/gcode.py` and mention which controller you tested it on.
- Update `CHANGELOG.md` under "Unreleased".
