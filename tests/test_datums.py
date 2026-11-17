"""Reference geometry made as a designer would (catia_mcp/tree/datums.py).

Planes are classified (origin, offset, angle about an axis, equation), named
after their role and made once per distinct plane; angle planes are
confirmed by the frame CATIA gives the first sketch on them. The builder runs
against the fake CATIA of test_tree.py, extended with plane objects (offset,
angle, equation) whose sketch frame is computed from their definition.
"""

from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import pytest
from test_tree import CLOSED_FORM_MM3, _builder, _FakePart, _mass, _Sketch, load

from catia_mcp.tree.datums import classify, frame_fits, plane_key
from catia_mcp.tree.geometry import Frame, add, cross, dot, scale, unit


def _frame(origin, normal, x_dir=None):
    normal = unit(tuple(float(c) for c in normal))
    if x_dir is None:
        seed = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
        x_dir = unit(cross(cross(normal, seed), normal))
    return Frame.from_tree({"origin": list(origin), "x_dir": list(x_dir), "normal": list(normal)})


def test_planes_are_classified_and_named_as_a_designer_would():
    assert classify(_frame((0, 0, 0), (0, 0, 1))).kind == "origin"
    offset = classify(_frame((3, 4, 20), (0, 0, 1)))
    assert (offset.kind, offset.name, offset.base, offset.offset) == \
        ("offset", "Plane Z+20", "PlaneXY", 20)
    # A reversed normal is the same plane (the sketch is padded the other way).
    assert classify(_frame((0, 0, 20), (0, 0, -1))).name == "Plane Z+20"
    assert classify(_frame((-7.5, 0, 0), (1, 0, 0))).name == "Plane X-7.5"
    assert classify(_frame((0, 5, 0), (0, -1, 0))).name == "Plane Y+5"
    s = math.sqrt(0.5)
    tilted = classify(_frame((0, 0, 0), (0, -s, s)))
    assert (tilted.kind, tilted.base, tilted.axis, tilted.angle) == ("angle", "PlaneXY", "X",
                                                                     pytest.approx(45))
    assert tilted.name == "Plane XY +45deg about X"
    turned = classify(_frame((0, 0, 0), (math.sin(math.radians(30)), 0,
                                         math.cos(math.radians(30)))))
    # Z turned 30 deg about Y lands on (sin 30, 0, cos 30).
    assert (turned.base, turned.axis, turned.angle) == ("PlaneXY", "Y", pytest.approx(30))
    off_axis = classify(_frame((0, 0, 10), (0, -s, s)))
    assert off_axis.kind == "angle" and off_axis.through == pytest.approx((0, -5, 5))
    assert off_axis.name == "Plane XY +45deg about X through (0, -5, 5)"
    general = classify(_frame((10, 0, 0), (1, 1, 1)))
    assert general.kind == "equation" and general.name == "Plane 0.5774x+0.5774y+0.5774z=5.7735"


def test_a_plane_and_its_reverse_share_one_key():
    assert plane_key(_frame((0, 0, 20), (0, 0, 1))) == plane_key(_frame((1, 2, 20), (0, 0, -1)))
    assert plane_key(_frame((0, 0, 20), (0, 0, 1))) != plane_key(_frame((0, 0, 21), (0, 0, 1)))


def test_a_sketch_frame_fits_its_plane_by_normal_and_position():
    frame = _frame((0, 0, 20), (0, 0, 1))
    assert frame_fits(frame, ((5, 5, 20), (0, 1, 0), (1, 0, 0)))  # reversed normal is fine
    assert not frame_fits(frame, ((5, 5, 21), (1, 0, 0), (0, 1, 0)))
    assert not frame_fits(frame, ((0, 0, 20), (1, 0, 0), (0, 0, 1)))


# ── Builder against the fake CATIA ──


def _rotate(p, axis, angle_deg):
    a = math.radians(angle_deg)
    k = unit(axis)
    return tuple(p[i] * math.cos(a) + cross(k, p)[i] * math.sin(a)
                 + k[i] * dot(k, p) * (1 - math.cos(a)) for i in range(3))


