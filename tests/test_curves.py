"""3D curves and the second slice of surface features (catia_mcp/tree/curves.py,
curve_builder.py, curve_reference.py), offline.

The fake CATIA of test_surfaces.py is extended with GSD curves, primitives,
transforms, trims and extracts built by build123d from what the builder
passed (the helix's axis line, start point, pitch, height and senses; the
spline's and polyline's points; the circle's centre and plane; ...), so the
tests check the builder's COM arguments and its choice among CATIA's
undocumented senses against the build123d reference. Options reverse each
sense the way an unknown CATIA convention could.
"""

from __future__ import annotations

import copy
import math
from itertools import accumulate
from types import SimpleNamespace

import pytest
from test_surfaces import _SurfacePart, _wire_of
from test_tree import _builder, load

pytest.importorskip("build123d")

from build123d import Axis, Box, Edge, Location, Plane, Shell, Solid, Vector, Wire

from catia_mcp.tree import requirements
from catia_mcp.tree.anchors import FaceCandidate
from catia_mcp.tree.builder import TreeBuilder
from catia_mcp.tree.curve_builder import SPLINE_REL_TOL
from catia_mcp.tree.curve_reference import (
    curve_properties,
    project,
    section,
    split_parts,
    transformed,
)
from catia_mcp.tree.curves import helix_point, helix_spec, occ_x_dir
from catia_mcp.tree.dressup import DressUps
from catia_mcp.tree.geometry import cross, dot, unit
from catia_mcp.tree.preflight import analyze
from catia_mcp.tree.reference import expected
from catia_mcp.tree.surface_reference import _sewn, surface_properties

CURVE_FIXTURES = ("s7_helix_coil", "s7_helix_taper_left", "s7_spline_ribbon",
                  "s7_polyline_circle", "s7_project_intersect", "s7_loft_three_sections",
                  "s7_trim_extract", "s7_transforms_primitives")


def _point(ref):
    return tuple(ref[1])


