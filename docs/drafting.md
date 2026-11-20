# Drafting: the drawing a designer would make

`python -m catia_mcp.tree build TREE.json --drawing auto` makes, after a
successful build, a drawing of each built part or, with `--assembly`, one
drawing of the product with a sheet per distinct part after the product's.
The build stays a success whatever happens to the drawings; each step is
recorded in the report's `drawing` section.

```bat
python -m catia_mcp.tree build TREE.json --drawing auto --drawing-pdf out --drawing-dxf out
python -m catia_mcp.tree build TREE.json --drawing auto --drawing-projection third
python -m catia_mcp.tree build TREE.json --drawing auto --drawing-max-sheet A3
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
  - **Stepped.** When no straight line meets a hole of every hole feature,
    the line steps at right angles through one hole of each (the one
    nearest the best straight line), halfway between holes
    (`Offset` profile).
  - **Aligned.** Holes on a bolt circle about a centre (a revolution axis
    seen end on, or a single hole at the centre of a pattern of three or
    more), with no straight line through the centre finding holes on both
    sides (three or five holes): two lines from the centre through holes,
    at an angle (`Aligned` profile). Six holes keep a straight section.
- **Auxiliary view.** The inclined planar face whose true shape the main
  views miss, seen edge on in the front, top or side view: a face holes
  are drilled into first, else the largest, at least a tenth of the part's
  largest projected area, sloped at least 15° off every axis (no drafts),
  not a chamfer (no face within twice the chamfer's size of a chamfered
  edge), and only on a part with one or two such slopes (a hexagonal
  prism gets none). The view looks along the face's normal, from the side
  the normal points to; its cutting line in the parent runs along the
  face's edge, and its arrow points at the face from outside the part. It
  sits next to the parent in the arrow's direction, else where there is
  room. Labelled with the next letter (View B after section A-A). A face
  oblique to every main view gets a note (no secondary auxiliary view).
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
- **Hole and thread callouts.** One callout per hole feature, in the first
  view that shows its holes as circles, with a leader, in ISO 129-1
  symbols: `⌀6 THRU`, `⌀4 ↧4`, `⌀4 THRU ⌴⌀8 ↧2`, `⌀4 THRU ⌵⌀8 × 90°`,
  `12× ⌀6 THRU ⌴⌀10 ↧3`. A tapped hole leads with its thread: designation,
  tolerance class (`6H` unless the tree says), thread depth, then the tap
  drill: `M6 - 6H ↧10 / ⌀5 ↧12`, `M8 - 6H THRU / ⌀6.8 THRU`,
  `M8×1 LH - 6H ↧10 / ...`. Each callout also has its words
  (`Ø4 THRU, CBORE Ø8 x 2`, `M6 - 6H x 10 DEEP / Ø5 x 12 DEEP`): the
  builder writes them instead where CATIA reads the text back changed (a
  font without the symbols). `--callouts text` in the plan command plans
  the words.
- **Centre lines.** In every orthographic view: a centre mark (two crossed
  lines) on each hole and extruded circle seen end on, reaching past its
  largest diameter; an axis line along each hole, cylinder and revolution
  axis seen side on, over its length (a through hole: as far as the part
  goes); a pitch circle through three or more holes of one feature on a
  circle. Lines overshoot the outline by 3 mm of paper (ISO 128-24);
  collinear axes merge. A section shows only those its cutting line meets;
  an aligned section's second line is unfolded about the centre.
- **Title block.** Title (part name), number (part id), scale, sheet,
  projection method with the ISO 5456-2 symbol (a truncated cone and its end
  view), material (`part.material`, see below), date, units.
- **Further sheets.** When the views do not fit the largest allowed sheet
  (`--drawing-max-sheet`, A0 by default), or fit it only at a smaller scale
  than the orthographic views alone reach there, the isometric and
  auxiliary views go to further sheets; when even those do not fit, the
  front view (and its section) stays on the first sheet and every other
  view goes further. A view on another sheet than its parent is made on
  its own, along its plane (an auxiliary view gets its arrow drawn in the
  parent). Further sheets take every leftover view at the first sheet's
  scale where they fit together, else one each at the largest scale that
  fits. Each sheet has its frame and title block, `A4 2/3` in its Sheet
  cell. With a forced sheet and scale, this is where it shows most.
- **Assembly.** A front view (the product upright, from the front or the
  side, whichever is larger) and an isometric view. A bill of materials
  above the title block: item, part, quantity, one row per part name (two
  bolts are one row, quantity 2, with or without the STEP file). One balloon
  per row in the front view, with a leader to the part's first instance.
  Then one sheet (or more) per bill of materials row: the part's own plan,
  projecting the part. The part ids each sheet shows are the plan's
  `covers`. When the product's drawing cannot be made (not assembled, a
  standalone build on 3DEXPERIENCE), each part gets its own drawing as
  before.

## The builder (`catia_mcp/tree/drawing_builder.py`)

| Step | V5 | 3DEXPERIENCE |
|---|---|---|
| Drawing | `Documents.Add("Drawing")` | `PLMNewService.PLMCreate "Drawing"` |
| Sheet | `PaperSize`, `Orientation`, `Scale`, `ProjectionMethod` (enum values read from the type library, documented values otherwise), size read back | same; not run live yet |
| Source | the part document (the product document for an assembly) | the editor's root occurrence (3D Part, or the adopted product); with `--assembly adopt`, a part's drawing projects its first occurrence |
| Views | `Views.Add`, `GenerativeBehavior.Document`, `DefineFrontView`, `DefineProjectionView`, `DefineSectionView`, `DefineCircularDetailView`, `DefineIsometricView`, then `x`, `y`, `Scale`, `Update`; section and detail labels by `SetViewName` | the `DrawingDefineGenView` factory, bound by parameter name (`catia_mcp/tools/drawing.py`), or `GenerativeBehavior` where a release still exposes it |
| Auxiliary view | `DefineAuxiliaryView(x1, y1, x2, y2, parent)`, then `(..., 1, parent)`, then `(profile, parent)`; refused, or the parent on another sheet: `DefineFrontView` along the face's normal, its arrow (three lines) and letter drawn in the parent | `DrawingDefineGenView.DefineAuxiliaryView` bound by name (`iX1`... or `iProfile`, `iParentView`); same fallback |
| Stepped, aligned section | `DefineSectionView(profile, "SectionView", "Offset" or "Aligned", 1, parent)` | the factory, `CatOffsetSectionProfile` or `CatAlignedSectionProfile` |
| Generation settings | on each orthographic view, `CenterLineMode`, `GenerateCenterLines`, `CenterLine`, then the same for axes and threads, on `GenerativeBehavior` then the view, where the type library has the name; set to true and read back, then the view updated | same where a view has them |
| Centre lines | unless the view generates them: `Factory2D.CreateLine` (and `CreateClosedCircle` for a pitch circle) in the view, then `Selection.VisProperties.SetRealLineType(4, 1)` (dash-dot) and `SetRealWidth(1, 1)` | same, the editor's `Selection` if the drawing has none |
| Sheets | `Sheets.Add(name)`, then format, scale, projection, views, frame and title block as for the first | same |
| Dimensions | `GenerateDimensions` with the drawing, the sheet and each generative view active in turn; counted per view; views left without any are listed with how many generated elements they expose (`GeometricElements.Count`) | same (live R428: sketch constraints and Pad/Pocket lengths) |
| Callouts | `Texts.Add` and `Leaders.Add` in the view; read back, the words written instead if CATIA changed the symbols | same API |
| Frame, title block, symbol | lines and circles by `Factory2D` in the background view, then texts | same API; not run live yet |
| BOM | `Tables.Add` in the background view | same API |
| Balloons | a text with `FrameType` = circle and a leader | same API; not run live yet |
| PDF, DXF | each sheet active in turn, `ExportData(path, "pdf")` then `"PDF"` (`"dxf"`, `"DXF"`) to `NAME.pdf`, or `NAME_2.pdf`... with several sheets, checked on disk (a file CATIA names itself after the stem counts) | the same, then on `ActiveDocument`: each route once, every refusal recorded (live on R428: `ExportData` refused, E_FAIL); never `PrintOut` |

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
"assembly": entry or null}`. Each entry holds `ok` (the front view of the
first sheet was made), `route` (`v5/generative`, `3dx/factory`, ...),
`sheet`, `scale`, `projection`, `geometry` (`build123d` or `estimate`),
`sheets` (index, name, format, scale, source part, ok), `views` (name, kind,
scale, sheet, ok or error; `route` for an auxiliary view; `generation` for
the centre line settings found; `geometric_elements`), `dimensions` and
`dimensions_by_view`, `views_without_dimensions`, `annotations`,
`callouts_in_words`, `centre_lines` and `centre_line_type`, `title_block`,
`bom_rows` and `balloons` for a product, `pdf` and `pdf_files`, `dxf` and
`dxf_files`, `exports` (every route tried per format and sheet), `steps`
(each step ok or its error, `sheet 2: ...` on further sheets) and `notes`
(why these views). A part drawn on a sheet of the product's drawing has
the entry `{"ok", "drawing": product name, "catia_name", "sheet": index}`.

