# Surfaces and reference geometry

Two additions to the tree builder ([feature-tree.md](feature-tree.md)):
reference geometry made as a designer would, and surface features
(Generative Shape Design) that can become bodies.

## Reference geometry (`catia_mcp/tree/datums.py`)

A designer sketching off the origin planes first makes the plane, names it,
and sketches every profile on that plane on the same element. The builder
now does the same, in the part's `Tree_Construction` geometrical set:

| Tree plane | CATIA element | Name |
|---|---|---|
| xy, yz or zx through the origin | the origin plane itself | (CATIA's) |
| parallel to an origin plane | Offset plane (`AddNewPlaneOffset`) | `Plane Z+20`, `Plane X-7.5` |
| holds the X, Y or Z direction | Angle plane (`AddNewPlaneAngle`) about that axis, from the origin plane nearest to it | `Plane XY +30deg about X` |
| the same, not through the origin | Angle plane about a parallel axis through the plane | `Plane XY +45deg about X through (0, -5, 5)` |
| any other | Equation plane (`AddNewPlaneEquation`) | `Plane 0.5774x+0.5774y+0.5774z=5.7735` |

- **One plane per distinct plane.** A plane and its reverse share one
  element (a sketch on the reverse is padded the other way, as on the origin
  planes). Mirrors, splits, affinities and surface splits reuse them too.
- **Axes and points** are made once per place and named: `Axis X`,
  `Axis Z through (0, 10, 0)`, `Direction (0, 0.6, 0.8) through (...)` (an
  oblique pad's direction), `Point (10, 0, -5)`. Lines stay two points and a
  line between them (proven live).
- **Angle planes are confirmed.** The sense CATIA turns an angle plane by is
  not documented. The first sketch on it gives its frame
  (`GetAbsoluteAxisData`); if the frame is not on the tree's plane, the
  sketch is deleted, the angle's sign is changed, and the sketch is made
  again. If neither sense fits, or CATIA refuses the angle plane, an equation
  plane replaces it, with a note. A plane first wanted by a mirror, a split
  or an affinity (nothing to read it back by) is an equation plane.
- **Driving parameters.** A sketch's `params.offset` (mm, along the tree
  plane's normal) and `params.angle` (degrees) become user parameters
  driving the plane's offset or angle, as the other script numbers do
  (human style). The number must be the plane's: a sketch moved by the
  builder (a centred box's middle plane) does not drive it.
- **Report.** Each part's `construction` lists the elements: `type`
  (`plane`, `axis`, `point`), `name`, `definition`, `used_by` (feature ids),
  and, when set, `parameter`, `verified` (an angle plane confirmed by its
  sketch) and `notes`.

Volumes are unchanged: only the support of each sketch changed, and every
feature keeps its checks.

## Surface features

New, optional tree features (`catia_mcp/tree/surfaces.py` for the contract,
`surface_builder.py` for CATIA, `surface_reference.py` for build123d).
Surfaces have names of their own, apart from the bodies.

| Tree feature | build123d reference | CATIA |
|---|---|---|
| `surface` `op: extrude`, `inputs` [sketch], `distance` or `direction`, `symmetric` | `Shell.extrude` of the sketch's curve | Extrude (`AddNewExtrude`, `AddNewDirectionByCoord`) |
| `surface` `op: revolve`, `inputs` [sketch], `axis`, `angle` | `Shell.revolve` | Revolution (`AddNewRevol`) about a named axis |
| `surface` `op: loft`, `inputs` [two sketches, one closed profile each], `ruled` | `Shell.make_loft` | Multi-sections Surface (`AddNewLoft`): sections redrawn with build123d's vertex pairing, coupled from 3D closing points, as the solid loft |
| `surface` `op: sweep`, `inputs` [sketch], `path` | `Shell.sweep` | Sweep (`AddNewSweepExplicit`), the path drawn as a sketch in its plane |
| `surface` `op: fill`, `inputs` [sketch with one closed curve] | the planar `Face` | Fill (`AddNewFill`, `AddBound`) |
| `surface` `op: offset`, `inputs` [surface], `distance` | OpenCascade offset (`BRepOffsetAPI_MakeOffsetShape`) | Offset (`AddNewOffset`) |
| `join_surfaces`, `inputs` [surfaces] | faces sewn into one shell | Join (`AddNewJoin`, `AddElement`) |
| `split_surface`, `inputs` [surface], `plane`, `keep` `top` / `bottom` | the shell cut by the half-space on the other side | Split (`AddNewHybridSplit`) by a named plane |
| `thicken`, `inputs` [surface], `thickness`, `operation` | `Solid.thicken` | Thick Surface (`AddNewThickSurface`) in a body of its own |
| `close_surface`, `inputs` [surface], `operation` | the shell closed by planar faces on its openings | Close Surface (`AddNewCloseSurface`) in a body of its own |

Every surface feature `creates` one surface name. `thicken` and
`close_surface` take `operation` (`new_body`, `join`, `cut`, `intersect`)
with `creates` or `bodies`, as an extrude: other operations combine the new
body into the target with Add / Remove / Intersect.

- **Where.** Surfaces go in a `Surfaces` geometrical set, named after the
  tree's surface. Their sketches go in the same set (`HybridSketches`), or
  in the main body if CATIA refuses that.
- **Checks.** Each surface's area, and its centre when CATIA measures it,
  must agree with build123d's within 1e-3 (the expected properties'
  `surfaces`). Each solid's volume and centre must agree with build123d's
  (`solids`, by feature id); then the part's volume is checked as usual.
- **Undocumented senses.** An extrude's direction, a revolve's side, an
  offset's and a thick surface's side, a split's kept side, a loft's closing
  points and orientation: the variants are built in turn, and the first that
  matches build123d is kept. The feature's notes say which one and why the
  others failed. A surface that no variant matches stops the build.
- **Without build123d.** The first variant CATIA builds is kept, and the
  notes say it was not checked.
- **Parameters.** An extrude surface's `params.distance` drives its
  `BeginOffset` (and `EndOffset` when symmetric); a revolve's `params.angle`
  its angle, when CATIA exposes them as parameters.
- **Report.** Each part's `surfaces` lists name, feature, CATIA name,
  measured and expected area, and centre.
- **Measures.** On 3DEXPERIENCE the area is read from the `surface`
  measurable, then the `volume` one; on V5 from `SPAWorkbench`
  (`catia_mcp/measure.py`).

### Contract additions requested from the app

All optional and additive; existing trees are unchanged.

- Sketch `wire: true`: an open or closed chain of curves used as a curve, not
  a region (no `profiles` needed). Such a sketch cannot make a solid.
- Sketch `params.offset` and `params.angle`: the script numbers behind an
  offset or rotated sketch plane (`Plane.XY.offset(20)`), as for other
  features' `params`.
- The surface features above: `surface` (`op`, `inputs`, op fields),
  `join_surfaces`, `split_surface`, `thicken`, `close_surface`. `distance` and
  `thickness` follow build123d's face normal, as the script sees it.
- Part `bodies` may be empty: a part of surfaces only.

### Limits of this first slice

Refused by `check`: spline entities in a surface's curves, a curve that is
not one connected chain, fills of open curves, lofts through more than two
sections or with wire sections, sweeps outside the solid sweep's rules
(planar, tangent-continuous path; profile perpendicular at the start), and
thickening on both sides. Close Surface needs planar openings in the
reference.

## Generative Shape Design coverage

Status of each GSD tool in the tree builder (the interactive MCP tools in
`catia_mcp/tools/gsd.py` already wrap many of them one by one).

| GSD tool | Tree builder | Notes |
|---|---|---|
| Point | done | coordinates, named, made once (construction set) |
| Line | done | point-point, named axes and directions |
| Plane | done | offset, angle, equation; one per distinct plane |
| Curves (sketch) | done | sketches on named planes; `wire: true` curves |
| Circle / conic (3D) | not yet | sketch circles and arcs only |
| Helix | not yet | |
| Spiral | not yet | |
| Spline (3D) | not yet | sketch splines exist for solids, not for surfaces |
| Projection | not yet | |
| Intersection | not yet | |
| Extrude | done | `surface` `op: extrude` |
| Revolve | done | `surface` `op: revolve` |
| Sphere | not yet | |
| Cylinder | not yet | an extruded circle gives the same surface |
| Offset | done | `surface` `op: offset` |
| Sweep | done | explicit sweep along a planar path |
| Fill | done | planar fill of one closed curve |
| Multi-sections Surface | done | two sections |
| Blend | not yet | |
| Join | done | `join_surfaces` |
| Healing | not yet | |
| Split | done | `split_surface` by a plane |
| Trim | not yet | two surfaces trimming each other |
| Boundary | not yet | |
| Extract | not yet | |
| Shape fillets | not yet | |
| Translate / Rotate / Symmetry / Scaling / Affinity | not yet for surfaces | bodies: `copy` (Rotate, Translate), `mirror`, `scale` (Affinity) |
| Extrapolate | not yet | |
| Near | not yet | |
| Thick Surface | done | `thicken` |
| Close Surface | done | `close_surface` |
| Sew Surface | not yet | |

## Fixtures

| Fixture | What it shows |
|---|---|
| `m10_reference_planes` | two sketches on one `Plane Z+20` (its offset a parameter), a block on `Plane XY +30deg about X` |
| `s1_extrude_curve_thicken` | an open curve (line and half circle) extruded 30 mm, thickened 2 mm |
| `s2_revolve_close` | a cup profile revolved a full turn, closed into a cylinder |
| `s3_fill_join_close` | a tube, two fills, joined and closed into a box |
| `s4_loft_split_offset_thicken` | a frustum skin lofted, split at mid height, offset 1 mm, thickened |
| `s5_sweep_ribbon_thicken` | a ribbon swept along a line and a quarter turn, thickened |
| `s6_surfaces_only` | a quarter revolution and a symmetric arc extrude, no body |

## Live status

Not yet run live. To run on 3DEXPERIENCE (PowerShell, CATIA open, a 3D Shape
can be created):

```powershell
python -m catia_mcp.tree build tests\fixtures\trees\m10_reference_planes.json --report m10.json
python -m catia_mcp.tree build tests\fixtures\trees\s1_extrude_curve_thicken.json --report s1.json
```

What to read in each report: `success`, each part's `construction` (names,
`verified`, notes), `surfaces` (measured against expected area) and the
features' `notes` (which variant CATIA needed).
