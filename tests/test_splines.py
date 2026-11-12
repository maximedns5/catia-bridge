"""Spline sketch entities (catia_mcp/tree/nurbs.py): exact rational NURBS
evaluation, conic recognition, the points CATIA sketch splines are drawn
through, preflight, the build123d reference, and the builder against the
fake CATIA of test_tree.py.

Comparisons with OpenCascade run when build123d is installed; the stored
OpenCascade volumes below keep the pure-Python checks running without it.
"""

from __future__ import annotations

import copy
import json
import math
from itertools import pairwise
from types import SimpleNamespace

import pytest
import test_tree
from test_tree import TREES, _builder, _FakePart, _mass, load

from catia_mcp.tree.geometry import (
    Frame,
    approximation_area,
    profile_area_centroid,
    revolve_expectation,
)
from catia_mcp.tree.nurbs import Nurbs, clamped, conic, drawing
from catia_mcp.tree.preflight import analyze

# Volumes of the m6_* fixtures (mm3): closed forms, or OpenCascade's
# (build123d reference, Gauss-Kronrod integration) for free-form splines.
M6_MM3 = {
    "m6_ellipse_pad": (math.pi * 12 * 6 - math.pi * 2**2) * 10,
    "m6_spheroid_cut": 30**3 - 4 / 3 * math.pi * 6**2 * 10,
    "m6_spline_pad": 2343.9380643023414,
    "m6_spline_revolve": 3779.2127246820696,
}

S3 = math.sqrt(3)
# A circle of radius 10 as OpenCascade converts it: periodic, degree 2, six
# poles, written in the three knot conventions the reader accepts.
CIRCLE = {"type": "spline", "id": "c", "degree": 2, "periodic": True,
          "poles": [[10, 0], [10, 10 * S3], [-5, 5 * S3], [-20, 0], [-5, -5 * S3],
                    [10, -10 * S3]],
          "weights": [1, 0.5, 1, 0.5, 1, 0.5]}
T = 2 * math.pi / 3
FLAT = [-T, 0, 0, T, T, 2 * T, 2 * T, 3 * T, 3 * T, 4 * T]
CONVENTIONS = ("flat", "period", "textbook")


def _circle(convention: str) -> dict:
    if convention == "flat":  # OpenCascade's flat knots: the 7th pole wraps
        return dict(CIRCLE, knots=FLAT)
    if convention == "period":  # one period, multiplicities expanded
        return dict(CIRCLE, knots=[0, 0, T, T, 2 * T, 2 * T, 3 * T, 3 * T])
    # len(knots) = len(poles) + degree + 1, the wrapped pole written out
    return dict(CIRCLE, knots=FLAT, poles=CIRCLE["poles"] + CIRCLE["poles"][:1],
                weights=CIRCLE["weights"] + [1])


def _ellipse_entity(center, angle, a, b, t0, t1, eid="el", reverse=False, spans=None):
    """An exact elliptical arc from eccentric angle t0 to t1 (counter-
    clockwise from the major axis at `angle`) as a clamped rational
    quadratic: each span is a circular arc of at most 120 degrees mapped onto
    the ellipse."""
    e1, e2 = (math.cos(angle), math.sin(angle)), (-math.sin(angle), math.cos(angle))

    def at(t, k=1.0):
        return [center[0] + k * (a * math.cos(t) * e1[0] + b * math.sin(t) * e2[0]),
                center[1] + k * (a * math.cos(t) * e1[1] + b * math.sin(t) * e2[1])]

    n = spans or max(1, math.ceil((t1 - t0) / (2 * math.pi / 3) - 1e-9))
    phi = (t1 - t0) / n
    poles, weights = [at(t0)], [1.0]
    for i in range(n):
        mid = t0 + (i + 0.5) * phi
        poles += [at(mid, 1 / math.cos(phi / 2)), at(t0 + (i + 1) * phi)]
        weights += [math.cos(phi / 2), 1.0]
    knots = [0.0] * 3 + [float(i) for i in range(1, n) for _ in range(2)] + [float(n)] * 3
    if reverse:
        poles, weights = poles[::-1], weights[::-1]
    return {"type": "spline", "id": eid, "degree": 2, "poles": poles, "weights": weights,
            "knots": knots, "periodic": False}


