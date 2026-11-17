"""Make the planned drawing in CATIA (catia_mcp/tree/drawing_plan.py).

V5: a new Drawing document (`Documents.Add("Drawing")`); the sheet's
format, orientation, scale and projection method; each view added to the
sheet (`Views.Add`), linked to the part or product document
(`GenerativeBehavior.Document`), defined (`DefineFrontView`,
`DefineProjectionView`, `DefineSectionView`, `DefineCircularDetailView`,
`DefineIsometricView`), placed, scaled and updated; the dimensions CATIA
generates from the part's constraints and pad lengths
(`DrawingSheet.GenerateDimensions`, with the drawing and each view active);
hole callouts as texts with a leader; the frame, title block and projection
symbol drawn in the background view; for an assembly, the bill of materials
as a table and one balloon (a circled text with a leader) per row; the PDF
(`ExportData`).

3DEXPERIENCE: the drawing is a Drawing created with `PLMNewService`; the
views project product instances (the editor's root occurrence: a 3D Part or
a product, never a standalone 3D Shape, for which CATIA creates no view,
live R428) through the sheet's `DrawingDefineGenView` factory, bound by
parameter name (catia_mcp/tools/drawing.py), or through
`GenerativeBehavior` where a release still exposes it. Texts, tables and
dimensions keep the V5 API. No file export is documented there: the PDF is
tried and the refusal recorded.

Every step is recorded (ok, or the error); none stops the others, except
that a view whose parent failed is skipped. Nothing is saved.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable
from typing import Any

from catia_mcp.tree.drawing_plan import DrawingPlan, ViewPlan

# CatPaperSize, CatPaperOrientation, CatSheetProjectionMethod and
# CatTextFrameType as documented for V5; read from the type library first.
PAPER_SIZES = {"A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4}
LANDSCAPE = 1
PROJECTION_METHODS = {"first": 0, "third": 1}
FRAME_CIRCLE = 3
PROJECTION_TYPES = {"right": 0, "left": 1, "top": 2, "bottom": 3, "rear": 4}
TEXT_HEIGHT = 3.5
LABEL_HEIGHT = 2.5


class DrawingSkipped(Exception):
    """This drawing cannot be made on this platform: the reason is reported."""


def _enum(objs: list[Any], name: str, member: str, fallback: int) -> int:
    with contextlib.suppress(Exception):
        from catia_mcp.introspect import find_enum

        values = find_enum(objs, name)
        if values and member in values:
            return int(values[member])
    return fallback


def safe_file_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "drawing"


class _Recorder:
    """The drawing's report: each step ok or its error."""

    def __init__(self, plan: DrawingPlan, route: str, log: Callable[[str], None]) -> None:
        self.log = log
        self.entry: dict[str, Any] = {
            "ok": False, "route": route, "sheet": plan.sheet, "scale": plan.scale_label,
            "projection": plan.projection, "geometry": plan.geometry_source,
            "views": [], "dimensions": 0, "annotations": 0, "pdf": None, "steps": [],
            "notes": list(plan.notes),
        }

    def step(self, name: str, action: Callable[[], Any]) -> Any:
        try:
            result = action()
        except Exception as error:  # recorded; the drawing goes on
            self.entry["steps"].append({"step": name, "ok": False, "error": str(error)})
            self.log(f"    drawing: {name} failed: {error}")
            return None
        self.entry["steps"].append({"step": name, "ok": True})
        return result


