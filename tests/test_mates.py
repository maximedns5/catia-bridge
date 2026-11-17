"""Assembly constraints: inference from faces (catia_mcp/tree/mates.py) and
their application to a fake CATIA product (assembly_constraints.py).

The synthetic components below need nothing; the fixtures' faces are read
from their STEP files with OCP (installed with build123d)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from catia_mcp.tree import mates
from catia_mcp.tree.assembly_constraints import (
    CST_TYPES,
    Constrainer,
    V5Backend,
    constraint_collection,
    expected_positions,
    v5_paths,
)
from catia_mcp.tree.mates import Component, CylinderFace, Mate, MatePlan, Placement, PlaneFace
from catia_mcp.tree.placement import catia_components
from catia_mcp.tree.step_structure import IDENTITY
from tests import constraint_fakes as fakes

FIXTURES = Path(__file__).parent / "fixtures" / "assembly"


# ── Synthetic components (no OCP) ──


def _circle(cx, cy, z, r, n=32):
    return tuple((cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n),
                  z) for i in range(n))


def _box(cid, lo, hi, holes=(), volume=None):
    """A box's six faces, with vertical holes (x, y, r) through it."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    faces = []

    def rect(points):
        return (tuple(points),)

    tops = tuple(_circle(x, y, z1, r) for x, y, r in holes)
    bottoms = tuple(_circle(x, y, z0, r) for x, y, r in holes)
    faces.append(PlaneFace("F1", ((x0 + x1) / 2, (y0 + y1) / 2, z0), (0, 0, -1), 1.0,
                           rect([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)])
                           + bottoms))
    faces.append(PlaneFace("F2", ((x0 + x1) / 2, (y0 + y1) / 2, z1), (0, 0, 1), 1.0,
                           rect([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]) + tops))
    faces.append(PlaneFace("F3", (x0, (y0 + y1) / 2, (z0 + z1) / 2), (-1, 0, 0), 1.0,
                           rect([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)])))
    faces.append(PlaneFace("F4", (x1, (y0 + y1) / 2, (z0 + z1) / 2), (1, 0, 0), 1.0,
                           rect([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)])))
    faces.append(PlaneFace("F5", ((x0 + x1) / 2, y0, (z0 + z1) / 2), (0, -1, 0), 1.0,
                           rect([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)])))
    faces.append(PlaneFace("F6", ((x0 + x1) / 2, y1, (z0 + z1) / 2), (0, 1, 0), 1.0,
                           rect([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)])))
    for i, (x, y, r) in enumerate(holes, start=7):
        faces.append(CylinderFace(f"F{i}", (x, y, z0), (0, 0, 1), r, True, (0.0, z1 - z0)))
    size = (x1 - x0) * (y1 - y0) * (z1 - z0)
    return Component(cid, cid.rsplit("/", 1)[-1], faces, [list(r) for r in IDENTITY],
                     volume=size if volume is None else volume)


def _pin(cid, x, y, z_tip, length, r, head_r=None, head_h=0.0):
    """A pin standing along +Z from z_tip, with a head on top when head_r."""
    top = z_tip + length
    faces = [PlaneFace("F1", (x, y, z_tip), (0, 0, -1), 1.0, (_circle(x, y, z_tip, r),)),
             CylinderFace("F2", (x, y, z_tip), (0, 0, 1), r, False, (0.0, length))]
    if head_r:
        faces.append(PlaneFace("F3", (x, y, top), (0, 0, -1), 1.0,
                               (_circle(x, y, top, head_r),
                                tuple(reversed(_circle(x, y, top, r))))))
        faces.append(CylinderFace("F4", (x, y, top), (0, 0, 1), head_r, False, (0.0, head_h)))
        top += head_h
    faces.append(PlaneFace("F5", (x, y, top), (0, 0, 1), 1.0,
                           (_circle(x, y, top, head_r or r),)))
    return Component(cid, cid.rsplit("/", 1)[-1], faces, [list(r) for r in IDENTITY],
                     volume=math.pi * r * r * length)