def _sketch(entities, loops=None):
    loops = loops or [[e["id"] for e in entities]]
    return {"plane": {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "normal": [0, 0, 1]},
            "entities": entities,
            "profiles": [{"outer": loops[0], "inner": loops[1:]}]}


# ── Evaluation ──


@pytest.mark.parametrize("convention", CONVENTIONS)
def test_a_rational_circle_reads_the_same_in_every_knot_convention(convention):
    curve = Nurbs.from_entity(_circle(convention))
    assert curve.closed and len(curve.spans) == 3
    lo, hi = curve.domain
    for k in range(37):
        x, y = curve.point(lo + (hi - lo) * k / 36)
        assert math.hypot(x, y) == pytest.approx(10, abs=1e-12)
    assert curve.length() == pytest.approx(20 * math.pi, rel=1e-12)
    area, centroid = profile_area_centroid(_sketch([_circle(convention)]))
    assert area == pytest.approx(100 * math.pi, rel=1e-6)
    assert centroid == pytest.approx((0, 0), abs=1e-9)


def test_derivatives_match_finite_differences():
    curve = Nurbs.from_entity({"type": "spline", "degree": 3, "periodic": False,
                               "poles": [[0, 0], [3, 5], [8, -2], [12, 4], [15, 0]],
                               "weights": [1, 2, 0.5, 1.5, 1],
                               "knots": [0, 0, 0, 0, 0.4, 1, 1, 1, 1]})
    h = 1e-6
    for u in (0.05, 0.3, 0.4 + 1e-3, 0.7, 0.95):
        (x0, y0), (x1, y1) = curve.point(u - h), curve.point(u + h)
        assert curve.derivative(u) == pytest.approx(((x1 - x0) / 2 / h, (y1 - y0) / 2 / h),
                                                    rel=1e-6)
    assert curve.start == pytest.approx((0, 0)) and curve.end == pytest.approx((15, 0))


def _occ_curve(edge):
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_TrimmedCurve
    from OCP.GeomConvert import GeomConvert

    curve = BRep_Tool.Curve_s(edge.wrapped, 0.0, 1.0)
    first, last = BRep_Tool.Range_s(edge.wrapped)
    if not curve.IsPeriodic() or abs(last - first - curve.Period()) > 1e-12:
        curve = Geom_TrimmedCurve(curve, first, last)
    return GeomConvert.CurveToBSplineCurve_s(curve)


def _entity(bs, convention):
    n = bs.NbPoles()
    if convention == "flat":
        sequence = bs.KnotSequence()
        knots = [sequence.Value(i) for i in range(1, sequence.Length() + 1)]
    else:
        knots = [bs.Knot(i) for i in range(1, bs.NbKnots() + 1) for _ in range(bs.Multiplicity(i))]
    return {"type": "spline", "id": "s", "degree": bs.Degree(), "periodic": bs.IsPeriodic(),
            "poles": [[bs.Pole(i).X(), bs.Pole(i).Y()] for i in range(1, n + 1)],
            "weights": [bs.Weight(i) for i in range(1, n + 1)], "knots": knots}