class _CurvePart(_SurfacePart):
    """GSD curves, primitives, transforms, trims and extracts, computed with
    build123d. `hand_reversed`, `axis_reversed`, `cylinder_reversed`,
    `rotate_reversed`, `trim_reversed` make CATIA's sense the other one;
    `project_all` gives every solution unless one is asked for;
    `spline_parameters` interpolates the spline CATIA's own way."""

    def __init__(self, hand_reversed=False, axis_reversed=False, cylinder_reversed=False,
                 rotate_reversed=False, trim_reversed=False, project_all=False,
                 spline_parameters=None, box=None, **kwargs):
        super().__init__(**kwargs)
        self.hand_reversed, self.axis_reversed = hand_reversed, axis_reversed
        self.cylinder_reversed, self.rotate_reversed = cylinder_reversed, rotate_reversed
        self.trim_reversed, self.project_all = trim_reversed, project_all
        self.spline_parameters, self.box = spline_parameters, box
        f = self.HybridShapeFactory
        f.AddNewHelix = lambda axis, invert, start, pitch, height, clockwise, angle0, taper, \
            outward: self._new("helix", "AddNewHelix", axis=axis, invert=invert, start=start,
                               pitch=pitch, height=height, clockwise=clockwise, taper=taper,
                               outward=outward)
        f.AddNewSpline = self._spline
        f.AddNewPolyline = self._polyline
        f.AddNewCircleCtrRad = lambda center, support, geodesic, radius: self._limited(
            "circle", "AddNewCircleCtrRad", center=center, support=support, radius=radius)
        f.AddNewProject = self._project
        f.AddNewIntersection = lambda a, b: self._new("intersect", "AddNewIntersection",
                                                      first=a, second=b)
        f.AddNewSphere = lambda center, axis, radius, *angles: self._limited(
            "sphere", "AddNewSphere", center=center, radius=radius)
        f.AddNewCylinder = lambda center, radius, l1, l2, d: self._new(
            "cylinder", "AddNewCylinder", center=center, radius=radius, l1=l1, l2=l2,
            direction=d[1])
        f.AddNewTranslate = lambda ref, d, dist: self._new("translate", "AddNewTranslate",
                                                           ref=ref, direction=d[1], dist=dist)
        f.AddNewRotate = lambda ref, axis, angle: self._new("rotate", "AddNewRotate", ref=ref,
                                                            axis=axis, angle=angle)
        f.AddNewSymmetry = lambda ref, plane: self._new("symmetry", "AddNewSymmetry", ref=ref,
                                                        plane=plane)
        f.AddNewHybridScaling = lambda ref, point, ratio: self._new(
            "scaling", "AddNewHybridScaling", ref=ref, point=point, ratio=ratio)
        f.AddNewAffinity = lambda ref, rx, ry, rz: self._new(
            "affinity", "AddNewAffinity", ref=ref, ratios=(rx, ry, rz), Origin=None)
        f.AddNewHybridTrim = lambda r1, o1, r2, o2: self._new(
            "trim", "AddNewHybridTrim", refs=(r1, r2), orientations=(o1, o2))
        f.AddNewExtract = lambda ref: self._new("extract", "AddNewExtract", ref=ref,
                                                PropagationType=None)

    def _new(self, kind, method, **fields):
        if method in self.refuse:
            raise RuntimeError(f"La méthode {method} a échoué")
        return self._gsd(kind, **fields)

    def _limited(self, kind, method, **fields):
        made = self._new(kind, method, Limitation=0, **fields)
        made.SetLimitation = lambda value: setattr(made, "Limitation", value)
        return made

    def _spline(self):
        made = self._new("spline", "AddNewSpline", points=[], tangents=[], closed=False)
        made.AddPoint = lambda ref: (made.points.append(_point(ref)), made.tangents.append(None))
        made.AddPointWithConstraintExplicit = lambda ref, d, tension, orientation, curvature, \
            radius: (made.points.append(_point(ref)), made.tangents.append(d[1]))
        made.SetClosing = lambda value: setattr(made, "closed", bool(value))
        return made

    def _polyline(self):
        made = self._new("polyline", "AddNewPolyline", points=[], Closure=False)
        made.InsertElement = lambda ref, at: made.points.insert(at - 1, _point(ref))
        return made

    def _project(self, source, target):
        made = self._new("project", "AddNewProject", source=source, target=target, Normal=True,
                         Direction=None, SolutionType=None)
        made.SetDirection = lambda d: setattr(made, "Direction", d)
        return made

    def _make(self, obj):
        op = obj.op
        if op == "helix":
            return self._helix(obj)
        if op == "spline":
            given = [t for t in obj.tangents if t is not None]
            tangents = None
            if given:
                tangents = [t for t in obj.tangents] if len(given) == len(obj.points) else \
                    [obj.tangents[0], obj.tangents[-1]]
            parameters = None
            if self.spline_parameters == "centripetal":
                steps = [math.dist(a, b) ** 0.5 for a, b in zip(obj.points, obj.points[1:])]
                parameters = [0.0] + list(accumulate(steps))
            return Edge.make_spline(obj.points, tangents, periodic=obj.closed,
                                    parameters=parameters)
        if op == "polyline":
            return Wire.make_polygon(obj.points, close=bool(obj.Closure))
        if op == "circle":
            assert obj.Limitation == 1
            origin, h, v = obj.support
            return Edge.make_circle(obj.radius, Plane(origin=_point(obj.center), x_dir=h,
                                                      z_dir=cross(h, v)))
        if op == "project":
            wire = _wire_of(obj.source)
            if obj.Normal:
                return project(wire, obj.target.shape, None)
            direction = obj.Direction[1]
            if obj.SolutionType is None and self.project_all:
                from build123d import Compound

                return Compound(wire.project_to_shape(obj.target.shape, Vector(*direction)))
            return project(wire, obj.target.shape, direction)
        if op == "intersect":
            first = obj.first.shape
            if isinstance(obj.second, tuple):
                origin, h, v = obj.second
                return section(first, {"origin": origin, "normal": cross(h, v)})
            return section(first, obj.second.shape)
        if op == "sphere":
            sphere = Solid.make_sphere(obj.radius).moved(Location(Vector(*_point(obj.center))))
            faces = sphere.faces()
            return faces[0] if obj.Limitation == 1 else faces[0].split(Plane.XY)
        if op == "cylinder":
            d = unit(obj.direction)
            d = tuple(-c for c in d) if self.cylinder_reversed else d
            circle = Wire([Edge.make_circle(obj.radius, Plane(origin=_point(obj.center),
                                                              z_dir=d))])
            start = circle.moved(Location(Vector(*d) * -obj.l2))
            return Shell.extrude(start, Vector(*d) * (obj.l1 + obj.l2))
        if op == "translate":
            return obj.ref.shape.moved(Location(Vector(*obj.direction) * obj.dist))
        if op == "rotate":
            _, origin, direction = obj.axis
            sign = -1 if self.rotate_reversed else 1
            return transformed({"op": "rotate", "axis": {"origin": origin, "direction": direction},
                                "angle": sign * obj.angle}, obj.ref.shape)
        if op == "symmetry":
            origin, h, v = obj.plane
            return transformed({"op": "mirror", "plane": {"origin": origin,
                                                          "normal": cross(h, v)}}, obj.ref.shape)
        if op == "scaling":
            return transformed({"op": "scale", "center": _point(obj.point), "ratio": obj.ratio},
                               obj.ref.shape)
        if op == "affinity":
            origin = _point(obj.Origin) if obj.Origin else (0, 0, 0)
            return transformed({"op": "affinity", "origin": origin, "ratios": obj.ratios},
                               obj.ref.shape)
        if op == "trim":
            kept = []
            first, second = (ref.shape for ref in obj.refs)
            for shape, tool, sense in ((first, second, obj.orientations[0]),
                                       (second, first, obj.orientations[1])):
                parts = sorted(split_parts(shape, tool),
                               key=lambda faces: sum(f.area for f in faces))
                take_last = (sense == 1) != self.trim_reversed
                kept += parts[-1] if take_last else parts[0]
            return _sewn(kept)
        if op == "extract":
            if obj.PropagationType in (2, 3):
                return obj.ref.shape
            return _sewn(list(self.box.faces()))  # point continuity: the whole skin
        return super()._make(obj)

    def _helix(self, obj):
        _, origin, direction = obj.axis
        d = unit(direction)
        if obj.invert != self.axis_reversed:
            d = tuple(-c for c in d)
        start = _point(obj.start)
        left = obj.clockwise != self.hand_reversed
        taper = obj.taper * (1 if obj.outward else -1)
        offset = tuple(s - o for s, o in zip(start, origin))
        radius = math.sqrt(dot(offset, offset) - dot(offset, d) ** 2)
        made = Edge.make_helix(obj.pitch / math.cos(math.radians(taper)), obj.height, radius,
                               origin, d, taper, left)
        first = tuple((made @ 0) - Vector(*origin))
        turn = math.atan2(dot(cross(first, offset), d), dot(first, offset))
        return made.rotate(Axis(origin, d), math.degrees(turn))