## Live probe

`scripts/probe_drawing.py` tries every known route on the active part or
product and writes a JSON report. It never saves; close its drawings
without saving.

```bat
python scripts\probe_drawing.py --report drawing_probe.json
python scripts\probe_drawing.py --window "DraftingParts A.1" --report drawing_probe.json
python scripts\probe_drawing.py --tree tests\fixtures\trees\m1_holes.json --pdf out
```

It records the source (and its root occurrence), each way to create a
drawing, the sheet's members, the enums used, the signature of every
`DrawingDefineGenView.Define*`, the sheet set to A3 and read back, each way
to make a front view until one works, the derived views, generated
dimensions per view, a text with a leader, a balloon, a table, background
lines, and the PDF export. Then the second slice's routes from that front
view: the view's members about axes, centre lines and threads, the
generation settings set and read back, a dash-dot centre mark, an auxiliary
view by each known signature (else along the normal, with its arrow), a
stepped and an aligned section, a callout in ISO 129 symbols read back,
`GeometricElements.Count` of the front view, a second sheet with a line and
a text, and the PDF and DXF by every route, once each. It records the
signature of any `PrintToFile` (sheet, drawing, window) and calls it only
with `--print-to-file`; never `PrintOut`. With `--window NAME` it activates
that one window first and never loops over windows (CATIA may hold
hundreds). With `--tree`, it also runs the drafting step of the tree build
on the active source (on an imported product, the occurrence of the tree's
part).

