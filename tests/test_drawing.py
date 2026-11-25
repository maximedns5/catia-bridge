"""The drawing of a built part or assembly: the plan (offline) and the
builder against the fake drafting model (tests/drawing_fakes.py)."""

from __future__ import annotations

import itertools
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
    # hole1 and hole2 are at y = 0, the counterbore hole3 at (0, 10): no
    # straight line meets the three, so the cut steps up to it and back.
    assert section.profile == [-33.0, 0.0, -7.5, 0.0, -7.5, 10.0, 7.5, 10.0, 7.5, 0.0,
                               33.0, 0.0]
    assert section.profile_type == "Offset"
    assert section.parent == "Front view" and section.label == "A"
    assert any("stepped section A-A through hole1, hole2, hole3" in n for n in plan.notes)
    # First angle: the section (seen from above) is under the front view.
    assert section.box[3] < front.box[1]
    assert [a.text for a in plan.annotations] == [
        "\u23006 THRU", "\u23004 \u21a74", "\u23004 THRU \u2334\u23008 \u21a72"]
    assert [a.plain for a in plan.annotations] == [
        "\u00d86 THRU", "\u00d84 x 4 DEEP", "\u00d84 THRU, CBORE \u00d88 x 2"]
    cells = {c.label: c.value for c in plan.title.cells}
    assert cells["Title"] == "Holes" and cells["Scale"] == "1:1" and cells["Sheet"] == "A4 1/1"
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
    assert dp.hole_callout(holes["hole3"], "text") == "\u00d84 THRU, CBORE \u00d88 x 2"
    assert dp.hole_callout(holes["hole3"]) == "\u23004 THRU \u2334\u23008 \u21a72"
    assert dp.hole_callout(holes["hole2"]) == "\u23004 \u21a74"
    sink = next(f for f in load("m2_countersink")["parts"][0]["features"] if f["type"] == "hole")
    assert dp.hole_callout(sink, "text") == "\u00d84 THRU, CSK \u00d88 x 90\u00b0"
    assert dp.hole_callout(sink) == "\u23004 THRU \u2335\u23008 \u00d7 90\u00b0"
    grid = next(f for f in load("m7_hole_grid")["parts"][0]["features"] if f["type"] == "hole")
    assert dp.hole_callout(grid, "text") == "12 x \u00d86 THRU, CBORE \u00d810 x 3"
    assert dp.hole_callout(grid) == "12\u00d7 \u23006 THRU \u2334\u230010 \u21a73"


def test_thread_callouts_give_the_designation_class_and_depths():
    tapped = {f["id"]: f for f in load("m10_tapped_holes")["parts"][0]["features"]
              if f["type"] == "hole"}
    # A blind M6 tapped 10 deep in a 5 mm tap drill 12 deep.
    assert dp.hole_callout(tapped["hole1"]) == "M6 - 6H \u21a710 / \u23005 \u21a712"
    assert dp.hole_callout(tapped["hole1"], "text") == "M6 - 6H x 10 DEEP / \u00d85 x 12 DEEP"
    assert dp.hole_callout(tapped["hole2"]) == "M8 - 6H THRU / \u23006.8 THRU"
    fine = {**tapped["hole1"], "positions": [[0, 0, 15]] * 4,
            "thread": {"designation": "M8x1", "depth": 10, "side": "left", "tolerance": "5H"},
            "diameter": 7}
    assert dp.hole_callout(fine) == "4\u00d7 M8\u00d71 LH - 5H \u21a710 / \u23007 \u21a712"
    assert dp.hole_callout({**tapped["hole1"], "thread": "1/4-20 UNC"}) == "\u23005 \u21a712"


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
                                      "sheet": 1, "error": "E_FAIL"}
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


