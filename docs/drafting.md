# Drafting: the drawing a designer would make

`python -m catia_mcp.tree build TREE.json --drawing auto` makes, after a
successful build, a drawing of each built part and, with `--assembly`, of the
product. The build stays a success whatever happens to the drawings; each
step is recorded in the report's `drawing` section.

```bat
python -m catia_mcp.tree build TREE.json --drawing auto --drawing-pdf out
python -m catia_mcp.tree build TREE.json --drawing auto --drawing-projection third
python -m catia_mcp.tree.drawing_plan TREE.json --report drawing_plan.json
python -m catia_mcp.tree.drawing_plan MODEL.tree.json --step MODEL.step
```

The last two commands plan the drawings offline, with no CATIA, and write
the plan as JSON.

## The plan (`catia_mcp/tree/drawing_plan.py`)

Pure Python, tested offline. Its inputs are the part in world coordinates
and its geometry: the bounding box and planar face areas of the build123d
reference, or, without build123d, a box estimated from the features
(sketches swept along their extrudes, turned about their revolve axes, loft
sections, sweep paths).

- **Main view.** The world axis the most features are seen along: 2 points
  per hole drilled along it, 3 per sketch drawn normal to it. Ties go to the
  largest planar faces, then the largest projected area. The viewer stands
  on the side the holes are drilled from. A part of revolution (every solid
  turned about one axis, or extruded from concentric circles on it) is seen
  across its axis, the axis horizontal. Otherwise Z stays up; seen along Z,
  the longest side is horizontal.
- **Projection method.** First angle (ISO E) by default, third angle (ISO A)
  with `--drawing-projection third`. The side view always goes right of the
  front view: the left view in first angle, the right view in third angle.
  The top view goes below the front view in first angle, above it in third.
- **Projected views.** Front, top and side for a block. One view for the
  thickness of a thin part (thickness at most a quarter of its smaller
  side), unless features are seen along both other axes. Front and end view
  for a part of revolution.
- **Section.** When the part has holes, cuts or a shell: a straight cutting
  line across the front view, through the most hole axes (or the revolution
  axis). It takes the slot of the projection it replaces and is labelled
  A-A. A part without internal features gets no section.
- **Detail.** When the smallest hole, fillet or chamfer seen in the front
  view is under 3 mm on paper: a circular detail B at a standard scale at
  least twice the sheet's, drawing the feature at least 8 mm big.
- **Isometric view.** Seen from the front, the right and above, at the
  sheet's scale, or smaller (down to a quarter) where there is room.
- **Sheet and scale.** ISO A4 to A0, landscape. Scales are ISO 5455's
  (10:1, 5:1, 2:1, 1:1, 1:2, 1:5, 1:10, ...). The plan takes the smallest
  sheet that shows the part at 1:1 or larger, else at 1:2, else at 1:5, and
  so on. On that sheet it takes the largest scale that fits.
- **Layout.** The frame keeps ISO 5457 margins (20 mm left, 10 mm
  elsewhere). The title block (ISO 7200, 180 x 40 mm) sits bottom right.
  Each view is padded by 10 mm for its dimensions, plus 8 mm under a
  labelled view. Views never overlap each other or the title block (tested
  on every fixture). Projections stay aligned with the front view.
- **Annotations.** One callout per hole feature seen as circles, with a
  leader: `Ø6 THRU`, `Ø4 x 4 DEEP`, `Ø4 THRU, CBORE Ø8 x 2`,
  `Ø4 THRU, CSK Ø8 x 90°`, `12 x Ø6 THRU, CBORE Ø10 x 3`.
- **Title block.** Title (part name), number (part id), scale, sheet,
  projection method with the ISO 5456-2 symbol (a truncated cone and its end
  view), material (`part.material`, see below), date, units.
- **Assembly.** A front view (the product upright, from the front or the
  side, whichever is larger) and an isometric view. A bill of materials
  above the title block: item, part, quantity, one row per part name (two
  bolts are one row, quantity 2, with or without the STEP file). One balloon
  per row in the front view, with a leader to the part's first instance.