def test_evaluation_matches_opencascade():
    """Points, first derivatives, lengths and enclosed areas against
    OpenCascade's own curves: a rational circle and ellipse, a periodic
    free-form cubic, and a clamped rational cubic."""
    pytest.importorskip("build123d")
    from build123d import Edge, Face, Plane, Wire
    from OCP.BRepGProp import BRepGProp
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.GProp import GProp_GProps

    clamped_edge = Edge.make_bspline([(0, 0, 0), (3, 5, 0), (8, -2, 0), (12, 4, 0), (15, 0, 0)],
                                     [0, 0, 0, 0, 0.4, 1, 1, 1, 1], 3, [1, 2, 0.5, 1.5, 1])
    cases = [
        (Edge.make_circle(7, Plane((1, 2, 0))), "flat"),
        (Edge.make_ellipse(12, 6, Plane((5, 3, 0), x_dir=(S3 / 2, 0.5, 0))), "period"),
        (Edge.make_spline([(0, 0), (12, -4), (20, 6), (10, 14), (2, 9)], periodic=True), "period"),
        (Edge.make_spline([(0, 0), (12, -4), (20, 6), (10, 14), (2, 9)], periodic=True), "flat"),
        (clamped_edge, "flat"),
    ]
    for edge, convention in cases:
        bs = _occ_curve(edge)
        curve = Nurbs.from_entity(_entity(bs, convention))
        assert curve.domain == pytest.approx((bs.FirstParameter(), bs.LastParameter()))
        lo, hi = curve.domain
        for k in range(41):
            u = lo + (hi - lo) * k / 40
            p, d = gp_Pnt(), gp_Vec()
            bs.D1(u, p, d)
            assert curve.point(u) == pytest.approx((p.X(), p.Y()), abs=1e-10)
            assert curve.derivative(u) == pytest.approx((d.X(), d.Y()), rel=1e-9, abs=1e-9)
        exact = Edge.make_bspline(*_clamped_args(curve))
        assert curve.length() == pytest.approx(exact.length, rel=1e-7)  # OCC: 5e-8 on ellipses
        if curve.closed:
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(Face(Wire([exact])).wrapped, props, 1e-10)
            area, _ = profile_area_centroid(_sketch([_entity(bs, convention)]))
            assert area == pytest.approx(props.Mass(), rel=1e-6)


def _clamped_args(curve):
    degree, poles, weights, knots = clamped(curve)
    return [(u, v, 0) for u, v in poles], knots, degree, weights


def test_malformed_splines_are_refused_by_preflight():
    tree = load("m6_spline_pad")
    entity = tree["parts"][0]["features"][0]["entities"][0]
    for broken in ({"knots": entity["knots"][:-3]}, {"weights": [0] * len(entity["poles"])},
                   {"knots": entity["knots"][::-1]}, {"poles": None}):
        bad = copy.deepcopy(tree)
        bad["parts"][0]["features"][0]["entities"][0].update(broken)
        assert "sketch spline is invalid" in next(iter(analyze(bad))), broken


# ── Conics ──


def test_ellipses_and_their_arcs_are_recognised_in_either_direction():
    for reverse in (False, True):
        entity = _ellipse_entity((5, 3), math.radians(30), 12, 6, 0.4, 0.4 + 4.0, reverse=reverse)
        curve = Nurbs.from_entity(entity)
        shape = conic(curve)
        assert shape is not None and not shape.closed and not shape.is_circle
        assert shape.center == pytest.approx((5, 3)) and (shape.a, shape.b) == pytest.approx((12, 6))
        assert abs(shape.major[0] * math.sin(math.radians(30))
                   - shape.major[1] * math.cos(math.radians(30))) < 1e-9
        assert shape.end - shape.start == pytest.approx(4.0)
        ends = {tuple(round(c, 9) for c in shape.point(t)) for t in (shape.start, shape.end)}
        assert ends == {tuple(round(c, 9) for c in p) for p in (curve.start, curve.end)}
    full = conic(Nurbs.from_entity(_ellipse_entity((0, 0), 1.0, 9, 4, 0.2, 0.2 + 2 * math.pi)))
    assert full.closed and (full.a, full.b) == pytest.approx((9, 4))
    item = drawing(Nurbs.from_entity(_ellipse_entity((0, 0), 1.0, 9, 4, 0, 2 * math.pi))).items[0]
    assert item[0] == "ellipse" and item[3:] == pytest.approx((9, 4))


def test_circles_and_arcs_are_drawn_as_circles():
    plan = drawing(Nurbs.from_entity(_circle("flat")))
    assert plan.exact and plan.items[0][0] == "circle" and plan.items[0][2] == pytest.approx(10)
    arc = _ellipse_entity((1, 1), 0.3, 5, 5, -0.5, 2.0, reverse=True)  # clockwise
    ((kind, center, r, start, end),) = drawing(Nurbs.from_entity(arc)).items
    assert kind == "arc" and center == pytest.approx((1, 1)) and r == pytest.approx(5)
    assert (start, end) == pytest.approx((-0.2, 2.3))  # polar angles, counter-clockwise