class _PlanePart(_FakePart):
    """CATIA planes as objects: offset planes with an Offset length, angle
    planes turning their base about an axis line (by `angle_sense` times the
    angle: -1 when CATIA turns the other way), equation planes."""

    def __init__(self, angle_sense=1, angle_refused=False, **kwargs):
        super().__init__(**kwargs)
        self.angle_sense, self.planes, self.sketches_deleted = angle_sense, [], []
        factory = self.HybridShapeFactory
        factory.AddNewPlaneOffset = self._offset_object
        factory.AddNewPlaneEquation = self._equation_object
        if not angle_refused:
            factory.AddNewPlaneAngle = self._angle_object

    def _offset_object(self, ref, offset, reverse):
        plane = SimpleNamespace(Name="Plan.1", kind="offset", base=ref,
                                Offset=SimpleNamespace(Value=offset))

        def frame():
            origin, h, v = plane.base
            n = cross(h, v)
            return (add(origin, scale(n, plane.Offset.Value)), h, v)

        plane.frame = frame
        self.planes.append(plane)
        return plane

    def _angle_object(self, ref, axis, angle, orientation):
        plane = SimpleNamespace(Name="Plan.1", kind="angle", base=ref, axis=axis,
                                Angle=SimpleNamespace(Value=angle))

        def frame():
            origin, h, v = plane.base
            _, through, direction = plane.axis
            turn = self.angle_sense * plane.Angle.Value
            moved = add(through, _rotate(add(origin, scale(through, -1)), direction, turn))
            return (moved, _rotate(h, direction, turn), _rotate(v, direction, turn))

        plane.frame = frame
        self.planes.append(plane)
        return plane

    def _equation_object(self, a, b, c, d):
        normal = (a, b, c)
        seed = (1.0, 0.0, 0.0) if abs(a) < 0.9 else (0.0, 1.0, 0.0)
        h = unit(cross(cross(normal, seed), normal))
        plane = SimpleNamespace(Name="Plan.1", kind="equation",
                                frame=lambda: (scale(normal, d), h, cross(normal, h)))
        self.planes.append(plane)
        return plane

    def CreateReferenceFromObject(self, obj):
        if hasattr(obj, "frame"):
            return obj.frame()
        return super().CreateReferenceFromObject(obj)

    def delete(self, feature):
        if isinstance(feature, _Sketch):
            self.sketches_deleted.append(feature)
            return
        if getattr(feature, "kind", None) in ("offset", "angle", "equation"):
            self.planes.remove(feature)
            return
        super().delete(feature)


def _two_pads_on_one_plane():
    """Two blocks sketched on z = 20 (one with its normal reversed), each
    padded 5 mm up."""
    tree = load("m1_offset_block")
    part = tree["parts"][0]
    first, extrude = part["features"]
    first["plane"]["origin"] = [0, 0, 20]
    second = copy.deepcopy(first)
    second.update(id="sketch2", name="Sketch2")
    second["plane"] = {"origin": [100, 0, 20], "x_dir": [0, 1, 0], "normal": [0, 0, -1]}
    extrude2 = dict(extrude, id="extrude2", name="Extrude2", sketch="sketch2", distance=-5,
                    operation="join", bodies=["Body1"])
    extrude2.pop("creates")
    extrude["distance"] = 5
    part["features"] = [first, extrude, second, extrude2]
    return tree


def test_one_named_plane_per_distinct_sketch_plane_reused(monkeypatch):
    part = _PlanePart()
    tree = _two_pads_on_one_plane()
    report = _builder(part, monkeypatch).build(
        tree, {"/M1/OffsetBlock": {"volume_mm3": 2 * 80 * 50 * 5}}, check_editability=False)
    assert report["success"], report.get("error")
    (plane,) = part.planes
    assert plane.Name == "Plane Z+20" and plane.Offset.Value == 20
    (row,) = report["parts_detail"][0]["construction"]
    assert row == {"type": "plane", "name": "Plane Z+20", "definition": "PlaneXY offset by 20 mm",
                   "used_by": ["extrude1", "extrude2"]}
    assert _mass(part.MainBody.solids) == (pytest.approx(40000), pytest.approx((50, 0, 22.5)))


def test_a_sketch_offset_from_the_script_is_a_user_parameter(monkeypatch):
    part = _PlanePart()
    tree = load("m1_offset_block")
    sketch = tree["parts"][0]["features"][0]
    sketch["params"] = {"offset": {"arg": "amount", "kind": "literal", "value": -10,
                                   "file": "model.py", "line": 2, "col": 0}}
    expected = {"/M1/OffsetBlock": {"volume_mm3": 80 * 50 * 20, "features": {
        "extrude1": {"Body1": {"volume_mm3": 80 * 50 * 20, "cog_mm": [0, 0, 0]}}}}}
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    (plane,) = part.planes
    assert detail["parameters"][0]["name"] == "Sketch1_amount"
    assert part.Relations.formulas["Sketch1_amount_f1"] == (plane.Offset, "Sketch1_amount")
    assert detail["construction"][0]["parameter"] == "Sketch1_amount"