def test_make_drawings_draws_the_v5_product_with_a_sheet_per_part(tmp_path):
    assembly = _bracket(False)
    product = fakes.product_document("Bracket")
    documents = fakes.Documents([product])
    conn = fakes.ConnV5(documents)
    part_documents = {p["id"]: fakes.part_document(p["name"]) for p in assembly.tree["parts"]}
    builder = SimpleNamespace(part_documents=part_documents, part_editors={})
    report = {"assembly": {"ok": True, "catia_product": "Bracket"}}
    logs: list[str] = []
    section = make_drawings(conn, fakes.Worker(), assembly.tree, builder, report, assembly,
                            dp.DrawingOptions(date=DATE), pdf_dir=str(tmp_path),
                            log=logs.append, dxf_dir=str(tmp_path / "dxf"))
    # One drawing: the product, then the plate, then the bolt (two prototypes).
    assert len(documents.drawings) == 1
    made = section["assembly"]
    assert made["ok"] and made["bom_rows"] == 2 and made["balloons"] == 2
    assert [s["name"] for s in made["sheets"]] == ["Bracket", "Plate", "Bolt"]
    assert all(s["ok"] for s in made["sheets"])
    assert section["parts"]["/Bracket/Plate"] == {
        "ok": True, "drawing": "Bracket", "catia_name": "Drawing1.CATDrawing", "sheet": 2,
        "error": None}
    assert section["parts"]["/Bracket/Bolt"]["sheet"] == section["parts"]["/Bracket/Bolt#2"][
        "sheet"] == 3
    drawing = documents.drawings[0]
    sheets = drawing.Sheets.items
    assert [s.Name for s in sheets] == ["A4 2:1", "2 Plate 2:1", "3 Bolt 5:1"]
    front = sheets[0].Views.Item("Front view")
    assert front.GenerativeBehavior.Document is product
    assert sheets[1].Views.Item("Front view").GenerativeBehavior.Document is part_documents[
        "/Bracket/Plate"]
    assert sheets[2].Views.Item("Front view").GenerativeBehavior.Document is part_documents[
        "/Bracket/Bolt"]
    assert all(t.FrameType == 3 and t.Leaders.heads for t in front.Texts.items)
    table = sheets[0].Views.Item("Background View").Tables.items[0]
    assert table.cells[(3, 2)] == "Bolt" and table.cells[(3, 3)] == "2"
    for index, sheet in enumerate(sheets, start=1):  # a frame and title block on each
        texts = [t.Text for t in sheet.Views.Item("Background View").Texts.items]
        assert f"A4 {index}/3" in texts
    # A PDF and a DXF per sheet.
    assert [Path(f).name for f in made["pdf_files"]] == ["Bracket_1.pdf", "Bracket_2.pdf",
                                                         "Bracket_3.pdf"]
    assert [Path(f).name for f in made["dxf_files"]] == ["Bracket_1.dxf", "Bracket_2.dxf",
                                                         "Bracket_3.dxf"]
    assert [fmt for _, fmt in drawing.exported] == ["pdf"] * 3 + ["dxf"] * 3
    assert any("files" in line for line in logs)


