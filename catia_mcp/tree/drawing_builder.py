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
as a table and one balloon (a circled text with a leader) per row; centre
marks and axis lines, by the views' generation settings where CATIA has
them, else drawn dash-dot in the view; auxiliary views
(`DefineAuxiliaryView`, else a view along the face's normal with its arrow
drawn); further sheets (`Sheets.Add`), each with its frame and title block;
the PDF and DXF of each sheet (`ExportData`).

3DEXPERIENCE: the drawing is a Drawing created with `PLMNewService`; the
views project product instances (the editor's root occurrence: a 3D Part or
a product, never a standalone 3D Shape, for which CATIA creates no view,
live R428) through the sheet's `DrawingDefineGenView` factory, bound by
parameter name (catia_mcp/tools/drawing.py), or through
`GenerativeBehavior` where a release still exposes it. Texts, tables and
dimensions keep the V5 API. No file export is documented there: the PDF and
DXF are tried once by each known route and the refusals recorded.

Every step is recorded (ok, or the error); none stops the others, except
that a view whose parent failed is skipped. Nothing is saved.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

from catia_mcp.tree.drawing_plan import DrawingPlan, ViewPlan

# CatPaperSize, CatPaperOrientation, CatSheetProjectionMethod and
# CatTextFrameType as documented for V5; read from the type library first.
PAPER_SIZES = {"A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4}
LANDSCAPE = 1
PROJECTION_METHODS = {"first": 0, "third": 1}
FRAME_CIRCLE = 3
FACTORY_PROFILE_TYPES = {"Offset": "CatOffsetSectionProfile",
                         "Aligned": "CatAlignedSectionProfile"}
PROJECTION_TYPES = {"right": 0, "left": 1, "top": 2, "bottom": 3, "rear": 4}
TEXT_HEIGHT = 3.5
LABEL_HEIGHT = 2.5
# CATIA's line type 4 is the dash-dot (axis) line of the default standard.
AXIS_LINE_TYPE = 4
THIN_LINE = 1
# The views' generation settings for centre lines, axes and threads, by the
# names V5 and 3DEXPERIENCE releases are known or said to use; each is set
# where the view has it and read back.
GENERATION_SETTINGS = {
    "centre lines": ("CenterLineMode", "GenerateCenterLines", "CenterLine"),
    "axes": ("AxisMode", "GenerateAxis", "Axis"),
    "threads": ("ThreadMode", "GenerateThreads", "Thread"),
}
ORTHOGRAPHIC = ("front", "projection", "section", "auxiliary")


class DrawingSkipped(Exception):
    """This drawing cannot be made on this platform: the reason is reported."""


def _enum(objs: list[Any], name: str, member: str, fallback: int) -> int:
    with contextlib.suppress(Exception):
        from catia_mcp.introspect import find_enum

        values = find_enum(objs, name)
        if values and member in values:
            return int(values[member])
    return fallback


def _plain(value: Any) -> Any:
    return value if isinstance(value, (bool, int, float, str)) else str(value)


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
            "notes": list(plan.notes), "sheets": [], "centre_lines": 0,
        }
        self.prefix = ""  # "sheet 2: " on further sheets

    def step(self, name: str, action: Callable[[], Any]) -> Any:
        name = self.prefix + name
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
            list(plan.profile), "SectionView", plan.profile_type or "Offset", 1,
            parent.GenerativeBehavior))

    def detail(self, plan: ViewPlan, parent: Any) -> Any:
        cx, cy, radius = plan.detail
        return self._new(plan, lambda gb: gb.DefineCircularDetailView(
            cx, cy, radius, parent.GenerativeBehavior))

    def auxiliary(self, plan: ViewPlan, parent: Any) -> tuple[Any, str]:
        """DefineAuxiliaryView from the cutting line in the parent; its
        signature is not settled (V5 documents the line's two points and the
        parent), so the known variants are tried in turn."""
        x1, y1, x2, y2 = plan.profile
        pgb = parent.GenerativeBehavior
        variants = [
            ("DefineAuxiliaryView(x1, y1, x2, y2, parent)",
             lambda gb: gb.DefineAuxiliaryView(x1, y1, x2, y2, pgb)),
            ("DefineAuxiliaryView(x1, y1, x2, y2, 1, parent)",
             lambda gb: gb.DefineAuxiliaryView(x1, y1, x2, y2, 1, pgb)),
            ("DefineAuxiliaryView(profile, parent)",
             lambda gb: gb.DefineAuxiliaryView(list(plan.profile), pgb)),
        ]
        used: list[str] = []

        def define(gb: Any) -> None:
            errors = []
            for label, call in variants:
                try:
                    call(gb)
                except Exception as error:
                    errors.append(f"{label}: {error}")
                    continue
                used.append(label)
                return
            raise RuntimeError("; ".join(errors))

        return self._new(plan, define), used[0]


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
            "iProfileType": FACTORY_PROFILE_TYPES[plan.profile_type or "Offset"], "iSideToDraw": 1,
            "iParentView": parent})

    def detail(self, plan: ViewPlan, parent: Any) -> Any:
        cx, cy, radius = plan.detail
        return self._make(plan, "DefineCircularDetailView", {
            "iXCenter": cx, "iYCenter": cy, "iRadius": radius, "iParentView": parent})

    def auxiliary(self, plan: ViewPlan, parent: Any) -> tuple[Any, str]:
        x1, y1, x2, y2 = plan.profile
        view = self._make(plan, "DefineAuxiliaryView", {
            "iX1": x1, "iY1": y1, "iX2": x2, "iY2": y2, "iXStart": x1, "iYStart": y1,
            "iXEnd": x2, "iYEnd": y2, "iProfile": list(plan.profile), "iSideToDraw": 1,
            "iParentView": parent})
        return view, "DrawingDefineGenView.DefineAuxiliaryView"


