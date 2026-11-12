"""Tapered and oblique extrudes, and part placement (milestone 5), offline.

The builder runs against the fake CATIA of test_tree.py, extended with pads
that follow a direction set with SetDirection. The closed forms of the m5
fixtures are prismatoids (tapers), sheared prisms (oblique extrudes) and a
placed block whose centre of gravity moves with its placement.
"""

from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import pytest
from test_tree import CLOSED_FORM_MM3, _builder, _FakePart, _lofts, _mass, load

from catia_mcp.tree import builder as builder_module
from catia_mcp.tree.extrude import offset_loop, taper_plan
from catia_mcp.tree.geometry import Frame, cross, dot, profile_area_centroid
from catia_mcp.tree.loft import section_loop
from catia_mcp.tree.placement import place_part
from catia_mcp.tree.preflight import analyze


def _prismatoid(height, area, delta):
    """h/6 (A0 + 4 Am + A1) for a section whose loops are offset by up to delta."""
    return abs(height) / 6 * (area(0) + 4 * area(delta / 2) + area(delta))


def _frustum(r0, r1, h):
    return math.pi * h / 3 * (r0 * r0 + r0 * r1 + r1 * r1)


_T10 = 8 * math.tan(math.radians(10))
M5_CLOSED_FORM_MM3 = {
    "m5_taper_rect": _prismatoid(10, lambda d: (20 + 2 * d) * (10 + 2 * d),
                                 -10 * math.tan(math.radians(5))),
    "m5_taper_ring_join": 60 * 40 * 10 + _frustum(12, 12 - _T10, 8) - _frustum(5, 5 + _T10, 8),
    "m5_taper_slot_symmetric_tilted": 2 * _prismatoid(
        6, lambda d: 20 * 2 * (4 + d) + math.pi * (4 + d) ** 2, 6 * math.tan(math.radians(3))),
    "m5_taper_pocket_down": 80 * 50 * 20 - _prismatoid(8, lambda d: (30 + 2 * d) ** 2,
                                                       -8 * math.tan(math.radians(6))),
    "m5_oblique_rect_hole": (20 * 10 - math.pi * 2**2) * 10,
    "m5_oblique_taper_yz": _prismatoid(12, lambda d: (16 + 2 * d) ** 2,
                                       -13 * math.tan(math.radians(4))),
    "m5_oblique_symmetric_cut": 80 * 50 * 20 - math.pi * 5**2 * 16,
    "m5_placed_block": 50 * 40 * 20 - math.pi * 3**2 * 20,
}
# A turn of 120 degrees about (1, 1, 1): x -> y -> z -> x.
CYCLE = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]


def _placement(rotation, translation):
    return [list(map(float, row)) + [float(t)] for row, t in zip(rotation, translation, strict=True)]


