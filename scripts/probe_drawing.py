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
7. the PDF export (ExportData) of the drawing;
8. with --tree TREE.json: the whole drafting step of the tree build for the
   tree's first part (or --part ID), on the active source (the part must be
   the one the tree describes, already built).

Usage (Windows, CATIA started and logged in, the built part or product in
the active window):
    python scripts/probe_drawing.py --report drawing_probe.json
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

    # ── 7. Export ──

    def export(self, folder: str) -> None:
        os.makedirs(folder, exist_ok=True)
        path = os.path.abspath(os.path.join(folder, "drawing_probe.pdf"))

        def export_with(target_name: str, target) -> dict[str, Any]:
            if os.path.exists(path):
                os.remove(path)
            target().ExportData(path, "pdf")
            return {"target": target_name, "written": os.path.exists(path), "path": path}

        for name, target in (("drawing", lambda: self.drawing),
                             ("drawing.Parent", lambda: self.drawing.Parent),
                             ("ActiveDocument", lambda: self.conn.app.ActiveDocument)):
            result = self.step(f"PDF export through {name}.ExportData",
                               lambda n=name, t=target: export_with(n, t))
            if result and result.get("written"):
                break


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
        source = worker.run_guarded(lambda: instances_3dx(conn, probe.editor),
                                    label="the instances")
    else:
        source = probe.source
    builder = DrawingBuilder(conn, log=print, pdf_dir=pdf)
    entry = worker.run_guarded(lambda: builder.draw(plan, source), label="the drawing")
    entry["plan"] = plan.to_dict()
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", default="drawing_probe.json")
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--pdf", default="drawing_probe_pdf",
                        help="folder for the PDF exports (created)")
    parser.add_argument("--tree", help="a tree JSON whose part is the active one: also run the "
                        "tree build's drafting step on it")
    parser.add_argument("--part", help="with --tree: the part id (default: the first)")
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
        probe.export(args.pdf)
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