def _summary(plan):
    return [(p.component, p.status, [(m.kind, m.element.face, m.other, m.other_element.face,
                                      m.value, m.orientation) for m in p.mates], p.spin_free)
            for p in plan.placements]


def test_a_pin_in_a_hole_is_a_coincidence_and_a_contact_its_spin_left_free():
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10), holes=[(10, 5, 4)])
    pin = _pin("/A/Pin", 10, 5, -10, 20, 4, head_r=7, head_h=4)
    plan = mates.infer([pin, plate])  # the plate is larger: grounded although second
    assert plan.ground == "/A/Plate"
    assert _summary(plan) == [
        ("/A/Plate", "ground", [], False),
        ("/A/Pin", "mates", [("coincidence", "F2", "/A/Plate", "F7", None, None),
                             ("contact", "F3", "/A/Plate", "F2", None, None)], True)]


def test_a_box_on_a_base_is_a_contact_and_two_offsets():
    base = _box("/B/Base", (-50, -30, -10), (50, 30, 0))
    box = _box("/B/Box", (5, -5, 0), (25, 25, 15))
    assert _summary(mates.infer([base, box])) == [
        ("/B/Base", "ground", [], False),
        ("/B/Box", "mates", [("contact", "F1", "/B/Base", "F2", None, None),
                             ("offset", "F6", "/B/Base", "F6", 5.0, "same"),
                             ("offset", "F4", "/B/Base", "F4", 25.0, "same")], False)]


def test_a_component_nothing_holds_is_fixed_in_space():
    base = _box("/C/Base", (-50, -30, -10), (50, 30, 0))
    pin = _pin("/C/Pin", 0, 0, 0, 20, 3)  # stands on the base: free to slide and turn
    floating = _pin("/C/Float", 0, 0, 40, 5, 2)
    plan = mates.infer([base, pin, floating])
    # The pin stands on the base: a contact leaves it free to slide and turn.
    # The floating pin is on the pin's axis but apart from it: no coincidence.
    assert [(p.component, p.status, p.dof_left, p.mates) for p in plan.placements] == [
        ("/C/Base", "ground", 0, []), ("/C/Pin", "fix", 3, []), ("/C/Float", "fix", 3, [])]
    assert plan.placement("/C/Pin").note == "the constraints found leave 3 degree(s) of freedom"
    assert "2 fixed in space (/C/Pin, /C/Float)" in plan.summary()


def test_components_are_placed_once_something_holds_them_whatever_their_order():
    lower = _box("/D/Lower", (-40, -20, 0), (40, 20, 6), holes=[(25, 0, 3), (-25, 0, 3)])
    upper = _box("/D/Upper", (-40, -20, 6), (40, 20, 12), holes=[(25, 0, 3), (-25, 0, 3)])
    bolt = _pin("/D/Bolt", 25, 0, 1, 11, 3, head_r=5, head_h=4)  # its tip inside the plate
    plan = mates.infer([lower, bolt, upper])  # the bolt's head sits on the upper plate
    assert [p.component for p in plan.placements] == ["/D/Lower", "/D/Upper", "/D/Bolt"]
    assert [(m.kind, m.other) for m in plan.placement("/D/Upper").mates] == [
        ("coincidence", "/D/Lower"), ("coincidence", "/D/Lower"), ("contact", "/D/Lower")]
    assert [(m.kind, m.other) for m in plan.placement("/D/Bolt").mates] == [
        ("coincidence", "/D/Upper"), ("contact", "/D/Upper")]  # the longer fit


def test_rank_and_overlap():
    assert mates.rank([[1, 0, 0, 0, 0, 0], [2, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 1]]) == 2
    assert mates.rank([[float(i == j) for j in range(6)] for i in range(6)]) == 6
    a = PlaneFace("F1", (0, 0, 0), (0, 0, 1), 4.0,
                  (((-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)),))
    b = PlaneFace("F2", (1, 1, 0), (0, 0, -1), 4.0,
                  (((0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)),))
    assert mates.overlap_area(a, b) == pytest.approx(1.0, rel=1e-9)