def _rotation(axis, degrees):
    x, y, z = (c / math.sqrt(sum(a * a for a in axis)) for c in axis)
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    k = 1 - c
    return [[c + x * x * k, x * y * k - z * s, x * z * k + y * s],
            [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
            [z * x * k - y * s, z * y * k + x * s, c + z * z * k]]


def _moved(rotation, translation, point):
    return tuple(sum(rotation[i][k] * point[k] for k in range(3)) + translation[i] for i in range(3))


def _feature(tree, fid):
    return next(f for f in tree["parts"][0]["features"] if f["id"] == fid)


# ── Tapers: semantics and closed forms ──


def test_taper_plans_match_the_closed_forms():
    """Without build123d: a taper alone is the prismatoid of its fixture."""
    for name in ("m5_taper_rect", "m5_taper_slot_symmetric_tilted", "m5_oblique_taper_yz"):
        tree = load(name)
        plan = taper_plan(_feature(tree, "sketch1"), _feature(tree, "extrude1"))
        # Simpson along arcs: exact for lines, 1e-8 for arcs.
        assert plan.volume == pytest.approx(M5_CLOSED_FORM_MM3[name], rel=1e-7), name
    tree = load("m5_taper_ring_join")
    plan = taper_plan(_feature(tree, "sketch2"), _feature(tree, "extrude2"))
    assert plan.volume == pytest.approx(M5_CLOSED_FORM_MM3["m5_taper_ring_join"] - 24000, rel=1e-7)
    assert [len(piece.inners) for piece in plan.pieces] == [1]


def test_a_positive_taper_shrinks_the_outer_loop_and_widens_holes():
    tree = load("m5_taper_ring_join")
    plan = taper_plan(_feature(tree, "sketch2"), _feature(tree, "extrude2"))
    (piece,) = plan.pieces
    assert piece.outer.second[0].radius == pytest.approx(12 - _T10)
    assert piece.inners[0].second[0].radius == pytest.approx(5 + _T10)
    assert piece.outer.height == pytest.approx(8)
    # The rectangle's corners move inward along their bisectors.
    rect = load("m5_taper_rect")
    plan = taper_plan(_feature(rect, "sketch1"), _feature(rect, "extrude1"))
    delta = 10 * math.tan(math.radians(5))
    assert plan.pieces[0].outer.second[0].a == pytest.approx((-10 + delta, -5 + delta))


def test_offset_keeps_each_vertex_paired_with_its_own_offset():
    sketch = _feature(load("m5_taper_slot_symmetric_tilted"), "sketch1")
    loop = section_loop(sketch, Frame.from_tree(sketch["plane"]))
    grown = offset_loop(loop, 1.0)
    assert [s.center is None for s in grown] == [s.center is None for s in loop]
    assert grown[0].a == pytest.approx((-10, -5)) and grown[1].radius == pytest.approx(5)
    # Either drawing direction moves the loop the same way.
    backward = offset_loop([s.reversed() for s in reversed(loop)], 1.0)
    assert {tuple(round(c, 9) for c in s.a) for s in backward} == \
        {tuple(round(c, 9) for c in s.a) for s in grown}


def test_reference_matches_the_m5_closed_forms_and_the_taper_plans():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    for name, volume in M5_CLOSED_FORM_MM3.items():
        (props,) = expected(load(name)).values()
        assert props["volume_mm3"] == pytest.approx(volume, rel=1e-9), name
    # build123d's own taper (its loft to a 2D offset, or LocOpe_DPrism) is
    # the solid the plan describes, centre of gravity included.
    for name in ("m5_taper_rect", "m5_taper_slot_symmetric_tilted", "m5_oblique_taper_yz"):
        tree = load(name)
        plan = taper_plan(_feature(tree, "sketch1"), _feature(tree, "extrude1"))
        (props,) = expected(tree).values()
        assert props["cog_mm"] == pytest.approx(plan.cog, abs=1e-5), name


def test_oblique_extrudes_shear_the_profile_along_the_full_vector():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    (props,) = expected(load("m5_oblique_rect_hole")).values()
    assert props["cog_mm"] == pytest.approx((3, 0, 5), abs=1e-9)  # centroid + direction / 2


def test_preflight_refuses_tapers_that_change_topology_and_unusable_directions():
    def refusals(edit):
        tree = load("m5_taper_rect")
        edit(_feature(tree, "sketch1"), _feature(tree, "extrude1"))
        return "\n".join(analyze(tree))

    def collapse(sketch, extrude):
        extrude["taper"] = 30  # 10 mm high: each side moves 5.8 mm in, the 10 mm side collapses
    assert "collapses an edge" in refusals(collapse)

    def hole_meets_wall(sketch, extrude):
        sketch["entities"].append({"type": "circle", "center": [0, 0], "radius": 4.5, "id": "h"})
        sketch["profiles"][0]["inner"] = [["h"]]
    assert "cross itself" in refusals(hole_meets_wall)

    def d_shape(sketch, extrude):
        sketch["entities"] = [{"type": "line", "start": [-5, 0], "end": [5, 0], "id": "l"},
                              {"type": "arc", "start": [5, 0], "mid": [0, 5], "end": [-5, 0],
                               "id": "a"}]
        sketch["profiles"] = [{"outer": ["l", "a"], "inner": []}]
    assert "joins an arc without tangency" in refusals(d_shape)

    def flat(sketch, extrude):
        extrude.pop("taper")
        extrude["direction"] = [5, 5, 0]
    assert "lies in the sketch plane" in refusals(flat)

    def unit_vector(sketch, extrude):
        extrude.pop("taper")
        extrude["direction"] = [0, 0.6, 0.8]  # a unit vector, with a 10 mm distance
    assert "neither the length of its direction" in refusals(unit_vector)

    def no_distance(sketch, extrude):
        extrude.pop("distance")
        extrude["direction"] = [0, 6, 8]
    assert refusals(no_distance) == ""


# ── Builder against the fake CATIA ──


class _DirectedPart(_FakePart):
    """Pads may follow a line set with SetDirection, their length measured
    along that line (CATIA's choice is unknown) or along the sketch normal.
    Profiles may hold holes: the largest loop is the outer one."""

    def __init__(self, length_along_normal=False, **kwargs):
        super().__init__(**kwargs)
        self.length_along_normal = length_along_normal
        self.HybridShapeFactory.AddNewPlaneEquation = self._equation_plane

    @staticmethod
    def _equation_plane(a, b, c, d):
        """A plane a x + b y + c z = d, with a sketch frame of CATIA's choosing."""
        normal = (a, b, c)
        seed = (1.0, 0.0, 0.0) if abs(a) < 0.9 else (0.0, 1.0, 0.0)
        h = cross(cross(normal, seed), normal)
        h = tuple(x / math.sqrt(dot(h, h)) for x in h)
        return (tuple(d * x for x in normal), h, cross(normal, h))

    def _pad(self, sketch, length):
        pad = super()._pad(sketch, length)
        pad.direction = None
        pad.SetDirection = lambda ref: setattr(pad, "direction", ref[2])
        holder = SimpleNamespace(Value=length)
        pad.FirstLimit = SimpleNamespace(Dimension=holder)
        pad.limit = holder
        return pad

    def _prism(self, pad):
        if hasattr(pad, "limit"):
            pad.length = pad.limit.Value
        origin, h, v = pad.sketch.ref
        normal = cross(h, v)
        entities = [dict(e, id=str(i)) for i, e in enumerate(pad.sketch.factory.entities)]
        circles = [e["id"] for e in entities if e["type"] == "circle"]
        lines = [e["id"] for e in entities if e["type"] != "circle"]
        loops = ([lines] if lines else []) + [[c] for c in circles]
        sketch = {"entities": entities,
                  "profiles": [{"outer": loops[0], "inner": loops[1:]}]}
        area, (cu, cv) = profile_area_centroid(sketch)
        side = -1 if (pad.DirectionOrientation == 1) != self.pads_reversed else 1
        direction = getattr(pad, "direction", None) or normal
        cosine = dot(direction, normal)
        travel = pad.length / abs(cosine) if self.length_along_normal else pad.length
        vector = tuple(side * travel * c for c in direction)
        start = tuple(origin[i] + cu * h[i] + cv * v[i] for i in range(3))
        height = abs(dot(vector, normal))
        if pad.IsSymmetric:
            return area * 2 * height, start
        return area * height, tuple(s + c / 2 for s, c in zip(start, vector, strict=True))


def _expect(name):
    tree = load(name)
    return tree, {tree["parts"][0]["id"]: {"volume_mm3": M5_CLOSED_FORM_MM3[name]}}


def test_a_tapered_extrude_is_a_loft_to_the_offset_profile(monkeypatch):
    part = _DirectedPart()
    tree, want = _expect("m5_taper_rect")
    report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    (loft,) = _lofts(part)
    (s1, _, c1), (s2, sense2, c2) = loft.HybridShape.sections
    assert [len(s.factory.entities) for s in (s1, s2)] == [4, 4] and sense2 == 1
    delta = 10 * math.tan(math.radians(5))
    assert c1[1] == pytest.approx((-10, -5, 0)) and c2[1] == pytest.approx((-10 + delta, -5 + delta, 10))


def test_a_tapered_ring_cuts_its_widened_hole_then_joins_its_target(monkeypatch):
    part = _DirectedPart()
    tree, want = _expect("m5_taper_ring_join")
    report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    assert len(_lofts(part)) == 2
    assert [f.kind for f in part.pads if hasattr(f, "tool")] == ["remove", "add"]


def test_symmetric_tapers_and_tapered_cuts_build(monkeypatch):
    for name in ("m5_taper_slot_symmetric_tilted", "m5_taper_pocket_down", "m5_oblique_taper_yz"):
        part = _DirectedPart(lofts_reversed=True)
        tree, want = _expect(name)
        report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
        assert report["success"], (name, report.get("error"))
    # The symmetric slot: one loft per side, joined.
    part = _DirectedPart()
    tree, _ = _expect("m5_taper_slot_symmetric_tilted")
    _builder(part, monkeypatch).build(tree, check_editability=False)
    assert len(_lofts(part)) == 2 and [f.kind for f in part.pads if hasattr(f, "tool")] == ["add"]


@pytest.mark.parametrize("along_normal", [False, True])
def test_an_oblique_extrude_is_a_pad_along_a_3d_line(monkeypatch, along_normal):
    part = _DirectedPart(length_along_normal=along_normal)
    tree, want = _expect("m5_oblique_rect_hole")
    report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    (pad,) = part.pads
    assert pad.direction == pytest.approx(tuple(c / math.sqrt(136) for c in (6, 0, 10)))
    assert _mass(part.MainBody.solids)[1] == pytest.approx((3, 0, 5))
    notes = report["parts_detail"][0]["features"][1].get("notes", [])
    assert notes == (["Extrusion.1: length measured along the sketch normal"] if along_normal else [])
    assert pad.limit.Value == pytest.approx(10 if along_normal else math.sqrt(136))


def test_an_oblique_pad_is_flipped_once_and_symmetric_cuts_build(monkeypatch):
    part = _DirectedPart(pads_reversed=True)
    tree, want = _expect("m5_oblique_rect_hole")
    report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    assert report["parts_detail"][0]["features"][1]["notes"] == [
        "Extrusion.1: direction flipped to match the tree"]
    part = _DirectedPart()
    tree, want = _expect("m5_oblique_symmetric_cut")
    report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    assert part.pads[1].IsSymmetric and part.pads[1].direction == pytest.approx(
        (0, 1 / math.sqrt(5), -2 / math.sqrt(5)))


# ── Part placement ──


def test_place_part_moves_every_world_coordinate_and_nothing_else():
    rotation, translation = _rotation((1, 2, 2), 70), (10, -4, 3)
    move = lambda p: pytest.approx(_moved(rotation, translation, p))
    turn = lambda v: pytest.approx(_moved(rotation, (0, 0, 0), v))
    part = {"id": "/P", "placement": _placement(rotation, translation), "features": [
        {"type": "sketch", "id": "s", "plane": {"origin": [1, 2, 3], "x_dir": [1, 0, 0],
                                                "normal": [0, 0, 1]},
         "entities": [{"type": "circle", "center": [4, 5], "radius": 2, "id": "c"}],
         "profiles": [{"outer": ["c"], "inner": []}]},
        {"type": "extrude", "id": "e", "sketch": "s", "distance": 5, "direction": [0, 3, 4]},
        {"type": "revolve", "id": "r", "angle": 90, "axis": {"origin": [1, 0, 0],
                                                             "direction": [0, 1, 0]}},
        {"type": "sweep", "id": "w", "path": [{"type": "arc", "start": [0, 0, 0],
                                               "mid": [1, 1, 0], "end": [2, 0, 0]}]},
        {"type": "hole", "id": "h", "positions": [[1, 1, 1]], "direction": [0, 0, -1],
         "frames": [[[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1]]]},
        {"type": "fillet", "id": "f", "radius": 2, "edges": [
            {"type": "edge", "point": [1, 2, 3], "samples": [[0, 0, 0], [1, 0, 0], [2, 0, 0]],
             "length": 8.0}], "source": {"line": 3, "col": 1, "api": [1, 2, 3]}},
        {"type": "copy", "id": "k", "matrix": [[1, 0, 0, 5], [0, 1, 0, 0], [0, 0, 1, 0]]},
    ]}
    before = copy.deepcopy(part)
    placed = place_part(part)
    assert part == before and "placement" not in placed
    sketch, extrude, revolve, sweep, hole, fillet, copied = placed["features"]
    assert sketch["plane"]["origin"] == move((1, 2, 3))
    assert sketch["plane"]["normal"] == turn((0, 0, 1))
    assert sketch["entities"] == before["features"][0]["entities"]
    assert extrude["direction"] == turn((0, 3, 4)) and extrude["distance"] == 5
    assert revolve["axis"]["origin"] == move((1, 0, 0)) and revolve["angle"] == 90
    assert sweep["path"][0]["mid"] == move((1, 1, 0))
    assert hole["positions"][0] == move((1, 1, 1)) and hole["direction"] == turn((0, 0, -1))
    frame = hole["frames"][0]
    assert [frame[i][3] for i in range(3)] == move((1, 1, 1))
    assert [frame[i][2] for i in range(3)] == turn((0, 0, 1))
    assert fillet["edges"][0]["samples"][1] == move((1, 0, 0))
    assert fillet["edges"][0]["length"] == 8.0 and fillet["source"]["api"] == [1, 2, 3]
    # A copy translating by +5 X in the part frame translates by R (5, 0, 0) in the world.
    matrix = copied["matrix"]
    assert [[matrix[i][j] for j in range(3)] for i in range(3)] == [
        pytest.approx([1.0 if i == j else 0.0 for j in range(3)], abs=1e-12) for i in range(3)]
    assert [matrix[i][3] for i in range(3)] == turn((5, 0, 0))
    assert place_part({"id": "/Q", "features": []}) == {"id": "/Q", "features": []}


def test_preflight_refuses_placed_parts_for_now():
    assert list(analyze(load("m5_placed_block"))) == [
        "parts placed by a parent location are not supported yet"]


def test_place_part_accepts_rigid_placements_only():
    tree = load("m5_placed_block")
    part = tree["parts"][0]
    part["placement"][0][0] = 2.0  # a scale
    with pytest.raises(ValueError, match="not a rigid motion"):
        place_part(part)
    part["placement"] = _placement([[-1, 0, 0], [0, 1, 0], [0, 0, 1]], (0, 0, 0))
    with pytest.raises(ValueError, match="mirrors the part"):
        place_part(part)
    part["placement"] = [[1, 0, 0]]
    with pytest.raises(ValueError, match="not a 3x4 matrix"):
        place_part(part)


PLACED_FIXTURES = ("m1_holes", "m1_tilted_plane", "m2_shaft_axis_off_origin",
                   "m2_partial_revolve_cut_h_axis", "m2_mirror_offset_plane",
                   "m2_split_tilted_keep_top", "m2_countersink", "m4_loft_frustum",
                   "m4_sweep_offset_profile", "m5_taper_ring_join", "m5_oblique_rect_hole")


def test_a_placed_part_keeps_its_volume_and_moves_its_centre_of_gravity():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    (props,) = expected(load("m5_placed_block")).values()
    assert props["volume_mm3"] == pytest.approx(M5_CLOSED_FORM_MM3["m5_placed_block"], rel=1e-9)
    x = -5 * 50 * 40 * 20 / M5_CLOSED_FORM_MM3["m5_placed_block"]  # the hole is at x = 0
    assert props["cog_mm"] == pytest.approx(_moved(CYCLE, (100, -20, 5), (x, 0, 10)), abs=1e-6)
    rotation, translation = _rotation((3, -1, 2), 37), (40, -15, 25)
    for name in PLACED_FIXTURES:
        tree = load(name)
        (plain,) = expected(tree).values()
        tree["parts"][0]["placement"] = _placement(rotation, translation)
        (placed,) = expected(tree).values()
        assert placed["volume_mm3"] == pytest.approx(plain["volume_mm3"], rel=1e-7), name
        assert placed["cog_mm"] == pytest.approx(
            _moved(rotation, translation, plain["cog_mm"]), abs=1e-5), name


def test_the_builder_builds_a_placed_part_in_world_coordinates(monkeypatch):
    # The preflight refuses placed parts for now; the builder's path is kept ready.
    monkeypatch.setattr(builder_module, "check", lambda tree: None)
    rotation, translation = _rotation((0, 1, 1), -50), (5, 6, -7)
    tree = load("m2_shaft_axis_off_origin")
    tree["parts"][0]["placement"] = _placement(rotation, translation)
    part = _DirectedPart()
    report = _builder(part, monkeypatch).build(
        tree, {tree["parts"][0]["id"]: {"volume_mm3": CLOSED_FORM_MM3["m2_shaft_axis_off_origin"]}},
        check_editability=False)
    assert report["success"], report.get("error")
    (shaft,) = [f for f in part.pads if getattr(f, "kind", None) == "shaft"]
    _, origin, direction = shaft.RevoluteAxis
    assert origin == pytest.approx(_moved(rotation, translation, (10, 0, 0)))
    assert direction == pytest.approx(_moved(rotation, (0, 0, 0), (0, 0, 1)))