def _box_candidates(box):
    out = []
    for face in box.faces():
        c = face.center()
        out.append(FaceCandidate(face, float(face.area), (c.X, c.Y, c.Z)))
    return out


def _build(name, part=None, monkeypatch=None, tree=None, reference=True):
    tree = tree or load(name)
    box = Box(40, 30, 20).moved(Location((0, 0, 10)))
    part = part or _CurvePart()
    part.box = box
    built = _builder(part, monkeypatch)
    monkeypatch.setattr(TreeBuilder, "_surface_area",
                        lambda self, shape: surface_properties(shape.shape)["area_mm2"])
    monkeypatch.setattr(TreeBuilder, "_surface_cog",
                        lambda self, shape: tuple(surface_properties(shape.shape)["cog_mm"]))
    monkeypatch.setattr(TreeBuilder, "_curve_length",
                        lambda self, shape: curve_properties(shape.shape)["length_mm"])
    monkeypatch.setattr(TreeBuilder, "_curve_cog",
                        lambda self, shape: tuple(curve_properties(shape.shape)["cog_mm"]))
    # The extract's face: the topology search of the box (test_dressups.py
    # covers the search itself).
    monkeypatch.setattr(DressUps, "face_candidates",
                        lambda self, body, entry: _box_candidates(box))
    monkeypatch.setattr(DressUps, "_face_reference",
                        lambda self, candidate, anchor, body, entry:
                        SimpleNamespace(shape=candidate.handle))
    report = built.build(tree, expected(tree) if reference else None, check_editability=False)
    return part, report


def _notes(report):
    return " | ".join(n for f in report["parts_detail"][0]["features"] for n in f.get("notes", []))


# ── Contract and reference ──


def test_curve_fixtures_pass_preflight():
    for name in CURVE_FIXTURES:
        assert not analyze(load(name)), name