def test_inference_is_deterministic_and_reported():
    parts = [_box("/E/Base", (-50, -30, -10), (50, 30, 0)),
             _box("/E/Box", (5, -5, 0), (25, 25, 15))]
    first, second = mates.infer(parts).to_dict(), mates.infer(list(parts)).to_dict()
    assert first == second
    assert json.loads(json.dumps(first))["placements"][1]["mates"][1]["value"] == 5.0
    assert mates.fix_all(parts).summary() == "2 component(s): /E/Base grounded, 0 constraint(s), " \
                                             "1 fixed in space (/E/Box)"


# ── The fixtures (faces from the STEP files, needs OCP) ──


def _fixture(name, step=True):
    pytest.importorskip("OCP")
    from catia_mcp.tree.assembly import plan_assembly
    from catia_mcp.tree.step_structure import read

    tree = json.loads((FIXTURES / f"{name}.tree.json").read_text())
    structure = read(str(FIXTURES / f"{name}.step")) if step else None
    plan, problems = plan_assembly(tree, structure)
    assert problems == []
    components = (mates.components_from_structure(structure, plan) if step
                  else mates.components_from_tree(plan.tree))
    return plan, components


@pytest.mark.parametrize("name", ["pin_plate", "box_base", "bolted_plates"])
def test_the_synthetic_fixtures_match_their_step_files(name):
    pytest.importorskip("build123d")
    from catia_mcp.tree.assembly import plan_assembly
    from catia_mcp.tree.reference import expected
    from catia_mcp.tree.step_structure import read

    tree = json.loads((FIXTURES / f"{name}.tree.json").read_text())
    plan, problems = plan_assembly(tree, read(str(FIXTURES / f"{name}.step")),
                                   expected(tree, features=False))
    assert problems == []
    assert len(plan.tree["parts"]) == 2  # a part placed twice is modelled once


def test_the_bracket_bolts_are_fixed_nothing_holds_them():
    _, components = _fixture("bracket")
    result = mates.infer(components)
    assert [(p.component, p.status, p.dof_left) for p in result.placements] == [
        ("/Bracket/Plate", "ground", 0), ("/Bracket/Bolt", "fix", 3),
        ("/Bracket/Bolt#2", "fix", 3)]


def test_the_pin_plate_fixture():
    _, components = _fixture("pin_plate")
    assert _summary(mates.infer(components)) == [
        ("/PinPlate/Plate", "ground", [], False),
        ("/PinPlate/Pin", "mates", [("coincidence", "F4", "/PinPlate/Plate", "F7", None, None),
                                    ("contact", "F3", "/PinPlate/Plate", "F3", None, None)], True)]


def test_the_pin_plate_fixture_without_its_step_file():
    pytest.importorskip("build123d")
    _, components = _fixture("pin_plate", step=False)
    kinds = [(p.status, [m.kind for m in p.mates]) for p in mates.infer(components).placements]
    assert kinds == [("ground", []), ("mates", ["coincidence", "contact"])]


def test_the_box_base_fixture_turned_a_quarter_turn():
    _, components = _fixture("box_base")
    box = mates.infer(components).placement("/BoxBase/Box")
    assert [(m.kind, m.value, m.orientation) for m in box.mates] == [
        ("contact", None, None), ("offset", 5.0, "same"), ("offset", 25.0, "same")]
    # Each element is also given in the box's own frame (turned back).
    offset = box.mates[1]
    assert offset.element.own_direction == pytest.approx(
        components[1].own(direction=offset.element.direction))
    assert abs(offset.element.own_direction[0]) == pytest.approx(1.0)  # world Y is the box's X