def _place(view: Any, plan: ViewPlan) -> None:
    view.x, view.y = plan.anchor
    view.Scale = plan.scale


class DrawingBuilder:
    def __init__(self, conn: Any, log: Callable[[str], None] = lambda _: None,
                 pdf_dir: str | None = None, dxf_dir: str | None = None) -> None:
        self.conn = conn
        self.log = log
        self.pdf_dir = pdf_dir
        self.dxf_dir = dxf_dir
        self._generated: dict[int, dict[str, Any]] = {}

    # ── Entry points ──

    def draw(self, plan: DrawingPlan, source: Any,
             sources: dict[str, Callable[[], Any]] | None = None) -> dict[str, Any]:
        """Make one drawing, every sheet of it. `source`: on V5 the part or
        product document; on 3DEXPERIENCE the list of product instances to
        project (the editor's root occurrence, or an occurrence of the part).
        `sources` gives, by part id, the source of a sheet that projects
        another part (an assembly's part sheets)."""
        route = "3dx" if self.conn.is_3dx else "v5"
        rec = _Recorder(plan, route, self.log)
        try:
            drawing, first = self._new_drawing()
        except Exception as error:
            rec.entry["error"] = f"no drawing: {error}"
            return rec.entry
        rec.entry["catia_name"] = self._title_of(drawing)
        made: list[dict[str, Any]] = []
        sheets: list[tuple[Any, DrawingPlan]] = []
        for index, sheet_plan in enumerate(plan.sheets):
            rec.prefix = "" if index == 0 else f"sheet {index + 1}: "
            row = {"index": index + 1, "name": sheet_plan.name, "sheet": sheet_plan.sheet,
                   "scale": sheet_plan.scale_label, "source": sheet_plan.source, "ok": False}
            rec.entry["sheets"].append(row)
            sheet = first if index == 0 else rec.step(
                "new sheet", lambda p=sheet_plan, i=index: self._add_sheet(drawing, p, i))
            if sheet is None:
                made.append({})
                continue
            sheets.append((sheet, sheet_plan))
            self._setup_sheet(rec, sheet, sheet_plan, index)
            try:
                if sheet_plan.source is None:
                    projected = source
                else:
                    if sheet_plan.source not in (sources or {}):
                        raise DrawingSkipped(f"no source for {sheet_plan.source}")
                    projected = sources[sheet_plan.source]()
                maker = self._maker(sheet, projected)
            except Exception as error:
                row["error"] = f"no way to make views: {error}"
                if index == 0:
                    rec.entry["error"] = row["error"]
                    return rec.entry
                made.append({})
                self._title_block(rec, sheet, sheet_plan)
                continue
            if index == 0:
                rec.entry["route"] = (f"{route}/generative" if isinstance(maker, _GenerativeViews)
                                      else f"{route}/factory")
            views = self._views(rec, sheet_plan, maker, made, index + 1)
            made.append(views)
            self._centre_lines(rec, drawing, sheet_plan, views)
            self._title_block(rec, sheet, sheet_plan)
            rec.entry["annotations"] += self._annotations(rec, sheet_plan, views)
            if sheet_plan.bom is not None:
                self._bom(rec, sheet, sheet_plan)
                rec.entry["balloons"] = self._balloons(rec, sheet_plan, views)
            rec.entry["dimensions"] += self._dimensions(rec, drawing, sheet, sheet_plan, views,
                                                       index + 1)
            mine = [v for v in rec.entry["views"] if v["sheet"] == index + 1]
            row["ok"] = bool(mine) and mine[0]["ok"]
        rec.prefix = ""
        rec.step("update", lambda: drawing.Update())
        with contextlib.suppress(Exception):
            self.conn.refresh_display()
        for fmt, folder in (("pdf", self.pdf_dir), ("dxf", self.dxf_dir)):
            if folder:
                files = rec.step(f"{fmt} export", lambda f=fmt, d=folder: self._export(
                    rec, drawing, plan, sheets, f, d)) or []
                rec.entry[fmt] = files[0] if files else None
                rec.entry[f"{fmt}_files"] = files
        first_rows = [v for v in rec.entry["views"] if v["sheet"] == 1 and v["ok"]]
        rec.entry["ok"] = bool(first_rows) and first_rows[0]["kind"] == "front"
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

    def _add_sheet(self, drawing: Any, plan: DrawingPlan, index: int) -> Any:
        sheet = drawing.Sheets.Add(self._sheet_name(plan, index))
        with contextlib.suppress(Exception):
            sheet.Activate()
        return sheet

    @staticmethod
    def _sheet_name(plan: DrawingPlan, index: int) -> str:
        if index == 0:
            return f"{plan.sheet} {plan.scale_label}"
        return f"{index + 1} {plan.name} {plan.scale_label}"

    def _title_of(self, drawing: Any) -> str:
        if self.conn.is_3dx:
            with contextlib.suppress(Exception):
                from catia_mcp.plm import PLMSession

                return PLMSession(self.conn).title_of(drawing)
        try:
            return str(drawing.Name)
        except Exception:
            return "<drawing>"

    def _setup_sheet(self, rec: _Recorder, sheet: Any, plan: DrawingPlan,
                     index: int = 0) -> None:
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
            if index == 0:
                rec.entry["paper_mm"] = [round(width, 1), round(height, 1)]
            if abs(width - plan.width) > 1 or abs(height - plan.height) > 1:
                raise RuntimeError(f"the sheet is {width:g} x {height:g} mm, not "
                                   f"{plan.width:g} x {plan.height:g} ({plan.sheet})")

        def projection() -> None:
            member = "catFirstAngle" if plan.projection == "first" else "catThirdAngle"
            sheet.ProjectionMethod = _enum([sheet], "CatSheetProjectionMethod", member,
                                           PROJECTION_METHODS[plan.projection])

        def name() -> None:
            sheet.Name = self._sheet_name(plan, index)

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

    def _views(self, rec: _Recorder, plan: DrawingPlan, maker: Any,
               earlier: list[dict[str, Any]] | None = None, sheet: int = 1) -> dict[str, Any]:
        made: dict[str, Any] = {}
        for view in plan.views:
            row: dict[str, Any] = {"name": view.name, "kind": view.kind,
                                   "scale": view.scale, "ok": False, "sheet": sheet}
            rec.entry["views"].append(row)
            parent = made.get(view.parent) if view.parent else None
            if view.parent and parent is None:  # on an earlier sheet of the same part
                parent = next((m[view.parent] for m in reversed(earlier or [])
                               if view.parent in m), None)
            if view.parent and parent is None and not view.independent \
                    and view.kind != "auxiliary":
                row["error"] = f"its parent {view.parent} was not made"
                continue
            try:
                if view.kind in ("front", "isometric"):
                    catia = getattr(maker, view.kind)(view)
                elif view.kind == "projection" and view.independent:
                    catia = maker.front(view)  # its parent is on another sheet
                    row["route"] = "a front view of its plane (the parent is on another sheet)"
                elif view.kind == "auxiliary":
                    catia = self._auxiliary(rec, maker, view, parent,
                                            view.parent in made and not view.independent, row)
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
                kind = {"section": "SECTION ", "detail": "DETAIL "}.get(view.kind, "VIEW ")
                with contextlib.suppress(Exception):
                    catia.SetViewName(kind, view.label, "")
            with contextlib.suppress(Exception):
                row["applied_scale"] = float(catia.Scale)
            if view.kind in ORTHOGRAPHIC:
                row["generation"] = self._generation_settings(catia)
                with contextlib.suppress(Exception):
                    # What a manual dimension would need: the generated
                    # geometry exposed to automation (none on V5R20, R428).
                    row["geometric_elements"] = int(catia.GeometricElements.Count)
        return made

    def _auxiliary(self, rec: _Recorder, maker: Any, view: ViewPlan, parent: Any,
                   same_sheet: bool, row: dict[str, Any]) -> Any:
        """DefineAuxiliaryView where the parent is on this sheet; else, or
        when CATIA refuses it, a view along the face's normal (a front view
        of that plane) with its arrow and letter drawn in the parent."""
        if same_sheet and parent is not None:
            try:
                catia, used = maker.auxiliary(view, parent)
                row["route"] = used
                return catia
            except Exception as error:
                row["auxiliary_refused"] = str(error)
        catia = maker.front(view)
        row["route"] = "a front view along the face's normal"
        if parent is not None and view.arrow is not None:
            row["arrow"] = bool(rec.step(f"arrow {view.label}", lambda: self._arrow(parent, view)))
        return catia

    def _arrow(self, parent: Any, view: ViewPlan) -> bool:
        tx, ty, hx, hy = view.arrow
        with contextlib.suppress(Exception):
            parent.Activate()
        factory = parent.Factory2D
        factory.CreateLine(tx, ty, hx, hy)
        length = math.hypot(hx - tx, hy - ty) or 1.0
        dx, dy = (tx - hx) / length, (ty - hy) / length
        head = 3.0 / view.scale
        for turn in (math.radians(20), -math.radians(20)):
            c, s = math.cos(turn), math.sin(turn)
            factory.CreateLine(hx, hy, hx + head * (c * dx - s * dy), hy + head * (s * dx + c * dy))
        self._text(parent, view.label, tx + dx * 2.0 / view.scale, ty + dy * 2.0 / view.scale,
                   TEXT_HEIGHT)
        return True

    def _generation_settings(self, view: Any) -> dict[str, Any]:
        """Centre lines, axes and threads by the view's own generation
        settings, where it has them: each found, set and read back."""
        from catia_mcp.automation import name_exists

        targets = []
        with contextlib.suppress(Exception):
            if name_exists(view, "GenerativeBehavior"):
                targets.append(("GenerativeBehavior", view.GenerativeBehavior))
        targets.append(("view", view))
        out: dict[str, Any] = {}
        changed = False
        for what, names in GENERATION_SETTINGS.items():
            out[what] = None
            for label, target in targets:
                for name in names:
                    if out[what] is not None or not name_exists(target, name):
                        continue
                    try:
                        setattr(target, name, True)
                        value = getattr(target, name)
                    except Exception:
                        continue
                    if value:
                        out[what] = {"property": f"{label}.{name}", "value": _plain(value)}
                        changed = True
        if changed:
            for update in (lambda: view.GenerativeBehavior.Update(), lambda: view.Update()):
                with contextlib.suppress(Exception):
                    update()
                    break
        self._generated[id(view)] = out
        return out

    # ── Centre lines ──

    def _centre_lines(self, rec: _Recorder, drawing: Any, plan: DrawingPlan,
                      views: dict[str, Any]) -> None:
        """Each view's centre marks and axis lines, dash-dot, unless the
        view's generation settings already make them."""
        by_view: dict[str, list[Any]] = {}
        for line in plan.centre_lines:
            by_view.setdefault(line.view, []).append(line)
        for name, lines in by_view.items():
            view = views.get(name)
            if view is None:
                continue
            generated = self._generated.get(id(view)) or {}
            todo = [line for line in lines
                    if not (line.kind in ("mark", "circle") and generated.get("centre lines"))
                    and not (line.kind == "axis" and generated.get("axes"))]
            if not todo:
                continue

            def draw(view=view, todo=todo) -> list[Any]:
                with contextlib.suppress(Exception):
                    view.Activate()
                factory = view.Factory2D
                items = [factory.CreateLine(*segment) for line in todo
                         for segment in line.lines]
                return items + [factory.CreateClosedCircle(*line.circle) for line in todo
                                if line.circle is not None]

            items = rec.step(f"centre lines in {name}", draw)
            if items is None:
                continue
            rec.entry["centre_lines"] += len(todo)
            style = rec.step(f"dash-dot line type in {name}",
                             lambda d=drawing, i=items: self._dash_dot(d, i))
            if style:
                rec.entry["centre_line_type"] = style

    def _dash_dot(self, drawing: Any, items: list[Any]) -> str:
        """The axis line type on the lines, through the selection's visual
        properties (the drawing's, else the editor's)."""
        routes: list[tuple[str, Callable[[], Any]]] = [("drawing.Selection",
                                                        lambda: drawing.Selection)]
        if self.conn.is_3dx:
            routes.append(("ActiveEditor.Selection", lambda: self.conn.active_editor.Selection))
        errors = []
        for label, get in routes:
            try:
                selection = get()
                selection.Clear()
                for item in items:
                    selection.Add(item)
                properties = selection.VisProperties
                properties.SetRealLineType(AXIS_LINE_TYPE, 1)
                with contextlib.suppress(Exception):
                    properties.SetRealWidth(THIN_LINE, 1)
                selection.Clear()
                return f"{label}.VisProperties.SetRealLineType({AXIS_LINE_TYPE})"
            except Exception as error:
                errors.append(f"{label}: {error}")
        raise RuntimeError("the lines stay solid: " + "; ".join(errors))

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
                if note.plain:  # the font may lack the ISO 129 symbols
                    with contextlib.suppress(Exception):
                        if str(text.Text) != note.text:
                            text.Text = note.plain
                            rec.entry["callouts_in_words"] = rec.entry.get(
                                "callouts_in_words", 0) + 1
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
                    views: dict[str, Any], index: int = 1) -> int:
        """GenerateDimensions with the drawing, the sheet and each generative
        view active (a silent no-op otherwise, field-proven on V5R20); the
        dimensions counted in every view. The views left without any are
        listed: CATIA exposes no generated geometry to dimension by hand."""
        with contextlib.suppress(Exception):
            drawing.Activate()
        with contextlib.suppress(Exception):
            sheet.Activate()
        by_view: dict[str, int] = rec.entry.setdefault("dimensions_by_view", {})
        prefix = "" if index == 1 else f"sheet {index}: "
        for view_plan in plan.views:
            view = views.get(view_plan.name)
            if view is None or view_plan.kind in ("isometric", "detail"):
                continue

            def generate(view=view) -> None:
                view.Activate()
                sheet.GenerateDimensions()

            rec.step(f"generate dimensions in {view_plan.name}", generate)
        total = 0
        for name, view in views.items():
            with contextlib.suppress(Exception):
                count = int(view.Dimensions.Count)
                by_view[prefix + name] = count
                total += count
        kinds = {v.name: v.kind for v in plan.views}
        bare = [prefix + name for name in views if by_view.get(prefix + name) == 0
                and kinds.get(name) in ORTHOGRAPHIC]
        if bare:
            rec.entry.setdefault("views_without_dimensions", []).extend(bare)
        return total

    # ── Export ──

    def _export_routes(self, drawing: Any, fmt: str) -> list[tuple[str, Callable[[str], Any]]]:
        """ExportData on the drawing with the format in lower then upper case;
        on 3DEXPERIENCE also on the editor's compatibility document (live
        R428: ExportData refused, E_FAIL). Never a print dialog."""
        routes = [(f'drawing.ExportData(path, "{f}")',
                   lambda path, f=f: drawing.ExportData(path, f)) for f in (fmt, fmt.upper())]
        if self.conn.is_3dx:
            routes += [(f'ActiveDocument.ExportData(path, "{f}")',
                        lambda path, f=f: self.conn.app.ActiveDocument.ExportData(path, f))
                       for f in (fmt, fmt.upper())]
        return routes

    def _export(self, rec: _Recorder, drawing: Any, plan: DrawingPlan,
                sheets: list[tuple[Any, DrawingPlan]], fmt: str, folder: str) -> list[str]:
        """Each sheet, active in turn, to its own file (`NAME.pdf`, or
        `NAME_2.pdf`... with several sheets), checked on disk. The routes are
        tried in order; once one writes, the next sheets use it; when every
        route fails on the first sheet, the others are not tried."""
        os.makedirs(folder, exist_ok=True)
        attempts = rec.entry.setdefault("exports", {}).setdefault(fmt, [])
        routes = self._export_routes(drawing, fmt)
        files: list[str] = []
        stem = safe_file_name(plan.name)
        for index, (sheet, _) in enumerate(sheets, start=1):
            name = stem if len(sheets) == 1 else f"{stem}_{index}"
            path = os.path.abspath(os.path.join(folder, f"{name}.{fmt}"))
            with contextlib.suppress(Exception):
                sheet.Activate()
            written = None
            for label, call in routes:
                if os.path.exists(path):
                    os.remove(path)
                started = time.time() - 1
                try:
                    call(path)
                except Exception as error:
                    attempts.append({"sheet": index, "route": label, "error": str(error)})
                    continue
                if not os.path.exists(path):  # CATIA may name the file itself
                    others = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                              if f.startswith(name) and f.lower().endswith(f".{fmt}")
                              and os.path.getmtime(os.path.join(folder, f)) >= started]
                    path = os.path.abspath(others[0]) if others else path
                if os.path.exists(path):
                    attempts.append({"sheet": index, "route": label, "ok": True, "path": path})
                    written, routes = path, [(label, call)]
                    break
                attempts.append({"sheet": index, "route": label, "error": "no file written"})
            if written:
                files.append(written)
            elif not files:
                break  # every route refused: not tried again on the other sheets
        if not files:
            raise RuntimeError(f"no {fmt.upper()} written: " + "; ".join(
                f"{a['route']}: {a['error']}" for a in attempts if "error" in a))
        return files


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
            "R428). Import the model's STEP and build with --assembly adopt (a 3D Part, "
            "--plm-type 3DPart, also works where the licence allows it; the student "
            "licence refused it live).") from None
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
                  log: Callable[[str], None] = print,
                  dxf_dir: str | None = None) -> dict[str, Any]:
    """Draw the assembly (the V5 product the assembler made, or the adopted
    3DEXPERIENCE product) with one sheet per distinct part after the
    product's, then every built part no sheet of it shows (all of them when
    the product's drawing could not be made): the report's `drawing`
    section. Never raises: a failure is recorded."""
    from catia_mcp.errors import SessionError
    from catia_mcp.tree.drawing_plan import plan_drawings

    section: dict[str, Any] = {"mode": "auto", "parts": {}, "assembly": None}
    plans, product, problems = plan_drawings(tree, assembly, options)
    if problems:
        section["plan_problems"] = problems
    drafter = DrawingBuilder(conn, log=log, pdf_dir=pdf_dir, dxf_dir=dxf_dir)
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

    def run(label: str, plan: Any, source: Callable[[], Any],
            sources: dict[str, Callable[[], Any]] | None = None) -> dict[str, Any] | None:
        log(f"drawing {label}: " + "; ".join(
            f"{p.sheet} {p.scale_label}, {', '.join(v.name for v in p.views)}"
            for p in plan.sheets))
        try:
            return worker.run_guarded(lambda: drafter.draw(plan, source(), sources),
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
    covered: dict[str, int] = {}
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

        sources = {i: (lambda i=i: source_of(i, names.get(i, i))) for i in plans}
        entry = run(product.name, product, product_source, sources)
        section["assembly"] = entry
        if entry is None:
            return section
        if entry.get("catia_name") and not entry.get("error"):
            covered = product.covers
    for part_id, plan in plans.items():
        if part_id in covered:
            index = covered[part_id]
            rows = section["assembly"].get("sheets") or []
            sheet = rows[index - 1] if index <= len(rows) else {}
            section["parts"][part_id] = {"ok": bool(sheet.get("ok")), "drawing": product.name,
                                         "catia_name": section["assembly"].get("catia_name"),
                                         "sheet": index, "error": sheet.get("error")}
            continue
        entry = run(part_id, plan, lambda i=part_id: source_of(i, names[i]))
        if entry is None:
            return section
        section["parts"][part_id] = entry
    for label, entry in [*section["parts"].items(), ("assembly", section["assembly"])]:
        if not entry:
            continue
        if entry.get("drawing"):
            log(f"drawing {label}: {'ok' if entry['ok'] else 'incomplete'}, sheet "
                f"{entry['sheet']} of the drawing of {entry['drawing']}")
            continue
        state = "ok" if entry.get("ok") else (entry.get("skipped") or entry.get("error")
                                              or "incomplete")
        files = [f for key in ("pdf_files", "dxf_files") for f in entry.get(key) or []]
        log(f"drawing {label}: {state}; {len(entry.get('sheets') or [1])} sheet(s), "
            f"{entry.get('dimensions', 0)} dimension(s)"
            f"{', files ' + ', '.join(files) if files else ''}")
    return section
