"""Live probe of every known route to a drawing of the active part or product.

The tree build's --drawing auto (catia_mcp/tree/drawing_builder.py) makes a
drawing of each built part. This probe records what this CATIA session
allows, step by step, without saving anything (the drawings it creates stay
open, unsaved: close them without saving):

1. the source: the active document (V5) or editor (3DEXPERIENCE), its kind,
   and its product root occurrence when it has one (a standalone 3D Shape
   has none; CATIA creates no generative view for it, live R428);
2. each way to create a drawing: `Documents.Add("Drawing")` (V5, and tried
   on 3DEXPERIENCE), `PLMNewService.PLMCreate "Drawing"` (3DEXPERIENCE);
3. in the first drawing made: the sheet's members, the enums the builder
   uses (paper size, orientation, projection method, text frame) and the
   signature of every `DrawingDefineGenView.Define*` function;
4. the sheet set to A3 landscape, first angle, read back;
5. each way to make a front view, until one works: `GenerativeBehavior`
   with the part (or product) document, root occurrence, Part, its
   VPMRepReference; the `DrawingDefineGenView` factory with the root
   occurrence, the first child occurrence, the VPMRepReference;
6. from that front view: top and left projections, an isometric view, a
   section through the middle, a detail; GenerateDimensions per view; a
   text with a leader; a balloon (a circled text); a table; a line and a
   circle in the background view;
7. the second slice's routes from that front view: the view's generation
   settings for centre lines, axes and threads (every member whose name
   says so, then the ones the builder sets, read back); a dash-dot centre
   line drawn in the view; an auxiliary view (DefineAuxiliaryView, each
   known signature, else a view along the normal); a stepped and an aligned
   section; texts with the ISO 129 symbols, read back; the generated
   geometry a manual dimension would need (GeometricElements); a second
   sheet with a line and a text;
8. the PDF and DXF export of the drawing, by every known route, once each;
   the signature of any PrintToFile method (called only with
   --print-to-file; never PrintOut, which opens a dialog);
9. with --tree TREE.json: the whole drafting step of the tree build for the
   tree's first part (or --part ID), on the active source (the part must be
   the one the tree describes, already built).

With --window NAME, that one window is activated first (activating every
window in turn hung CATIA live; CATIA may hold hundreds of windows).

Usage (Windows, CATIA started and logged in, the built part or product in
the active window):
    python scripts/probe_drawing.py --report drawing_probe.json
    python scripts/probe_drawing.py --window "DraftingParts A.1" --report drawing_probe.json
    python scripts/probe_drawing.py --tree tests\\fixtures\\trees\\m1_holes.json --pdf out
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

DEFINE_METHODS = ("DefineFrontView", "DefineProjectionView", "DefineIsometricView",
                  "DefineSectionView", "DefineCircularDetailView", "DefineAuxiliaryView",
                  "DefineBreakoutView", "DefineUnfoldedView")
ENUMS = ("CatPaperSize", "CatPaperOrientation", "CatSheetProjectionMethod", "CatTextFrameType",
         "CatProjViewType", "CatDimType")


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as error:  # recorded, never fatal
        return f"error: {error}"


def _type(obj: Any) -> str | None:
    from catia_mcp.introspect import type_name

    return _safe(type_name, obj) if obj is not None else None


class Probe:
    def __init__(self, conn: Any, worker: Any, report: dict[str, Any]) -> None:
        self.conn, self.worker, self.report = conn, worker, report
        self.editor = self.source = self.root = self.part = None
        self.drawing = self.sheet = self.front = None

    def step(self, name: str, action, guard: bool = True) -> Any:
        """Run one step under the watchdog; record its result or its error."""
        print(f"  {name}")
        started = time.monotonic()
        try:
            result = self.worker.run_guarded(action, label=name) if guard else action()
            self.report["steps"].append({"step": name, "ok": True, "result": result,
                                         "s": round(time.monotonic() - started, 2)})
            return result
        except Exception as error:
            self.report["steps"].append({"step": name, "ok": False, "error": str(error),
                                         "traceback": traceback.format_exc(limit=3),
                                         "s": round(time.monotonic() - started, 2)})
            return None

    # ── 1. Source ──

    def find_source(self) -> dict[str, Any]:
        conn = self.conn
        out: dict[str, Any] = {"platform": "3dx" if conn.is_3dx else "v5"}
        with contextlib.suppress(Exception):
            out["window"] = str(conn.app.ActiveWindow.Caption)
        if not conn.is_3dx:
            document = conn.app.ActiveDocument
            self.source = document
            out["document"] = str(document.Name)
            return out
        from catia_mcp.plm import PLMSession

        self.editor = conn.active_editor
        obj = conn.active_object()
        out["kind"] = conn.classify(obj)
        out["title"] = PLMSession(conn).title_of(obj)
        if out["kind"] == "part":
            self.part = obj
        try:
            self.root = conn.root_occurrence(self.editor)
            out["root_occurrence"] = _type(self.root)
            out["root_title"] = PLMSession(conn).title_of(self.root)
            children = PLMSession(conn).child_occurrences(self.root)
            out["child_occurrences"] = len(children)
        except Exception as error:
            out["root_occurrence"] = f"none: {error}"
        self.source = obj
        return out

    # ── 2. Drawing creation ──

    def create_routes(self) -> None:
        """Every route is tried; the first drawing made is the one probed
        further (on 3DEXPERIENCE the PLM route is tried first)."""
        routes = [("Documents.Add(\"Drawing\")", self._documents_add)]
        if self.conn.is_3dx:
            routes.insert(0, ("PLMNewService.PLMCreate \"Drawing\"", self._plm_create))
        for name, route in routes:
            made = self.step(f"create drawing: {name}", route)
            if made and "drawing_route" not in self.report:
                self.report["drawing_route"] = name

    def _keep(self, drawing: Any) -> dict[str, Any]:
        try:
            sheet = drawing.Sheets.ActiveSheet
        except Exception:
            sheet = drawing.ActiveSheet
        if self.drawing is None:
            self.drawing, self.sheet = drawing, sheet
        views = sheet.Views
        return {"drawing": _type(drawing), "sheet": str(sheet.Name), "views": [
            str(views.Item(i).Name) for i in range(1, int(views.Count) + 1)]}

    def _documents_add(self) -> dict[str, Any]:
        return self._keep(self.conn.app.Documents.Add("Drawing"))

    def _plm_create(self) -> dict[str, Any]:
        from catia_mcp.plm import PLMSession

        PLMSession(self.conn).create("Drawing")
        return self._keep(self.conn.active_object())

    # ── 3. Members ──

    def members(self) -> dict[str, Any]:
        from catia_mcp.automation import name_exists
        from catia_mcp.introspect import find_enum, method_params

        views = self.sheet.Views
        out: dict[str, Any] = {
            "sheet_members": {m: name_exists(self.sheet, m) for m in (
                "PaperSize", "Orientation", "ProjectionMethod", "Scale", "GenerateDimensions",
                "GetPaperWidth", "PaperWidth", "SetPaperWidth")},
            "view_1_generative_behavior": name_exists(views.Item(1), "GenerativeBehavior"),
            "views_factory": name_exists(views, "DrawingDefineGenView"),
            "drawing_export": name_exists(self.drawing, "ExportData"),
        }
        if out["views_factory"]:
            factory = views.DrawingDefineGenView
            out["factory_signatures"] = {m: method_params(factory, m) for m in DEFINE_METHODS}
        out["enums"] = {e: _safe(find_enum, [self.sheet, self.drawing, self.conn.app], e)
                        for e in ENUMS}
        return out

    def sheet_format(self) -> dict[str, Any]:
        from catia_mcp.tree.drawing_builder import LANDSCAPE, PAPER_SIZES, _enum

        sheet = self.sheet
        sheet.PaperSize = _enum([sheet], "CatPaperSize", "catPaperA3", PAPER_SIZES["A3"])
        sheet.Orientation = _enum([sheet], "CatPaperOrientation", "catPaperLandscape", LANDSCAPE)
        sheet.ProjectionMethod = _enum([sheet], "CatSheetProjectionMethod", "catFirstAngle", 0)
        out: dict[str, Any] = {"PaperSize": _safe(lambda: int(sheet.PaperSize)),
                               "Orientation": _safe(lambda: int(sheet.Orientation)),
                               "ProjectionMethod": _safe(lambda: int(sheet.ProjectionMethod))}
        out["GetPaperWidth"] = _safe(lambda: float(sheet.GetPaperWidth()))
        out["GetPaperHeight"] = _safe(lambda: float(sheet.GetPaperHeight()))
        out["PaperWidth"] = _safe(lambda: float(sheet.PaperWidth))
        out["PaperHeight"] = _safe(lambda: float(sheet.PaperHeight))
        return out

    # ── 5. Front view ──

    def front_routes(self) -> None:
        candidates: list[tuple[str, Any]] = []
        if not self.conn.is_3dx:
            candidates.append(("GenerativeBehavior, Document = the document",
                               lambda: self._generative(self.source)))
        else:
            from catia_mcp.automation import safe_get
            from catia_mcp.plm import PLMSession

            plm = PLMSession(self.conn)
            if self.root is not None:
                candidates.append(("factory, iListofPrdInst = [root occurrence]",
                                   lambda: self._factory([self.root])))
                children = plm.child_occurrences(self.root)
                if children:
                    candidates.append(("factory, iListofPrdInst = [first child occurrence]",
                                       lambda: self._factory([children[0]])))
            if self.part is not None:
                rep = safe_get(self.part, "Parent")
                candidates.append(("factory, iListofPrdInst = [VPMRepReference]",
                                   lambda: self._factory([rep])))
            for label, document in (("root occurrence", self.root), ("Part", self.part)):
                if document is not None:
                    candidates.append((f"GenerativeBehavior, Document = {label}",
                                       lambda d=document: self._generative(d)))
        for name, route in candidates:
            if self.front is not None:
                break
            made = self.step(f"front view: {name}", route)
            if made:
                self.report["front_route"] = name

    def _count(self) -> int:
        return int(self.sheet.Views.Count)

    def _generative(self, document: Any) -> dict[str, Any] | None:
        before = self._count()
        view = self.sheet.Views.Add("Probe front")
        gb = view.GenerativeBehavior
        gb.Document = document
        gb.DefineFrontView(1, 0, 0, 0, 1, 0)
        view.x, view.y = 150.0, 180.0
        gb.Update()
        self.front = view
        return {"views_before": before, "views_after": self._count(),
                "name": str(view.Name)}

    def _factory(self, instances: list[Any]) -> dict[str, Any]:
        from catia_mcp.tools.drawing import DraftingTools

        before = self._count()
        view = DraftingTools(self.conn)._factory_view(
            self.sheet, "DefineFrontView",
            {"iListofPrdInst": instances, "iPlane": [1.0, 0, 0, 0, 1.0, 0]},
            "Probe front", 150.0, 180.0, None)
        self.front = view
        return {"views_before": before, "views_after": self._count(),
                "name": str(view.Name)}

    # ── 6. Derived views and annotations ──

    def derived(self) -> None:
        from catia_mcp.automation import name_exists
        from catia_mcp.tree.drawing_builder import FRAME_CIRCLE, _enum

        front = self.front
        factory = self.conn.is_3dx and not name_exists(front, "GenerativeBehavior")
        tools = None
        if factory:
            from catia_mcp.tools.drawing import DraftingTools

            tools = DraftingTools(self.conn)

        def make(name: str, method: str, generative, values: dict[str, Any], x, y):
            if factory:
                view = tools._factory_view(self.sheet, method, values, name, x, y, None)
            else:
                view = self.sheet.Views.Add(name)
                view.GenerativeBehavior.Document = front.GenerativeBehavior.Document
                generative(view.GenerativeBehavior)
                view.x, view.y = x, y
                view.GenerativeBehavior.Update()
            return {"name": str(view.Name), "views": self._count()}

        parent_gb = None if factory else front.GenerativeBehavior
        self.step("projection view (top)", lambda: make(
            "Probe top", "DefineProjectionView",
            lambda gb: gb.DefineProjectionView(parent_gb, 2),
            {"iParentView": front, "iType": 2}, 150.0, 80.0))
        self.step("projection view (left)", lambda: make(
            "Probe left", "DefineProjectionView",
            lambda gb: gb.DefineProjectionView(parent_gb, 1),
            {"iParentView": front, "iType": 1}, 280.0, 180.0))
        self.step("isometric view", lambda: make(
            "Probe iso", "DefineIsometricView",
            lambda gb: gb.DefineIsometricView(0.7071, -0.7071, 0, -0.4082, -0.4082, 0.8165),
            {"iListofPrdInst": [self.root], "iPlane": [0.7071, -0.7071, 0, -0.4082, -0.4082,
                                                       0.8165]}, 330.0, 80.0))
        profile = [-1000.0, 0.0, 1000.0, 0.0]
        self.step("section view through y = 0", lambda: make(
            "Probe section", "DefineSectionView",
            lambda gb: gb.DefineSectionView(profile, "SectionView", "Offset", 1, parent_gb),
            {"iProfile": profile, "iSectionType": "SectionView",
             "iProfileType": "CatOffsetSectionProfile", "iSideToDraw": 1,
             "iParentView": front}, 150.0, 40.0))
        self.step("detail view at the origin, r = 10", lambda: make(
            "Probe detail", "DefineCircularDetailView",
            lambda gb: gb.DefineCircularDetailView(0.0, 0.0, 10.0, parent_gb),
            {"iXCenter": 0.0, "iYCenter": 0.0, "iRadius": 10.0, "iParentView": front},
            360.0, 200.0))

        def dimensions() -> dict[str, Any]:
            with contextlib.suppress(Exception):
                self.drawing.Activate()
            out = {}
            views = self.sheet.Views
            for i in range(3, int(views.Count) + 1):
                view = views.Item(i)
                with contextlib.suppress(Exception):
                    view.Activate()
                before = int(view.Dimensions.Count)
                self.sheet.GenerateDimensions()
                out[str(view.Name)] = [before, int(view.Dimensions.Count)]
            return out

        self.step("GenerateDimensions per view (before, after)", dimensions)

        def text() -> dict[str, Any]:
            item = front.Texts.Add("Ø6 THRU", 20.0, 20.0)
            item.SetFontSize(0, 0, 3.5)
            item.Leaders.Add(3.0, 3.0)
            return {"text": str(item.Text)}

        self.step("text with a leader", text)

        def balloon() -> dict[str, Any]:
            item = front.Texts.Add("1", -30.0, 30.0)
            item.FrameType = _enum([item], "CatTextFrameType", "catCircle", FRAME_CIRCLE)
            item.Leaders.Add(0.0, 0.0)
            return {"frame": _safe(lambda: int(item.FrameType))}

        self.step("balloon (circled text, leader)", balloon)

        def background() -> dict[str, Any]:
            views = self.sheet.Views
            try:
                view = views.Item("Background View")
            except Exception:
                view = views.Item(2)
            view.Activate()
            factory2d = view.Factory2D
            factory2d.CreateLine(20.0, 10.0, 410.0, 10.0)
            factory2d.CreateClosedCircle(400.0, 30.0, 3.0)
            item = view.Texts.Add("Probe title", 240.0, 30.0)
            table = view.Tables.Add(230.0, 90.0, 3, 3, 8.0, 40.0)
            table.SetCellString(1, 1, "Item")
            table.SetCellString(2, 2, "Plate")
            views.Item(1).Activate()
            return {"background": str(view.Name), "text": str(item.Text),
                    "table": str(table.Name)}

        self.step("background view: line, circle, text, table", background)
        self.step("view label SetViewName", lambda: self.sheet.Views.Item(
            int(self.sheet.Views.Count)).SetViewName("DETAIL ", "B", ""))

    # ── 7. Second slice ──

    def second_slice(self) -> None:
        from catia_mcp.tree.drawing_builder import DrawingBuilder, _FactoryViews, _GenerativeViews
        from catia_mcp.tree.drawing_plan import ViewPlan

        front = self.front
        builder = DrawingBuilder(self.conn)
        generative = not self.conn.is_3dx or _has(front, "GenerativeBehavior")

        def settings() -> dict[str, Any]:
            from catia_mcp.introspect import describe_members

            out: dict[str, Any] = {}
            for label, target in (("GenerativeBehavior", _get(front, "GenerativeBehavior")),
                                  ("view", front)):
                if target is None:
                    continue
                members = _safe(describe_members, target)
                if isinstance(members, list):
                    out[f"{label} members"] = sorted(
                        m["name"] for m in members if any(
                            k in m["name"].lower() for k in ("axis", "center", "centre",
                                                              "thread", "hidden", "fillet")))
            out["set and read back"] = builder._generation_settings(front)
            return out

        self.report["generation_settings"] = self.step("view generation settings (centre "
                                                       "lines, axes, threads)", settings)

        def centre_line() -> dict[str, Any]:
            with contextlib.suppress(Exception):
                front.Activate()
            factory = front.Factory2D
            items = [factory.CreateLine(-40.0, 0.0, 40.0, 0.0),
                     factory.CreateLine(0.0, -40.0, 0.0, 40.0)]
            return {"line_type": builder._dash_dot(self.drawing, items)}

        self.step("centre mark drawn dash-dot in the front view", centre_line)

        def plan(name: str, kind: str, profile, **extra) -> ViewPlan:
            return ViewPlan(name, kind, (1.0, 0.0, 0.0), (0.0, 0.7071, 0.7071), 1.0,
                            extra.pop("anchor", (330.0, 200.0)), (300, 170, 360, 230),
                            parent="Probe front", profile=profile, **extra)

        maker = (_GenerativeViews(self.sheet, front.GenerativeBehavior.Document) if generative
                 else _FactoryViews(self._tools(), self.sheet, [self.root]))
        aux = plan("Probe aux", "auxiliary", [-20.0, -20.0, 20.0, 20.0], label="C",
                   arrow=(40.0, -40.0, 25.0, -25.0))

        def auxiliary() -> dict[str, Any]:
            try:
                view, used = maker.auxiliary(aux, front)
                return {"route": used, "name": str(view.Name)}
            except Exception as error:
                view = maker.front(aux)
                arrow = _safe(builder._arrow, front, aux)
                return {"refused": str(error), "fallback": "a front view along the normal",
                        "name": str(view.Name), "arrow": arrow}

        self.step("auxiliary view (DefineAuxiliaryView, else along the normal)", auxiliary)
        stepped = plan("Probe stepped", "section", [-1000.0, 0.0, 0.0, 0.0, 0.0, 10.0,
                                                    1000.0, 10.0],
                       profile_type="Offset", anchor=(150.0, 60.0))
        aligned = plan("Probe aligned", "section", [-100.0, 0.0, 0.0, 0.0, 70.7, 70.7],
                       profile_type="Aligned", anchor=(280.0, 60.0))
        for view in (stepped, aligned):
            self.step(f"{view.profile_type.lower()} section", lambda v=view: str(
                maker.section(v, front).Name))

        def symbols() -> dict[str, Any]:
            text = ("\u23006.6 THRU \u2334\u230011 \u21a76.5 / M6 - 6H \u21a78 / "
                    "4\u00d7 \u2335")
            item = front.Texts.Add(text, 40.0, -40.0)
            back = str(item.Text)
            return {"written": text, "read_back": back, "same": back == text}

        self.step("callout with ISO 129 symbols, read back", symbols)
        self.step("generated geometry exposed (GeometricElements.Count)", lambda: {
            "front": _safe(lambda: int(front.GeometricElements.Count)),
            "dimensions": _safe(lambda: int(front.Dimensions.Count))})

        def second_sheet() -> dict[str, Any]:
            sheets = self.drawing.Sheets
            sheet = sheets.Add("Probe sheet 2")
            _safe(sheet.Activate)
            views = sheet.Views
            background = views.Item("Background View") if _has_item(views, "Background View") \
                else views.Item(2)
            background.Activate()
            background.Factory2D.CreateLine(20.0, 10.0, 410.0, 10.0)
            background.Texts.Add("Probe sheet 2", 240.0, 30.0)
            out = {"sheets": int(sheets.Count), "name": str(sheet.Name),
                   "views": int(views.Count)}
            _safe(self.sheet.Activate)
            return out

        self.step("second sheet (Sheets.Add), a line and a text", second_sheet)

    def _tools(self) -> Any:
        from catia_mcp.tools.drawing import DraftingTools

        return DraftingTools(self.conn)

    # ── 8. Export ──

    def export(self, folder: str, print_to_file: bool = False) -> None:
        from catia_mcp.introspect import method_params
        from catia_mcp.tree.drawing_builder import DrawingBuilder

        os.makedirs(folder, exist_ok=True)
        builder = DrawingBuilder(self.conn)
        for fmt in ("pdf", "dxf"):
            path = os.path.abspath(os.path.join(folder, f"drawing_probe.{fmt}"))
            for label, call in builder._export_routes(self.drawing, fmt):
                def export_with(call=call, path=path) -> dict[str, Any]:
                    if os.path.exists(path):
                        os.remove(path)
                    call(path)
                    return {"written": os.path.exists(path), "path": path}

                result = self.step(f"{fmt.upper()} export: {label}", export_with)
                if result and result.get("written"):
                    break
        targets = {"sheet": self.sheet, "drawing": self.drawing,
                   "window": _get(self.conn.app, "ActiveWindow")}
        self.report["print_to_file"] = {
            name: _safe(method_params, target, "PrintToFile") for name, target in
            targets.items() if target is not None}
        if print_to_file:
            path = os.path.abspath(os.path.join(folder, "drawing_probe_print.prn"))
            for name, target in targets.items():
                if target is None or not _has(target, "PrintToFile"):
                    continue
                result = self.step(f"{name}.PrintToFile", lambda t=target: (
                    t.PrintToFile(path), {"written": os.path.exists(path)})[1])
                if result and result.get("written"):
                    break


def _has(obj: Any, name: str) -> bool:
    from catia_mcp.automation import name_exists

    return obj is not None and name_exists(obj, name)


def _get(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _has_item(collection: Any, name: str) -> bool:
    try:
        collection.Item(name)
        return True
    except Exception:
        return False


def select_window(conn: Any, wanted: str | None) -> dict[str, Any]:
    """Activate the one window named `wanted` (never every window in turn:
    that hung CATIA live); count the open windows."""
    out: dict[str, Any] = {}
    windows = conn.app.Windows
    count = int(windows.Count)
    out["windows"] = count
    if not wanted:
        return out
    for i in range(1, count + 1):
        window = windows.Item(i)
        if str(window.Name) == wanted or str(_get(window, "Caption")) == wanted:
            window.Activate()
            out["window"] = f"activated {wanted!r}"
            return out
    out["window"] = f"no open window is named {wanted!r}"
    return out


def run_builder(conn: Any, worker: Any, probe: Probe, tree_path: str, part_id: str | None,
                pdf: str | None) -> dict[str, Any]:
    """The tree build's drafting step on the active source."""
    from catia_mcp.tree.drawing_builder import DrawingBuilder, instances_3dx
    from catia_mcp.tree.drawing_plan import part_geometry, plan_part_drawing
    from catia_mcp.tree.placement import place_part

    with open(tree_path, encoding="utf-8") as handle:
        tree = json.load(handle)
    part = next(p for p in tree["parts"] if part_id in (None, p["id"]))
    placed = place_part(part)
    plan = plan_part_drawing(placed, part_geometry(placed))
    if conn.is_3dx:
        def instances() -> Any:
            try:  # the part's occurrence in an imported product
                return instances_3dx(conn, probe.editor, title=part.get("name") or part["id"])
            except Exception:
                return instances_3dx(conn, probe.editor)

        source = worker.run_guarded(instances, label="the instances")
    else:
        source = probe.source
    builder = DrawingBuilder(conn, log=print, pdf_dir=pdf, dxf_dir=pdf)
    entry = worker.run_guarded(lambda: builder.draw(plan, source), label="the drawing")
    entry["plan"] = plan.to_dict()
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", default="drawing_probe.json")
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--pdf", default="drawing_probe_pdf",
                        help="folder for the PDF and DXF exports (created)")
    parser.add_argument("--tree", help="a tree JSON whose part is the active one: also run the "
                        "tree build's drafting step on it")
    parser.add_argument("--part", help="with --tree: the part id (default: the first)")
    parser.add_argument("--window", help="the name of the window to activate first (the "
                        "imported product's); only that one is activated")
    parser.add_argument("--print-to-file", action="store_true",
                        help="also call PrintToFile where the type library has it (it may "
                             "use the default printer; never PrintOut)")
    args = parser.parse_args()

    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    worker = ComWorker(conn.watchdog, timeout_s=120, name="probe-drawing")
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}
    try:
        report["connect"] = worker.run_guarded(conn.connect, label="connect")
    except Exception as error:
        print(error)
        return 2
    probe = Probe(conn, worker, report)
    report["window"] = probe.step("window", lambda: select_window(conn, args.window))
    report["source"] = probe.step("source", probe.find_source)
    if args.tree:
        # First, while the source is still the active window.
        report["builder"] = _safe(lambda: run_builder(conn, worker, probe, args.tree, args.part,
                                                      args.pdf))
    probe.create_routes()
    if probe.sheet is not None:
        report["members"] = probe.step("members, enums, factory signatures", probe.members)
        report["sheet_format"] = probe.step("sheet A3 landscape, first angle",
                                            probe.sheet_format)
        probe.front_routes()
        if probe.front is not None:
            probe.derived()
            probe.second_slice()
        probe.export(args.pdf, args.print_to_file)
    report["summary"] = {
        "drawing_route": report.get("drawing_route"),
        "front_route": report.get("front_route"),
        "failed": [s["step"] for s in report["steps"] if not s["ok"]],
        "note": "Nothing was saved. Close the probe drawings without saving.",
    }
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