def test_the_bolted_plates_fixture():
    _, components = _fixture("bolted_plates")
    plan = mates.infer(components)
    assert [(p.component, [m.kind for m in p.mates], p.spin_free) for p in plan.placements] == [
        ("/BoltedPlates/Plate", [], False),
        ("/BoltedPlates/Plate#2", ["coincidence", "coincidence", "contact"], False),
        ("/BoltedPlates/Bolt", ["coincidence", "contact"], True),
        ("/BoltedPlates/Bolt#2", ["coincidence", "contact"], True)]
    bolt, bolt2 = plan.placement("/BoltedPlates/Bolt"), plan.placement("/BoltedPlates/Bolt#2")
    # One part placed twice: the same faces, the same elements in its own frame.
    assert bolt.mates[0].element.face == bolt2.mates[0].element.face
    assert bolt.mates[0].element.own_point == pytest.approx(bolt2.mates[0].element.own_point)


# ── Applying them to a fake V5 product ──


def _apply(plan, components, positions=None, **root_options):
    positions = positions or {c.id: catia_components(c.world) for c in components}
    root = fakes.Root("Asm", positions, **root_options)
    documents = {c.prototype: fakes.document(c.name) for c in components}
    paths = {c.id: f"Asm/{c.name}.{i}" for i, c in enumerate(components, start=1)}
    backend = V5Backend(None, root, root.components, paths, documents, lambda p: p.values)
    report = Constrainer(backend).apply(plan, components, positions)
    return report, root, documents


def test_a_pin_is_constrained_by_published_reference_elements():
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10), holes=[(10, 5, 4)])
    pin = _pin("/A/Pin", 10, 5, -10, 20, 4, head_r=7, head_h=4)
    report, root, documents = _apply(mates.infer([plate, pin]), [plate, pin])
    assert report["ok"] and report["positions_ok"]
    assert report["counts"] == {"fix": 1, "coincidence": 1, "contact": 1}
    types = [c.Type for c in root.constraints.items]
    assert types == [CST_TYPES["reference"], CST_TYPES["on"], CST_TYPES["surf_contact"]]
    fix, coincidence, _ = root.constraints.items
    assert fix.references[0].name == "Asm/Plate.1/!Asm/Plate.1/"
    assert [r.name for r in coincidence.references] == ["Asm/Pin.2/!Axis F2",
                                                         "Asm/Plate.1/!Axis F7"]
    plate_doc = documents["/A/Plate"]
    geoset = plate_doc.Part.HybridBodies.Item(1)
    assert geoset.Name == "Assembly references"
    assert [s.Name for s in geoset.shapes] == ["Axis F7 point", "Axis F7", "Plane F2"]
    line = geoset.shapes[1]
    assert line.kind == "line" and line.args[1].args == (0.0, 0.0, 1.0)
    assert geoset.shapes[2].args == (0.0, 0.0, 1.0, 10.0)  # z = 10, the plate's top
    assert plate_doc.Product.Publications.names == ["Axis F7", "Plane F2"]
    assert report["components"][1]["status"] == "mates"


def test_a_part_placed_twice_gets_its_reference_elements_once():
    lower = _box("/D/Lower", (-40, -20, 0), (40, 20, 6), holes=[(25, 0, 3), (-25, 0, 3)])
    upper = _box("/D/Upper", (-40, -20, 6), (40, 20, 12), holes=[(25, 0, 3), (-25, 0, 3)])
    bolts = [_pin(f"/D/Bolt{'#2' if x < 0 else ''}", x, 0, 0, 12, 3, head_r=5, head_h=4)
             for x in (25, -25)]
    for bolt in bolts:
        bolt.prototype = "/D/Bolt"
        bolt.world = [[1.0, 0.0, 0.0, bolt.faces[0].point[0]], [0.0, 1.0, 0.0, 0.0],
                      [0.0, 0.0, 1.0, 0.0]]
    components = [lower, upper, *bolts]
    report, _, documents = _apply(mates.infer(components), components)
    assert report["ok"]
    assert documents["/D/Bolt"].Product.Publications.names == ["Axis F2", "Plane F3"]
    shapes = documents["/D/Bolt"].Part.HybridBodies.Item(1).shapes
    assert shapes[0].args == (0.0, 0.0, 0.0)  # the axis in the bolt's own frame