def test_the_helix_starts_where_build123d_s_does_whatever_its_axis():
    for direction in ((0, 0, 1), (1, 0, 0), (0, -1, 0), (1, 1, 1), (1, 2, 0), (0, 0, -1)):
        for hand, taper in (("right", 0), ("left", 0), ("right", 12), ("left", -8)):
            feature = {"axis": {"origin": [1, 2, 3], "direction": list(direction)}, "pitch": 7,
                       "height": 16, "radius": 4, "hand": hand, "taper": taper}
            spec = helix_spec(feature)
            edge = Edge.make_helix(7, 16, 4, (1, 2, 3), direction, taper, hand == "left")
            assert tuple(edge @ 0) == pytest.approx(spec["start"], abs=1e-9)
            assert tuple(edge @ 1) == pytest.approx(helix_point(spec, 1.0), abs=1e-6)
            inside = tuple(edge @ 0.37)
            height = dot(tuple(a - b for a, b in zip(inside, (1, 2, 3))), spec["direction"])
            assert inside == pytest.approx(helix_point(spec, height / 16), abs=1e-6)
    assert occ_x_dir((0, 0, 1)) == pytest.approx((1, 0, 0))


def test_reference_curves_and_surfaces_are_the_closed_forms():
    coil = expected(load("s7_helix_coil"))["/S7/Coil"]
    helix = 2.5 * math.hypot(2 * math.pi * 8, 10)
    assert coil["curves"]["Helix"]["length_mm"] == pytest.approx(helix, rel=1e-9)
    assert coil["curves"]["Helix"]["cog_mm"][1] == pytest.approx(8 / (2.5 * math.pi))
    # A tube's area is its circumference times the length of its centre line.
    assert coil["surfaces"]["Coil"]["area_mm2"] == pytest.approx(2 * math.pi * 1.5 * helix,
                                                                 rel=1e-6)
    drum = expected(load("s7_project_intersect"))["/S7/Drum"]
    assert drum["surfaces"]["Lid"]["area_mm2"] == pytest.approx(math.pi * 400 * math.sqrt(5) / 2,
                                                                rel=1e-9)
    assert drum["surfaces"]["Tunnel"]["area_mm2"] == pytest.approx(10 * 2 * math.pi * 8, rel=1e-5)
    assert drum["curves"]["Seams"]["length_mm"] == pytest.approx(80)
    assert drum["curves"]["Window"]["cog_mm"][1] < -18  # the nearest solution
    vase = expected(load("s7_loft_three_sections"))["/S7/Vase"]
    assert vase["volume_mm3"] == pytest.approx(5 * math.pi * (196 + 171), rel=1e-9)
    assert vase["surfaces"]["Vase band 1"]["area_mm2"] + vase["surfaces"]["Vase band 2"][
        "area_mm2"] == pytest.approx(vase["surfaces"]["Vase"]["area_mm2"])
    collar = expected(load("s7_trim_extract"))["/S7/Collar"]["surfaces"]
    assert collar["Top"]["area_mm2"] == pytest.approx(1200)
    assert collar["Collar"]["area_mm2"] == pytest.approx(1200 - 100 * math.pi + 400 * math.pi)
    shapes = expected(load("s7_transforms_primitives"))["/S7/Shapes"]["surfaces"]
    assert shapes["Post3"]["cog_mm"] == pytest.approx([0, 30, 10], abs=1e-9)
    assert shapes["Post4"]["cog_mm"] == pytest.approx([70, 0, 10], abs=1e-9)
    assert shapes["Pebble"]["area_mm2"] == pytest.approx(100 * math.pi)
    assert shapes["Pebble"]["cog_mm"] == pytest.approx([10, 0, 0], abs=1e-9)
    e = math.sqrt(0.75)  # a prolate spheroid of semi-axes 20, 10, 10
    egg = 2 * math.pi * 100 * (1 + 2 * math.asin(e) / e)
    assert shapes["Egg"]["area_mm2"] == pytest.approx(egg, rel=1e-5)
    wires = expected(load("s7_polyline_circle"))["/S7/Wires"]["surfaces"]
    assert wires["Band"]["area_mm2"] == pytest.approx(700)
    assert wires["Disc"]["area_mm2"] == pytest.approx(36 * math.pi)


