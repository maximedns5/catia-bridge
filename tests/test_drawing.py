"""The drawing of a built part or assembly: the plan (offline) and the
builder against the fake drafting model (tests/drawing_fakes.py)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree import drawing_plan as dp
from catia_mcp.tree.drawing_builder import DrawingBuilder, DrawingSkipped, make_drawings
from catia_mcp.tree.placement import place_part
from tests import drawing_fakes as fakes

FIXTURES = Path(__file__).parent / "fixtures"
TREES = FIXTURES / "trees"
DATE = "2026-11-17"


def load(name: str) -> dict:
    with open(TREES / f"{name}.json", encoding="utf-8") as handle:
        return json.load(handle)


def part_of(name: str) -> dict:
    return place_part(load(name)["parts"][0])


def plan_of(name: str, **options) -> dp.DrawingPlan:
    part = part_of(name)
    return dp.plan_part_drawing(part, dp.estimate_geometry(part),
                                dp.DrawingOptions(date=DATE, **options))


def plate(width: float, height: float, thickness: float, holes=()) -> dict:
    """A plate in XY, extruded along +Z, with simple holes drilled down."""
    w, h = width / 2, height / 2
    corners = [(-w, -h), (w, -h), (w, h), (-w, h)]
    entities = [{"type": "line", "id": f"e{i}", "start": list(corners[i]),
                 "end": list(corners[(i + 1) % 4])} for i in range(4)]
    features = [
        {"type": "sketch", "id": "sketch1",
         "plane": {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "normal": [0, 0, 1]},
         "entities": entities, "profiles": [{"outer": ["e0", "e1", "e2", "e3"], "inner": []}]},
        {"type": "extrude", "id": "extrude1", "sketch": "sketch1", "distance": thickness,
         "operation": "new_body", "creates": ["Body1"]},
    ]
    for i, (x, y, d) in enumerate(holes, start=1):
        features.append({"type": "hole", "id": f"hole{i}", "kind": "simple", "diameter": d,
                         "depth": None, "positions": [[x, y, thickness]],
                         "direction": [0, 0, -1], "bodies": ["Body1"]})
    return {"id": "/T/Plate", "name": "Plate", "features": features, "bodies": ["Body1"]}


def assert_laid_out(plan: dp.DrawingPlan) -> None:
    """Every view inside the frame, clear of the title block, the bill of
    materials and every other view."""
    area = dp._drawing_area(plan.width, plan.height)
    others = [plan.title.box] + ([plan.bom.box()] if plan.bom else [])
    boxes = [v.padded() for v in plan.views]
    for i, box in enumerate(boxes):
        assert dp._inside(box, area), (plan.views[i].name, box, area)
        assert not any(dp._overlap(box, o) for o in others), plan.views[i].name
        assert not any(dp._overlap(box, b) for b in boxes[i + 1:]), plan.views[i].name


# ── Plan ──


def test_a_plate_with_holes_is_drawn_along_its_holes_with_a_section_through_them():
    plan = plan_of("m1_holes")
    front = plan.view("Front view")
    assert (front.u, front.v) == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))  # seen from +Z
    assert (plan.sheet, plan.scale_label, plan.projection) == ("A4", "1:1", "first")
    assert [v.kind for v in plan.views] == ["front", "section", "isometric"]
    section = plan.view("Section A-A")
    # Through hole1 and hole2 (both at y = 0), across the part.
    assert section.profile[1] == section.profile[3] == 0.0
    assert section.profile[0] < -30 and section.profile[2] > 30
    assert section.parent == "Front view" and section.label == "A"
    assert any("hole1, hole2" in n for n in plan.notes)
    # First angle: the section (seen from above) is under the front view.
    assert section.box[3] < front.box[1]
    assert [a.text for a in plan.annotations] == [
        "Ø6 THRU", "Ø4 x 4 DEEP", "Ø4 THRU, CBORE Ø8 x 2"]
    cells = {c.label: c.value for c in plan.title.cells}
    assert cells["Title"] == "Holes" and cells["Scale"] == "1:1"
    assert cells["Projection"] == "ISO E (first angle)" and cells["Date"] == DATE
    assert_laid_out(plan)


def test_view_anchors_put_the_model_origin_where_the_box_says():
    plan = plan_of("m1_holes")
    front = plan.view("Front view")
    # The part spans x -30..30, y -20..20: the origin is the box's middle.
    assert front.anchor == pytest.approx(((front.box[0] + front.box[2]) / 2,
                                          (front.box[1] + front.box[3]) / 2))
    assert front.box[2] - front.box[0] == pytest.approx(60 * plan.scale)


def test_third_angle_puts_the_top_view_above_and_the_right_view_on_the_right():
    plan = plan_of("m1_offset_block", projection="third")
    front, top = plan.view("Front view"), plan.view("Top view")
    side = plan.view("Right view")
    assert top.box[1] > front.box[3]
    assert side.box[0] > front.box[2] and side.projection == "right"
    cells = {c.label: c.value for c in plan.title.cells}
    assert cells["Projection"] == "ISO A (third angle)"
    # The symbol's end view (circles) left of the cone in third angle.
    circle_x = plan.title.symbol_circles[0][0]
    assert all(circle_x < min(x0, x1) for x0, _, x1, _ in plan.title.symbol_lines)
    assert_laid_out(plan)


def test_first_angle_symbol_has_the_end_view_right_of_the_cone():
    plan = plan_of("m1_offset_block")
    circle_x = plan.title.symbol_circles[0][0]
    assert all(circle_x > max(x0, x1) for x0, _, x1, _ in plan.title.symbol_lines)
    side = plan.view("Left view")
    assert side.projection == "left" and side.box[0] > plan.view("Front view").box[2]


def test_a_block_gets_front_top_and_side_views_and_no_section():
    plan = plan_of("m1_offset_block")
    assert [v.name for v in plan.views] == ["Front view", "Top view", "Left view",
                                            "Isometric view"]
    top = plan.view("Top view")
    assert top.projection == "top" and top.v == (0.0, 0.0, -1.0)  # the viewer's side down


def test_a_part_of_revolution_is_drawn_across_its_axis_with_its_end_view():
    plan = plan_of("m2_shaft_axis_off_origin")
    front = plan.view("Front view")
    assert front.u == (0.0, 0.0, 1.0)  # the axis (Z) horizontal
    assert [v.kind for v in plan.views] == ["front", "projection", "isometric"]
    assert plan.views[1].u in ((0.0, -1.0, 0.0), (0.0, 1.0, 0.0))  # seen along the axis


def test_a_bored_part_of_revolution_gets_a_section_through_its_axis():
    part = part_of("m2_shaft_axis_off_origin")
    part["features"].append({"type": "hole", "id": "bore", "kind": "simple", "diameter": 4,
                             "depth": None, "positions": [[10, 0, 40]],
                             "direction": [0, 0, -1], "bodies": ["Body1"]})
    plan = dp.plan_part_drawing(part, dp.estimate_geometry(part), dp.DrawingOptions(date=DATE))
    section = plan.view("Section A-A")
    assert section.profile[1] == section.profile[3]  # along the axis, horizontal
    assert plan.view("Front view").u == (0.0, 0.0, 1.0)


def test_a_small_hole_in_a_big_plate_gets_a_detail_view():
    part = plate(400, 200, 10, holes=[(150, 60, 1.5), (-150, -60, 20)])
    plan = dp.plan_part_drawing(part, dp.estimate_geometry(part), dp.DrawingOptions(date=DATE))
    detail = plan.view("Detail B")
    assert detail.parent == "Front view" and detail.label == "B"
    assert detail.detail[:2] == (150.0, 60.0)
    assert detail.scale >= 2 * plan.scale and detail.scale in dp.SCALES
    assert 1.5 * detail.scale >= dp.DETAIL_ON_PAPER or detail.scale == max(dp.SCALES)
    assert_laid_out(plan)


def test_a_big_part_is_reduced_to_a_standard_scale():
    part = plate(2000, 1000, 40)
    plan = dp.plan_part_drawing(part, dp.estimate_geometry(part), dp.DrawingOptions(date=DATE))
    assert plan.scale < 1 and plan.scale in dp.SCALES
    assert plan.scale_label.startswith("1:")
    assert_laid_out(plan)


def test_a_small_part_is_enlarged_on_a4():
    part = plate(12, 8, 2)
    plan = dp.plan_part_drawing(part, dp.estimate_geometry(part), dp.DrawingOptions(date=DATE))
    assert plan.sheet == "A4" and plan.scale > 1 and plan.scale_label.endswith(":1")


def test_a_forced_sheet_and_scale_are_kept_or_refused():
    plan = plan_of("m1_holes", sheet="A3", scale=2.0)
    assert (plan.sheet, plan.scale_label) == ("A3", "2:1")
    with pytest.raises(dp.DrawingPlanError):
        plan_of("m1_holes", sheet="A4", scale=10.0)
    with pytest.raises(ValueError):
        dp.DrawingOptions(sheet="B5")
    with pytest.raises(ValueError):
        dp.DrawingOptions(projection="second")


@pytest.mark.parametrize("path", sorted(TREES.glob("*.json")), ids=lambda p: p.stem)
def test_every_fixture_gets_a_drawing_laid_out_without_overlap(path):
    with open(path, encoding="utf-8") as handle:
        tree = json.load(handle)
    for part in tree["parts"]:
        placed = place_part(part)
        plan = dp.plan_part_drawing(placed, dp.estimate_geometry(placed),
                                    dp.DrawingOptions(date=DATE))
        assert plan.views[0].kind == "front" and plan.scale in dp.SCALES
        assert any(v.kind == "isometric" for v in plan.views)
        assert_laid_out(plan)
        json.dumps(plan.to_dict())  # the report can hold it


def test_the_estimate_matches_the_reference_box():
    pytest.importorskip("build123d")
    for name in ("m1_holes", "m2_countersink", "m1_offset_block", "m2_shaft_axis_off_origin"):
        part = part_of(name)
        reference, estimate = dp.reference_geometry(part), dp.estimate_geometry(part)
        assert reference.bbox_min == pytest.approx(estimate.bbox_min, abs=1e-6)
        assert reference.bbox_max == pytest.approx(estimate.bbox_max, abs=1e-6)
        assert reference.source == "build123d"


def test_hole_callouts_follow_the_hole_kind():
    holes = {f["id"]: f for f in load("m1_holes")["parts"][0]["features"]
             if f["type"] == "hole"}
    assert dp.hole_callout(holes["hole3"]) == "Ø4 THRU, CBORE Ø8 x 2"
    sink = next(f for f in load("m2_countersink")["parts"][0]["features"] if f["type"] == "hole")
    assert dp.hole_callout(sink) == "Ø4 THRU, CSK Ø8 x 90°"
    grid = next(f for f in load("m7_hole_grid")["parts"][0]["features"] if f["type"] == "hole")
    assert dp.hole_callout(grid) == "12 x Ø6 THRU, CBORE Ø10 x 3"


def _bracket(step: bool):
    from catia_mcp.tree.assembly import plan_assembly

    with open(FIXTURES / "assembly" / "bracket.tree.json", encoding="utf-8") as handle:
        tree = json.load(handle)
    structure = None
    if step:
        pytest.importorskip("OCP")
        from catia_mcp.tree.step_structure import read

        structure = read(str(FIXTURES / "assembly" / "bracket.step"))
    assembly, problems = plan_assembly(tree, structure)
    assert problems == []
    return assembly


@pytest.mark.parametrize("step", [False, True], ids=["tree only", "with the STEP"])
def test_an_assembly_gets_front_iso_a_bill_of_materials_and_balloons(step):
    assembly = _bracket(step)
    plans, product, problems = dp.plan_drawings(assembly.tree, assembly,
                                                dp.DrawingOptions(date=DATE))
    assert problems == [] and set(plans) == {p["id"] for p in assembly.tree["parts"]}
    assert [v.kind for v in product.views] == ["front", "isometric"]
    # Two bolts: one row, quantity 2, by part name with or without the STEP.
    assert product.bom.rows == [["Item", "Part", "Qty"], ["1", "Plate", "1"],
                                ["2", "Bolt", "2"]]
    assert [b.item for b in product.balloons] == [1, 2]
    bolt = product.balloons[1]
    assert bolt.leader != (0.0, 0.0)  # at the first bolt, not the plate
    assert product.bom.box()[0] == product.title.box[0]  # above the title block
    assert_laid_out(product)


def test_the_plan_command_writes_every_drawing_offline(tmp_path, capsys):
    out = tmp_path / "plan.json"
    assert dp.main([str(TREES / "m2_countersink.json"), "--report", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["parts"]["/M2/Countersink"]["sheet"] == "A4"
    assert report["assembly"] is None
    assert "Section A-A" in capsys.readouterr().out


# ── Builder ──


def _draw(name: str, fail=None, pdf_dir=None):
    documents = fakes.Documents(fail=fail)
    builder = DrawingBuilder(fakes.ConnV5(documents), pdf_dir=pdf_dir)
    plan = plan_of(name)
    source = fakes.part_document(name)
    entry = builder.draw(plan, source)
    return entry, documents.drawings[0], plan, source


def test_v5_drawing_has_every_view_defined_linked_placed_and_dimensioned(tmp_path):
    entry, drawing, plan, source = _draw("m1_holes", pdf_dir=str(tmp_path))
    sheet = drawing.Sheets.ActiveSheet
    assert entry["ok"] is True and entry["route"] == "v5/generative"
    assert (sheet.PaperSize, sheet.Orientation, sheet.ProjectionMethod) == (4, 1, 0)
    assert entry["paper_mm"] == [297.0, 210.0]
    views = {v.Name: v for v in sheet.Views.items}
    front, section, iso = (views[n] for n in ("Front view", "Section A-A", "Isometric view"))
    assert front.GenerativeBehavior.Document is source
    assert front.GenerativeBehavior.calls == [("DefineFrontView", (1.0, 0.0, 0.0, 0.0, 1.0, 0.0))]
    name, args = section.GenerativeBehavior.calls[0]
    assert name == "DefineSectionView" and args[:4] == (
        plan.view("Section A-A").profile, "SectionView", "Offset", 1)
    assert args[4] is front.GenerativeBehavior
    assert section.label == "SECTION A-A"
    assert iso.GenerativeBehavior.calls[0][0] == "DefineIsometricView"
    assert (front.x, front.y) == plan.view("Front view").anchor
    assert front.Scale == plan.scale
    # Dimensions generated in the front view and the section, not the iso.
    assert entry["dimensions_by_view"] == {"Front view": 2, "Section A-A": 2,
                                           "Isometric view": 0}
    assert entry["dimensions"] == 4
    # Hole callouts with leaders in the front view.
    texts = [t.Text for t in front.Texts.items]
    assert texts == [a.text for a in plan.annotations] and entry["annotations"] == 3
    assert all(t.Leaders.heads for t in front.Texts.items)
    # Frame, title block and symbol in the background view.
    background = views["Background View"]
    assert len(background.Factory2D.circles) == 2
    assert len(background.Factory2D.lines) >= 8 + 2 * len(plan.title.cells) + 4
    values = [t.Text for t in background.Texts.items]
    assert "Holes" in values and "1:1" in values and entry["title_block"] == len(
        plan.title.cells)
    assert entry["pdf"] == str(tmp_path / "Holes.pdf") and Path(entry["pdf"]).exists()
    assert all(step["ok"] for step in entry["steps"]), entry["steps"]


def test_a_refused_view_is_recorded_and_the_rest_is_drawn():
    entry, drawing, _, _ = _draw("m1_holes", fail={"DefineIsometricView": "E_FAIL"})
    rows = {r["name"]: r for r in entry["views"]}
    assert rows["Isometric view"] == {"name": "Isometric view", "kind": "isometric",
                                      "scale": rows["Isometric view"]["scale"], "ok": False,
                                      "error": "E_FAIL"}
    assert rows["Section A-A"]["ok"] and entry["ok"]
    assert "Isometric view" in drawing.Sheets.ActiveSheet.Views.removed


def test_a_refused_front_view_skips_the_views_derived_from_it():
    entry, _, _, _ = _draw("m1_offset_block", fail={"DefineFrontView": "no"})
    rows = {r["name"]: r for r in entry["views"]}
    assert entry["ok"] is False
    assert rows["Top view"]["error"] == "its parent Front view was not made"
    assert rows["Isometric view"]["ok"]  # independent of the front view


class _FactoryTools:
    """DraftingTools with a recording _factory_view."""

    calls: list = []

    def __init__(self, conn) -> None:
        pass

    def _factory_view(self, sheet, method, values, name, x, y, scale=None):
        _FactoryTools.calls.append((method, values, name, x, y, scale))
        view = sheet.Views.Add(name)
        del view.GenerativeBehavior
        return view


def test_3dexperience_views_go_through_the_factory_with_the_product_instances(monkeypatch):
    import catia_mcp.tools.drawing as drawing_tools

    monkeypatch.setattr(drawing_tools, "DraftingTools", _FactoryTools)
    _FactoryTools.calls = []
    drawing = fakes.Drawing()
    conn = SimpleNamespace(is_3dx=True, refresh_display=lambda: None)
    builder = DrawingBuilder(conn)
    monkeypatch.setattr(builder, "_new_drawing",
                        lambda: (drawing, drawing.Sheets.ActiveSheet))
    monkeypatch.setattr(builder, "_title_of", lambda d: "Drawing1")
    plan = plan_of("m1_offset_block")
    root = object()
    entry = builder.draw(plan, [root])
    assert entry["route"] == "3dx/factory" and entry["ok"]
    methods = [c[0] for c in _FactoryTools.calls]
    assert methods == ["DefineFrontView", "DefineProjectionView", "DefineProjectionView",
                       "DefineIsometricView"]
    front = _FactoryTools.calls[0][1]
    assert front["iListofPrdInst"] == [root] and front["iPlane"] == [1.0, 0, 0, 0, 1.0, 0]
    assert _FactoryTools.calls[1][1]["iType"] == 2  # top
    assert _FactoryTools.calls[2][1]["iType"] == 1  # left (first angle)


def test_a_standalone_3d_shape_is_skipped_with_the_reason():
    from catia_mcp.errors import WrongDocumentType
    from catia_mcp.tree.drawing_builder import instances_3dx

    def no_product(editor):
        raise WrongDocumentType("no product structure")

    conn = SimpleNamespace(is_3dx=True, root_occurrence=no_product)
    with pytest.raises(DrawingSkipped, match="standalone 3D Shape"):
        instances_3dx(conn, object())


def test_make_drawings_draws_each_part_and_the_v5_product(tmp_path):
    assembly = _bracket(False)
    product = fakes.product_document("Bracket")
    documents = fakes.Documents([product])
    conn = fakes.ConnV5(documents)
    builder = SimpleNamespace(part_documents={p["id"]: fakes.part_document(p["name"])
                                              for p in assembly.tree["parts"]},
                              part_editors={})
    report = {"assembly": {"ok": True, "catia_product": "Bracket"}}
    logs: list[str] = []
    section = make_drawings(conn, fakes.Worker(), assembly.tree, builder, report, assembly,
                            dp.DrawingOptions(date=DATE), pdf_dir=str(tmp_path),
                            log=logs.append)
    assert set(section["parts"]) == {p["id"] for p in assembly.tree["parts"]}
    assert all(e["ok"] for e in section["parts"].values())
    made = section["assembly"]
    assert made["ok"] and made["bom_rows"] == 2 and made["balloons"] == 2
    product_drawing = documents.drawings[-1]
    front = product_drawing.Sheets.ActiveSheet.Views.Item("Front view")
    assert front.GenerativeBehavior.Document is product
    assert all(t.FrameType == 3 and t.Leaders.heads for t in front.Texts.items)
    table = product_drawing.Sheets.ActiveSheet.Views.Item("Background View").Tables.items[0]
    assert table.cells[(3, 2)] == "Bolt" and table.cells[(3, 3)] == "2"
    assert len(fakes.exported_files(documents.drawings)) == 4
    assert any("PDF" in line for line in logs)


def test_make_drawings_records_what_it_cannot_draw():
    tree = load("m1_holes")
    conn = SimpleNamespace(is_3dx=True, root_occurrence=lambda editor: (_ for _ in ()).throw(
        __import__("catia_mcp.errors", fromlist=["x"]).WrongDocumentType("no product")))
    builder = SimpleNamespace(part_documents={}, part_editors={"/M1/Holes": object()})
    section = make_drawings(conn, fakes.Worker(), tree, builder, {}, None,
                            dp.DrawingOptions(date=DATE), log=lambda _: None)
    entry = section["parts"]["/M1/Holes"]
    assert entry["ok"] is False and "standalone 3D Shape" in entry["skipped"]
    assert entry["sheet"] == "A4"


def test_the_live_probe_records_every_route_on_v5(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "probe_drawing", Path(__file__).parent.parent / "scripts" / "probe_drawing.py")
    probe_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe_module)
    part = fakes.part_document("Holes")
    documents = fakes.Documents([part])
    conn = fakes.ConnV5(documents)
    conn.app.ActiveDocument = part
    conn.app.ActiveWindow = SimpleNamespace(Caption="Holes.CATPart")
    report = {"steps": []}
    probe = probe_module.Probe(conn, fakes.Worker(), report)
    report["source"] = probe.step("source", probe.find_source)
    probe.create_routes()
    report["sheet_format"] = probe.step("sheet", probe.sheet_format)
    probe.front_routes()
    probe.derived()
    probe.export(str(tmp_path))
    assert report["source"]["document"] == "Holes.CATPart"
    assert report["drawing_route"] == 'Documents.Add("Drawing")'
    assert report["front_route"].startswith("GenerativeBehavior")
    assert report["sheet_format"]["GetPaperWidth"] == 420.0
    failed = [s["step"] for s in report["steps"] if not s["ok"]]
    assert failed == [], failed
    assert (tmp_path / "drawing_probe.pdf").exists()
    json.dumps(report, default=str)


def test_the_tree_command_takes_the_drawing_options(tmp_path):
    from catia_mcp.tree.__main__ import main

    assert main(["check", str(TREES / "m1_holes.json"), "--drawing", "auto",
                 "--drawing-projection", "third", "--drawing-pdf", str(tmp_path)]) == 0
    with pytest.raises(SystemExit):
        main(["check", str(TREES / "m1_holes.json"), "--drawing", "always"])