def test_a_refused_contact_falls_back_to_a_coincidence_of_planes():
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10), holes=[(10, 5, 4)])
    pin = _pin("/A/Pin", 10, 5, -10, 20, 4, head_r=7, head_h=4)
    report, root, _ = _apply(mates.infer([plate, pin]), [plate, pin],
                             refuse={CST_TYPES["surf_contact"]})
    contact = root.constraints.items[-1]
    assert contact.Type == CST_TYPES["on"] and contact.Orientation == 1  # opposite
    entry = report["components"][1]["constraints"][1]
    assert (entry["kind"], entry["catia_type"], entry["orientation"]) == ("contact", "on",
                                                                          "opposite")


def test_a_constraint_that_moves_a_part_is_removed_and_the_part_fixed():
    base = _box("/B/Base", (-50, -30, -10), (50, 30, 0))
    box = _box("/B/Box", (5, -5, 0), (25, 25, 15))
    report, root, _ = _apply(mates.infer([base, box]), [base, box],
                             movers={CST_TYPES["distance"]: ("/B/Box", 9, 3.0)})
    row = report["components"][1]
    assert row["fallback"] == "it moved /B/Box" and row["status"] == "fix"
    assert row["removed"] == ["Constraint.2", "Constraint.3", "Constraint.4"]
    assert [c.Type for c in root.constraints.items] == [0, 0]
    assert root.components["/B/Box"].Position.values[9] == 0.0  # put back
    assert report["ok"] and report["counts"] == {"fix": 2}


def test_offsets_carry_their_value_and_orientation():
    base = _box("/B/Base", (-50, -30, -10), (50, 30, 0))
    box = _box("/B/Box", (5, -5, 0), (25, 25, 15))
    report, root, _ = _apply(mates.infer([base, box]), [base, box])
    offsets = [c for c in root.constraints.items if c.Type == CST_TYPES["distance"]]
    assert [(c.Dimension.Value, c.Orientation) for c in offsets] == [(5.0, 0), (25.0, 0)]
    assert report["counts"] == {"fix": 1, "contact": 1, "offset": 2}


def test_an_unresolved_constraint_falls_back_to_a_fix():
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10), holes=[(10, 5, 4)])
    pin = _pin("/A/Pin", 10, 5, -10, 20, 4, head_r=7, head_h=4)
    plan = mates.infer([plate, pin])
    positions = {c.id: catia_components(c.world) for c in (plate, pin)}
    root = fakes.Root("Asm", positions)
    original = root.constraints.AddBiEltCst

    def broken(kind, first, second):
        constraint = original(kind, first, second)
        constraint.Status = 3
        return constraint

    root.constraints.AddBiEltCst = broken
    documents = {c.prototype: fakes.document(c.name) for c in (plate, pin)}
    backend = V5Backend(None, root, root.components, {"/A/Plate": "Asm/Plate.1",
                                                      "/A/Pin": "Asm/Pin.1"}, documents,
                        lambda p: p.values)
    report = Constrainer(backend).apply(plan, [plate, pin], positions)
    assert report["components"][1]["fallback"].startswith("not resolved: Constraint.2 (status 3)")
    assert report["counts"] == {"fix": 2}


def test_without_a_constraint_collection_nothing_is_touched():
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10))
    report, root, _ = _apply(mates.fix_all([plate]), [plate], no_connections=True)
    assert not report["ok"] and report["error"].startswith("no constraint collection")
    assert root.updates == 0


def test_fix_mode_fixes_every_instance():
    parts = [_box("/E/Base", (-50, -30, -10), (50, 30, 0)),
             _box("/E/Box", (5, -5, 0), (25, 25, 15))]
    report, root, _ = _apply(mates.fix_all(parts), parts)
    assert [r["status"] for r in report["components"]] == ["ground", "fix"]
    assert [c.Type for c in root.constraints.items] == [0, 0]