## The builder (`catia_mcp/tree/drawing_builder.py`)

| Step | V5 | 3DEXPERIENCE |
|---|---|---|
| Drawing | `Documents.Add("Drawing")` | `PLMNewService.PLMCreate "Drawing"` |
| Sheet | `PaperSize`, `Orientation`, `Scale`, `ProjectionMethod` (enum values read from the type library, documented values otherwise), size read back | same; not run live yet |
| Source | the part document (the product document for an assembly) | the editor's root occurrence (3D Part, or the adopted product); with `--assembly adopt`, a part's drawing projects its first occurrence |
| Views | `Views.Add`, `GenerativeBehavior.Document`, `DefineFrontView`, `DefineProjectionView`, `DefineSectionView`, `DefineCircularDetailView`, `DefineIsometricView`, then `x`, `y`, `Scale`, `Update`; section and detail labels by `SetViewName` | the `DrawingDefineGenView` factory, bound by parameter name (`catia_mcp/tools/drawing.py`), or `GenerativeBehavior` where a release still exposes it |
| Dimensions | `GenerateDimensions` with the drawing active and each generative view active in turn; counted per view | same (live R428: sketch constraints and Pad/Pocket lengths) |
| Callouts | `Texts.Add` and `Leaders.Add` in the front view | same API |
| Frame, title block, symbol | lines and circles by `Factory2D` in the background view, then texts | same API; not run live yet |
| BOM | `Tables.Add` in the background view | same API |
| Balloons | a text with `FrameType` = circle and a leader | same API; not run live yet |
| PDF | `ExportData(path, "pdf")`, checked on disk | tried and the refusal recorded (live on R428: `ExportData` refused, E_FAIL) |

A standalone 3D Shape (the default `--plm-type 3DShape`) cannot be drawn:
3DEXPERIENCE generative views project product instances, and CATIA creates
no view for a 3D Shape outside a product (live R428). The report says so
(`skipped`). Import the model's STEP and build with `--assembly adopt`: the
imported product is what gets drawn. `--plm-type 3DPart` also gives a
product where the licence allows it; live on R428 with a student licence it
opened "Gestion des licences: Opération non autorisée" before the first
feature, and the build stopped.

### First live probe (3DEXPERIENCE R428, French, student role)

- `PLMNewService.PLMCreate "Drawing"` makes the drawing (sheet `Calque.1`,
  views `Vue.1`, `Vue.2`); `Documents.Add("Drawing")` is refused.
- The sheet takes A3 landscape, first angle (`PaperSize` 5, read back
  420 × 297 mm through `GetPaperWidth` / `GetPaperHeight`).
- A front view of a standalone 3D Shape: `DefineFrontView` returns nothing,
  and views have no `GenerativeBehavior`. A product is needed (above).
- PDF export: `ExportData` on the drawing is missing, on the document E_FAIL.

### First live drawings (bracket, `--assembly adopt --drawing auto`)

Every step ran: one drawing per part and one for the product, A4 landscape,
first angle, frame, title block (8 texts), generated dimensions.

| Drawing | Scale | Views | Dimensions | Extras |
| --- | --- | --- | --- | --- |
| Plate (8 mm thick) | 2:1 | front, top, isometric | 3 | |
| Bolt (part of revolution) | 5:1 | front across the axis, left, isometric | 2 | |
| Bracket (product) | 2:1 (isometric 1:1) | front, isometric | 3 | BOM of 2 rows, 2 balloons |

No PDF: `ExportData` is refused (above).

A failed view is removed from the sheet and recorded. The views derived
from it are skipped. Everything else goes on. A dialog that blocks a
drawing stops the drafting step (`stopped`); the build report is still
written.

### Report