def test_curves_that_are_not_ellipses_are_not_taken_for_one():
    parabola = {"type": "spline", "degree": 2, "poles": [[0, 0], [5, 10], [10, 0]],
                "weights": [1, 1, 1], "knots": [0, 0, 0, 1, 1, 1], "periodic": False}
    hyperbola = dict(parabola, weights=[1, 2, 1])
    two = _ellipse_entity((0, 0), 0, 10, 5, 0, math.pi)
    other = _ellipse_entity((0, 0), 0, 10, 6, math.pi, 2 * math.pi)  # same ends, other b
    mixed = {**two, "poles": two["poles"] + other["poles"][1:],
             "weights": two["weights"] + other["weights"][1:],
             "knots": [0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 4]}
    cubic = {"type": "spline", "degree": 3, "poles": [[0, 0], [3, 5], [8, -2], [12, 4]],
             "knots": [0, 0, 0, 0, 1, 1, 1, 1], "periodic": False}
    for entity in (parabola, hyperbola, mixed, cubic):
        curve = Nurbs.from_entity(entity)
        assert conic(curve) is None
        assert not drawing(curve).exact


def test_a_degree_one_spline_is_drawn_as_lines():
    entity = {"type": "spline", "degree": 1, "poles": [[0, 0], [10, 0], [10, 5], [0, 0]],
              "knots": [0, 0, 1, 2, 3, 3], "periodic": False}
    plan = drawing(Nurbs.from_entity(entity))
    assert plan.exact and [item[0] for item in plan.items] == ["line"] * 3
    assert profile_area_centroid(_sketch([dict(entity, id="t")]))[0] == pytest.approx(25)


# ── Approximating sketch splines ──


def test_a_closed_free_form_spline_is_two_sketch_splines_through_enough_points():
    sketch = load("m6_spline_pad")["parts"][0]["features"][0]
    curve = Nurbs.from_entity(sketch["entities"][0])
    plan = drawing(curve)
    assert not plan.exact and [item[0] for item in plan.items] == ["spline", "spline"]
    first, second = (item[1] for item in plan.items)
    assert first[0] == curve.start and second[-1] == pytest.approx(curve.start, abs=1e-12)
    assert first[-1] == second[0]
    area, _ = profile_area_centroid(sketch)
    # The chord bound stays under the per-feature tolerance (1e-3).
    assert 0 < plan.deviation_area == approximation_area(sketch) < 2.5e-4 * area
    chords = first + second[1:]
    chord_area = abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in pairwise(chords))) / 2
    assert area - chord_area == pytest.approx(plan.deviation_area, rel=1e-2)


def test_a_spline_is_split_at_its_corners():
    corner = {"type": "spline", "degree": 3, "periodic": False,
              "poles": [[0, 0], [4, 6], [8, 6], [10, 0], [14, 3], [18, 1], [20, 5]],
              "knots": [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2]}  # C0 at u = 1
    plan = drawing(Nurbs.from_entity(corner))
    assert [item[0] for item in plan.items] == ["spline", "spline"]
    assert plan.items[0][1][-1] == pytest.approx((10, 0)) == plan.items[1][1][0]


def test_an_interpolating_spline_through_the_points_is_far_inside_the_chord_bound():
    """OpenCascade's interpolation stands in for CATIA's sketch spline: through
    the sampled points it encloses the curve's area to well under the chord
    bound the build allows, and under the part tolerance."""
    pytest.importorskip("build123d")
    from build123d import Edge, Face, Wire

    sketch = load("m6_spline_pad")["parts"][0]["features"][0]
    plan = drawing(Nurbs.from_entity(sketch["entities"][0]))
    wire = Wire([Edge.make_spline([(u, v, 0) for u, v in item[1]]) for item in plan.items])
    area, _ = profile_area_centroid(sketch)
    error = abs(Face(wire).area - area)
    assert error < plan.deviation_area / 20 and error < 1e-5 * area


