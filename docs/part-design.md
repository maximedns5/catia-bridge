# Part Design in the tree replay

What the feature tree replay (`docs/feature-tree.md`) covers of CATIA's Part
Design workbench, and the features added after the first milestones: drafts,
extrudes up to a limit, tapped and V-bottomed holes, and lofts to a point
(the first slice, live on R428); sweeps along a helix, cosmetic threads on a
shaft, user patterns of holes, and bodies moved or mirrored in place (the
second slice, not run live yet). They are built as a designer would model
them: one native feature each, with its parameters editable.

The offline tests drive them through a fake CATIA (`tests/test_part_design.py`,
`tests/test_part_design_extra.py`), and the build123d reference agrees with
each fixture's closed form (1e-9 for the first slice, 1e-5 for the helix
sweeps, whose helix OpenCascade approximates).
`scripts/probe_part_design.py` runs them live; see [Live checks](#live-checks).

## Coverage

Status: **done** (in `main`, every row validated live on 3DEXPERIENCE R428,
the drafts, limits, tapped holes and lofts to a point in five probe runs;
CATIA V5 only against the test fake), **built, not live** (the tree field,
preflight, build123d reference, CATIA build and fixtures exist and pass the
fake; waiting for a live probe run), **contract** (CATIA can, but the tree
has no field for it yet: asked of the app), **no build123d** (build123d has
no matching operation, so no tree can carry it).

| CATIA Part Design feature | Status | Tree feature / note |
|---|---|---|
| Pad | done | `extrude` (`new_body`, `join`), `direction`, `symmetric` |
| Pad up to next / last / plane / surface | done | `extrude` with `until` |
| Pocket | done | `extrude` `cut` |
| Pocket up to next / last / plane / surface | done | `extrude` `cut` with `until` |
| Drafted filleted pad / pocket | no build123d | build123d tapers (`extrude(taper=...)`, built as lofts) and fillets apart; the tree carries both features |
| Multi-pad / multi-pocket | no build123d | one length per extrude; a sketch of several profiles is one Pad |
| Shaft | done | `revolve` `new_body` / `join` |
| Groove | done | `revolve` `cut` |
| Hole: simple, counterbored, countersunk | done | `hole` `kind` |
| Hole: tapped (thread, designation, depth, pitch) | done | `hole` `thread` |
| Hole: V bottom | done | `hole` `bottom` |
| Hole: tapered, counterdrilled | no build123d | |
| Rib | done | `sweep` along a planar path |
| Rib or Slot along a helix (threads, springs) | built, not live | `sweep` along a `helix` path entity, Frenet |
| Slot (removed rib) | done | `sweep` `cut` |
| Stiffener | no build123d | build123d has no stiffener; a tree would carry it as an extrude |
| Multi-sections Solid | done | `loft` between two sections, ruled through more |
| Multi-sections Solid to a point | done | `loft` with a point section |
| Removed Multi-sections Solid | done | `loft` `cut` |
| Multi-sections Solid, smooth through 3+ sections, with guides or spines | contract | OpenCascade's smooth loft is not CATIA's; needs guide curves in the tree |
| Solid Combine | no build123d | |
| Edge Fillet, constant radius | done | `fillet` |
| Variable radius fillet | contract | build123d has none on edges; refused |
| Face-face fillet, tritangent fillet | no build123d | |
| Chamfer (two lengths, length and angle) | done | `chamfer` |
| Draft angle (basic) | done | `draft` |
| Draft reflect line, variable angle draft | no build123d | |
| Shell | done | `shell` with removed faces |
| Thickness | done | `shell` without removed faces (build123d's `offset`) |
| Thread / Tap (cosmetic, on a cylindrical face) | built, not live | `thread` on a shaft; a tapped `hole` is done |
| Remove Face / Replace Face | no build123d | |
| Split | done | `split` by a plane |
| Thick Surface, Close Surface | done | `thicken`, `close_surface` ([surfaces.md](surfaces.md)), live on R428 |
| Sew Surface | contract | a surface sewn onto a body needs its own tree feature |
| Translation, Rotation | done | `copy` (a body copied, then moved); in place: `move` (built, not live) |
| Symmetry | built, not live | `mirror_in_place` |
| Axis to axis | contract | a `move` between two axis systems would carry it; `move` takes the matrix |
| Mirror | done | `mirror` joined back |
| Rectangular pattern, circular pattern | done | of holes (`hole` frames); patterns of other features reach the tree unrolled |
| User pattern | built, not live | of holes in one plane that form no grid or circle, or `pattern: {"kind": "user"}`; of other features: contract |
| Scaling, Affinity | done | `scale` |
| Add, Remove, Intersect (boolean operations) | done | `combine`, and every `join` / `cut` / `intersect` |
| Assemble | no build123d | |
| Remove lump | no build123d | |

## Contract additions requested from the app

All optional and additive: existing trees keep their meaning.

### `draft`

```json
{"type": "draft", "id": "draft1", "bodies": ["Body1"],
 "faces": [{"type": "face", "geometry": "plane", "point": [20, 0, 10],
            "normal": [1, 0, 0], "area": 600.0}],
 "neutral_plane": {"origin": [0, 0, 0], "normal": [0, 0, 1]},
 "angle": 3}
```

build123d's `draft(faces, neutral_plane, angle)`. `faces` are face anchors
(as a shell's), taken on the body before the draft. The neutral plane's
normal is the pulling direction, as in build123d; `pull_direction`, if
given, must be that normal. A positive angle narrows the solid along the
pulling direction (OpenCascade's convention, measured with build123d 0.13).
Faces must be planes, cylinders or cones, and none square to the pulling
direction. A draft without `faces` and `neutral_plane` (the tree's form
before drafts were native) still goes to the STEP fallback.

### `extrude` with `until`

```json
{"type": "extrude", "id": "extrude4", "sketch": "sketch4", "operation": "cut",
 "bodies": ["Body1"], "until": "last"}
```

`until` is `next`, `last`, `previous` or `first` (build123d's `Until`, the
limit being the body the extrude changes), `plane` with `until_plane`
(`origin`, `normal`), or `face` with `until_face` (a planar face anchor of
the body). The extrude goes along its sketch normal, against it for
`previous` and `first` (as build123d), or toward the plane for `plane` and
`face`; a `direction`, if given, must be along the normal and sets the
sign. `distance` is not needed. Only `join` and `cut`, not tapered, not
symmetric. `until_target`, if given, must be the body changed.

### `hole` `thread` and `bottom`

```json
{"type": "hole", "kind": "simple", "diameter": 5.0, "depth": 12, ...,
 "thread": {"designation": "M6", "depth": 10},
 "bottom": {"type": "v", "angle": 118}}
```

`diameter` stays the drilled diameter (the tap drill): build123d has no
thread, and CATIA's thread is cosmetic, so the solid is the drilled hole.
`thread` is an ISO metric designation (`"M6"`, `"M8x1"`) or a mapping with
`designation`, `nominal_diameter`, `pitch` (default: ISO coarse), `depth`
(default: the blind hole's depth, or the whole through hole) and `side`
(`right`, `left`). `bottom` is `flat` (the default) or `v` with its point
`angle` (default 118°), for blind holes: the cone goes below `depth`.

### `loft` to a point

```json
{"type": "loft", "sections": ["sketch1", {"point": [3, 2, 15]}], ...}
```

One section is a point (`{"point": [x, y, z]}`), first or last, the other a
sketch of one profile without holes. build123d's `loft([face, Vertex])`.

### `sweep` along a `helix`

```json
{"type": "sweep", "id": "sweep1", "sketch": "sketch1", "frenet": true,
 "operation": "new_body", "creates": ["Body1"],
 "path": [{"type": "helix", "pitch": 4, "height": 10, "radius": 10,
           "center": [0, 0, 0], "direction": [0, 0, 1],
           "lefthand": false, "cone_angle": 0}]}
```

build123d's `sweep(profile, Helix(pitch, height, radius, center, direction,
cone_angle, lefthand), is_frenet=True)`. The path is that one entity.
`height` or `turns` (`height = turns x pitch x cos(cone_angle)`, both may be
given if they agree); `center` (default the origin) and `direction`
(default +Z) are the axis; `lefthand` defaults to false; `cone_angle`
(degrees, default 0, positive widens along the direction) is
build123d's: the pitch is then measured along the cone, so the advance per
turn along the axis is `pitch x cos(cone_angle)`. The helix starts where
OpenCascade starts it: `center + radius x X`, X the direction `gp_Ax3` picks
for the axis (+X for a Z axis). An optional `start` point, if given, must be
that point. `frenet` must be true: build123d's default frame twists the
profile along a helix (0.8 % off for a 2 x 2 square). The profile is any
planar sketch of one profile, anywhere (usually in a plane through the
axis at the start), not reaching the axis, no taller along the axis than
the pitch when there is more than one turn, and never edge-on to the motion.

### `thread`

```json
{"type": "thread", "id": "thread1", "bodies": ["Body1"],
 "face": {"type": "face", "geometry": "cylinder", "point": [0, 0, 25],
          "area": 942.4778, "radius": 5},
 "limit_face": {"type": "face", "geometry": "plane", "point": [0, 0, 40],
                "normal": [0, 0, 1], "area": 78.5398},
 "thread": "M10", "length": 15}
```

A cosmetic thread on a shaft: build123d has none, so the solid is
unchanged. `face` is the cylindrical face anchor, `limit_face` the planar
face anchor the thread starts from (which end), `length` its depth from
there. `thread` is a designation or a mapping as a hole's (`designation`,
`nominal_diameter`, `pitch`, default ISO coarse, `side` `right` or `left`).
A `radius` on the face is optional; a shaft whose diameter is not the
thread's nominal one is noted, not refused. A thread in a bore is a
tapped `hole`.

### `hole` `pattern`

```json
{"type": "hole", ..., "frames": [...], "pattern": {"kind": "user"}}
```

The hole's frames are the positions. Holes whose frames share their
drilling direction and their plane, and form no grid or circle, are one
seed Hole and a User Pattern without the field; `{"kind": "user"}` asks for
it even for a grid. An optional `positions` list must be the frames'
origins, in order. Patterns of other features still reach the tree
unrolled: a `pattern` on them is a contract for later.

### `move`

```json
{"type": "move", "id": "move1", "bodies": ["Body1"],
 "matrix": [[0, -1, 0, 50], [1, 0, 0, 0], [0, 0, 1, 0]]}
```

The body moved in place by the rigid matrix `[R|t]` (determinant +1), its
name kept: build123d's `body = body.moved(Location(...))`. A copy that
moves is `copy`; a mirror is `mirror_in_place`.

### `mirror_in_place`

```json
{"type": "mirror_in_place", "id": "mirror1", "bodies": ["Body1"],
 "plane": {"origin": [40, 0, 0], "normal": [1, 1, 0]}}
```

The body replaced by its mirror image: build123d's
`mirror(body, Plane(...), mode=Mode.REPLACE)`. A mirror joined back into
its source stays `mirror`.

## How each is built

| Tree | CATIA | Checked by |
|---|---|---|
| `draft` | One Draft (`AddNewDraft`): the matched faces in its first domain, the neutral plane (an origin plane or a construction plane in `Tree_Construction`) as neutral element, pulled along its normal, the angle on the domain's `DraftAngle` (a user parameter when the script gave it). The parting element is none, else the neutral plane | build123d's body after the draft (human style); else the first-order estimate, each face turning about its line on the neutral plane: `−A·h·tan(angle)`, exact for faces that share no edge, 3 % off on a box drafted on four sides at 5° |
| `extrude` `until` | A Pad or Pocket straight into its body (never a tool body: up to next needs the body's material), `FirstLimit.LimitMode` up to next, last, plane or surface, `LimitingElement` the plane or the matched face | build123d's body after the feature; without build123d, only the sign of the volume change |
| `hole` `thread` | The Hole as before, then `ThreadingMode` threaded, `HoleThreadStandard` "Metric Thick Pitch" (or "Metric Thin Pitch") with `HoleThreadDescription` the designation, `ThreadDepth`, `ThreadSide` | the hole's volume, as for any hole |
| `hole` `bottom` `v` | `BottomType` V bottom, `BottomAngle` | the hole's volume |
| `loft` to a point | A Multi-sections Solid from the sketch to a construction point, in its own body | the cone's closed form: volume `A·h/3`, centre of gravity a quarter of the way from the section's centroid to the apex |
| `sweep` along a `helix` | The helix axis and start point (construction elements), a GSD Helix in `Tree_Construction` (pitch along the axis, height, taper; editable), the profile sketch, then a Rib (a Slot to cut) pulled along the axis: straight into the body in the human style, else in its own body | the helix's distance to two points of the tree's (a quarter turn in, the end); build123d's body after the feature; else the screw motion's closed form (below); a tapered helix has none, so only the part's volume |
| `thread` | One Thread on the matched cylindrical face from the matched planar face, its side, diameter, depth and pitch set, the ISO table loaded (`CreateStandardThreadDesignTable`) | the body's volume unchanged (1e-6), the Thread reading back the tree's diameter, depth and pitch |
| `hole` `pattern` user | The seed Hole, a sketch of the other positions (constrained in the human style), and a User Pattern (`AddNewUserPattern`, `AddFeatureToLocatePositions`) | build123d's body after the holes |
| `move` | A Rotate about a construction line through the origin and a Translate of the body in work (`AddNewRotate2`, `AddNewTranslate2`) | volume unchanged, centre of gravity at `R c + t` |
| `mirror_in_place` | A Symmetry of the body in work about the plane (an origin plane or a construction plane) | volume unchanged, centre of gravity mirrored |

The screw motion's closed form: along a cylindrical helix the Frenet frame
turns about the axis and advances `pitch / 2π` per radian, so every point of
the profile moves rigidly. With `w(q) = s (d × (q − O))·n + (p / 2π) d·n`
the motion's flux through the profile's plane (`s` +1 right-handed, `d` the
axis, `n` the profile's normal), the volume is `Θ ∫w dA` (`Θ = 2π turns`):
`A × 2π r_c × turns` for a profile in a plane through the axis (Pappus),
`π a² × L` for a circle square to the helix. The centre of gravity
integrates the screw over the profile's second moments
(`catia_mcp/tree/helix.py`). build123d agrees to 2e-6 on the volume and
1e-5 mm on the centre, both hands, on and off the origin.

What is not documented, and so tried in order, each refusal or mismatch
noted in the feature's report entry:

- **Draft orientation:** as given, then pulled the other way, then the angle
  negated. The first that changes the body as the tree's draft did is kept.
- **Limit modes:** up to next, then up through next; up to a face, then up
  to the plane holding it. Each with one direction flip (`DirectionOrientation`).
  The `CatLimitMode` values come from the type library, else the CAA help's
  order (offset 0, up to next 1, up to last 2, up to plane 3, up to surface
  4, up through next 5).
- **Thread standard:** the ISO standard and designation first. A standard
  that changes the hole's diameter (CATIA's tables give the minor diameter,
  4.917 mm for M6, not the 5 mm tap drill) is replaced by "No Standard" with
  `ThreadDiameter` and `ThreadPitch`. A thread CATIA refuses altogether
  leaves the Hole smooth, never lost: the entry's `thread` says
  `threaded: false` and why. The `CatHoleThreadingMode` value comes from the
  type library, else the V5 B30 value (0), marked `verified: false`.
- **V bottom depth:** the depth of the cylinder first, then the depth to the
  tip, whichever matches build123d.
- **Loft to a point:** the sections in the tree's order, then the other
  order. A right circular cone CATIA will not loft is a Shaft of its
  triangle (as a designer would turn it); any other section fails the
  build.
- Without the Hole features (exact style, or every Hole layout refused), a V
  bottom is cut as a turned cone and the thread is noted as not modelled.

The second slice, not run live yet, tries in order:

- **Helix:** `AddNewHelix(axis, invert, start, pitch, height, clockwise,
  0, taper, outward)` counter-clockwise first (right-handed about the axis
  line's direction), then clockwise, each with the axis as given then
  inverted. Each is measured against the tree's helix (a quarter turn in
  tells the hand, the end the way); a helix CATIA cannot measure is kept
  only if the Rib then matches, and with a whole number of turns its hand
  is noted as unverified (either hand's centre of gravity is on the axis).
- **Rib:** `AddNewRibFromRef` (references), then `AddNewRib` (objects);
  each pulled along the helix axis (`PullingDirElement`), then with the
  default profile control (keep angle). `AddNewSlotFromRef` and
  `AddNewSlot` to cut. CATIA's helix is a spline approximation: the part's
  volume check allows 5e-4 of each swept solid.
- **Thread:** `AddNewThreadWithRef(face, limit)`, then
  `AddNewThreadWithOutRef` and its `LateralFaceElement` and
  `LimitFaceElement`. The ISO table (`CreateStandardThreadDesignTable`,
  thick or thin pitch, `CatThreadStandard` from the type library, else the
  Hole's values read live) first; a table that changes a value is removed
  (the relations it added) for "No Standard". An update that fails is tried
  once with `ReverseDirection`.
- **User pattern:** as many instances as points (V5's recipe), then with
  `AnchorPoint` the seed's position, then counting the seed. Live on R428
  the interactive tool saw a user pattern of a Hole refused on update; then
  the holes are one Hole each, as before, and the positions sketch is
  hidden, not deleted.
- **Symmetry:** `AddNewSymmetry2(plane)`, then `AddNewSymmetry2` of nothing
  with its `HybridShape.Reference` set after.

## Live checks

The m10 fixtures in `tests/fixtures/trees/` are the live cases:

| Fixture | Feature | Expected (mm³) |
|---|---|---|
| `m10_draft_two_sides` | 40×30×20 block, two opposite faces drafted 3° about its bottom | 23371.1066 |
| `m10_draft_four_sides` | the same block, four sides at 5° (a prismatoid) | 21631.9629 |
| `m10_pad_up_to_next` | a C bracket, a Ø8 column padded up to next (to the plate) | 29005.3096 |
| `m10_pocket_up_to_last` | the bracket, a Ø6 pocket from above up to last | 27717.2567 |
| `m10_pad_up_to_plane` | a plate, a 20×20 pad up to a tilted plane | 17200 |
| `m10_pad_up_to_face` | the bracket, a 20×20 pad up to the plate's underside | 36000 |
| `m10_tapped_holes` | a blind M6 tapped hole with a 118° V bottom, a through M8 | 17209.7968 |
| `m10_loft_pyramid_tip` | a 20×20 square lofted to an off-centre point | 2000 |
| `m10_loft_cone_join` | a Ø16 cone lofted onto a block | 10005.3096 |

The m11 fixtures are the second slice's (not run live yet):

| Fixture | Feature | Expected (mm³) |
|---|---|---|
| `m11_move_in_place` | an L block turned 90° about Z and moved 50 mm in X, then drilled where it now is | 3499.4690 |
| `m11_mirror_in_place` | the L block mirrored in place about the plane x + y = 40 | 3600 (centre 30.8333, 28.3333, 4) |
| `m11_shaft_thread` | a cosmetic M10 thread, 15 mm from the end of a stepped shaft | 4366.8138 (unchanged) |
| `m11_user_pattern_holes` | four Ø6 through holes at no grid: a Hole and a user pattern | 22869.0266 |
| `m11_helix_spring` | a Ø2 wire, 2.5 turns of radius 10, pitch 4 | 493.4802 (centre 0, 1.2764, 5) |
| `m11_helix_thread_cut` | a left-hand 60° groove, 9.5 turns of pitch 1.5, cut in a Ø12 shaft | 3266.4683 |
| `m11_helix_tapered_spring` | a Ø1.6 wire on a helix widening 8° | 414.8661 (build123d only) |

`python scripts/probe_part_design.py --report part_design_probe.json` reads
the enums above (and the thread's, the helix's and the transformations'
signatures) from the type library, then builds every fixture with the
tree command, each in a new part, and writes one JSON report with every
build's notes (which way each feature was built). One fixture at a time:
`python -m catia_mcp.tree build tests\fixtures\trees\m10_draft_two_sides.json --report draft.json`.

### First live run (3DEXPERIENCE R428, French, student role)

| Fixture | Result | Change made |
|---|---|---|
| `m10_pad_up_to_next`, `m10_pocket_up_to_last`, `m10_pad_up_to_plane`, `m10_pad_up_to_face` | Exact, first mode tried each time | None |
| `m10_loft_cone_join` | Exact, as a Shaft: the loft to a point was refused | None |
| `m10_loft_pyramid_tip` | Refused: the point section failed to update one way, could not be added the other | A pyramid (a section of lines) is now a Close Surface of its filled faces, checked by volume and centre |
| `m10_draft_two_sides`, `m10_draft_four_sides` | `AddNewDraft` failed (E_FAIL) with both parting elements | The body is now made the in-work object first, as for every dress-up, and a third route adds every face after an empty first one |
| `m10_tapped_holes` | `HoleThreadStandard` cannot be set; the Hole left threaded with no thread broke the final update | The standard is loaded with `CreateStandardThreadDesignTable` when the property is read-only; "No Standard" keeps the default; a refused thread sets the Hole back to smooth |

The enums read live: `CatLimitMode` (0 to 6, as in the V5 help),
`CatHoleThreadingMode` (threaded 0, smooth 1), `CatHoleBottomType` (flat 0,
V 1, trimmed 2), `CatDraftMode`, `CatDraftNeutralPropagationMode` and
`CatDraftMultiselectionMode` (0 first). `CatHoleThreadStandard` and
`CatDraftPartingMode` were not found.

Second run, after those changes:

- `m10_loft_pyramid_tip`: exact (2000 mm3), as a Close Surface of its faces.
- `m10_tapped_holes`: the blind M6 threaded live ("Metric Thick Pitch",
  loaded from its table). The through M8 failed: the M8 table locked the
  Hole's diameter away from the tree's 6.8 mm tap drill, and the hole was
  rebuilt as cut cylinders. A table that does not fit is now removed (the
  relations it added) and the tap drill set again, before "No Standard".
- Drafts: still refused, with every route above. A fourth route sets the
  neutral element on the draft's domain, and the probe now reads the exact
  signature of `AddNewDraft` from the type library.

Third run:

- Drafts: both exact (23371.1066 and 21631.9629 mm3) through the fourth
  route: `AddNewDraft` with an empty face, neutral and parting element, then
  the neutral element and the faces set on its domain. The type library
  confirms the documented signature (ten arguments).
- `m10_loft_cone_join`: exact (10005.3096 mm3) as a Shaft when run on its
  own; in the probe it stopped in 0.03 s with no report, so the probe now
  keeps the command's output of a failed build.
- `m10_tapped_holes`: the M8 still fell back to cut cylinders. The second
  Hole came out threaded to the first one's standard, its diameter locked,
  and setting 6.8 mm failed. Every new Hole is now set smooth before its
  diameter. `CatHoleThreadStandard` read live: thin pitch 0, thick pitch 1.

Fifth run: all nine m10 fixtures built in one probe, each exact in volume;
both tapped holes are threaded Hole features ("Metric Thick Pitch", M6 and
M8, at the tree's tap drills).

## Not done yet

- Stiffener: no build123d operation, so no tree carries one.
- Helix paths mixed with lines or arcs (a spring with straight ends), and
  sweeps whose profile is not Frenet (`frenet: false`).
- Patterns of other features than holes (`pattern` on an extrude), and user
  patterns of holes in several planes.
- Sew Surface (a surface sewn onto a body), Axis to axis.
- Internal cosmetic threads (taps) on an existing bore: a tapped `hole`
  covers the usual case.
- Drafts with a parting element, reflect-line and variable drafts; drafts of
  faces CATIA can only reach after a fillet.
- Extrudes up to a limit in another body (`until_target`), tapered or
  symmetric; up to a non-planar face.