class _GenerativeViews:
    """Views through DrawingViewGenerativeBehavior (V5, and 3DEXPERIENCE
    releases that still expose it)."""

    def __init__(self, sheet: Any, document: Any) -> None:
        self.sheet, self.document = sheet, document

    def _new(self, plan: ViewPlan, define: Callable[[Any], None]) -> Any:
        view = self.sheet.Views.Add(plan.name)
        try:
            view.GenerativeBehavior.Document = self.document
            define(view.GenerativeBehavior)
            _place(view, plan)
            view.GenerativeBehavior.Update()
            return view
        except Exception:
            with contextlib.suppress(Exception):
                self.sheet.Views.Remove(view.Name)
            raise

    def front(self, plan: ViewPlan) -> Any:
        return self._new(plan, lambda gb: gb.DefineFrontView(*plan.u, *plan.v))

    def isometric(self, plan: ViewPlan) -> Any:
        return self._new(plan, lambda gb: gb.DefineIsometricView(*plan.u, *plan.v))

    def projection(self, plan: ViewPlan, parent: Any) -> Any:
        return self._new(plan, lambda gb: gb.DefineProjectionView(
            parent.GenerativeBehavior, PROJECTION_TYPES[plan.projection]))

    def section(self, plan: ViewPlan, parent: Any) -> Any:
        return self._new(plan, lambda gb: gb.DefineSectionView(
            list(plan.profile), "SectionView", "Offset", 1, parent.GenerativeBehavior))

    def detail(self, plan: ViewPlan, parent: Any) -> Any:
        cx, cy, radius = plan.detail
        return self._new(plan, lambda gb: gb.DefineCircularDetailView(
            cx, cy, radius, parent.GenerativeBehavior))


class _FactoryViews:
    """Views through the 3DEXPERIENCE DrawingDefineGenView factory, bound by
    parameter name by the drafting tools."""

    def __init__(self, tools: Any, sheet: Any, instances: list[Any]) -> None:
        self.tools, self.sheet, self.instances = tools, sheet, instances

    def _make(self, plan: ViewPlan, method: str, values: dict[str, Any]) -> Any:
        x, y = plan.anchor
        return self.tools._factory_view(self.sheet, method, values, plan.name, x, y, plan.scale)

    def front(self, plan: ViewPlan) -> Any:
        return self._make(plan, "DefineFrontView", {
            "iListofPrdInst": self.instances, "iPlane": [float(c) for c in plan.u + plan.v]})

    def isometric(self, plan: ViewPlan) -> Any:
        return self._make(plan, "DefineIsometricView", {
            "iListofPrdInst": self.instances, "iPlane": [float(c) for c in plan.u + plan.v]})

    def projection(self, plan: ViewPlan, parent: Any) -> Any:
        return self._make(plan, "DefineProjectionView", {
            "iParentView": parent, "iType": PROJECTION_TYPES[plan.projection]})

    def section(self, plan: ViewPlan, parent: Any) -> Any:
        return self._make(plan, "DefineSectionView", {
            "iProfile": list(plan.profile), "iSectionType": "SectionView",
            "iProfileType": "CatOffsetSectionProfile", "iSideToDraw": 1,
            "iParentView": parent})

    def detail(self, plan: ViewPlan, parent: Any) -> Any:
        cx, cy, radius = plan.detail
        return self._make(plan, "DefineCircularDetailView", {
            "iXCenter": cx, "iYCenter": cy, "iRadius": radius, "iParentView": parent})


def _place(view: Any, plan: ViewPlan) -> None:
    view.x, view.y = plan.anchor
    view.Scale = plan.scale


