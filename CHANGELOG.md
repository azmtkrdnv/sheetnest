# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-20

First public release.

### Added
- True-shape nesting with no-fit polygons, part-in-part placement, per-part rotation limits and mirroring.
- Multiple sheet sizes, stock quantities, irregular remnants with defects, material/thickness grouping.
- Order, rotation and scoring search with simulated annealing on multiple CPU cores.
- Importers: DXF (DWG via ODA File Converter), SVG, STEP/IGES flat patterns (optional), JSON polygons, CSV BOM.
- Exports: per-sheet DXF with true arcs and CAM layers, SVG previews, HTML report, JSON and CSV, offcut suggestion.
- G-code with true arcs, tangential lead-ins, holes-first and nested-parts-first cut order;
  posts for generic plasma, LinuxCNC QtPlasmaC, Mach3 and GRBL laser.
- Independent validation of every layout against the real part geometry.
- `sheetnest` command line (`run`, `quick`, `inspect`, `posts`) and YAML/JSON job files.