def test_make_drawings_draws_each_part_when_the_product_cannot_be(tmp_path):
    assembly = _bracket(False)
    documents = fakes.Documents()
    builder = SimpleNamespace(part_documents={p["id"]: fakes.part_document(p["name"])
                                              for p in assembly.tree["parts"]},
                              part_editors={})
    section = make_drawings(fakes.ConnV5(documents), fakes.Worker(), assembly.tree, builder,
                            {"assembly": {"ok": False}}, assembly, dp.DrawingOptions(date=DATE),
                            log=lambda _: None)
    assert "not assembled" in section["assembly"]["skipped"]
    assert all(e["ok"] and "drawing" not in e for e in section["parts"].values())
    assert len(documents.drawings) == 3


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
    activated = []
    windows = [SimpleNamespace(Name=f"Part{i}", Activate=lambda i=i: activated.append(i))
               for i in range(1, 181)]
    conn.app.Windows = SimpleNamespace(Count=len(windows), Item=lambda i: windows[i - 1])
    conn.app.ActiveWindow = SimpleNamespace(Caption="Holes.CATPart")
    report = {"steps": []}
    probe = probe_module.Probe(conn, fakes.Worker(), report)
    report["window"] = probe.step("window", lambda: probe_module.select_window(conn, "Part42"))
    report["source"] = probe.step("source", probe.find_source)
    probe.create_routes()
    report["sheet_format"] = probe.step("sheet", probe.sheet_format)
    probe.front_routes()
    probe.derived()
    probe.second_slice()
    probe.export(str(tmp_path))
    assert activated == [42] and report["window"]["windows"] == 180
    assert report["source"]["document"] == "Holes.CATPart"
    assert report["drawing_route"] == 'Documents.Add("Drawing")'
    assert report["front_route"].startswith("GenerativeBehavior")
    assert report["sheet_format"]["GetPaperWidth"] == 420.0
    failed = [s["step"] for s in report["steps"] if not s["ok"]]
    assert failed == [], failed
    results = {s["step"]: s.get("result") for s in report["steps"]}
    assert results["auxiliary view (DefineAuxiliaryView, else along the normal)"]["route"]
    assert results["callout with ISO 129 symbols, read back"]["same"] is True
    assert results["second sheet (Sheets.Add), a line and a text"]["sheets"] == 2
    assert (tmp_path / "drawing_probe.pdf").exists() and (tmp_path / "drawing_probe.dxf").exists()
    assert report["print_to_file"] == {"sheet": None, "drawing": None, "window": None}
    json.dumps(report, default=str)


def test_the_tree_command_takes_the_drawing_options(tmp_path):
    from catia_mcp.tree.__main__ import main

    assert main(["check", str(TREES / "m1_holes.json"), "--drawing", "auto",
                 "--drawing-projection", "third", "--drawing-pdf", str(tmp_path)]) == 0
    with pytest.raises(SystemExit):
        main(["check", str(TREES / "m1_holes.json"), "--drawing", "always"])


# ── Second slice: sections, auxiliary views, centre lines, sheets, exports ──


def _on_profile(profile, point):
    return dp._on_polyline(point, profile)


def test_a_stepped_section_meets_one_hole_of_each_feature():
    plan = plan_of("d2_holes_not_in_line")
    section = plan.view("Section A-A")
    assert section.profile_type == "Offset"
    points = list(zip(section.profile[::2], section.profile[1::2]))
    # Every segment along u or v: a stepped cut, from one side to the other.
    assert all(a[0] == b[0] or a[1] == b[1] for a, b in itertools.pairwise(points))
    assert points[0][0] < -40 and points[-1][0] > 40
    for x, y in ((-20, -12), (0, 12), (20, -12), (-34, -19)):  # hole1..3, one countersink
        assert _on_profile(section.profile, (x, y)), (x, y)
    assert any("stepped section A-A through hole1, hole2, hole3, hole4" in n for n in plan.notes)
    texts = [a.text for a in plan.annotations]
    assert "M6 - 6H ↧8 / ⌀5 ↧10" in texts
    assert "4× ⌀4.5 THRU ⌵⌀9 × 90°" in texts
    assert "⌀6.6 THRU ⌴⌀11 ↧6.5" in texts
    assert_laid_out(plan)


def test_an_aligned_section_turns_through_a_bolt_circle():
    plan = plan_of("d3_flange_three_holes")
    section = plan.view("Section A-A")
    assert section.profile_type == "Aligned" and len(section.profile) == 6
    assert section.profile[2:4] == [0.0, 0.0]  # through the bore's centre
    bolts = [(0.0, 30.0), (-25.980762114, -15.0), (25.980762114, -15.0)]
    assert sum(_on_profile(section.profile, b) for b in bolts) == 2
    # Both bolts the section meets are unfolded, one each side of the bore.
    axes = sorted(c.lines[0][0] for c in plan.centre_lines
                  if c.view == "Section A-A" and c.kind == "axis")
    assert axes == pytest.approx([-30.0, 0.0, 30.0])
    pitch = [c for c in plan.centre_lines if c.kind == "circle"]
    assert [c.circle for c in pitch] == [(0.0, 0.0, 30.0)]
    assert_laid_out(plan)