class DrawingBuilder:
    def __init__(self, conn: Any, log: Callable[[str], None] = lambda _: None,
                 pdf_dir: str | None = None) -> None:
        self.conn = conn
        self.log = log
        self.pdf_dir = pdf_dir

    # ── Entry points ──

    def draw(self, plan: DrawingPlan, source: Any) -> dict[str, Any]:
        """Make one drawing. `source`: on V5 the part or product document; on
        3DEXPERIENCE the list of product instances to project (the editor's
        root occurrence, or an occurrence of the part)."""
        route = "3dx" if self.conn.is_3dx else "v5"
        rec = _Recorder(plan, route, self.log)
        try:
            drawing, sheet = self._new_drawing()
        except Exception as error:
            rec.entry["error"] = f"no drawing: {error}"
            return rec.entry
        rec.entry["catia_name"] = self._title_of(drawing)
        self._setup_sheet(rec, sheet, plan)
        try:
            maker = self._maker(sheet, source)
        except Exception as error:
            rec.entry["error"] = f"no way to make views: {error}"
            return rec.entry
        rec.entry["route"] = (f"{route}/generative" if isinstance(maker, _GenerativeViews)
                              else f"{route}/factory")
        views = self._views(rec, plan, maker)
        self._title_block(rec, sheet, plan)
        rec.entry["annotations"] = self._annotations(rec, plan, views)
        if plan.bom is not None:
            self._bom(rec, sheet, plan)
            rec.entry["balloons"] = self._balloons(rec, plan, views)
        rec.entry["dimensions"] = self._dimensions(rec, drawing, sheet, plan, views)
        rec.step("update", lambda: drawing.Update())
        with contextlib.suppress(Exception):
            self.conn.refresh_display()
        if self.pdf_dir:
            rec.entry["pdf"] = rec.step("pdf", lambda: self._pdf(drawing, plan))
        made = [v for v in rec.entry["views"] if v["ok"]]
        rec.entry["ok"] = bool(made) and made[0]["kind"] == "front"
        return rec.entry

    # ── Document and sheet ──

    def _new_drawing(self) -> tuple[Any, Any]:
        if self.conn.is_3dx:
            from catia_mcp.plm import PLMSession

            PLMSession(self.conn).create("Drawing")
            drawing = self.conn.active_object()
        else:
            drawing = self.conn.app.Documents.Add("Drawing")
        try:
            sheet = drawing.Sheets.ActiveSheet
        except Exception:
            sheet = drawing.ActiveSheet
        return drawing, sheet

    def _title_of(self, drawing: Any) -> str:
        if self.conn.is_3dx:
            with contextlib.suppress(Exception):
                from catia_mcp.plm import PLMSession

                return PLMSession(self.conn).title_of(drawing)
        try:
            return str(drawing.Name)
        except Exception:
            return "<drawing>"

    def _setup_sheet(self, rec: _Recorder, sheet: Any, plan: DrawingPlan) -> None:
        def paper() -> None:
            sheet.PaperSize = _enum([sheet], "CatPaperSize", f"catPaper{plan.sheet}",
                                    PAPER_SIZES[plan.sheet])
            sheet.Orientation = _enum([sheet], "CatPaperOrientation", "catPaperLandscape",
                                      LANDSCAPE)

        def size_read_back() -> None:
            width = height = None
            for getter in (lambda: (sheet.GetPaperWidth(), sheet.GetPaperHeight()),
                           lambda: (sheet.PaperWidth, sheet.PaperHeight)):
                with contextlib.suppress(Exception):
                    width, height = (float(x) for x in getter())
                    break
            if width is None:
                raise RuntimeError("the paper size cannot be read back")
            rec.entry["paper_mm"] = [round(width, 1), round(height, 1)]
            if abs(width - plan.width) > 1 or abs(height - plan.height) > 1:
                raise RuntimeError(f"the sheet is {width:g} x {height:g} mm, not "
                                   f"{plan.width:g} x {plan.height:g} ({plan.sheet})")

        def projection() -> None:
            member = "catFirstAngle" if plan.projection == "first" else "catThirdAngle"
            sheet.ProjectionMethod = _enum([sheet], "CatSheetProjectionMethod", member,
                                           PROJECTION_METHODS[plan.projection])

        def name() -> None:
            sheet.Name = f"{plan.sheet} {plan.scale_label}"

        rec.step("sheet format", paper)
        rec.step("sheet size check", size_read_back)
        rec.step("sheet scale", lambda: setattr(sheet, "Scale", plan.scale))
        rec.step("projection method", projection)
        rec.step("sheet name", name)

    def _maker(self, sheet: Any, source: Any) -> Any:
        if not self.conn.is_3dx:
            return _GenerativeViews(sheet, source)
        from catia_mcp.automation import name_exists

        views = sheet.Views
        with contextlib.suppress(Exception):
            if int(views.Count) >= 1 and name_exists(views.Item(1), "GenerativeBehavior"):
                return _GenerativeViews(sheet, source[0])
        from catia_mcp.tools.drawing import DraftingTools

        return _FactoryViews(DraftingTools(self.conn), sheet, list(source))

    # ── Views ──

    def _views(self, rec: _Recorder, plan: DrawingPlan, maker: Any) -> dict[str, Any]:
        made: dict[str, Any] = {}
        for view in plan.views:
            row: dict[str, Any] = {"name": view.name, "kind": view.kind,
                                   "scale": view.scale, "ok": False}
            rec.entry["views"].append(row)
            parent = made.get(view.parent) if view.parent else None
            if view.parent and parent is None:
                row["error"] = f"its parent {view.parent} was not made"
                continue
            try:
                if view.kind in ("front", "isometric"):
                    catia = getattr(maker, view.kind)(view)
                else:
                    catia = getattr(maker, view.kind)(view, parent)
            except Exception as error:
                row["error"] = str(error)
                self.log(f"    drawing: {view.name} failed: {error}")
                continue
            made[view.name] = catia
            row["ok"] = True
            with contextlib.suppress(Exception):
                row["catia_name"] = str(catia.Name)
            if view.label:
                kind = "SECTION " if view.kind == "section" else "DETAIL "
                with contextlib.suppress(Exception):
                    catia.SetViewName(kind, view.label, "")
            with contextlib.suppress(Exception):
                row["applied_scale"] = float(catia.Scale)
        return made

    # ── Annotations ──

    def _text(self, view: Any, text: str, x: float, y: float, height: float) -> Any:
        item = view.Texts.Add(text, x, y)
        with contextlib.suppress(Exception):
            item.SetFontSize(0, 0, height)
        return item

    def _annotations(self, rec: _Recorder, plan: DrawingPlan, views: dict[str, Any]) -> int:
        count = 0
        for note in plan.annotations:
            view = views.get(note.view)
            if view is None:
                continue

            def add(note=note, view=view) -> None:
                text = self._text(view, note.text, note.x, note.y, TEXT_HEIGHT)
                if note.leader is not None:
                    text.Leaders.Add(*note.leader)

            if rec.step(f"callout {note.text}", lambda add=add: add() or True):
                count += 1
        return count

    def _background(self, sheet: Any) -> Any:
        views = sheet.Views
        try:
            view = views.Item("Background View")
        except Exception:
            view = views.Item(2)
        with contextlib.suppress(Exception):
            view.Activate()
        return view

    def _title_block(self, rec: _Recorder, sheet: Any, plan: DrawingPlan) -> None:
        block = plan.title

        def frame() -> None:
            factory = self._background(sheet).Factory2D
            for x0, y0, x1, y1 in (block.frame, block.box):
                for a, b, c, d in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1),
                                   (x0, y1, x0, y0)):
                    factory.CreateLine(a, b, c, d)
            for cell in block.cells:
                factory.CreateLine(cell.x, cell.y, cell.x + cell.w, cell.y)
                factory.CreateLine(cell.x, cell.y, cell.x, cell.y + cell.h)
            for line in block.symbol_lines:
                factory.CreateLine(*line)
            for circle in block.symbol_circles:
                factory.CreateClosedCircle(*circle)

        def texts() -> int:
            view = self._background(sheet)
            for cell in block.cells:
                self._text(view, cell.label, cell.x + 1.0, cell.y + cell.h - 1.0, LABEL_HEIGHT)
                self._text(view, cell.value, cell.x + 2.0, cell.y + cell.h - 4.0, TEXT_HEIGHT)
            return len(block.cells)

        rec.step("frame and title block lines", frame)
        rec.entry["title_block"] = rec.step("title block texts", texts) or 0
        with contextlib.suppress(Exception):
            sheet.Views.Item(1).Activate()

    def _bom(self, rec: _Recorder, sheet: Any, plan: DrawingPlan) -> None:
        bom = plan.bom

        def table() -> int:
            view = self._background(sheet)
            rows, columns = len(bom.rows), len(bom.columns)
            x, y = bom.anchor
            item = view.Tables.Add(x, y, rows, columns, bom.row_height, bom.columns[0])
            for c, width in enumerate(bom.columns, start=1):
                with contextlib.suppress(Exception):
                    item.SetColumnSize(c, width)
            for r, row in enumerate(bom.rows, start=1):
                for c, value in enumerate(row, start=1):
                    item.SetCellString(r, c, value)
            return rows - 1

        rec.entry["bom_rows"] = rec.step("bill of materials", table) or 0
        with contextlib.suppress(Exception):
            sheet.Views.Item(1).Activate()

    def _balloons(self, rec: _Recorder, plan: DrawingPlan, views: dict[str, Any]) -> int:
        count = 0
        for balloon in plan.balloons:
            view = views.get(balloon.view)
            if view is None:
                continue

            def add(balloon=balloon, view=view) -> bool:
                text = self._text(view, str(balloon.item), balloon.x, balloon.y, TEXT_HEIGHT)
                text.FrameType = _enum([text], "CatTextFrameType", "catCircle", FRAME_CIRCLE)
                text.Leaders.Add(*balloon.leader)
                return True

            if rec.step(f"balloon {balloon.item}", add):
                count += 1
        return count

    # ── Dimensions ──

    def _dimensions(self, rec: _Recorder, drawing: Any, sheet: Any, plan: DrawingPlan,
                    views: dict[str, Any]) -> int:
        """GenerateDimensions with the drawing and each generative view active
        (a silent no-op otherwise, field-proven on V5R20); the dimensions
        counted in every view."""
        with contextlib.suppress(Exception):
            drawing.Activate()
        by_view: dict[str, int] = {}
        for view_plan in plan.views:
            view = views.get(view_plan.name)
            if view is None or view_plan.kind in ("isometric", "detail"):
                continue

            def generate(view=view) -> None:
                view.Activate()
                sheet.GenerateDimensions()

            rec.step(f"generate dimensions in {view_plan.name}", generate)
        for name, view in views.items():
            with contextlib.suppress(Exception):
                by_view[name] = int(view.Dimensions.Count)
        rec.entry["dimensions_by_view"] = by_view
        return sum(by_view.values())

    # ── Export ──

    def _pdf(self, drawing: Any, plan: DrawingPlan) -> str:
        os.makedirs(self.pdf_dir, exist_ok=True)
        path = os.path.abspath(os.path.join(self.pdf_dir, safe_file_name(plan.name) + ".pdf"))
        if os.path.exists(path):
            os.remove(path)
        drawing.ExportData(path, "pdf")
        if not os.path.exists(path):
            raise RuntimeError(f"ExportData wrote no file at {path}")
        return path


