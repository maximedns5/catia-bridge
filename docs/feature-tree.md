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
python -m catia_mcp.tree capabilities [--create] [--risky 3dpart]
```

`build` first probes what this CATIA can do and refuses a tree that needs
something found unavailable; `capabilities` runs that probe on its own
([capabilities.md](capabilities.md)).

- **`check`:** the preflight only, with no CATIA. If any feature is unsupported or malformed, the whole tree is refused. Each reason is listed once, with its count and model lines.
- **`reference`:** the expected per-part volume, area and centre of gravity, built with build123d (`pip install -e ".[differential]"`).
- **`build`:** builds the tree in CATIA. It attaches to a running CATIA and never starts one: on 3DEXPERIENCE a fresh start needs an interactive login. Each part goes into a new part, a 3D Shape by default on 3DEXPERIENCE. Nothing is saved.
  - **Checks:** each part's volume is compared with build123d's when build123d is installed. Editability is proved by lengthening the first pad by 1 mm, checking that the volume moves, and restoring it.
  - **Report:** written to `tree_report.json`, with `success`, `native_history`, `editable`, `parts_detail` and `error`.
  - **Prefixes:** `--part ID --until FEATURE_ID` builds a prefix of one part, for development.
  - **Topology filter:** `--ownership auto|strict|off` (or `CATIA_MCP_TOPOLOGY_OWNERSHIP`) sets the ownership filter fillets, chamfers and shells use (see below). `auto` is the default.

Keep the new part's window active during a build. The build stops if the
active window changes, because 3DEXPERIENCE measures and searches only in the
active editor.

## Supported features (milestones 1 to 5)

| Tree feature | CATIA |
|--------------|-------|
| `sketch` (`line`, `circle`, 3-point `arc`) | A sketch on the origin plane, or on an offset or equation plane in a `Tree_Construction` geometrical set |
| `extrude` `new_body` | Pad in the part's main body, then in new bodies |
| `extrude` `join` / `cut` / `intersect` | Pad in a tool body, then Add / Remove / Intersect into the target |
| `extrude` with `taper` (degrees, build123d's sign: positive narrows the solid), one-sided or symmetric, along the normal or a `direction` | One Multi-sections Solid per loop, from the loop to its 2D offset, as build123d builds it: inner loops get the opposite offset (holes widen when the outer loop shrinks) and are removed, the pieces are joined |
| `extrude` with `direction` (the full vector, as build123d's `amount` along `dir`; a `distance`, if given, must be its length or its height) | Pad along a 3D line in `Tree_Construction` (`SetDirection`), symmetric or not |
| `revolve` (any axis in the sketch plane, any angle up to 360) | Shaft in its own body, then combined like an extrude. The axis is the CATIA sketch's own H or V axis when the tree's axis is one of them, otherwise a 3D line in `Tree_Construction` |
| `combine` | Add / Remove / Intersect |
| `hole` `simple` / `counterbore` | Cylinders cut as the contract's hole profiles: through holes span ±`effective_depth`/2, and a counterbore clears `effective_depth` above |
| `hole` `countersink` | A revolved profile cut, as build123d's `CounterSinkHole` builds it: the hole, a cone of the given angle, and clearance above |
| `mirror` followed by a `combine` `join` of its copy into its source | One CATIA Mirror of the source body. Any other use of a mirrored body is refused |
| `split` (`keep` `top` / `bottom`) | Split by the plane, keeping the side the tree keeps |
| `loft` between two sections in parallel planes (one profile each, no holes), every operation | Multi-sections Solid in its own body. Each section is redrawn with the vertices build123d pairs the sections by, coupled vertex to vertex from matching closing points (GSD points in `Tree_Construction`) |
| `sweep` of one profile along a planar, tangent-continuous path of lines and arcs, the profile perpendicular to the path where it starts, every operation | Rib in its own body, along the path drawn as a sketch in its own plane |
| `fillet` with a constant radius on line and circle edges | One Edge Fillet (`AddNewSolidEdgeFilletWithConstantRadius`, tangency propagation) on the matched edges, one edge per tangent chain |
| `chamfer` with one or two distances on line and circle edges | One Chamfer (`AddNewChamfer`, two-length mode, tangency propagation). With two distances, `distance` goes on the plane `face`, which must hold every edge |
| `shell` with at least one removed face, inside or outside, `Kind.ARC` or `Kind.INTERSECTION` | One Shell (`AddNewShell`, then `AddFaceToRemove`), the thickness inside (`thickness` < 0) or outside |

Everything else is refused by `check`, for now: variable-radius fillets,
fillets and chamfers on edges other than lines and circles, chamfers given by
an angle, asymmetric chamfers without a plane reference face holding their
edges or with three edges meeting at a corner, shells with no removed face
(build123d's `offset` without openings shrinks or grows the solid rather than
hollowing it), lofts through more than two sections, to a point, between
sections that are not parallel or with correspondence rails, Frenet sweeps,
sweeps along 3D paths or paths with corners, tapers whose offset changes the
profile's topology (a loop collapsing or crossing itself or another) or that
meet an arc at a corner without tangency, and parts placed by a parent
location (`part.placement`). For those, `catia_mcp/tree/placement.py` is ready
to move every 3D coordinate of a part's features into the world (sketch
planes, revolve axes, sweep paths, hole frames, mirror and split planes, and
any anchor point or direction), but it is not used until the contract says
whether a placed part's features are in its own frame or already in world
coordinates.

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
- **Taper checks:** build123d tapers by lofting each loop to its offset by `-|vector|·tan(taper)` (sharp corners between lines, concentric arcs), the opposite offset for inner loops. `catia_mcp/tree/extrude.py` builds the same offsets with each vertex paired with its own offset, so every section along the extrude is an exact offset and its area is quadratic: the volume is the prismatoid `h/6·(A0 + 4·Am + A1)` (on the `m5_*` fixtures, build123d matches the closed forms to 1e-9, and the plan's centre of gravity to 1e-5 mm). Each loft is checked as a loft, and the whole extrude against the prismatoid's volume and centre of gravity. A Pad and a Draft would keep the taper angle editable, but need a reference to every side face and a pulling direction whose sign CATIA's API does not document, and build123d itself uses this loft whenever the profile has holes or the taper is negative.
- **Oblique checks:** the Pad must give the profile's area times the height (`|direction·normal|`) and a centre of gravity half the vector away. If the first volume shows CATIA measured the length along the normal rather than the line, the length becomes the height, once; a reversed Pad is flipped once.
- **Sweep checks:** a sweep is checked by Pappus: the profile's area times the length its centroid travels along the path.
- **Combine checks:** a combine is checked against what its operation can give.
- **Fillets, chamfers and shells** (`catia_mcp/tree/anchors.py`, `catia_mcp/tree/dressup.py`): the tree names edges and faces by geometry, as they are when the feature applies, never by index.
  - **Enumeration:** the body's edges or faces come from the localized topology search (`Topologie.Arête,sel` in French). On a body with booleans that search also returns the cells of the tool bodies nested under them: after a Remove, the tool body's own edges come back with the final ones, and a tool edge lying on the final shape (the floor of a blind pocket) then matches an anchor twice.
  - **Ownership filter:** each cell's `Reference.DisplayName` ends with the result it belongs to (`...;Remove.1_ResultOUT;Z0;G...)`), and only the cells of the body's last feature are kept. `auto` keeps every cell, with a note, when no name carries a context or none names the last feature; `strict` stops the build then; `off` keeps everything. The report records, per search, the cells found and kept and the contexts seen.
  - **Matching** (the contract's rules): an edge's length must agree within max(1e-4 mm, 1e-6 L) and every sample point lie within max(1e-4 mm, 1e-5 L) of it; the closest fit wins. A face's area must agree within max(1e-3 mm², 1e-5 A), then the nearest centroid wins; it must also lie within max(0.01 mm, 1e-3 √A) of the anchor's, so a missing top face is never replaced by the bottom one. Two cells fitting equally well (within 1e-9), or none, stop the build with the anchor. Edges are measured by their length and start, middle and end points (`GetPointsOnCurve`) and rebuilt as a line or the arc through the three points.
  - **References:** each cell is referenced by `Part.CreateReferenceFromBRepName` with its DisplayName turned into a label (`Selection_` and the result context dropped, `;WithPermanentBody;WithoutBuildError;WithSelectingFeatureSupport;MFBRepVersion_CXR15)` appended), in the context of the body's last feature. The resolved reference must measure as the anchor says; otherwise the selection's own reference is used, with a note.
  - **One feature:** a tree fillet or chamfer is one CATIA feature, since corners are solved jointly. With tangency propagation, which OpenCascade also applies, one edge per tangent chain is given.
  - **Checks:** the volume must change, within bounds (a fillet removes or adds at most 3 r² per mm of its chains' edges, a chamfer d1·d2/2; an inside shell leaves at most its area times the thickness). The strict check is the per-part volume against build123d, which applies the same anchors with the same matching.
  - **Asymmetric chamfers:** on straight edges both orientations give the same volume. The chamfer is built both ways round and keeps the one whose centre of gravity, along the reference face's normal, sits where `distance` on that face puts it: lower when `distance2` < `distance`, as the cut (or added) prism then lies nearer that face. The part's centre of gravity is then also compared with build123d's.
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
- **Milestone 3** (`m3_*`): a fillet of a block's vertical edges, a chamfer around a rounded top loop (lines and arcs, one tangent chain), a fillet of a blind pocket's floor (the tool body's edges coincide with the pocket's), an asymmetric chamfer, and shells open at the top and at both ends. `tests/test_dressups.py` runs them against a fake CATIA whose topology search returns the tool bodies' cells after a boolean, as CATIA's does.
- **Milestone 4** (`m4_*`):
  - lofts: a frustum, a rectangle prismatoid whose second section starts at another corner and runs the other way, a circle-to-square loft joined onto a box, and a loft cut;
  - sweeps: a pipe along a line and a tangent arc, an off-centre profile along a line, an arc and a line, and a half-buried groove cut.
- **Milestone 5** (`m5_*`):
  - tapers: a rectangle, a ring joined onto a block (its hole widens), a symmetric slot on a tilted plane with a negative taper, a tapered pocket cut downwards, and an oblique taper on the YZ plane;
  - oblique extrudes: a rectangle with a hole, and a symmetric cut whose distance is the direction's height;
  - a block placed by a rotation and a translation, with a split and a through hole. Tests also place eleven earlier fixtures and check that the reference volume stays and its centre of gravity moves with the placement.

The build123d reference reproduces the source build123d models exactly on
the parts these milestones cover, such as a revolved shaft, a split housing,
a circle-to-square loft, a swept pipe, a plate with filleted corners and a
chamfered top, and an open-top shell.

Not yet validated live: the Multi-sections Solid calls (`AddNewLoft`,
`HybridShape.SectionCoupling` = 4 for vertex coupling, `AddSectionToLoft` with
a closing point and an orientation of 1 or -1), deleting a twisted loft
through the selection, `AddNewRib` with a path sketch as its centre
curve, `Prism.SetDirection` with a 3D line (and whether CATIA then measures
the Pad's length along the line or along the sketch normal), and lofts drawn
on sketches in equation planes for tilted parts.

Not yet validated live, for fillets, chamfers and shells:
- the cause of the tool body cells and the ownership filter: whether
  DisplayName carries a `<feature>_ResultOUT` context on 3DEXPERIENCE R428
  and V5, and whether it names the body's last feature as `Shapes.Item(Count).Name`
  spells it (French names included);
- `CreateReferenceFromBRepName` with labels made from DisplayName, and the
  `MFBRepVersion_CXR15` flag on R428;
- edge measures on 3DEXPERIENCE (`GetPointsOnCurve` on the curve measurable,
  the length unit) and face measures (area, `GetCOfG`);
- the enum values (tangency propagation, two-length mode, reverse orientation)
  when the type library cannot be read, `AddObjectToFillet`,
  `AddElementToChamfer`, `Chamfer.Orientation`, `AddFaceToRemove`;
- whether CATIA accepts two edges of one tangent chain, which the builder
  avoids;
- corners: three equal fillets at a vertex, mitred fillets, chamfer corners;
- outside shells and inside shells of parts with concave edges, where
  OpenCascade's `Kind.ARC` rounds the offset edges and CATIA may not (the
  per-part volume check would stop the build).

`scripts/probe_topology_ownership.py` settles the first two points and the
measures on a live session: it censuses the cells each search returns, with
their DisplayName, after a Remove and an Add (P1), round-trips a BRep name and
fillets the resolved edge (P2), and compares every searched edge with
build123d's edges of the same part (P3). It writes a JSON report.