def test_a_straight_line_through_the_centre_wins_over_an_aligned_section():
    plan = plan_of("m7_hole_circle")  # six holes: two opposite ones on y = 0
    section = plan.view("Section A-A")
    assert section.profile_type == "Offset" and len(section.profile) == 4


def test_an_inclined_face_gets_an_auxiliary_view_along_its_normal():
    plan = plan_of("d1_inclined_block")
    aux = plan.view("View B")
    front = plan.view("Front view")
    assert aux.kind == "auxiliary" and aux.parent == "Front view" and aux.label == "B"
    s = 2 ** -0.5
    assert dp.cross(aux.u, aux.v) == pytest.approx((s, 0.0, s))
    # The cutting line runs along the face's edge (from (30, 15) to (10, 35)).
    x1, y1, x2, y2 = aux.profile
    assert (x2 - x1) == pytest.approx(-(y2 - y1))
    assert _on_profile(aux.profile, (20.0, 25.0))
    # The arrow, outside the part, points at the face along -normal.
    tx, ty, hx, hy = aux.arrow
    assert hx > 30 and hy > 25 and tx > hx and ty > hy
    assert (tx - hx) == pytest.approx(ty - hy)
    assert any("auxiliary view B" in n for n in plan.notes)
    # Next to the front view, in the arrow's direction (up and right).
    assert aux.box[0] > front.box[2] - 1e-6 or aux.box[1] > front.box[3] - 1e-6
    assert_laid_out(plan)


@pytest.mark.parametrize("name", ["m3_asymmetric_chamfer", "m9_chamfer_angle", "m8_hexagon_pad",
                                  "m10_draft_two_sides", "m10_pad_up_to_plane"])
def test_chamfers_drafts_and_prisms_get_no_auxiliary_view(name):
    part = part_of(name)
    for geometry in (dp.estimate_geometry(part), dp.part_geometry(part)):
        plan = dp.plan_part_drawing(part, geometry, dp.DrawingOptions(date=DATE))
        assert not any(v.kind == "auxiliary" for v in plan.views), geometry.source


def test_the_estimate_finds_the_inclined_face_build123d_finds():
    pytest.importorskip("build123d")
    part = part_of("d1_inclined_block")
    (estimated,) = dp.estimate_geometry(part).inclined
    (reference,) = dp.reference_geometry(part).inclined
    assert estimated.normal == pytest.approx(reference.normal, abs=1e-9)
    assert estimated.area == pytest.approx(reference.area)
    assert estimated.centre == pytest.approx(reference.centre)


def test_centre_lines_mark_holes_and_axes_in_each_orthographic_view():
    plan = plan_of("m1_holes")
    marks = {(c.lines[0][0] + c.lines[0][2]) / 2: c for c in plan.centre_lines
             if c.view == "Front view" and c.kind == "mark"}
    assert sorted(marks) == pytest.approx([-15.0, 0.0, 15.0])
    over = dp.CENTRE_OVERSHOOT / plan.scale
    # The counterbore's mark reaches past its 8 mm counterbore.
    assert marks[0.0].lines[1] == pytest.approx((0.0, 10 - 4 - over, 0.0, 10 + 4 + over))
    section = [c for c in plan.centre_lines if c.view == "Section A-A"]
    assert {c.of for c in section} == {"hole1", "hole2", "hole3"} and all(
        c.kind == "axis" for c in section)
    blind = next(c for c in section if c.of == "hole2")  # 4 deep, from z = 10 down
    (_, y0, _, y1), = blind.lines
    assert abs(y1 - y0) == pytest.approx(4 + 2 * over)
    assert not any(c.view == "Isometric view" for c in plan.centre_lines)