def test_paths_follow_the_names_catia_kept():
    from catia_mcp.tree.step_structure import Node

    sub = Node("/R/Sub", "Sub", "/R", IDENTITY)
    plan = MatePlan(None)
    plan_like = type("Plan", (), {})()
    plan_like.root = Node("/R", "R", None, IDENTITY)
    plan_like.nodes = [sub]
    plan_like.instances = [type("I", (), {"id": "/R/Sub/A", "parent": "/R/Sub",
                                          "local": IDENTITY})(),
                           type("I", (), {"id": "/R/B", "parent": "/R", "local": IDENTITY})()]
    report = {"nodes": [{"id": "/R/Sub", "catia_name": "Sub.1"}],
              "instances": [{"id": "/R/Sub/A", "catia_name": "A.1"},
                            {"id": "/R/B", "catia_name": "B.1"}]}
    assert v5_paths(plan_like, "Root", report) == {"/R/Sub/A": "Root/Sub.1/A.1",
                                                   "/R/B": "Root/B.1"}
    assert expected_positions(plan_like)["/R/B"] == catia_components(IDENTITY)
    assert plan.ground is None


# ── 3DEXPERIENCE: finding a collection that takes constraints ──


class _Dispatch:
    """Members exist only when given (as pywin32's late binding)."""

    def __init__(self, **members):
        self.__dict__.update(members)


def test_the_first_3dx_route_that_takes_constraints_is_used():
    usable = _Dispatch(AddMonoEltCst=lambda *a: None, AddBiEltCst=lambda *a: None)
    reference = _Dispatch(GetItem=lambda name: usable if name == "CATEngConnections" else None)

    def refuse(kind):
        raise RuntimeError("not a product")

    root = _Dispatch(Connections=refuse, ReferenceRootOccurrenceOf=reference)
    conn = _Dispatch(root_occurrence=lambda: root, app=None)
    found, attempts = constraint_collection(conn)
    assert found == ("root reference .GetItem('CATEngConnections')", usable)
    assert attempts[0]["error"] == "not a product"


def test_no_3dx_route_leaves_the_imported_product_as_it_is():
    from catia_mcp.tree.assembly_constraints import X3DBackend

    reference = _Dispatch(GetItem=lambda name: _Dispatch())  # no Add*EltCst

    def refuse(kind):
        raise RuntimeError("no")

    root = _Dispatch(Connections=refuse, ReferenceRootOccurrenceOf=reference)
    conn = _Dispatch(root_occurrence=lambda: root, app=_Dispatch(ActiveEditor=None))
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10))
    backend = X3DBackend(conn, {}, {"/A/Plate": "/Plate.1"}, {}, {}, lambda p: p)
    report = Constrainer(backend).apply(mates.fix_all([plate]), [plate], {})
    assert not report["ok"] and "no AddMonoEltCst/AddBiEltCst" in report["error"]


def test_mate_plan_placements_serialise():
    plate = _box("/A/Plate", (-20, -20, 0), (20, 20, 10), holes=[(10, 5, 4)])
    pin = _pin("/A/Pin", 10, 5, -10, 20, 4, head_r=7, head_h=4)
    data = mates.infer([plate, pin]).to_dict()
    assert data["placements"][1]["spin_free"] is True
    assert data["placements"][1]["mates"][0]["element"]["kind"] == "axis"
    assert Placement("x", "fix", dof_left=2, note="n").to_dict() == {
        "component": "x", "status": "fix", "mates": [], "dof_left": 2, "note": "n"}
    assert isinstance(Mate, type)


# ── Command line ──


def test_the_mates_command_writes_the_constraints(tmp_path):
    pytest.importorskip("OCP")
    from catia_mcp.tree.__main__ import main

    out = tmp_path / "mates.json"
    code = main(["mates", str(FIXTURES / "pin_plate.tree.json"), "--step",
                 str(FIXTURES / "pin_plate.step"), "--report", str(out), "--expect", "none"])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["ground"] == "/PinPlate/Plate"
    assert [m["kind"] for m in data["placements"][1]["mates"]] == ["coincidence", "contact"]
