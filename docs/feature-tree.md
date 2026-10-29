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
  - **Report:** written to `tree_report.json`, in the same shape as Fab's Send to Fusion report.
  - **Prefixes:** `--part ID --until FEATURE_ID` builds a prefix of one part, for development.

Keep the new part's window active during a build. The build stops if the
active window changes, because 3DEXPERIENCE measures and searches only in the
active editor.

## Supported features (milestones 1 and 2)

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

Everything else is refused by `check`, for now: fillet, chamfer, shell, loft,
sweep, and tapered or oblique extrudes.

## How the build stays faithful

- **Sketch frames:** sketch points are projected into the frame CATIA actually gives the sketch, read back with `GetAbsoluteAxisData`. A tree plane whose normal is the reverse of a CATIA origin plane's, such as `-XZ` (normal -Y), is drawn on that origin plane and padded the other way.
- **Pad checks:** every pad is checked in a body of its own, against the profile's area and centroid (volume, centre of gravity). It is flipped once if CATIA extruded it the other way, and anything else stops the build.
- **Revolve checks:** every revolve is checked by Pappus. The volume comes from the profile's first moment about the axis, and the centre of gravity, for a partial angle, also tells the turning direction. If CATIA turned the other way, the angle is moved to the profile's other side once.
- **Mirror and split checks:** a mirror must leave the centre of gravity on its plane. A split must leave it on the kept side, and is flipped once otherwise.
- **Combine checks:** a combine is checked against what its operation can give.
- **No retries:** nothing is retried. The first failure stops the build with its part and feature, and the document may then hold a partial model.

## Live status

Not yet run against CATIA. `tests/fixtures/trees/` holds synthetic trees for
each path.
- **Milestone 1** (`m1_*`): an offset plane, a reversed-normal plane, arcs with a negative distance, join, cut and intersect, the three hole kinds, and a tilted plane (the equation-plane path).
- **Milestone 2** (`m2_*`):
  - revolves about an axis off the sketch origin (3D line), about the sketch's V axis, and about its H axis with a partial cut;
  - mirrors about an origin plane and an offset plane;
  - splits along a normal and on a tilted plane;
  - a countersink.

The build123d reference reproduces Fab's sample models exactly on the parts
milestones 1 and 2 cover: sample 1's Shaft (2764.6015 mm3) and sample 2's
Housing, split included (68131.7169 mm3).