def test_a_turned_part_gets_its_axis_across_the_front_view():
    plan = plan_of("m2_shaft_axis_off_origin")
    front = plan.view("Front view")
    axis = [c for c in plan.centre_lines if c.view == "Front view" and c.kind == "axis"]
    assert len(axis) == 1  # the revolve and the extruded circles are one axis
    (x0, y0, x1, y1), = axis[0].lines
    assert y0 == y1 and x1 - x0 > (front.box[2] - front.box[0]) / plan.scale
    end = [c for c in plan.centre_lines if c.view != "Front view"]
    assert end and all(c.kind == "mark" for c in end)


def test_the_views_go_on_further_sheets_when_they_do_not_fit_one():
    plan = plan_of("d1_inclined_block", sheet="A4", scale=2.0)
    assert [v.name for v in plan.views] == ["Front view", "Section A-A"]
    assert [[v.name for v in s.views] for s in plan.more] == [
        ["Top view"], ["View B"], ["Isometric view"]]
    assert all(v.independent for s in plan.more for v in s.views)
    assert [c.value for s in plan.sheets for c in s.title.cells if c.label == "Sheet"] == [
        "A4 1/4", "A4 2/4", "A4 3/4", "A4 4/4"]
    assert any("further sheets" in n for n in plan.notes)
    for sheet in plan.sheets:
        assert_laid_out(sheet)
    with pytest.raises(dp.DrawingPlanError, match="front view"):
        plan_of("d1_inclined_block", sheet="A4", scale=5.0)
    with pytest.raises(ValueError):
        dp.DrawingOptions(max_sheet="A5")


def test_an_assembly_drawing_has_one_sheet_per_distinct_part():
    assembly = _bracket(False)
    plans, product, _ = dp.plan_drawings(assembly.tree, assembly, dp.DrawingOptions(date=DATE))
    assert [s.name for s in product.sheets] == ["Bracket", "Plate", "Bolt"]
    assert [s.source for s in product.sheets] == [None, "/Bracket/Plate", "/Bracket/Bolt"]
    assert product.covers == {"/Bracket/Plate": 2, "/Bracket/Bolt": 3, "/Bracket/Bolt#2": 3}
    assert [c.value for s in product.sheets for c in s.title.cells if c.label == "Sheet"] == [
        "A4 1/3", "A4 2/3", "A4 3/3"]
    # The part plans themselves stay one sheet each.
    assert all(not p.more for p in plans.values())
    _, alone, _ = dp.plan_drawings(assembly.tree, assembly,
                                   dp.DrawingOptions(date=DATE, assembly_sheets=False))
    assert len(alone.sheets) == 1


def test_the_drafting_assembly_fixture_matches_its_step():
    pytest.importorskip("OCP")
    from catia_mcp.tree.assembly import plan_assembly
    from catia_mcp.tree.step_structure import read

    folder = FIXTURES / "assembly"
    with open(folder / "drafting_parts.tree.json", encoding="utf-8") as handle:
        tree = json.load(handle)
    assembly, problems = plan_assembly(tree, read(str(folder / "drafting_parts.step")))
    assert problems == []
    _, product, problems = dp.plan_drawings(assembly.tree, assembly,
                                            dp.DrawingOptions(date=DATE))
    assert problems == []
    assert [s.name for s in product.sheets] == ["DraftingParts", "InclinedBlock", "HolePlate",
                                                "Flange"]
    kinds = {s.name: [v.kind for v in s.views] for s in product.sheets}
    assert "auxiliary" in kinds["InclinedBlock"]
    assert {s.name: next((v.profile_type for v in s.views if v.kind == "section"), None)
            for s in product.sheets[2:]} == {"HolePlate": "Offset", "Flange": "Aligned"}


# ── Second slice: the builder ──


def _draw_plan(plan, pdf_dir=None, dxf_dir=None, **drawing):
    documents = fakes.Documents(**drawing)
    builder = DrawingBuilder(fakes.ConnV5(documents), pdf_dir=pdf_dir, dxf_dir=dxf_dir)
    source = fakes.part_document(plan.name)
    entry = builder.draw(plan, source)
    return entry, documents.drawings[0]