def test_preflight_refuses_what_this_slice_cannot_build():
    def refusals(name, edit):
        tree = copy.deepcopy(load(name))
        edit(tree["parts"][0]["features"])
        return " | ".join(analyze(tree))

    assert "curve op 'spiral' is not supported" in refusals(
        "s7_helix_coil", lambda f: f[0].update(op="spiral"))
    assert "a helix needs a positive pitch" in refusals(
        "s7_helix_coil", lambda f: f[0].update(pitch=0))
    assert "the guide 'Nothing' is not a known curve" in refusals(
        "s7_helix_coil", lambda f: f[2].update(guide="Nothing"))
    assert "smooth loft surfaces through more than two sections" in refusals(
        "s7_loft_three_sections", lambda f: f[3].pop("ruled"))
    assert "a curve is extruded along a `direction`" in refusals(
        "s7_spline_ribbon", lambda f: f[1].pop("direction"))
    assert "a fill needs a closed planar curve" in refusals(
        "s7_polyline_circle", lambda f: f[3].update(inputs=["Loop"]))
    assert "a fill needs a closed planar curve" in refusals(
        "s7_polyline_circle", lambda f: f[2].update(closed=False))
    assert "trim_surfaces needs one keep point per surface" in refusals(
        "s7_trim_extract", lambda f: f[5].pop("keep_points"))
    assert "extract must name exactly one known body" in refusals(
        "s7_trim_extract", lambda f: f[2].update(bodies=["Body9"]))
    assert "already names a curve" in refusals(
        "s7_helix_coil", lambda f: f[2].update(creates=["Helix"]))
    assert "project a known curve or sketch onto a known surface" in refusals(
        "s7_project_intersect", lambda f: f[5].update(inputs=["Mark", "Nothing"]))
    assert "affinity ratios must be positive" in refusals(
        "s7_transforms_primitives", lambda f: f[6].update(ratios=[2, 0, 1]))


def test_curves_need_wireframe_and_surfaces():
    feature = load("s7_helix_coil")["parts"][0]["features"][0]
    assert {requirements.SURFACE, requirements.WIREFRAME} <= \
        requirements.feature_needs(feature, {})


# ── Builder against the fake CATIA ──


@pytest.mark.parametrize("name", CURVE_FIXTURES)
def test_curve_fixtures_build_and_match_build123d(monkeypatch, name):
    _, report = _build(name, monkeypatch=monkeypatch)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    for row in detail["curves"]:
        assert row["length_mm"] == pytest.approx(row["expected_length_mm"], rel=1e-3), row
    for row in detail["surfaces"]:
        assert row["area_mm2"] == pytest.approx(row["expected_area_mm2"], rel=1e-3), row
    assert detail["curves"] or detail["surfaces"]


def test_a_helix_is_named_about_a_named_axis_from_a_named_point(monkeypatch):
    part, report = _build("s7_helix_coil", monkeypatch=monkeypatch)
    helix = next(s for s in part.gsd if s.op == "helix")
    assert helix.Name == "Helix" and (helix.invert, helix.clockwise) == (False, False)
    assert helix.pitch == 10 and helix.height == 25 and helix.taper == 0
    rows = {r["name"]: r for r in report["parts_detail"][0]["construction"]}
    assert "Helix axis Z" in rows and "Point (8, 0, 0)" in rows
    assert part.geosets[-1].Name in ("Curves", "Surfaces")
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        expected(load("s7_helix_coil"))["/S7/Coil"]["volume_mm3"], rel=1e-4)


@pytest.mark.parametrize("option, label", [("hand_reversed", "axis as given, clockwise"),
                                           ("axis_reversed", "axis inverted")])
def test_catia_s_helix_senses_are_found_by_the_centre(monkeypatch, option, label):
    _, report = _build("s7_helix_taper_left", _CurvePart(**{option: True}), monkeypatch)
    assert report["success"], report.get("error")
    assert label in _notes(report)


@pytest.mark.parametrize("option, label", [
    ("cylinder_reversed", "Post: against the direction"),
    ("rotate_reversed", "Post3: angle reversed")])
def test_primitive_and_transform_senses_are_found_by_the_centre(monkeypatch, option, label):
    _, report = _build("s7_transforms_primitives", _CurvePart(**{option: True}), monkeypatch)
    assert report["success"], report.get("error")
    assert label in _notes(report)


def test_a_scaling_catia_refuses_becomes_an_affinity(monkeypatch):
    part, report = _build("s7_transforms_primitives",
                          _CurvePart(refuse=("AddNewHybridScaling",)), monkeypatch)
    assert report["success"], report.get("error")
    assert "Pebble: affinity" in _notes(report)
    egg = next(s for s in part.gsd if s.Name == "Egg")
    assert egg.ratios == (2, 1, 1) and egg.Origin is None
    pebble = next(s for s in part.gsd if s.Name == "Pebble")
    assert pebble.Origin == ("point", (20.0, 0.0, 0.0))


