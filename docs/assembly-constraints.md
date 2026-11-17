# Assembly constraints

An assembly rebuilt from the model's STEP file has every instance where the
STEP file puts it, by its position alone. A designer would rather ground one
component and position the others by constraints on their faces: a pin's
axis on its hole's, a box standing on its base, an offset from an edge.
`--constraints` adds those constraints to the product, without moving
anything.

```bash
python -m catia_mcp.tree mates MODEL.tree.json --step MODEL.step         # offline, no CATIA
python -m catia_mcp.tree build MODEL.tree.json --assembly product --step MODEL.step --constraints infer   # V5
python -m catia_mcp.tree build MODEL.tree.json --assembly adopt --step MODEL.step --constraints infer     # 3DEXPERIENCE
```

`--constraints none` (the default) changes nothing. `fix` fixes every
instance where it is. `infer` finds the constraints below. `--ground ID`
chooses the grounded instance (default: the largest by volume, the first
among equals).

## Inference (`catia_mcp/tree/mates.py`)

Offline, from each instance's faces in world coordinates: the STEP file's
shapes (each part's faces read once, moved to every instance), or without
`--step` the tree's build123d reference. Three kinds of pairs between two
components count:

- **Coincidence** of axes: two cylindrical faces on one axis line whose
  lengths along it overlap (a pin in a hole, a bolt in a bore) or touch (two
  holes in line, plate on plate).
- **Contact**: two planar faces in one plane, facing each other, whose areas
  overlap (estimated on a 32 x 32 grid of their common box; holes in a face
  count).
- **Offset**: two parallel planar faces, the distance an editable value
  (flush faces: 0), orientation `same` or `opposite`.
- **Angle** between two planar faces, used only when nothing else stops a
  rotation.

Each candidate is written as linear conditions on the component's small
motion (3 rotations, 3 translations); their rank says how many freedoms are
gone. Components are taken in order against those already placed:

1. the grounded component is fixed;
2. a component is given the best candidates that each remove a freedom
   (coincidences, then contacts, then offsets, then angles; then by overlap,
   distance and face number), until none is left; the set is then pruned
   to a minimal one;
3. components held by touching faces alone (coincidences, contacts) are
   placed before those that need offsets, as a designer assembles them;
4. a component left free only to spin about its own axis of symmetry (a
   pin, a bolt: every curved face on one axis, every plane square to it)
   counts as placed (`spin_free`), as a designer leaves it;
5. a component nothing holds is fixed in space instead, and the report says
   how many freedoms the constraints found left (`dof_left`). Others may then
   be placed against it.

Faces are numbered `F1`, `F2`... in the order OpenCascade explores the
part's shape; the result is deterministic.

What the fixtures give (`tests/fixtures/assembly`, made by
`scripts/make_mate_fixtures.py`):

| Fixture | Result |
|---|---|
| `bracket` | Plate grounded; both bolts fixed (one stands on the plate: 3 freedoms left; the other floats) |
| `pin_plate` | Plate grounded; pin: coincidence of axes + contact of its head, free to spin |
| `box_base` | Base grounded; box (a quarter turn): contact + offsets of 5 and 25 mm |
| `bolted_plates` | Lower plate grounded; upper plate (the same part, half a turn): two hole coincidences + contact; each bolt: coincidence + head contact, free to spin |

## In CATIA (`catia_mcp/tree/assembly_constraints.py`)

1. **Reference elements**, as a designer makes them: in each part a
   geometrical set "Assembly references" with an axis line (`Axis F4`, a
   point and a direction) per cylindrical face used and a plane (`Plane F3`,
   by equation) per planar face used, in the part's own frame, each
   published under its own name. A part placed twice gets them once.
2. **Constraints** in the root product's `Connections("CATIAConstraints")`:
   - fix: `AddMonoEltCst(catCstTypeReference, Root/Inst.1/!Root/Inst.1/)`;
   - coincidence: `AddBiEltCst(catCstTypeOn, ...)` between the two axes;
   - contact: `catCstTypeSurfContact` (20), else `catCstTypeOn` with
     orientation opposite, else an offset of 0;
   - offset: `catCstTypeDistance`, `Dimension.Value` and `Orientation`;
   - angle: `catCstTypeAngle` and its value.

   Elements are reached by name, `Root/Inst.1/!Plane F3` (the publication,
   which has the element's name).
3. **Checks**, after each component: the product is updated, every
   instance's position read back (1e-4) and every new constraint's `Status`
   read. A constraint that moves anything or is not resolved is removed, the
   position put back, and the component fixed instead (`fallback` in the
   report). A failure never leaves a part elsewhere than the STEP file puts
   it.

The report's `assembly.constraints` lists, per component, what was planned,
what CATIA took (type, name, route, value, orientation, status), the
fallbacks, the publications and `positions_ok`.

CatConstraintType values used: 0 reference (fix), 1 distance, 2 on, 6
angle (the four already used by `catia_mcp/tools/assembly.py`), 20 surface
contact (from the V5 documentation, to confirm: the probe reads the whole
enum from the installation's type library).

## 3DEXPERIENCE

A designer uses engineering connections there, each holding mechanical
constraints. The documented automation API does not say how to create one.
`--assembly adopt --constraints infer` tries, in order, the routes below and
uses the first object that has `AddMonoEltCst` and `AddBiEltCst`; if none
does, the report says so and the imported product stays as it is (every
instance already where the STEP file puts it):

1. the root occurrence's `Connections("CATIAConstraints")`;
2. the root reference's `GetItem("CATEngConnections")` (where
   `catia_list_constraints` reads engineering connections);
3. the root reference's `GetItem("CATIAConstraints")`;
4. the root reference's `Connections("CATIAConstraints")`;
5. the editor's active object's `Connections("CATIAConstraints")`.

Elements are published on the part's reference, else the Part; references
are made by name from the root occurrence, else its reference, with the
root's name in the path or not. Every variant tried is recorded.

**Not validated live.** `scripts/probe_constraints.py` records what a
session allows on the imported product: the constraint enums, the members of
the root, the instance, its reference and its Part, every route above, the
engineering connections already there (create one by hand first to see its
object model), with `--create` a published plane and a Fix (both removed
again), and with `--apply fix|infer|contact` the build's own code (left in
place: close without saving).

### First live probe (3DEXPERIENCE R428, French, student role, bracket imported)

- The three instances are matched by name, position and shared reference.
- None of the five routes has `AddMonoEltCst` / `AddBiEltCst`. Route 2
  answers: `GetItem("CATEngConnections")` gives an `EngConnections`
  collection (`Add`, `AddWithContext`, `Count`, `Item`, `Remove`), empty on
  an imported STEP. That is the engineering connections' API; how `Add` is
  called is not known yet.
- The `CatConstraint*` enums are not in the type libraries reached.
- `Publications` exists on the `VPMReference` of the product and of each
  part, not on the `Part` object.

Second live probe (the bracket's window named with `--window`): the
engineering connections' API is in the type library.

- `EngConnections.Add(iType As CatEngConnectionType, iImpacteds As array)`
  and `AddWithContext(iType, iImpacteds, iReferenceContext, iName)`;
  `Remove(iEngCnt)`.
- `CatEngConnectionType`: cylindrical 0, free 1, fix 2, planar 3, point-curve
  4, point-surface 5, prismatic 6, projection 7, revolute 8, roll-curve 9,
  rigid 10, slide-curve 11, spherical 12, symmetry 13, universal 14,
  user-defined 15, gear 16, rack 17, cable 18, screw 19.
- `CatAssemblyConstraintType` (63 values), among them fix instance 30,
  coincidence line-line 5, plane-plane 7, contact plane-plane 18,
  cylinder-cylinder 16, distance plane-plane 24, angle plane-plane 2;
  `CatAssemblyConstraintOption` (orientation: undefined 0, same 1,
  opposite 2); `CatAssemblyConstraintMode` (driving 0, measured 1).
- A part reference's `Publications` (`VPMPublications`) only reads
  (`GetItem`, `Item`).

The next probe creates a Fix, a Rigid and a Cylindrical connection (on
instances, else occurrences), describes each and the objects it holds (its
constraints collection), and removes it.

The probe's `engcnx` step now reads the signatures of `EngConnections`, the
enums whose names mention connections or constraints, and the publications
of a part reference; with `--engcnx-create` it tries `Add` with a few
candidate arguments, describes what each makes, and removes it.

## Assembly Design coverage

| Capability | Status |
|---|---|
| Fix | V5: `--constraints fix`/`infer` (fake-tested). 3DX: probed |
| Coincidence (axes) | V5: inferred and applied (fake-tested). 3DX: probed |
| Coincidence (planes) | Used as the fallback of a refused contact |
| Contact (planar) | V5: inferred and applied, SurfContact then On then offset 0. 3DX: probed |
| Offset | V5: inferred (value, orientation) and applied. 3DX: probed |
| Angle | Inferred only when nothing else stops a rotation; applied as catCstTypeAngle |
| Parallelism, perpendicularity | Not inferred (an offset or an angle of 90 covers them); types 8 and 11 in `CST_TYPES` |
| Fix together | Not done: no documented automation call |
| Quick constraint | Not applicable: interactive; the inference plays its role |
| Reuse pattern | Not done: instances are placed one by one |
| Flexible / rigid sub-assembly | Not done: sub-assemblies are rigid (V5 default) |
| Explode | Not done |
| Clash analysis | Not done (a natural next check: no interference between placed parts) |
| BOM | Not done by the bridge (CATIA's own Bill of Material works on the product) |

## Contract additions requested from the app

None required: the STEP file of the same compile gives the faces. Optional,
for later: the app could send the constraints the model's author meant
(`mates: [{kind, component, face, other, other_face, value}]`, faces named
by the app's topology), which the bridge would use instead of inferring.

## Limits

- Inference sees planes and cylinders only (cones, tori, spheres mark an
  axis of symmetry; free-form faces block it).
- A tilted part with no parallel faces may be fixed instead of constrained.
- Reference elements are explicit (by coordinates), not linked to the
  faces: editing a hole's position does not move its axis.
- V5 has only run against a fake CATIA; 3DEXPERIENCE only through the probe.