def test_v5_auxiliary_view_is_defined_from_its_parent():
    plan = plan_of("d1_inclined_block")
    entry, drawing = _draw_plan(plan)
    views = {v.Name: v for v in drawing.Sheets.ActiveSheet.Views.items}
    aux, front = views["View B"], views["Front view"]
    name, args = aux.GenerativeBehavior.calls[0]
    assert name == "DefineAuxiliaryView" and args[:4] == tuple(plan.view("View B").profile)
    assert args[4] is front.GenerativeBehavior
    assert aux.label == "VIEW B-B"
    row = next(r for r in entry["views"] if r["name"] == "View B")
    assert row["ok"] and row["route"] == "DefineAuxiliaryView(x1, y1, x2, y2, parent)"
    assert entry["ok"]


def test_a_refused_auxiliary_view_is_made_along_the_normal_with_its_arrow():
    plan = plan_of("d1_inclined_block")
    entry, drawing = _draw_plan(plan, fail={"DefineAuxiliaryView": "E_FAIL"})
    views = {v.Name: v for v in drawing.Sheets.ActiveSheet.Views.items}
    aux = views["View B"]
    assert aux.GenerativeBehavior.calls == [
        ("DefineFrontView", (*plan.view("View B").u, *plan.view("View B").v))]
    row = next(r for r in entry["views"] if r["name"] == "View B")
    assert row["ok"] and "E_FAIL" in row["auxiliary_refused"] and row["arrow"] is True
    front = views["Front view"]
    tx, ty, hx, hy = plan.view("View B").arrow
    assert (tx, ty, hx, hy) in front.Factory2D.lines  # the shaft, then two barbs
    assert any(t.Text == "B" for t in front.Texts.items)


def test_a_second_auxiliary_signature_is_tried():
    plan = plan_of("d1_inclined_block")
    entry, _ = _draw_plan(plan, fail={"DefineAuxiliaryView/5": "wrong count"})
    row = next(r for r in entry["views"] if r["name"] == "View B")
    assert row["route"] == "DefineAuxiliaryView(x1, y1, x2, y2, 1, parent)"


def test_centre_lines_are_drawn_dash_dot_in_their_views():
    plan = plan_of("m1_holes")
    entry, drawing = _draw_plan(plan)
    views = {v.Name: v for v in drawing.Sheets.ActiveSheet.Views.items}
    drawn = views["Front view"].Factory2D.items
    marks = [c for c in plan.centre_lines if c.view == "Front view"]
    assert len(drawn) == 2 * len(marks)
    assert all(item.line_type == 4 and item.width == 1 for item in drawn)
    assert entry["centre_lines"] == len(plan.centre_lines)
    assert entry["centre_line_type"].startswith("drawing.Selection")
    assert entry["views"][0]["generation"] == {"centre lines": None, "axes": None,
                                               "threads": None}
    assert all(step["ok"] for step in entry["steps"]), entry["steps"]


def test_views_that_generate_their_own_centre_lines_get_none_drawn():
    plan = plan_of("m1_holes")
    entry, drawing = _draw_plan(plan, generation=("CenterLineMode", "AxisMode"))
    views = {v.Name: v for v in drawing.Sheets.ActiveSheet.Views.items}
    assert views["Front view"].GenerativeBehavior.CenterLineMode is True
    assert not views["Front view"].Factory2D.items
    assert not views["Section A-A"].Factory2D.items
    generation = entry["views"][0]["generation"]
    assert generation["centre lines"] == {"property": "GenerativeBehavior.CenterLineMode",
                                          "value": True}
    assert generation["threads"] is None and entry["centre_lines"] == 0


def test_callouts_fall_back_to_words_where_the_font_lacks_the_symbols():
    plan = plan_of("m1_holes")
    entry, drawing = _draw_plan(plan, ascii_font=True)
    front = drawing.Sheets.ActiveSheet.Views.Item("Front view")
    assert [t.Text for t in front.Texts.items] == [a.plain for a in plan.annotations]
    assert entry["callouts_in_words"] == 3


