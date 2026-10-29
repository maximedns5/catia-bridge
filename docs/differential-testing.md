# Differential testing against build123d

`scripts/differential` checks the MCP tools against an independent CAD kernel.
Each case is written once as a list of operations. build123d, which runs on
OpenCascade, builds it for the expected values. The MCP tools build it in
CATIA. The measured properties are then compared. A match is evidence about
CATIA and the tools. A mismatch is a finding: a tool bug, a wrong convention,
or a wrong assumption written down in the case.

This is Route A: the model is rebuilt through the MCP tools, so it tests the
modeling tools. Route B, importing build123d STEP files into CATIA and
checking the measurement tools on them, is not implemented yet.

## Running it

On the Windows machine running CATIA, from the repository root:

```bat
pip install -e ".[differential]"
python -m scripts.differential --plm-type 3DShape
```

- **CATIA:** start it first, and on 3DEXPERIENCE log in.
- **`--plm-type 3DShape`:** needed without the licence for scripted 3D Part creation (see `docs/3dexperience.md`). Leave it out on V5.
- **Single cases:** `--case NAME`, repeatable.
- **Cleanup:** `--close` closes each part without saving. By default the parts stay open for inspection, and nothing is ever saved.
- **Report:** written to `differential_report.json`. It holds every case's checks (expected value, measured value, tolerance) and every tool call with its output.

`python -m scripts.differential --reference-only` prints the expected values
without CATIA, on any OS.

## What is compared

| Property | CATIA side | Tolerance |
|----------|------------|-----------|
| Volume, area | `catia_get_inertia` | 1e-5 relative (1e-3 for freeform cases), 1e-3 absolute floor |
| Centre of gravity | `catia_get_inertia` | 1e-5 × bounding-box diagonal, at least 1e-3 mm |
| Bounding box | `catia_get_bounding_box` | same as the centre of gravity |

The bounding box is only compared where the solid's extreme points are
vertices. CATIA's box is a sweep over vertices, so a curved face can extend
past it.

Face, edge and vertex counts are not compared. The two kernels split surfaces
differently, for example with seam edges on cylinders.

## Hypotheses the cases check

A case whose `hypothesis` is set encodes an assumption about CATIA. If it
fails, check the hypothesis first.

| Case | Hypothesis |
|------|------------|
| `xy_offset_pad`, `yz_offset_pad`, `zx_offset_pad` | Sketch axes and normals of the origin planes follow the build123d convention: xy is H=+X, V=+Y, normal +Z; yz is H=+Y, V=+Z, normal +X; zx is H=+Z, V=+X, normal +Y |
| `pad_symmetric` | `catia_pad` with `symmetric` gives a total length equal to `height`, as its schema says |
| `rect_pattern_pockets` | `catia_rect_pattern` without edges patterns along +X and +Y |
| `shaft_ring`, `groove_on_cylinder` | Shaft and groove revolve about the sketch V axis |

## Writing a case

Cases live in `scripts/differential/cases.py`, and the operations are in
`ops.py`. Every case needs its closed-form volume in
`tests/test_differential.py`, so the reference interpreter is checked against
geometry worked out by hand.

- **Pockets and holes:** have material on one side of the sketch plane only. The MCP tools flip to reach material, and the reference refuses an ambiguous side.
- **Mismatches:** keep a case that fails in CATIA, and document what it shows.

## Not covered yet

- **Fillet, chamfer, shell, draft, thickness:** these select edges or faces as `Edge.N` / `Face.N`, whose order is specific to CATIA. Matching them to a build123d edge selection needs a tool that reports each edge's geometry, such as its endpoints.
- **GSD surfaces:** loft, sweep, fill, and thick or closed surfaces made into solids.
- **Parameters and formulas:** changing a parameter and checking the predicted volume change.
- **Assemblies:** on 3DEXPERIENCE, Physical Product creation may need a licence, and assembly constraints are not available through automation.