def test_a_number_that_is_not_the_plane_s_does_not_drive_it(monkeypatch):
    part = _PlanePart()
    tree = load("m1_offset_block")
    tree["parts"][0]["features"][0]["params"] = {
        "offset": {"arg": "amount", "kind": "literal", "value": 7, "line": 2}}
    report = _builder(part, monkeypatch).build(
        tree, {"/M1/OffsetBlock": {"volume_mm3": 80 * 50 * 20}}, check_editability=False)
    assert report["success"], report.get("error")
    assert "parameter" not in report["parts_detail"][0]["construction"][0]
    assert not part.Relations.formulas


@pytest.mark.parametrize("sense", [1, -1])
def test_a_tilted_sketch_is_on_an_angle_plane_confirmed_by_its_frame(monkeypatch, sense):
    part = _PlanePart(angle_sense=sense)
    report = _builder(part, monkeypatch).build(
        load("m1_tilted_plane"), {"/M1/TiltedPlane": {"volume_mm3": CLOSED_FORM_MM3[
            "m1_tilted_plane"]}}, check_editability=False)
    assert report["success"], report.get("error")
    (plane,) = part.planes
    assert plane.kind == "angle" and plane.Name == "Plane XY +45deg about X"
    rows = report["parts_detail"][0]["construction"]
    angle = next(r for r in rows if r["type"] == "plane")
    assert angle["verified"] is True
    assert {r["name"] for r in rows if r["type"] == "axis"} == {"Axis X"}
    if sense == 1:
        assert plane.Angle.Value == pytest.approx(45) and not part.sketches_deleted
        assert "notes" not in angle
    else:
        assert plane.Angle.Value == pytest.approx(-45) and len(part.sketches_deleted) == 1
        assert angle["notes"] == ["CATIA turned the other way: angle sign changed"]
    assert _mass(part.MainBody.solids)[0] == pytest.approx(CLOSED_FORM_MM3["m1_tilted_plane"])


def test_an_angle_plane_catia_refuses_falls_back_to_an_equation_plane(monkeypatch):
    part = _PlanePart(angle_refused=True)
    report = _builder(part, monkeypatch).build(
        load("m1_tilted_plane"), {"/M1/TiltedPlane": {"volume_mm3": CLOSED_FORM_MM3[
            "m1_tilted_plane"]}}, check_editability=False)
    assert report["success"], report.get("error")
    (plane,) = part.planes
    assert plane.kind == "equation" and plane.Name == "Plane -0.7071y+0.7071z=0"
    row = next(r for r in report["parts_detail"][0]["construction"] if r["type"] == "plane")
    assert "equation plane instead" in row["notes"][0]


def test_an_angle_plane_on_neither_sense_becomes_an_equation_plane(monkeypatch):
    part = _PlanePart(angle_sense=0)  # CATIA never turns it: neither sense fits
    report = _builder(part, monkeypatch).build(
        load("m1_tilted_plane"), {"/M1/TiltedPlane": {"volume_mm3": CLOSED_FORM_MM3[
            "m1_tilted_plane"]}}, check_editability=False)
    assert report["success"], report.get("error")
    (plane,) = part.planes
    assert plane.kind == "equation" and len(part.sketches_deleted) == 2
    row = next(r for r in report["parts_detail"][0]["construction"] if r["type"] == "plane")
    assert row["notes"][-1].startswith("neither angle sense")


def test_split_and_mirror_planes_are_named_planes_too(monkeypatch):
    part = _PlanePart()
    report = _builder(part, monkeypatch).build(
        load("m2_mirror_offset_plane"),
        {"/M2/MirrorOffset": {"volume_mm3": CLOSED_FORM_MM3["m2_mirror_offset_plane"]}},
        check_editability=False)
    assert report["success"], report.get("error")
    names = [r["name"] for r in report["parts_detail"][0]["construction"]]
    assert names and all(name.startswith("Plane ") for name in names)
    assert len(names) == len(set(names))
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        CLOSED_FORM_MM3["m2_mirror_offset_plane"])