# ── Preflight ──


def test_splines_are_refused_in_loft_sections_and_sweeps():
    loft = load("m4_loft_frustum")
    loft["parts"][0]["features"][0]["entities"] = [_ellipse_entity((0, 0), 0, 10, 10, 0, 2 * math.pi,
                                                                   eid="c1")]
    loft["parts"][0]["features"][0]["profiles"] = [{"outer": ["c1"], "inner": []}]
    assert list(analyze(loft)) == ["loft sections with spline entities are not supported yet"]
    pipe = load("m4_sweep_pipe")
    profile = pipe["parts"][0]["features"][0]
    profile["entities"] = [dict(_circle("flat"), id="c1")]
    profile["profiles"] = [{"outer": ["c1"], "inner": []}]
    assert list(analyze(pipe)) == ["sweep profiles with spline entities are not supported yet"]
    pipe = load("m4_sweep_pipe")
    pipe["parts"][0]["features"][1]["path"][0] = {"type": "spline", "degree": 1}
    assert "'spline' path entities are not supported yet" in next(iter(analyze(pipe)))


# ── Expectations and the reference ──


def test_profile_expectations_match_the_reference_volumes():
    for name in ("m6_spline_pad", "m6_ellipse_pad"):
        sketch, extrude = load(name)["parts"][0]["features"]
        area, _ = profile_area_centroid(sketch)
        assert area * abs(extrude["distance"]) == pytest.approx(M6_MM3[name], rel=1e-6), name
    sketch, _ = load("m6_spline_revolve")["parts"][0]["features"]
    want = revolve_expectation(Frame.from_tree(sketch["plane"]), sketch, (0, 0, 0), (0, 1, 0), 360)
    assert want.volume == pytest.approx(M6_MM3["m6_spline_revolve"], rel=1e-6)


def test_reference_builds_the_exact_splines():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    for name, volume in M6_MM3.items():
        (props,) = expected(load(name)).values()
        assert props["volume_mm3"] == pytest.approx(volume, rel=1e-9), name


# ── Builder against the fake CATIA ──


class _SplineFactory(test_tree._Factory):
    """Ellipses are recorded exactly (as rational quadratics); sketch splines
    as the chords of their points, the worst a spline through them can do."""

    def CreateClosedEllipse(self, cu, cv, mx, my, a, b):
        self.CreateEllipse(cu, cv, mx, my, a, b, 0.0, 2 * math.pi)

    def CreateEllipse(self, cu, cv, mx, my, a, b, start, end):
        assert a >= b and math.hypot(mx, my) == pytest.approx(1) and end > start
        self.entities.append(_ellipse_entity((cu, cv), math.atan2(my, mx), a, b, start, end))

    def CreateControlPoint(self, u, v):
        return SimpleNamespace(uv=(u, v))

    def CreateSpline(self, points):
        assert isinstance(points, tuple) and len(points) >= 2
        self.splines = getattr(self, "splines", 0) + 1
        for p, q in pairwise(points):
            self.CreateLine(*p.uv, *q.uv)


class _SplinePart(_FakePart):
    """Pads of sketches with several loops: each closed entity is a loop of
    its own, the other entities one loop; the largest loop is the outer one."""

    def _prism(self, pad):
        origin, h, v = pad.sketch.ref
        entities = [dict(e, id=str(i)) for i, e in enumerate(pad.sketch.factory.entities)]
        closed = [e for e in entities if e["type"] == "circle" or (
            e["type"] == "spline" and Nurbs.from_entity(e).closed)]
        loops = [[e["id"]] for e in closed]
        rest = [e["id"] for e in entities if e not in closed]
        loops += [rest] if rest else []
        sketch = {"entities": entities, "profiles": []}
        loops.sort(key=lambda ids: -profile_area_centroid(
            {**sketch, "profiles": [{"outer": ids}]})[0])
        sketch["profiles"] = [{"outer": loops[0], "inner": loops[1:]}]
        area, (cu, cv) = profile_area_centroid(sketch)
        normal = test_tree.cross(h, v)
        side = -1 if (pad.DirectionOrientation == 1) != self.pads_reversed else 1
        offset = 0 if pad.IsSymmetric else side * pad.length / 2
        cog = tuple(origin[i] + cu * h[i] + cv * v[i] + offset * normal[i] for i in range(3))
        return area * pad.length * (2 if pad.IsSymmetric else 1), cog