### Fixtures

`scripts/make_drafting_fixtures.py` writes three parts made for the second
slice, each alone (`tests/fixtures/trees/d1_inclined_block.json`,
`d2_holes_not_in_line.json`, `d3_flange_three_holes.json`) and together as
an assembly with its STEP (`tests/fixtures/assembly/drafting_parts.step`
and `.tree.json`), for the live run on 3DEXPERIENCE:

| Part | What its drawing shows |
| --- | --- |
| InclinedBlock | auxiliary view B along the 45° face, section A-A through the hole |
| HolePlate | stepped section A-A through a through hole, a counterbore, a tapped M6 and a countersink; `M6 - 6H ↧8 / ⌀5 ↧10`, `4× ⌀4.5 THRU ⌵⌀9 × 90°` |
| Flange | aligned section A-A through the bore and two of three bolt holes; the pitch circle |

Fallback when no route works on 3DEXPERIENCE: put the shape in a 3D Part or
a product (`--assembly adopt` after importing the model's STEP, or
`--plm-type 3DPart` where the licence allows it), or create the drawing by hand (Drafting app, `Insert > Front
View` on the product) and use the interactive drafting tools
(`catia_drawing_*`).

## Drafting coverage

| Capability | Status |
|---|---|
| Front view | Automatic. Live on R428 for an imported product (`--assembly adopt`); a standalone 3D Shape gets no view |
| Projection views | Automatic: top and side, first or third angle (live on R428) |
| Auxiliary view | Automatic for one inclined face seen edge on in a main view (`DefineAuxiliaryView`, else a view along the normal with its arrow); not run live yet. No secondary auxiliary view |
| Isometric view | Automatic (live on R428) |
| Section view | Automatic: straight cut through hole or revolution axes (not yet met live: the bracket needs none) |
| Section cut | Not done (the builder uses `SectionView`; the tool `catia_drawing_section_view` takes `SectionCut`) |
| Aligned / stepped section | Automatic: stepped through one hole of each hole feature, aligned through a bolt circle of three or five holes; not run live yet |
| Detail view | Automatic: circular, for features under 3 mm on paper (not yet met live) |
| Clipping view | Not done |
| Broken view | Not done |
| Breakout view | Not done; signature recorded by the probe |
| Generated dimensions | Automatic: `GenerateDimensions` per view (from sketch constraints and pad lengths; live on R428) |
| Manual dimensions | Not possible on generative views: CATIA does not expose projected edges to automation (field-proven V5R20 and R428). The report lists the views left without a generated dimension and how many generated elements each exposes (`GeometricElements.Count`), so a release that exposes them shows |
| Hole and thread callouts | Automatic, ISO 129-1 symbols: simple, blind, counterbore, countersink, pattern counts (`4×`), tapped holes (`M6 - 6H ↧10 / ⌀5 ↧12`); in words where the font lacks a symbol |
| Centre lines and axes | Automatic: centre marks, axis lines, pitch circles in every orthographic view, by the views' generation settings where found, else drawn dash-dot; not run live yet |
| Annotations (texts) | Automatic: callouts; tool `catia_drawing_add_text` |
| GD&T | Not done |
| Surface roughness | Not done |
| BOM | Automatic for assemblies: a table, one row per part name, with quantities (live on R428) |
| Balloons | Automatic: circled texts with a leader (live on R428) |
| Title block and frame | Automatic: drawn lines and texts with the ISO projection symbol (no CATIA standard frame; live on R428) |
| Multi-sheet | Automatic: further sheets when the views do not fit the largest allowed sheet (`--drawing-max-sheet`); an assembly: the product's sheet, then one per distinct part. Each with its frame and title block |
| PDF export | V5 automatic per sheet (`--drawing-pdf`); 3DEXPERIENCE refused live (`ExportData` E_FAIL), every route tried once and recorded |
| DXF export | V5 automatic per sheet (`--drawing-dxf`); 3DEXPERIENCE as the PDF |

