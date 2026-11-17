# Part Design in the tree replay

What the feature tree replay (`docs/feature-tree.md`) covers of CATIA's Part
Design workbench, and the features added after the first milestones: drafts,
extrudes up to a limit, tapped and V-bottomed holes, and lofts to a point.
They are built as a designer would model them: one native feature each,
with its parameters editable.

None of the additions has run on a live CATIA yet. The offline tests drive
them through a fake CATIA (`tests/test_part_design.py`), and the build123d
reference agrees with each fixture's closed form to 1e-9.
`scripts/probe_part_design.py` runs them live; see [Live checks](#live-checks).

## Coverage

Status: **done** (in `main`), **this PR** (added here, not validated live),
**contract** (CATIA can, but the tree has no field for it yet), **no
build123d** (build123d has no matching operation, so no tree can carry it).

| CATIA Part Design feature | Status | Tree feature / note |
|---|---|---|
| Pad | done | `extrude` (`new_body`, `join`), `direction`, `symmetric` |
| Pad up to next / last / plane / surface | this PR | `extrude` with `until` |
| Pocket | done | `extrude` `cut` |
| Pocket up to next / last / plane / surface | this PR | `extrude` `cut` with `until` |
| Drafted filleted pad / pocket | no build123d | build123d tapers (`extrude(taper=...)`, built as lofts) and fillets apart; the tree carries both features |
| Multi-pad / multi-pocket | no build123d | one length per extrude; a sketch of several profiles is one Pad |
| Shaft | done | `revolve` `new_body` / `join` |
| Groove | done | `revolve` `cut` |
| Hole: simple, counterbored, countersunk | done | `hole` `kind` |
| Hole: tapped (thread, designation, depth, pitch) | this PR | `hole` `thread` |
| Hole: V bottom | this PR | `hole` `bottom` |
| Hole: tapered, counterdrilled | no build123d | |
| Rib | done | `sweep` along a planar path |
| Rib along a helix (threads, springs) | contract | `sweep` paths are lines and arcs; needs a helix path entity |
| Slot (removed rib) | done | `sweep` `cut` |
| Stiffener | no build123d | build123d has no stiffener; a tree would carry it as an extrude |
| Multi-sections Solid | done | `loft` between two sections, ruled through more |
| Multi-sections Solid to a point | this PR | `loft` with a point section |
| Removed Multi-sections Solid | done | `loft` `cut` |
| Multi-sections Solid, smooth through 3+ sections, with guides or spines | contract | OpenCascade's smooth loft is not CATIA's; needs guide curves in the tree |
| Solid Combine | no build123d | |
| Edge Fillet, constant radius | done | `fillet` |
| Variable radius fillet | contract | build123d has none on edges; refused |
| Face-face fillet, tritangent fillet | no build123d | |
| Chamfer (two lengths, length and angle) | done | `chamfer` |
| Draft angle (basic) | this PR | `draft` |
| Draft reflect line, variable angle draft | no build123d | |
| Shell | done | `shell` with removed faces |
| Thickness | done | `shell` without removed faces (build123d's `offset`) |
| Thread / Tap (cosmetic, on a cylindrical face) | contract | a tapped `hole` is covered; a thread on a shaft needs the face and the designation |
| Remove Face / Replace Face | no build123d | |
| Split | done | `split` by a plane |
| Thick Surface, Close Surface, Sew Surface | no build123d | surfaces do not reach the tree |
| Translation, Rotation | done | `copy` (a body copied, then moved) |
| Symmetry, Axis to axis | contract | a body moved in place has no tree feature yet; `copy` does the moved copy |
| Mirror | done | `mirror` joined back |
| Rectangular pattern, circular pattern | done | of holes (`hole` frames); patterns of other features reach the tree unrolled |
| User pattern | contract | holes at frames that form no grid or circle are one Hole each; a user pattern needs the positions sketch named in the tree |
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

## How each is built

| Tree | CATIA | Checked by |
|---|---|---|
| `draft` | One Draft (`AddNewDraft`): the matched faces in its first domain, the neutral plane (an origin plane or a construction plane in `Tree_Construction`) as neutral element, pulled along its normal, the angle on the domain's `DraftAngle` (a user parameter when the script gave it). The parting element is none, else the neutral plane | build123d's body after the draft (human style); else the first-order estimate, each face turning about its line on the neutral plane: `−A·h·tan(angle)`, exact for faces that share no edge, 3 % off on a box drafted on four sides at 5° |
| `extrude` `until` | A Pad or Pocket straight into its body (never a tool body: up to next needs the body's material), `FirstLimit.LimitMode` up to next, last, plane or surface, `LimitingElement` the plane or the matched face | build123d's body after the feature; without build123d, only the sign of the volume change |
| `hole` `thread` | The Hole as before, then `ThreadingMode` threaded, `HoleThreadStandard` "Metric Thick Pitch" (or "Metric Thin Pitch") with `HoleThreadDescription` the designation, `ThreadDepth`, `ThreadSide` | the hole's volume, as for any hole |
| `hole` `bottom` `v` | `BottomType` V bottom, `BottomAngle` | the hole's volume |
| `loft` to a point | A Multi-sections Solid from the sketch to a construction point, in its own body | the cone's closed form: volume `A·h/3`, centre of gravity a quarter of the way from the section's centroid to the apex |

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

`python scripts/probe_part_design.py --report part_design_probe.json` reads
the enums above from the type library, then builds every fixture with the
tree command, each in a new part, and writes one JSON report with every
build's notes (which way each feature was built). One fixture at a time:
`python -m catia_mcp.tree build tests\fixtures\trees\m10_draft_two_sides.json --report draft.json`.

## Not done yet

- Stiffener: no build123d operation, so no tree carries one.
- Helix paths for ribs and slots (springs, modelled threads): the `sweep`
  path would need a helix entity (build123d's `Helix`), drawn as a GSD
  helix in `Tree_Construction`.
- Drafts with a parting element, reflect-line and variable drafts; drafts of
  faces CATIA can only reach after a fillet.
- Extrudes up to a limit in another body (`until_target`), tapered or
  symmetric; up to a non-planar face.