def _spline_build(name, monkeypatch, **fake):
    monkeypatch.setattr(test_tree, "_Factory", _SplineFactory)
    part = _SplinePart(**fake)
    tree = load(name)
    want = {tree["parts"][0]["id"]: {"volume_mm3": M6_MM3[name]}}
    return part, _builder(part, monkeypatch).build(tree, want, check_editability=False)


def _drawn(part):
    return [s for f in part.pads for s in [getattr(f, "sketch", None)] if s is not None]


def test_an_ellipse_with_a_hole_is_padded_as_a_native_ellipse(monkeypatch):
    part, report = _spline_build("m6_ellipse_pad", monkeypatch)
    assert report["success"], report.get("error")
    (sketch,) = _drawn(part)
    kinds = [e["type"] for e in sketch.factory.entities]
    assert kinds == ["spline", "circle"] and not getattr(sketch.factory, "splines", 0)
    feature = report["parts_detail"][0]["features"][1]
    assert "notes" not in feature and report["parts_detail"][0]["spline_allowance_mm3"] == 0
    assert _mass(part.MainBody.solids)[1] == pytest.approx((5, 3, 10), abs=1e-5)


def test_a_free_form_pad_is_approximated_noted_and_checked_with_its_allowance(monkeypatch):
    """On -XZ the CATIA sketch frame is mirrored from the tree's, and the pad
    goes the other way: the chords lose about 1e-4 of the volume, which the
    allowance covers."""
    part, report = _spline_build("m6_spline_pad", monkeypatch)
    assert report["success"], report.get("error")
    (sketch,) = _drawn(part)
    assert sketch.factory.splines == 2
    detail = report["parts_detail"][0]
    (note,) = detail["features"][1]["notes"]
    assert note.startswith("spline e1: approximated by 2 CATIA sketch spline(s) through ")
    lost = M6_MM3["m6_spline_pad"] - detail["volume_mm3"]
    assert 0 < lost <= detail["spline_allowance_mm3"] < 3e-4 * M6_MM3["m6_spline_pad"]


def test_spline_revolves_are_checked_by_pappus(monkeypatch):
    for name in ("m6_spline_revolve", "m6_spheroid_cut"):
        part, report = _spline_build(name, monkeypatch)
        assert report["success"], (name, report.get("error"))
        shafts = [f for f in part.pads if getattr(f, "kind", None) == "shaft"]
        assert len(shafts) == 1 and shafts[0].RevoluteAxis[2] == pytest.approx((0, 1, 0))
    # The spheroid's half-ellipse is one native elliptical arc.
    factory = shafts[0].sketch.factory
    assert sorted(e["type"] for e in factory.entities) == ["line", "spline"]
    assert not getattr(factory, "splines", 0)


def test_a_misdrawn_spline_fails_its_check(monkeypatch):
    """A spline CATIA drew through every other point would lose more than the
    allowance: the build stops at that feature."""

    class Sparse(_SplineFactory):
        def CreateSpline(self, points):
            super().CreateSpline(points[::4] + ((points[-1],) if (len(points) - 1) % 4 else ()))

    monkeypatch.setattr(test_tree, "_Factory", Sparse)
    tree = load("m6_spline_revolve")
    part = _SplinePart()
    report = _builder(part, monkeypatch).build(
        tree, {"/M6/SplineRevolve": {"volume_mm3": M6_MM3["m6_spline_revolve"]}},
        check_editability=False)
    assert not report["success"]
    assert report["error"]["feature"] == "revolve1"
    assert "(Pappus)" in report["error"]["message"]


def test_m6_fixtures_are_listed():
    names = sorted(p.stem for p in TREES.glob("m6_*.json"))
    assert names == sorted(M6_MM3)
    for name in names:
        assert not analyze(json.loads((TREES / f"{name}.json").read_text())), name