## Contract additions requested from the app

- `part.material` (optional string, for example `"S235JR"` or
  `"AlMg3"`): written in the title block. The bridge accepts it today and
  prints `-` without it.
- `hole.thread.tolerance` (optional string, for example `"6H"` or `"5H"`):
  the internal thread's tolerance class in its callout. The bridge accepts
  it today; without it the callout says `6H` (ISO 965-1's default).

## Assumptions to check live

- View coordinates (section profiles, detail centres, text positions) are
  model millimetres in the view's plane, with the model origin at the
  view's `x`, `y`. A wrong guess shows as misplaced callouts and sections.
- `DefineIsometricView` takes the same two plane vectors as
  `DefineFrontView` on V5.
- `iSideToDraw` = 1 draws the side the plan expects for section A-A.
- `CatPaperSize` A0 to A4 are 0 to 4 when the type library cannot be read.
- `DefineAuxiliaryView` takes the cutting line's two points and the parent
  (the V5 documentation); the other variants are tried after it. Which side
  the view looks from is CATIA's: the plan expects the viewer on the side
  the face's normal points to.
- Line type 4 is the dash-dot (axis) line of the drafting standard in use.
- The generation settings' names (`CenterLineMode`, `AxisMode`,
  `ThreadMode`...) are guesses from the view properties dialog: the probe
  lists the members of the view and of its generative behaviour whose
  names mention axes, centre lines or threads.
- An aligned section unfolds its second cutting line about the centre, to
  the side away from the first one.
- `ExportData` of a drawing writes the active sheet (V5); if it writes all
  sheets at once, each per-sheet file holds them all.
- CATIA's drafting font draws `⌀ ↧ ⌴ ⌵ ×`; where it does not, the read-back
  text differs and the words are written instead.
