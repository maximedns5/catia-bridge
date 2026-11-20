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

### 3D curves, trims, extracts, transforms (second slice)

`catia_mcp/tree/curves.py` (contract), `curve_builder.py` (CATIA),
`curve_reference.py` (build123d). Curves have names of their own, in the
same namespace as surfaces, and go in a `Curves` geometrical set; their
points and axes are named construction elements (`Point (8, 0, 0)`,
`Helix axis Z`).

| Tree feature | build123d reference | CATIA |
|---|---|---|
| `curve` `op: helix`, `axis`, `pitch`, `height`, `radius`, `hand`, `taper` | `Edge.make_helix` | Helix (`AddNewHelix`) about a named axis, from a named start point |
| `curve` `op: spline`, `points`, `tangents`, `closed` | `Edge.make_spline` | Spline (`AddNewSpline`, `AddPoint` or `AddPointWithConstraintExplicit`) through named points |
| `curve` `op: polyline`, `points`, `closed` | `Wire.make_polygon` | Polyline (`AddNewPolyline`, `InsertElement`) |
| `curve` `op: circle`, `center`, `normal`, `radius` | `Edge.make_circle` | Circle (`AddNewCircleCtrRad`) on a named plane, whole |
| `curve` `op: project`, `inputs` [curve or sketch, surface], `direction` | `Wire.project_to_shape` (nearest solution), or OpenCascade's normal projection | Projection (`AddNewProject`), along a direction or normal |
| `curve` `op: intersect`, `inputs` [surface, surface] or [surface] and `plane` | OpenCascade's section | Intersection (`AddNewIntersection`) with a surface or a named plane |
| `surface` `op: extrude` / `revolve` / `fill` with a curve input, `sweep` with a `guide` curve | `Shell.extrude`, `Shell.revolve`, `Face`, `Shell.sweep` | Extrude, Revolution, Fill, explicit Sweep of the curve itself |
| `surface` `op: sphere`, `center`, `radius` | the sphere's face | Sphere (`AddNewSphere`), whole |
| `surface` `op: cylinder`, `axis`, `radius`, `length` | the extruded circle | Cylinder (`AddNewCylinder`) |
| `surface` `op: translate` (`vector`), `rotate` (`axis`, `angle`), `mirror` (`plane`), `scale` (`center`, `ratio`), `affinity` (`origin`, `ratios`) | OpenCascade transformations | Translate, Rotate, Symmetry, Scaling (`AddNewHybridScaling`, else an Affinity), Affinity (`AddNewAffinity`) |
| `surface` `op: loft`, three or more sections, `ruled: true` | one ruled band per pair, sewn | one Multi-sections Surface per pair (`<name> band 1`...), joined |
| `trim_surfaces`, `inputs` [two surfaces], `keep_points` | each surface split by the other, the part nearest its keep point kept, sewn | Trim (`AddNewHybridTrim`) |
| `extract`, `bodies` [one body], `face` (a face anchor) | the body's face the anchor names | Extract (`AddNewExtract`, no propagation) of the face found as a shell's faces are |

- **Checks.** Each curve's length, and its centre when CATIA measures it,
  must agree with build123d's within 1e-3 (the expected properties'
  `curves`); surfaces as in the first slice. The report lists each part's
  `curves` (name, op, CATIA name, measured and expected length, centre).
- **Undocumented senses, tried in order.** A helix's axis (as given or
  inverted), turning sense and taper side; a cylinder's direction; a
  translation's and a rotation's sense; a trim's two orientations; an
  extract's propagation (none, tangent, CATIA's default); a projection's
  solution type (CATIA's default, then nearest, then all). A fractional
  number of turns puts a helix's centre off its axis, so the centre tells
  its hand.
- **Helix numbers.** build123d's pitch runs along a tapered helix's cone; CATIA
  gets the pitch along the axis (`pitch x cos(taper)`). The helix starts at
  `origin + radius x X`, X being OpenCascade's reference direction for the
  axis (`gp_Ax2`), so the start point is the script's.
- **Splines.** CATIA interpolates its own spline through the tree's points
  (and tangents when the script gives them), not OpenCascade's: the spline
  and everything made from it are checked within 2 % and the report gives
  the `deviation`. A solid made from it adds 2 % of its volume to the part
  check's spline allowance.
- **Parameters.** A helix's `params.pitch` (untapered) and `params.height`,
  a circle's or a sphere's `params.radius`, a translation's
  `params.distance`, a rotation's `params.angle`, a scaling's
  `params.ratio` drive the CATIA values when CATIA exposes them.
- **3DEXPERIENCE lengths.** The curve measurable's unit is not documented:
  the reading that fits build123d's length (m or mm) is kept for the part.

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
- Second slice: `curve` (`op` `helix`, `spline`, `polyline`, `circle`,
  `project`, `intersect`; `creates` [one curve name]); surface ops `sphere`,
  `cylinder`, `translate`, `rotate`, `mirror`, `scale`, `affinity`; curve
  inputs to `extrude` (with `direction`, the full vector), `revolve`, `fill`
  and `sweep` (`guide`: a curve name instead of `path`); `loft` through three
  or more sections when `ruled`; `trim_surfaces` (`inputs`, `keep_points`: a
  point on the part of each surface to keep, as a designer clicks it);
  `extract` (`bodies`, `face`: a face anchor, as a shell's). Helices follow
  build123d's `Helix` (`taper` is its `cone_angle`, `hand: left` its
  `lefthand`), splines its `Spline` (`closed` is `periodic`).

### Limits

Refused by `check`: spline entities in a sketch used as a surface's curve,
a sketch curve that is not one connected chain, fills of open or non-planar
curves, smooth lofts through more than two sections, lofts with wire or
curve sections, sketch-path sweeps outside the solid sweep's rules (planar,
tangent-continuous path; profile perpendicular at the start), and
thickening on both sides. Close Surface needs planar openings in the
reference.