def test_a_trim_keeps_the_parts_build123d_keeps(monkeypatch):
    _, plain = _build("s7_trim_extract", monkeypatch=monkeypatch)
    _, flipped = _build("s7_trim_extract", _CurvePart(trim_reversed=True), monkeypatch)
    for report in (plain, flipped):
        assert report["success"], report.get("error")
        (row,) = [r for r in report["parts_detail"][0]["surfaces"] if r["name"] == "Collar"]
        assert row["area_mm2"] == pytest.approx(1200 + 300 * math.pi)
    assert "Collar: orientations" in _notes(flipped)


def test_an_extract_takes_only_its_face(monkeypatch):
    part, report = _build("s7_trim_extract", monkeypatch=monkeypatch)
    top = next(s for s in part.gsd if s.Name == "Top")
    assert top.PropagationType == 3
    assert report["parts_detail"][0]["features"][2]["catia"] == ["Top"]


def test_a_projection_keeps_the_nearest_solution(monkeypatch):
    part, report = _build("s7_project_intersect", _CurvePart(project_all=True), monkeypatch)
    assert report["success"], report.get("error")
    window = next(s for s in part.gsd if s.Name == "Window")
    assert window.SolutionType == 0 and window.Normal is False
    assert "Window: solution type 0" in _notes(report)
    shadow = next(s for s in part.gsd if s.Name == "Shadow")
    assert shadow.Normal is True


def test_a_ruled_loft_through_three_sections_is_two_bands_joined(monkeypatch):
    part, report = _build("s7_loft_three_sections", monkeypatch=monkeypatch)
    assert report["success"], report.get("error")
    names = [r["name"] for r in report["parts_detail"][0]["surfaces"]]
    assert names == ["Vase band 1", "Vase band 2", "Vase"]
    vase = next(s for s in part.gsd if s.Name == "Vase")
    assert vase.op == "join" and len(vase.refs) == 2
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(5 * math.pi * 367,
                                                                    rel=1e-6)


def test_a_spline_is_catia_s_own_interpolation_checked_loosely(monkeypatch):
    part, report = _build("s7_spline_ribbon", _CurvePart(spline_parameters="centripetal"),
                          monkeypatch)
    assert report["success"], report.get("error")
    (rail,) = report["parts_detail"][0]["curves"]
    assert 0 < rail["deviation"] < SPLINE_REL_TOL
    (ribbon,) = report["parts_detail"][0]["surfaces"]
    assert "deviation" in ribbon
    assert "CATIA's interpolation through the tree's points" in _notes(report)
    assert report["parts_detail"][0]["spline_allowance_mm3"] > 0
    spline = next(s for s in part.gsd if s.op == "spline")
    assert spline.points[1] == (10.0, 5.0, 3.0) and spline.tangents == [None] * 4


def test_spline_tangents_are_given_to_catia(monkeypatch):
    tree = load("s7_spline_ribbon")
    tree["parts"][0]["features"][0]["tangents"] = [[1, 0, 0], [1, -1, 0]]
    part, report = _build("s7_spline_ribbon", monkeypatch=monkeypatch, tree=tree)
    assert report["success"], report.get("error")
    spline = next(s for s in part.gsd if s.op == "spline")
    assert spline.tangents[0] == pytest.approx((1, 0, 0))
    assert spline.tangents[1:3] == [None, None]


def test_a_curve_no_variant_builds_stops_the_build(monkeypatch):
    _, report = _build("s7_helix_coil", _CurvePart(refuse=("AddNewHelix",)), monkeypatch)
    assert not report["success"]
    assert report["error"]["feature"] == "curve1"
    assert "no way of building curve 'Helix'" in report["error"]["message"]


def test_without_a_reference_the_first_curve_is_kept_and_noted(monkeypatch):
    _, report = _build("s7_polyline_circle", monkeypatch=monkeypatch, reference=False)
    assert report["success"], report.get("error")
    assert report["parts_detail"][0]["curves"][0]["expected_length_mm"] is None
    assert "Loop: no build123d reference, not checked" in _notes(report)


def test_a_3dexperience_length_is_read_in_the_unit_that_fits():
    built = TreeBuilder.__new__(TreeBuilder)
    built.part = SimpleNamespace()
    assert built._length_mm(-0.1281264, 128.1264) == pytest.approx(128.1264)
    assert built._surface_state()["length_factor"] == 1000.0
    assert built._length_mm(-0.5, None) == pytest.approx(500)
    built._surfaces = None
    assert built._length_mm(-128.1264, 128.1264) == pytest.approx(128.1264)