def test_further_sheets_are_added_each_with_its_frame_title_block_and_files(tmp_path):
    plan = plan_of("d1_inclined_block", sheet="A4", scale=2.0)
    entry, drawing = _draw_plan(plan, pdf_dir=str(tmp_path), dxf_dir=str(tmp_path))
    sheets = drawing.Sheets.items
    assert len(sheets) == 4 and [r["ok"] for r in entry["sheets"]] == [True] * 4
    for sheet in sheets:
        background = sheet.Views.Item("Background View")
        assert len(background.Factory2D.circles) == 2  # the projection symbol
        assert any(t.Text == "Inclined" or t.Text == "InclinedBlock"
                   for t in background.Texts.items)
    # Views on further sheets are made along their own plane.
    top = sheets[1].Views.Item("Top view")
    assert top.GenerativeBehavior.calls[0][0] == "DefineFrontView"
    aux = sheets[2].Views.Item("View B")
    assert aux.GenerativeBehavior.calls[0][0] == "DefineFrontView"
    # Its arrow goes to the front view on sheet 1.
    front = sheets[0].Views.Item("Front view")
    assert plan.more[1].views[0].arrow in front.Factory2D.lines
    assert [Path(f).name for f in entry["pdf_files"]] == [f"InclinedBlock_{i}.pdf"
                                                          for i in range(1, 5)]
    assert len(entry["dxf_files"]) == 4 and entry["dxf"].endswith("InclinedBlock_1.dxf")
    assert all(step["ok"] for step in entry["steps"]), entry["steps"]


def test_3dexperience_exports_try_each_route_once_and_record_the_refusals(monkeypatch, tmp_path):
    import catia_mcp.tools.drawing as drawing_tools

    monkeypatch.setattr(drawing_tools, "DraftingTools", _FactoryTools)
    _FactoryTools.calls = []
    drawing = fakes.Drawing(refuse_export="The method ExportData failed")
    refused = []

    def compat_export(path, fmt):
        refused.append(fmt)
        raise RuntimeError("E_FAIL")

    conn = SimpleNamespace(is_3dx=True, refresh_display=lambda: None,
                           app=SimpleNamespace(ActiveDocument=SimpleNamespace(
                               ExportData=compat_export)))
    builder = DrawingBuilder(conn, pdf_dir=str(tmp_path), dxf_dir=str(tmp_path))
    monkeypatch.setattr(builder, "_new_drawing", lambda: (drawing, drawing.Sheets.ActiveSheet))
    monkeypatch.setattr(builder, "_title_of", lambda d: "Drawing1")
    plan = plan_of("d3_flange_three_holes")
    entry = builder.draw(plan, [object()])
    assert entry["ok"] and entry["pdf"] is None and entry["dxf"] is None
    routes = [a["route"] for a in entry["exports"]["pdf"]]
    assert routes == ['drawing.ExportData(path, "pdf")', 'drawing.ExportData(path, "PDF")',
                      'ActiveDocument.ExportData(path, "pdf")',
                      'ActiveDocument.ExportData(path, "PDF")']
    assert refused == ["pdf", "PDF", "dxf", "DXF"]
    failed = {s["step"] for s in entry["steps"] if not s["ok"]}
    assert {"pdf export", "dxf export"} <= failed
    # The aligned section goes to the factory with its profile type.
    section = next(c for c in _FactoryTools.calls if c[0] == "DefineSectionView")
    assert section[1]["iProfileType"] == "CatAlignedSectionProfile"
    assert len(section[1]["iProfile"]) == 6


def test_the_tree_command_takes_the_dxf_and_sheet_options(tmp_path):
    from catia_mcp.tree.__main__ import main

    assert main(["check", str(TREES / "m1_holes.json"), "--drawing", "auto",
                 "--drawing-dxf", str(tmp_path), "--drawing-max-sheet", "A3"]) == 0
    with pytest.raises(SystemExit):
        main(["check", str(TREES / "m1_holes.json"), "--drawing-max-sheet", "B4"])