`report["drawing"]` = `{"mode": "auto", "parts": {part id: entry},
"assembly": entry or null}`. Each entry holds `ok` (the front view was
made), `route` (`v5/generative`, `3dx/factory`, ...), `sheet`, `scale`,
`projection`, `geometry` (`build123d` or `estimate`), `views` (name, kind,
scale, ok or error), `dimensions` and `dimensions_by_view`, `annotations`,
`title_block`, `bom_rows` and `balloons` for a product, `pdf`, `steps` (each
step ok or its error) and `notes` (why these views).

## Live probe

`scripts/probe_drawing.py` tries every known route on the active part or
product and writes a JSON report. It never saves; close its drawings
without saving.

```bat
python scripts\probe_drawing.py --report drawing_probe.json
python scripts\probe_drawing.py --tree tests\fixtures\trees\m1_holes.json --pdf out
```

It records the source (and its root occurrence), each way to create a
drawing, the sheet's members, the enums used, the signature of every
`DrawingDefineGenView.Define*`, the sheet set to A3 and read back, each way
to make a front view until one works, the derived views, generated
dimensions per view, a text with a leader, a balloon, a table, background
lines, and the PDF export. With `--tree`, it also runs the drafting step of
the tree build on the active source.

Fallback when no route works on 3DEXPERIENCE: put the shape in a 3D Part or
a product (`--assembly adopt` after importing the model's STEP, or
`--plm-type 3DPart` where the licence allows it), or create the drawing by hand (Drafting app, `Insert > Front
View` on the product) and use the interactive drafting tools
(`catia_drawing_*`).

## Drafting coverage

| Capability | Status |
|---|---|
| Front view | Automatic (V5, 3DEXPERIENCE factory: live for products through the tools) |
| Projection views | Automatic: top and side, first or third angle |
| Auxiliary view | Not done: no automatic choice; `DefineAuxiliaryView` signature recorded by the probe |
| Isometric view | Automatic; not yet run live |
| Section view | Automatic: straight cut through hole or revolution axes |
| Section cut | Not done (the builder uses `SectionView`; the tool `catia_drawing_section_view` takes `SectionCut`) |
| Aligned / stepped section | Not done automatically; the tool takes `Aligned` and offset profiles |
| Detail view | Automatic: circular, for features under 3 mm on paper |
| Clipping view | Not done |
| Broken view | Not done |
| Breakout view | Not done; signature recorded by the probe |
| Generated dimensions | Automatic: `GenerateDimensions` per view (from sketch constraints and pad lengths) |
| Manual dimensions | Not possible on generative views: CATIA does not expose projected edges to automation (field-proven V5R20 and R428) |
| Hole and thread callouts | Hole callouts as texts with a leader (simple, blind, counterbore, countersink, counts); threads: not in the tree contract |
| Centre lines and axes | Not done (CATIA's generation settings may add them) |
| Annotations (texts) | Automatic: callouts; tool `catia_drawing_add_text` |
| GD&T | Not done |
| Surface roughness | Not done |
| BOM | Automatic for assemblies: a table, one row per part name, with quantities |
| Balloons | Automatic: circled texts with a leader; not yet run live |
| Title block and frame | Automatic: drawn lines and texts with the ISO projection symbol (no CATIA standard frame) |
| Multi-sheet | Not done: one sheet per drawing |
| PDF export | V5 automatic (`--drawing-pdf`); 3DEXPERIENCE tried, documented as unavailable |
| DXF export | Not done (`ExportData(path, "dxf")` would be the V5 route) |

## Contract additions requested from the app

- `part.material` (optional string, for example `"S235JR"` or
  `"AlMg3"`): written in the title block. The bridge accepts it today and
  prints `-` without it.

## Assumptions to check live

- View coordinates (section profiles, detail centres, text positions) are
  model millimetres in the view's plane, with the model origin at the
  view's `x`, `y`. A wrong guess shows as misplaced callouts and sections.
- `DefineIsometricView` takes the same two plane vectors as
  `DefineFrontView` on V5.
- `iSideToDraw` = 1 draws the side the plan expects for section A-A.
- `CatPaperSize` A0 to A4 are 0 to 4 when the type library cannot be read.