# ── Sources ──


def instances_3dx(conn: Any, editor: Any, title: str | None = None) -> list[Any]:
    """What a 3DEXPERIENCE view projects: the editor's root occurrence, or,
    given a title, the first occurrence under it whose reference has it.
    Raises DrawingSkipped for a standalone 3D Shape."""
    from catia_mcp.errors import WrongDocumentType

    try:
        root = conn.root_occurrence(editor)
    except WrongDocumentType:
        raise DrawingSkipped(
            "a standalone 3D Shape: 3DEXPERIENCE generative views project product "
            "instances and CATIA creates no view for a 3D Shape outside a product (live "
            "R428). Build with --plm-type 3DPart, or --assembly adopt.") from None
    if title is None:
        return [root]
    from catia_mcp.automation import safe_get
    from catia_mcp.plm import PLMSession

    plm = PLMSession(conn)
    stack = list(plm.child_occurrences(root))
    while stack:
        occurrence = stack.pop(0)
        instance = safe_get(occurrence, "InstanceOccurrenceOf")
        reference = safe_get(instance, "ReferenceInstanceOf") if instance is not None else None
        if reference is not None and plm.attribute(reference, "V_Name") == title:
            return [occurrence]
        stack.extend(plm.child_occurrences(occurrence))
    raise DrawingSkipped(f"no occurrence of '{title}' in the product")


