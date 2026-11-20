# What the bridge covers of CATIA

The bridge rebuilds what a build123d model expresses: parts as native,
editable features with their parameters, and assemblies. The measure is
therefore what build123d can carry, not every CATIA workbench. Everything
marked live was validated on 3DEXPERIENCE R2026x (R428 SP6, French UI,
student role); the CATIA V5 routes have only run against the test fake.

| Workbench | Covered | Live on R428 | Missing (why) | Details |
|---|---|---|---|---|
| Part Design | 26 of 43 features | all 26 | 7 need a tree field from the app (helix, guided loft, variable fillet, shaft thread, user pattern, move in place, sew surface); 10 have no build123d operation (stiffener, multi-pad, Solid Combine, Remove/Replace Face, variable drafts...) | [part-design.md](part-design.md) |
| Sketcher | lines, circles, arcs, splines; fully constrained rectangles, circles, slots, polygons, stepped profiles | yes | constraints on free-form sketches | [feature-tree.md](feature-tree.md) |
| Generative Shape Design | 25 of 33 tools: points, lines, named planes, 3D circles, helices, splines, polylines, projection, intersection, extrude, revolve, sphere, cylinder, loft (ruled through 3+), sweep (along 3D guides), fill, offset, join, split, trim, extract, surface transforms, thick surface, close surface | 14 tools, 7 fixtures exact; the second slice (8 `s7_*` fixtures) not yet | blend, shape fillets, smooth or guided multi-sections (no build123d reference); spiral, healing, boundary, extrapolate, near, sew | [surfaces.md](surfaces.md) |
| Knowledgeware | typed parameters with comments, formulas, parameter sets, mass formula | yes | design tables (E_NOTIMPL on R428), rules and checks (no Knowledge Advisor licence) | [materials-properties.md](materials-properties.md) |
| Materials, appearance, properties | colours, BOM properties, PLM attributes, material (V5 catalog) | yes, the material as parameters | a 3DEXPERIENCE Core Material (no service reachable) | [materials-properties.md](materials-properties.md) |
| Assembly Design | STEP structure, native parts placed exactly, inferred constraints | positions yes, constraints no | constraints on 3DEXPERIENCE (no impacteds through COM: reported for doing by hand); V5 constraints fake-tested | [assemblies.md](assemblies.md), [assembly-constraints.md](assembly-constraints.md) |
| Generative Drafting | views, sections, details, ISO scale, generated dimensions, title block, BOM, balloons | yes (bracket) | auxiliary, broken, breakout views; centre lines; GD&T; roughness; multi-sheet; PDF/DXF on 3DEXPERIENCE | [drafting.md](drafting.md) |

Not covered, and why:

| Workbench | Why |
|---|---|
| Sheet Metal | build123d has no sheet metal; a sheet arrives as a solid |
| FreeStyle, Imagine & Shape, ICEM (class A) | build123d makes B-splines, not styling surfaces |
| Functional Tolerancing & Annotation (3D GD&T) | CATIA can, the tree carries no tolerance: a field asked of the app |
| DMU Kinematics, mechanisms | not in build123d; and on R428 engineering connections take no impacteds through COM |
| FEM / SIMULIA, machining, mould, weld, electrical, piping, rendering | outside a build123d geometric model |

The fields the app can already send, and those still to agree, are listed in
each page's "Contract additions requested from the app".
