# Replaying a feature tree in CATIA

`catia_mcp/tree` replays a CAD feature tree (version 1, the CAD-neutral JSON a
build123d model is traced into) as native, editable CATIA features. It is the
engine of a bridge that receives trees as data. It runs no code from the tree
and gives no agent access to CATIA.

## Commands

```bat
python -m catia_mcp.tree check TREE.json
python -m catia_mcp.tree reference TREE.json
python -m catia_mcp.tree build TREE.json --plm-type 3DShape
```

- **`check`:** the preflight only, with no CATIA. If any feature is unsupported or malformed, the whole tree is refused. Each reason is listed once, with its count and model lines.
- **`reference`:** the expected per-part volume, area and centre of gravity, built with build123d (`pip install -e ".[differential]"`).
- **`build`:** builds the tree in CATIA. It attaches to a running CATIA and never starts one: on 3DEXPERIENCE a fresh start needs an interactive login. Each part goes into a new part, a 3D Shape by default on 3DEXPERIENCE. Nothing is saved.
  - **Checks:** each part's volume is compared with build123d's when build123d is installed. Editability is proved by lengthening the first pad by 1 mm, checking that the volume moves, and restoring it.
  - **Report:** written to `tree_report.json`, with `success`, `native_history`, `editable`, `parts_detail` and `error`.
  - **Prefixes:** `--part ID --until FEATURE_ID` builds a prefix of one part, for development.

Keep the new part's window active during a build. The build stops if the
active window changes, because 3DEXPERIENCE measures and searches only in the
active editor.

## Supported features (milestones 1, 2 and 4)

| Tree feature | CATIA |
|--------------|-------|
| `sketch` (`line`, `circle`, 3-point `arc`) | A sketch on the origin plane, or on an offset or equation plane in a `Tree_Construction` geometrical set |
| `extrude` `new_body` | Pad in the part's main body, then in new bodies |
| `extrude` `join` / `cut` / `intersect` | Pad in a tool body, then Add / Remove / Intersect into the target |
| `revolve` (any axis in the sketch plane, any angle up to 360) | Shaft in its own body, then combined like an extrude. The axis is the CATIA sketch's own H or V axis when the tree's axis is one of them, otherwise a 3D line in `Tree_Construction` |
| `combine` | Add / Remove / Intersect |
| `hole` `simple` / `counterbore` | Cylinders cut as the contract's hole profiles: through holes span ±`effective_depth`/2, and a counterbore clears `effective_depth` above |
| `hole` `countersink` | A revolved profile cut, as build123d's `CounterSinkHole` builds it: the hole, a cone of the given angle, and clearance above |
| `mirror` followed by a `combine` `join` of its copy into its source | One CATIA Mirror of the source body. Any other use of a mirrored body is refused |
| `split` (`keep` `top` / `bottom`) | Split by the plane, keeping the side the tree keeps |
| `loft` between two sections in parallel planes (one profile each, no holes), every operation | Multi-sections Solid in its own body. Each section is redrawn with the vertices build123d pairs the sections by, coupled vertex to vertex from matching closing points (GSD points in `Tree_Construction`) |
| `sweep` of one profile along a planar, tangent-continuous path of lines and arcs, the profile perpendicular to the path where it starts, every operation | Rib in its own body, along the path drawn as a sketch in its own plane |

Everything else is refused by `check`, for now: fillet, chamfer and shell
(milestone 3), lofts through more than two sections, to a point, between
sections that are not parallel or with correspondence rails, Frenet sweeps,
sweeps along 3D paths or paths with corners, and tapered or oblique extrudes.

## How the build stays faithful

- **Sketch frames:** sketch points are projected into the frame CATIA actually gives the sketch, read back with `GetAbsoluteAxisData`. A tree plane whose normal is the reverse of a CATIA origin plane's, such as `-XZ` (normal -Y), is drawn on that origin plane and padded the other way.
- **Pad checks:** every pad is checked in a body of its own, against the profile's area and centroid (volume, centre of gravity). It is flipped once if CATIA extruded it the other way, and anything else stops the build.
- **Revolve checks:** every revolve is checked by Pappus. The volume comes from the profile's first moment about the axis, and the centre of gravity, for a partial angle, also tells the turning direction. If CATIA turned the other way, the angle is moved to the profile's other side once.
- **Mirror and split checks:** a mirror must leave the centre of gravity on its plane. A split must leave it on the kept side, and is flipped once otherwise.
- **Loft checks:** a loft's shape depends on which point of one section joins which point of the other, and build123d (OpenCascade's `BRepFill_CompatibleWires`) decides that pairing itself. `catia_mcp/tree/loft.py` reproduces its two rules. On 300 random pairs of typical sections (circles, rectangles, regular polygons and slots, offset or not) it gave the same solid as OpenCascade, to 1e-6, for every pair it accepted (273), and refused the other 27. The rules:
  - **Same number of edges, with a corner somewhere:** the second section starts at the vertex, and runs in the direction, with the smallest sum of 3D distances to the first section's vertices once both are moved onto the same barycentre.
  - **Otherwise** (different counts, or smooth loops like a circle): each section's vertices are carried onto the other along rays from its barycentre, and snap to an unused corner within 10 % of the edge they land on.

  Where the result depends on the order OpenCascade visits the vertices in, which the tree does not give, the loft is refused rather than guessed. That happens for symmetric sections that pair equally well in two ways (a hexagon turned 30° from another), and for a vertex landing near a contested corner (a circle to a 2:1 rectangle). Each loft is then checked against the ruled solid's volume and centre of gravity, and deleted and rebuilt once with the second section reversed if CATIA twisted it.

  Known gap: with far-offset or irregular sections (a triangle well off another section's centre, for example), OpenCascade sometimes pairs the vertices in a reversed, twisted order that this module does not reproduce. Those lofts already look broken in build123d. The per-part volume check against the reference catches any such difference.
- **Sweep checks:** a sweep is checked by Pappus: the profile's area times the length its centroid travels along the path.
- **Combine checks:** a combine is checked against what its operation can give.
- **No other retries:** the first failure stops the build with its part and feature, and the document may then hold a partial model.

## Live status

Not yet run against CATIA. `tests/fixtures/trees/` holds synthetic trees for
each path.
- **Milestone 1** (`m1_*`): an offset plane, a reversed-normal plane, arcs with a negative distance, join, cut and intersect, the three hole kinds, and a tilted plane (the equation-plane path).
- **Milestone 2** (`m2_*`):
  - revolves about an axis off the sketch origin (3D line), about the sketch's V axis, and about its H axis with a partial cut;
  - mirrors about an origin plane and an offset plane;
  - splits along a normal and on a tilted plane;
  - a countersink.
- **Milestone 4** (`m4_*`):
  - lofts: a frustum, a rectangle prismatoid whose second section starts at another corner and runs the other way, a circle-to-square loft joined onto a box, and a loft cut;
  - sweeps: a pipe along a line and a tangent arc, an off-centre profile along a line, an arc and a line, and a half-buried groove cut.

The build123d reference reproduces the source build123d models exactly on
the parts these milestones cover, such as a revolved shaft, a split housing,
a circle-to-square loft and a swept pipe.

Not yet validated live: the Multi-sections Solid calls (`AddNewLoft`,
`HybridShape.SectionCoupling` = 4 for vertex coupling, `AddSectionToLoft` with
a closing point and an orientation of 1 or -1), deleting a twisted loft
through the selection, and `AddNewRib` with a path sketch as its centre
curve.