def product_document(app: Any, part_number: str) -> Any:
    """The V5 product document whose root has this part number (the one
    the assembler made)."""
    documents = app.Documents
    for i in range(int(documents.Count), 0, -1):  # the newest first
        document = documents.Item(i)
        with contextlib.suppress(Exception):
            if str(document.Name).lower().endswith(".catproduct") and \
                    str(document.Product.PartNumber) == part_number:
                return document
    raise DrawingSkipped(f"no open product document '{part_number}'")


# ── The tree command's drafting step ──


def make_drawings(conn: Any, worker: Any, tree: dict[str, Any], builder: Any,
                  report: dict[str, Any], assembly: Any = None, options: Any = None,
                  pdf_dir: str | None = None, adopted: bool = False,
                  log: Callable[[str], None] = print) -> dict[str, Any]:
    """Draw every built part, then the assembly (the V5 product the
    assembler made, or the adopted 3DEXPERIENCE product): the report's
    `drawing` section. Never raises: a failure is recorded."""
    from catia_mcp.errors import SessionError
    from catia_mcp.tree.drawing_plan import plan_drawings

    section: dict[str, Any] = {"mode": "auto", "parts": {}, "assembly": None}
    plans, product, problems = plan_drawings(tree, assembly, options)
    if problems:
        section["plan_problems"] = problems
    drafter = DrawingBuilder(conn, log=log, pdf_dir=pdf_dir)
    product_editor = None
    if conn.is_3dx and adopted:
        with contextlib.suppress(Exception):
            product_editor = worker.run_guarded(lambda: conn.active_editor, label="the editor")

    def source_of(part_id: str, name: str) -> Any:
        if not conn.is_3dx:
            if part_id not in builder.part_documents:
                raise DrawingSkipped("no part document was kept for it")
            return builder.part_documents[part_id]
        if adopted:
            return instances_3dx(conn, product_editor, title=name)
        if part_id not in builder.part_editors:
            raise DrawingSkipped("no editor was kept for it")
        return instances_3dx(conn, builder.part_editors[part_id])

    def run(label: str, plan: Any, source: Callable[[], Any]) -> dict[str, Any] | None:
        log(f"drawing {label}: {plan.sheet} {plan.scale_label}, "
            f"{', '.join(v.name for v in plan.views)}")
        try:
            return worker.run_guarded(lambda: drafter.draw(plan, source()),
                                      label=f"the drawing of {label}")
        except DrawingSkipped as skipped:
            return {"ok": False, "skipped": str(skipped), "sheet": plan.sheet,
                    "scale": plan.scale_label, "notes": plan.notes}
        except SessionError as error:  # a dialog: the call is still pending
            section["stopped"] = f"{label}: {error}"
            return None
        except Exception as error:
            return {"ok": False, "error": str(error), "sheet": plan.sheet,
                    "scale": plan.scale_label}

    names = {p["id"]: p.get("name") or p["id"] for p in tree["parts"]}
    for part_id, plan in plans.items():
        entry = run(part_id, plan, lambda i=part_id: source_of(i, names[i]))
        if entry is None:
            return section
        section["parts"][part_id] = entry
    if product is not None:
        def product_source() -> Any:
            if conn.is_3dx:
                if not adopted:
                    raise DrawingSkipped("3DEXPERIENCE: no product was made (use --assembly "
                                         "adopt)")
                return instances_3dx(conn, product_editor)
            made = report.get("assembly") or {}
            if not made.get("ok"):
                raise DrawingSkipped("the product was not assembled")
            return product_document(conn.app, made.get("catia_product") or product.name)

        section["assembly"] = run(product.name, product, product_source)
    for label, entry in [*section["parts"].items(), ("assembly", section["assembly"])]:
        if entry:
            state = "ok" if entry.get("ok") else (entry.get("skipped") or entry.get("error")
                                                  or "incomplete")
            log(f"drawing {label}: {state}; {entry.get('dimensions', 0)} dimension(s)"
                f"{', PDF ' + entry['pdf'] if entry.get('pdf') else ''}")
    return section