Not done, and why:

- **Smooth multi-sections through three or more sections, guides.**
  OpenCascade interpolates one surface through every section, CATIA its own;
  build123d has no guided loft. No reference would agree with CATIA.
- **Blend.** build123d has no blend; a tangent or curvature blend has no
  OpenCascade counterpart, and a point blend is CATIA's own surface, not a
  ruled loft in general.
- **Shape fillet.** OpenCascade fillets edges of solids only, not two
  surfaces: no reference.
- **Spiral, conics, healing, boundary, extrapolate, near, sew.** Not in
  build123d's vocabulary yet.

## Generative Shape Design coverage

Status of each GSD tool in the tree builder (the interactive MCP tools in
`catia_mcp/tools/gsd.py` already wrap many of them one by one).

| GSD tool | Tree builder | Notes |
|---|---|---|
| Point | done | coordinates, named, made once (construction set) |
| Line | done | point-point, named axes and directions |
| Plane | done | offset, angle, equation; one per distinct plane |
| Curves (sketch) | done | sketches on named planes; `wire: true` curves |
| Circle / conic (3D) | done, not live | `curve` `op: circle`, a whole circle; conics not yet |
| Helix | done, not live | `curve` `op: helix`, taper and hand |
| Spiral | not yet | |
| Spline (3D) | done, not live | `curve` `op: spline`, checked within 2 % (CATIA's interpolation) |
| Polyline | done, not live | `curve` `op: polyline` |
| Projection | done, not live | `curve` `op: project`, along a direction or normal |
| Intersection | done, not live | `curve` `op: intersect`, two surfaces or a surface and a plane |
| Extrude | done | `surface` `op: extrude` |
| Revolve | done | `surface` `op: revolve` |
| Sphere | done, not live | `surface` `op: sphere`, whole |
| Cylinder | done, not live | `surface` `op: cylinder` |
| Offset | done | `surface` `op: offset` |
| Sweep | done | explicit sweep along a planar path |
| Fill | done | planar fill of one closed curve |
| Multi-sections Surface | done | two sections (live); three or more ruled (not live); smooth or guided through more: no reference |
| Blend | no | no build123d reference (see Limits) |
| Join | done | `join_surfaces` |
| Healing | not yet | |
| Split | done | `split_surface` by a plane |
| Trim | done, not live | `trim_surfaces` with keep points |
| Boundary | not yet | |
| Extract | done, not live | `extract` of a face named by a face anchor |
| Shape fillets | no | no OpenCascade surface-to-surface fillet (see Limits) |
| Translate / Rotate / Symmetry / Scaling / Affinity | done for surfaces, not live | `surface` ops; bodies: `copy`, `mirror`, `scale` |
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
| `s7_helix_coil` | a helix (2.5 turns) and a 3D circle at its start, swept into a coil, thickened |
| `s7_helix_taper_left` | a tapered left-hand helix about X and a helix about Y, curves only |
| `s7_spline_ribbon` | a spline through four points, extruded, thickened (checked within 2 %) |
| `s7_polyline_circle` | a closed saddle polyline extruded, a planar triangle filled, a tilted 3D circle filled and extruded |
| `s7_project_intersect` | a drum cut by a tilted plane (ellipse filled), a circle projected along Y and normally, two surfaces intersected |
| `s7_loft_three_sections` | a ruled vase through three circles (two bands joined), closed |
| `s7_trim_extract` | a block's top face extracted and trimmed with a cylinder |
| `s7_transforms_primitives` | a sphere and a cylinder, translated, rotated, mirrored, scaled, affine |

## Live status

Run live on 3DEXPERIENCE R428 (French, student role): every fixture built,
every surface area and every volume exact.

| Fixture | Result | Senses CATIA needed |
| --- | --- | --- |
| `m10_reference_planes` | 12047.6107 mm3; `Plane Z+20` driven by `Sketch1_amount`, `Plane XY +30deg about X` verified | none flipped |
| `s1_extrude_curve_thicken` | surface 1542.4778 mm2, thickened 3156.7985 mm3 | none |
| `s2_revolve_close` | surface 3063.0528 mm2, closed 17671.4587 mm3 | none |
| `s3_fill_join_close` | Tube 900, Floor 200, Lid 200, Skin 1300 mm2; closed 3000 mm3 | none |
| `s4_loft_split_offset_thicken` | Skin 1025.2186, Lower 576.6855, Outer 639.5173 mm2; 1029.9618 mm3 | split orientation -1, offset orientation True |
| `s5_sweep_ribbon_thicken` | Band 321.3717 mm2, 321.3717 mm3 | thicken orientation 1 |
| `s6_surfaces_only` | Quarter 314.1593, Vault 502.6548 mm2, no body | none |

Also live with the new reference geometry, each exact: `p1_properties_plate`
(`Plane Z+4`), the m10 Part Design fixtures (`Plane Z+5`, `Plane Z+25`,
`Plane Z+40`, an equation plane for a tilted limit).

What to read in each report: `success`, each part's `construction` (names,
`verified`, notes), `surfaces` (measured against expected area), `curves`
(measured against expected length) and the features' `notes` (which variant
CATIA needed).

The `s7_*` fixtures (second slice) have not run live yet.
