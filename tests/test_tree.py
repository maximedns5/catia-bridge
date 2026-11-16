"""Offline tests of the feature tree package (catia_mcp/tree).

The builder is exercised against a small fake CATIA whose pads extrude the
profile drawn in the fake sketch's own frame, so the tests check the builder's
projection into CATIA's sketch frame, its direction choice and flip, and its
failure reporting. Real CATIA behaviour is checked live (tree CLI).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree import builder as builder_module
from catia_mcp.tree.builder import TreeBuilder
from catia_mcp.tree.geometry import (
    Arc,
    Frame,
    _loop_area_centroid,
    cross,
    dot,
    profile_area_centroid,
    revolve_expectation,
    unit,
)
from catia_mcp.tree.loft import loft_expectation, section_loop
from catia_mcp.tree.preflight import analyze, refusal, truncate
from catia_mcp.tree.sweep import sweep_expectation

TREES = Path(__file__).parent / "fixtures" / "trees"

# Closed-form volumes of the fixture trees (mm3).
CLOSED_FORM_MM3 = {
    "m1_offset_block": 80 * 50 * 20,
    "m1_reversed_normal_plane": 30 * 20 * 10,
    "m1_slot_arcs_negative": (40 * 10 + math.pi * 5**2) * 8,
    "m1_tilted_plane": 30 * 20 * 12,
    "m1_holes": 60 * 40 * 10 - math.pi * (3**2 * 10 + 2**2 * 4 + 4**2 * 2 + 2**2 * 8),
    "m2_shaft_axis_off_origin": math.pi * (8**2 * 25 + 5**2 * 15),
    "m2_ring_sketch_v_axis": 2 * math.pi * 15 * (10 * 20),
    "m2_partial_revolve_cut_h_axis": 80 * 50 * 20 - (120 / 360) * 2 * math.pi * 10 * (10 * 10),
    "m2_mirror_join": 2 * 20 * 10 * 10,
    "m2_mirror_offset_plane": 2 * 20 * 10 * 10,
    "m2_split_keep_bottom": 70 * 50 * 20,
    "m2_split_tilted_keep_top": 80 * 50 * 20 / 2,
    "m4_loft_frustum": math.pi * 30 / 3 * (10**2 + 10 * 4 + 4**2),
    "m4_loft_rect_prismatoid": 30 / 6 * (20 * 10 + 4 * 15 * 8 + 10 * 6),
    "m4_loft_join_circle_square": 60 * 60 * 10 + 5222.09875885885,  # build123d's loft
    "m4_loft_cut_frustum": 80 * 50 * 20 - math.pi * 15 / 3 * (12**2 + 12 * 6 + 6**2),
    "m4_sweep_pipe": math.pi * 3**2 * (40 + 15 * math.pi / 2),
    "m4_sweep_offset_profile": 3 * 2 * (40 + (15 - 2) * math.pi / 2 + 20),
    "m4_sweep_cut_groove": 60 * 40 * 20 - math.pi * 2**2 / 2 * (20 + 10 * math.pi / 2),
}


def load(name: str) -> dict:
    return json.loads((TREES / f"{name}.json").read_text())


# ── Geometry ──


def test_three_point_arcs_sweep_counter_clockwise_in_either_drawing_order():
    ccw = Arc.through((1, 0), (0, 1), (-1, 0))
    cw = Arc.through((-1, 0), (0, 1), (1, 0))
    for arc in (ccw, cw):
        assert arc.center == pytest.approx((0, 0)) and arc.radius == pytest.approx(1)
        assert arc.start == pytest.approx(0) and arc.end == pytest.approx(math.pi)


def test_profile_area_and_centroid_of_lines_arcs_and_holes():
    slot = load("m1_slot_arcs_negative")["parts"][0]["features"][0]
    area, centroid = profile_area_centroid(slot)
    assert area == pytest.approx(400 + math.pi * 25, rel=1e-5)
    assert centroid == pytest.approx((0, 0), abs=1e-6)
    washer = {"entities": [{"type": "circle", "id": "o", "center": [3, 4], "radius": 2},
                           {"type": "circle", "id": "i", "center": [3, 4], "radius": 1}],
              "profiles": [{"outer": ["o"], "inner": [["i"]]}]}
    area, centroid = profile_area_centroid(washer)
    assert area == pytest.approx(3 * math.pi) and centroid == pytest.approx((3, 4))


def test_tree_frame_maps_v_to_normal_cross_x():
    frame = Frame.from_tree({"origin": [0, 0, 0], "x_dir": [1, 0, 0], "normal": [0, -1, 0]})
    assert frame.y_dir == pytest.approx((0, 0, 1))
    assert frame.world(2, 3, 1) == pytest.approx((2, -1, 3))


# ── Preflight ──


def test_preflight_refuses_with_each_reason_once_and_where():
    tree = load("m1_offset_block")
    part = tree["parts"][0]
    part["features"] += [
        {"type": "draft", "id": "draft1", "source": {"line": 7}},
        {"type": "draft", "id": "draft2", "source": {"line": 9}},
        {"type": "base_feature", "id": "base1", "creates": ["Body2"]},
    ]
    reasons = analyze(tree)
    text = refusal(reasons)
    assert "'draft' features are not supported yet (2x): /M1/OffsetBlock model.py line 7, " \
           "/M1/OffsetBlock model.py line 9" in text
    assert "base_feature" in text and text.endswith("the STEP download is not affected.")


def test_preflight_does_not_cascade_from_an_unsupported_feature():
    tree = {"version": 1, "parts": [{"id": "/P", "features": [
        {"type": "draft", "id": "draft1", "creates": ["Body1"]},
        {"type": "copy", "id": "copy1", "bodies": ["Body1"], "creates": ["Body2"]},
        {"type": "combine", "id": "combine1", "operation": "join",
         "targets": ["Body1"], "tools": ["Body2"]},
    ]}]}
    assert list(analyze(tree)) == ["'draft' features are not supported yet",
                                   "'copy' features are not supported yet"]


def test_fixture_trees_pass_preflight():
    for path in TREES.glob("*.json"):
        tree = json.loads(path.read_text())
        if any(part.get("placement") for part in tree["parts"]):
            continue  # placed parts are refused for now (test_extrude_placement)
        assert not analyze(tree), path.name


def test_truncate_keeps_a_prefix_and_its_live_bodies():
    tree = load("m1_join_cut_intersect")
    cut = truncate(tree, "/M1/JoinCutIntersect", "extrude2")
    assert [f["id"] for f in cut["parts"][0]["features"]] == ["sketch1", "extrude1", "sketch2", "extrude2"]
    assert cut["parts"][0]["bodies"] == ["Body1"]
    with pytest.raises(ValueError):
        truncate(tree, None, "nope")


# ── Reference (build123d) ──


def test_reference_matches_the_closed_form_of_the_fixtures():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    for name, volume in CLOSED_FORM_MM3.items():
        (props,) = expected(load(name)).values()
        assert props["volume_mm3"] == pytest.approx(volume, rel=1e-9), name


# ── Builder against a fake CATIA ──


class _Factory:
    """Sketch drawing: each curve is recorded as a tree entity, and returned
    as an object whose StartPoint / EndPoint a connected contour sets."""

    def __init__(self):
        self.entities, self.points = [], []

    def _curve(self, entity):
        self.entities.append(entity)
        return SimpleNamespace(entity=entity, StartPoint=None, EndPoint=None)

    def CreateLine(self, u0, v0, u1, v1):
        return self._curve({"type": "line", "start": [u0, v0], "end": [u1, v1]})

    def CreateClosedCircle(self, cu, cv, r):
        return self._curve({"type": "circle", "center": [cu, cv], "radius": r})

    def CreateCircle(self, cu, cv, r, a0, a1):
        point = lambda a: [cu + r * math.cos(a), cv + r * math.sin(a)]
        return self._curve({"type": "arc", "start": point(a0), "mid": point((a0 + a1) / 2),
                            "end": point(a1)})

    def CreatePoint(self, u, v):
        self.points.append(SimpleNamespace(uv=(u, v)))
        return self.points[-1]


def _localized(name):
    raise RuntimeError("La méthode Item a échoué")  # French session: no "AbsoluteAxis"


class _Sketch:
    def __init__(self, ref):
        # ref: the (origin, H, V) frame of the support the sketch lies on.
        self.ref, self.factory, self.Name = ref, _Factory(), "Esquisse"
        origin, h, v = ref
        self.GeometricElements = SimpleNamespace(Item=_localized)
        self.AbsoluteAxis = SimpleNamespace(HorizontalReference=("axis", origin, h),
                                            VerticalReference=("axis", origin, v))

    def OpenEdition(self):
        return self.factory

    def CloseEdition(self):
        pass


class _Body:
    def __init__(self, name):
        self.Name, self.solids, self.consumed = name, [], False
        self.Sketches = SimpleNamespace(Add=self._add_sketch)

    def _add_sketch(self, ref):
        sketch = _Sketch(ref)
        self.last_sketch = sketch
        self.sketch_list = getattr(self, "sketch_list", []) + [sketch]
        return sketch


class _FakePart:
    """Pads extrude the drawn profile along the fake sketch's normal (or the
    opposite when `pads_reversed`); booleans assume disjoint or nested solids."""

    def __init__(self, pads_reversed=False, turns_reversed=False, split_reversed=False,
                 lofts_reversed=False, pockets_reversed=False, holes_reversed=False,
                 through_depth=None):
        self.Name, self.pads_reversed, self.pads = "Part1", pads_reversed, []
        self.pockets_reversed = pockets_reversed
        # Holes drill against their sketch's normal (live on R428) unless
        # reversed; "up to last" goes `through_depth` deep.
        self.holes_reversed, self.through_depth = holes_reversed, through_depth
        self.turns_reversed, self.split_reversed = turns_reversed, split_reversed
        self.lofts_reversed, self.deleted = lofts_reversed, []
        self.MainBody = _Body("Corps principal")
        bodies = [self.MainBody]
        self.Bodies = SimpleNamespace(Add=lambda: bodies.append(_Body(f"Body.{len(bodies) + 1}")) or bodies[-1])
        self.OriginElements = SimpleNamespace(**{name: name for name in builder_module.ORIGIN_PLANES})
        sets = []
        self.HybridBodies = SimpleNamespace(
            Count=0, Item=lambda i: sets[i - 1],
            Add=lambda: sets.append(SimpleNamespace(Name="", AppendHybridShape=lambda s: None)) or sets[-1])
        self.HybridShapeFactory = SimpleNamespace(
            AddNewPlaneOffset=self._offset_plane,
            AddNewPointCoord=lambda x, y, z: ("point", (x, y, z)),
            AddNewLinePtPt=lambda p1, p2: ("axis", p1[1], unit(tuple(b - a for a, b in zip(p1[1], p2[1])))))
        self.ShapeFactory = SimpleNamespace(
            AddNewPad=self._pad, AddNewShaft=self._shaft,
            AddNewPocket=lambda sketch, length: self._pad(sketch, length, "Poche", -1),
            AddNewGroove=lambda sketch: self._shaft(sketch, "groove"),
            AddNewHoleFromSketch=self._hole,
            AddNewMirror=lambda ref: self._feature("mirror", ref=ref),
            AddNewSplit=lambda ref, side: self._feature("split", ref=ref, SplitSide=side),
            AddNewLoft=self._loft,
            AddNewRib=lambda profile, center: self._feature("rib", sketch=profile, center=center),
            AddNewAdd=lambda t: self._boolean("add", t),
            AddNewRemove=lambda t: self._boolean("remove", t),
            AddNewIntersect=lambda t: self._boolean("intersect", t))
        self.InWorkObject = None
        self.Parameters = _FakeParameters()
        self.Relations = _FakeRelations()

    def CreateReferenceFromObject(self, obj):
        if isinstance(obj, str):
            return ((0.0, 0.0, 0.0), *builder_module.ORIGIN_PLANES[obj])
        return obj

    @staticmethod
    def _offset_plane(ref, offset, reverse):
        origin, h, v = ref
        normal = cross(h, v)
        return (tuple(origin[i] + offset * normal[i] for i in range(3)), h, v)

    def _pad(self, sketch, length, name="Extrusion", sign=1):
        """A Pad (sign 1) or a Pocket (sign -1): what it adds or removes is
        appended to what its body held before (no clipping)."""
        pad = SimpleNamespace(Name=f"{name}.{len(self.pads) + 1}", sketch=sketch, length=length,
                              IsSymmetric=False, DirectionOrientation=0, body=self.InWorkObject,
                              sign=sign)
        pad.FirstLimit = SimpleNamespace(Dimension=SimpleNamespace(Value=length, owner=pad))
        self.pads.append(pad)
        return pad

    def _shaft(self, sketch, kind="shaft"):
        return self._feature(kind, sketch=sketch, RevoluteAxis=None, FirstAngle=0.0,
                             SecondAngle=0.0)

    @staticmethod
    def _onto(feature, solid, sign=1):
        """The body after `feature`: what it held before the feature's first
        update, plus (or minus) the feature's solid."""
        if not hasattr(feature, "base"):
            feature.base = list(feature.body.solids)
        volume, cog = solid
        feature.body.solids = feature.base + [(sign * volume, cog)]

    def _feature(self, kind, **fields):
        feature = SimpleNamespace(Name=f"{kind}.{len(self.pads) + 1}", kind=kind,
                                  body=self.InWorkObject, **fields)
        self.pads.append(feature)
        return feature

    def _loft(self):
        loft = self._feature("loft")
        sections = []
        loft.HybridShape = SimpleNamespace(
            SectionCoupling=1, sections=sections,
            AddSectionToLoft=lambda ref, sense, closing: sections.append((ref, sense, closing)))
        return loft

    def delete(self, feature):
        self.deleted.append(feature)
        feature.body.solids = getattr(feature, "base", [])

    def _hole(self, sketch, depth):
        value = lambda v=None: SimpleNamespace(Value=v)
        hole = self._feature("hole", sketch=sketch, Type=0, BottomType=1, Diameter=value(10.0),
                             HeadDiameter=value(), HeadDepth=value(), HeadAngle=value(),
                             BottomLimit=SimpleNamespace(LimitMode=0, Dimension=value(depth)),
                             flipped=False)
        hole.Reverse = lambda: setattr(hole, "flipped", not hole.flipped)
        return hole

    def _drilled(self, hole):
        """A flat-bottomed simple or counterbored hole from its sketch's point."""
        origin, h, v = hole.sketch.ref
        (point,) = hole.sketch.factory.points
        normal = cross(h, v)
        at = tuple(origin[i] + point.uv[0] * h[i] + point.uv[1] * v[i] for i in range(3))
        down = -1 if hole.flipped == self.holes_reversed else 1  # along -normal by default
        limit = hole.BottomLimit
        depth = self.through_depth if limit.LimitMode == 2 else limit.Dimension.Value
        r = hole.Diameter.Value / 2
        pieces = [(math.pi * r * r * depth, depth / 2)]
        if hole.Type == 2:  # counterbored: the head replaces the top of the hole
            R, head = hole.HeadDiameter.Value / 2, hole.HeadDepth.Value
            pieces = [(math.pi * R * R * head, head / 2),
                      (math.pi * r * r * (depth - head), head + (depth - head) / 2)]
        volume = sum(v for v, _ in pieces)
        along = sum(v * z for v, z in pieces) / volume
        return volume, tuple(at[i] + down * along * normal[i] for i in range(3))

    def _boolean(self, kind, tool):
        target = self.InWorkObject
        feature = SimpleNamespace(Name=f"{kind}.{len(self.pads)}", kind=kind, tool=tool, target=target)
        self.pads.append(feature)
        return feature

    def UpdateObject(self, obj):
        kind = getattr(obj, "kind", None)
        if kind == "hole":
            self._onto(obj, self._drilled(obj), -1)
        elif kind in ("shaft", "groove"):
            self._onto(obj, self._revolved(obj), -1 if kind == "groove" else 1)
        elif kind == "mirror":
            origin, h, v = obj.ref
            n = cross(h, v)
            volume, cog = _mass(obj.body.solids)
            off = dot(tuple(c - o for c, o in zip(cog, origin)), n)
            obj.body.solids = [(2 * volume, tuple(c - off * n[i] for i, c in enumerate(cog)))]
        elif kind == "loft":
            obj.body.solids = [self._lofted(obj)]
        elif kind == "rib":
            obj.body.solids = [self._ribbed(obj)]
        elif kind == "split":
            origin, h, v = obj.ref
            n = cross(h, v)
            if not hasattr(obj, "original"):
                obj.original = list(obj.body.solids)
            volume, cog = _mass(obj.original)
            off = dot(tuple(c - o for c, o in zip(cog, origin)), n)
            keeps_positive = (obj.SplitSide == 0) != self.split_reversed
            on_plane = tuple(c - off * n[i] for i, c in enumerate(cog))
            obj.body.solids = [(volume / 2, tuple(c + (1 if keeps_positive else -1) * n[i]
                                                  for i, c in enumerate(on_plane)))]
        elif hasattr(obj, "length"):
            self._onto(obj, self._prism(obj), obj.sign)
        elif hasattr(obj, "tool"):
            tool_volume, tool_cog = _mass(obj.tool.solids)
            sign = {"add": 1, "remove": -1}.get(obj.kind)
            if sign is None:
                obj.target.solids = [(tool_volume, tool_cog)]
            else:
                obj.target.solids = obj.target.solids + [(sign * tool_volume, tool_cog)]

    def Update(self):
        pass

    def _prism(self, pad):
        origin, h, v = pad.sketch.ref
        normal = cross(h, v)
        entities = [dict(e, id=str(i)) for i, e in enumerate(pad.sketch.factory.entities)]
        area, (cu, cv) = _loop_area_centroid(entities)
        reversed_ = self.pockets_reversed if pad.sign < 0 else self.pads_reversed
        side = -1 if (pad.DirectionOrientation == 1) != reversed_ else 1
        offset = 0 if pad.IsSymmetric else side * pad.length / 2
        cog = tuple(origin[i] + cu * h[i] + cv * v[i] + offset * normal[i] for i in range(3))
        return area * pad.length * (2 if pad.IsSymmetric else 1), cog


    def _revolved(self, shaft):
        origin, h, v = shaft.sketch.ref
        entities = [dict(e, id=str(i)) for i, e in enumerate(shaft.sketch.factory.entities)]
        sketch = {"plane": {"origin": list(origin), "x_dir": list(h), "normal": list(cross(h, v))},
                  "entities": entities, "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}
        _, axis_origin, axis_direction = shaft.RevoluteAxis
        angle = shaft.FirstAngle + shaft.SecondAngle
        want = revolve_expectation(Frame.from_tree(sketch["plane"]), sketch, axis_origin,
                                   axis_direction, angle)
        turns_positive = (shaft.FirstAngle > 0) != self.turns_reversed
        return want.volume, want.cog if turns_positive else want.cog_reversed


    def _lofted(self, loft):
        """Vertex coupling: each drawn section from the vertex at its closing
        point, the second one run backwards when CATIA's orientation is."""
        shape = loft.HybridShape
        assert shape.SectionCoupling == builder_module.LOFT_VERTEX_COUPLING
        (s1, _, c1), (s2, sense2, c2) = shape.sections
        common = Frame.from_tree(_tree_sketch(s1)["plane"])
        loops = []
        for sketch, closing, sense in ((s1, c1, 1), (s2, c2, sense2)):
            loop = section_loop(_tree_sketch(sketch), common)
            if (sense != 1) != (self.lofts_reversed and sketch is s2):
                loop = [seg.reversed() for seg in reversed(loop)]
            offset = tuple(c - o for c, o in zip(closing[1], common.origin))
            at = (dot(offset, common.x_dir), dot(offset, common.y_dir))
            (start,) = [i for i, seg in enumerate(loop) if math.dist(seg.a, at) < 1e-6]
            loops.append(loop[start:] + loop[:start])
        if len(loops[0]) != len(loops[1]):
            raise RuntimeError("vertex coupling needs as many vertices on each section")
        height = dot(tuple(a - b for a, b in zip(s2.ref[0], s1.ref[0])), common.normal)
        want = loft_expectation(common, height, *loops)
        return want.volume, want.cog

    @staticmethod
    def _ribbed(rib):
        origin, h, v = rib.center.ref
        world = lambda uv: [origin[i] + uv[0] * h[i] + uv[1] * v[i] for i in range(3)]
        path = [{k: (world(e[k]) if k != "type" else e[k]) for k in e}
                for e in rib.center.factory.entities]
        _, volume = sweep_expectation(_tree_sketch(rib.sketch), path)
        return volume, tuple(origin)


class _FakeParameters:
    def __init__(self):
        self.items, self.removed = {}, []

    def CreateDimension(self, name, magnitude, value):
        parameter = SimpleNamespace(Name=name, magnitude=magnitude, text=None, Comment="")
        parameter.ValuateFromString = lambda text: setattr(parameter, "text", text)
        self.items[name] = parameter
        return parameter

    def Item(self, name):
        return self.items[name]

    def Remove(self, name):
        self.removed.append(name)
        self.items.pop(name, None)


class _FakeRelations:
    def __init__(self, refuse=False):
        self.formulas, self.removed, self.refuse = {}, [], refuse

    def CreateFormula(self, name, comment, target, expression):
        if self.refuse:
            raise RuntimeError("La méthode CreateFormula a échoué")
        self.formulas[name] = (target, expression)
        return SimpleNamespace(Name=name)

    def Remove(self, name):
        self.removed.append(name)
        self.formulas.pop(name, None)


def _tree_sketch(sketch):
    """A fake CATIA sketch's drawing as a tree sketch, in its own frame."""
    origin, h, v = sketch.ref
    entities = [dict(e, id=str(i)) for i, e in enumerate(sketch.factory.entities)]
    return {"plane": {"origin": list(origin), "x_dir": list(h), "normal": list(cross(h, v))},
            "entities": entities, "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}


def _mass(solids):
    volume = sum(v for v, _ in solids)
    if not volume:
        return 0.0, (0.0, 0.0, 0.0)
    return volume, tuple(sum(v * c[i] for v, c in solids) / volume for i in range(3))


def _builder(part, monkeypatch, active=None):
    picked = []
    selection = SimpleNamespace(Clear=picked.clear, Add=picked.append,
                                Delete=lambda: [part.delete(f) for f in picked])
    conn = SimpleNamespace(is_3dx=True, app=None, hso=selection,
                           get_active_part=lambda: active() if active else part)
    built = TreeBuilder(conn, log=lambda _: None)
    built.documents = SimpleNamespace(_new_part=lambda name, kind: None)
    # Measures go through the real active-window check, then read the fake.
    monkeypatch.setattr(TreeBuilder, "_volume",
                        lambda self, body: (self._ensure_home(), _mass(body.solids)[0])[1])
    monkeypatch.setattr(TreeBuilder, "_cog",
                        lambda self, body: (self._ensure_home(), _mass(body.solids)[1])[1])
    # A fake sketch's frame is its support's (CATIA reads it with GetAbsoluteAxisData).
    monkeypatch.setattr(TreeBuilder, "_sketch_frame", lambda self, sketch, known: known or sketch.ref)
    monkeypatch.setattr(builder_module, "_force_solid_inwork", lambda part, sketch: None)
    return built


def test_builder_projects_into_catia_sketch_frame_on_a_reversed_normal_plane(monkeypatch):
    """The tree's -XZ plane (normal -Y) is CATIA's zx plane (H=Z, V=X, normal
    +Y): points are projected, and the pad is reversed to go along -Y."""
    part = _FakePart()
    report = _builder(part, monkeypatch).build(
        load("m1_reversed_normal_plane"), {"/M1/ReversedNormal": {"volume_mm3": 6000.0}},
        check_editability=False)
    assert report["success"], report.get("error")
    (pad,) = part.pads
    assert pad.DirectionOrientation == 1
    assert _mass(part.MainBody.solids)[1] == pytest.approx((0, -5, 10))
    assert "notes" not in report["parts_detail"][0]["features"][1]


def test_builder_flips_a_pad_catia_extrudes_the_other_way(monkeypatch):
    part = _FakePart(pads_reversed=True)
    report = _builder(part, monkeypatch).build(load("m1_offset_block"), check_editability=False)
    assert report["success"], report.get("error")
    feature = report["parts_detail"][0]["features"][1]
    assert feature["notes"] == ["Extrusion.1: direction flipped to match the tree"]
    assert _mass(part.MainBody.solids)[1] == pytest.approx((0, 0, 0))


def test_builder_cuts_holes_and_combines_tool_bodies(monkeypatch):
    part = _FakePart()
    tree = load("m1_holes")
    tree["parts"][0]["features"] = tree["parts"][0]["features"][:3]  # plate + through hole
    tree["parts"][0]["features"][2]["effective_depth"] = 10.0  # the fake has no clipping
    report = _builder(part, monkeypatch).build(tree, check_editability=False)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    assert detail["volume_mm3"] == pytest.approx(24000 - math.pi * 9 * 10, rel=1e-4)
    assert detail["features"][2]["catia"] == ["Extrusion.2", "remove.2"]


def test_builder_stops_with_part_and_feature_when_the_window_changes(monkeypatch):
    part, other = _FakePart(), object()
    calls = {"n": 0}

    def active():
        calls["n"] += 1
        return part if calls["n"] < 2 else other

    report = _builder(part, monkeypatch, active=active).build(load("m1_offset_block"),
                                                             check_editability=False)
    assert not report["success"]
    assert report["error"]["part"] == "/M1/OffsetBlock"
    assert report["error"]["feature"] == "extrude1"
    assert "active CATIA window changed" in report["error"]["message"]
    assert "partial model" in report["error"]["note"]


def test_builder_refuses_before_touching_catia():
    from catia_mcp.tree.preflight import TreeRefused

    touched = []
    conn = SimpleNamespace(is_3dx=True, get_active_part=lambda: touched.append(1))
    tree = load("m1_offset_block")
    tree["parts"][0]["features"].append({"type": "draft", "id": "draft1"})
    with pytest.raises(TreeRefused):
        TreeBuilder(conn).build(tree)
    assert touched == []


def test_part_volume_mismatch_is_reported(monkeypatch):
    part = _FakePart()
    report = _builder(part, monkeypatch).build(
        load("m1_offset_block"), {"/M1/OffsetBlock": {"volume_mm3": 79000.0}}, check_editability=False)
    assert not report["success"]
    assert "expected 79000.0000 mm3" in report["error"]["message"]


def test_cli_check_reports_acceptance_and_refusal(tmp_path, capsys):
    from catia_mcp.tree.__main__ import main

    assert main(["check", str(TREES / "m1_holes.json")]) == 0
    assert "Accepted: 1 part(s), 5 feature(s)." in capsys.readouterr().out
    refused = load("m1_holes")
    refused["parts"][0]["features"].append({"type": "draft", "id": "draft1"})
    path = tmp_path / "refused.json"
    path.write_text(json.dumps(refused))
    assert main(["check", str(path)]) == 1
    assert "'draft' features are not supported yet" in capsys.readouterr().out


def test_build_and_check_do_not_need_build123d():
    """build123d is optional: only `reference` and `--expect auto` use it."""
    code = ("import sys; sys.modules['build123d'] = None; "
            "import catia_mcp.tree.builder, catia_mcp.tree.__main__")
    subprocess.run([sys.executable, "-c", code], check=True)


# ── Milestone 2: revolve, mirror, split ──


def _shafts(part):
    return [f for f in part.pads if getattr(f, "kind", None) == "shaft"]


def test_revolve_about_an_axis_off_the_sketch_origin_uses_a_3d_line(monkeypatch):
    """A -XZ sketch revolved about a vertical axis that is not the CATIA
    sketch's own axis."""
    part = _FakePart()
    report = _builder(part, monkeypatch).build(
        load("m2_shaft_axis_off_origin"),
        {"/M2/ShaftOffAxis": {"volume_mm3": CLOSED_FORM_MM3["m2_shaft_axis_off_origin"]}},
        check_editability=False)
    assert report["success"], report.get("error")
    (shaft,) = _shafts(part)
    assert shaft.RevoluteAxis == ("axis", (10.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert shaft.FirstAngle == 360


def test_revolve_about_the_sketch_v_axis_uses_its_absolute_axis(monkeypatch):
    part = _FakePart()
    report = _builder(part, monkeypatch).build(load("m2_ring_sketch_v_axis"), check_editability=False)
    assert report["success"], report.get("error")
    (shaft,) = _shafts(part)
    assert shaft.RevoluteAxis == ("axis", (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))  # VerticalReference


def test_partial_revolve_swaps_its_angle_when_catia_turns_the_other_way(monkeypatch):
    part = _FakePart(turns_reversed=True)
    report = _builder(part, monkeypatch).build(
        load("m2_partial_revolve_cut_h_axis"),
        {"/M2/PartialRevolveCut": {"volume_mm3": CLOSED_FORM_MM3["m2_partial_revolve_cut_h_axis"]}},
        check_editability=False)
    assert report["success"], report.get("error")
    (shaft,) = _shafts(part)
    assert shaft.RevoluteAxis[2] == pytest.approx((0, 0, 1))  # the zx sketch's H axis
    assert (shaft.FirstAngle, shaft.SecondAngle) == (0.0, 120.0)
    assert any("angle swapped" in note for f in report["parts_detail"][0]["features"]
               for note in f.get("notes", []))


def test_a_mirror_joined_back_is_one_catia_mirror(monkeypatch):
    part = _FakePart()
    report = _builder(part, monkeypatch).build(
        load("m2_mirror_join"), {"/M2/MirrorJoin": {"volume_mm3": 4000.0}}, check_editability=False)
    assert report["success"], report.get("error")
    assert [f.kind for f in part.pads if hasattr(f, "kind")] == ["mirror"]
    assert report["parts_detail"][0]["features"][3]["catia"] == ["mirror.2"]


def test_a_split_flips_once_when_catia_keeps_the_other_side(monkeypatch):
    part = _FakePart(split_reversed=True)
    report = _builder(part, monkeypatch).build(load("m2_split_keep_bottom"), check_editability=False)
    assert report["success"], report.get("error")
    (split,) = [f for f in part.pads if getattr(f, "kind", None) == "split"]
    assert split.SplitSide == 0  # asked for bottom (1), flipped after the check
    assert report["parts_detail"][0]["features"][2]["notes"] == ["split.2: side flipped to match the tree"]


# ── Milestone 4: loft, sweep ──


def _lofts(part):
    return [f for f in part.pads if getattr(f, "kind", None) == "loft"]


def test_a_loft_redraws_its_sections_with_paired_vertices_and_closing_points(monkeypatch):
    """Circle to square: both sections are drawn with five vertices, coupled
    from the circle's x_dir point and the square's mid-side."""
    part = _FakePart()
    report = _builder(part, monkeypatch).build(
        load("m4_loft_join_circle_square"),
        {"/M4/CircleSquare": {"volume_mm3": CLOSED_FORM_MM3["m4_loft_join_circle_square"]}},
        check_editability=False)
    assert report["success"], report.get("error")
    (loft,) = _lofts(part)
    (s1, _, c1), (s2, sense2, c2) = loft.HybridShape.sections
    assert [len(s.factory.entities) for s in (s1, s2)] == [5, 5]
    assert c1[1] == pytest.approx((10, 0, 10)) and c2[1] == pytest.approx((4, 0, 40))
    assert sense2 == 1 and not part.deleted


def test_a_twisted_loft_is_rebuilt_once_with_the_second_section_reversed(monkeypatch):
    part = _FakePart(lofts_reversed=True)
    report = _builder(part, monkeypatch).build(load("m4_loft_rect_prismatoid"),
                                               check_editability=False)
    assert report["success"], report.get("error")
    first, second = _lofts(part)
    assert part.deleted == [first] and second.HybridShape.sections[1][1] == -1
    assert report["parts_detail"][0]["features"][2]["notes"] == [
        f"{second.Name}: closing points given as point",
        f"{second.Name}: second section reversed to match the tree"]
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(3700.0)


def test_sweeps_are_ribs_along_the_path_drawn_in_its_plane(monkeypatch):
    # The fake's booleans assume nested solids, so the half-buried groove is
    # checked for its Rib and its cut, and its volume by the reference.
    for name, expected in (("m4_sweep_pipe", True), ("m4_sweep_cut_groove", False)):
        part = _FakePart()
        tree = load(name)
        want = {tree["parts"][0]["id"]: {"volume_mm3": CLOSED_FORM_MM3[name]}} if expected else None
        report = _builder(part, monkeypatch).build(tree, want, check_editability=False)
        assert report["success"], report.get("error")
        (rib,) = [f for f in part.pads if getattr(f, "kind", None) == "rib"]
        assert [e["type"] for e in rib.center.factory.entities] == ["line", "arc"]
    assert [f.kind for f in part.pads if hasattr(f, "tool")] == ["remove"]


def test_preflight_refuses_lofts_and_sweeps_it_cannot_pair_or_follow():
    tree = load("m4_loft_frustum")
    features = tree["parts"][0]["features"]
    features[1]["entities"] = [  # a hexagon turned 30 deg from a hexagon: two twists fit
        {"type": "line", "start": [8 * math.cos(a), 8 * math.sin(a)],
         "end": [8 * math.cos(a + math.pi / 3), 8 * math.sin(a + math.pi / 3)], "id": f"h{i}"}
        for i, a in enumerate(math.pi / 6 + k * math.pi / 3 for k in range(6))]
    features[1]["profiles"] = [{"outer": [f"h{i}" for i in range(6)], "inner": []}]
    features[0]["entities"] = [
        {"type": "line", "start": [8 * math.cos(a), 8 * math.sin(a)],
         "end": [8 * math.cos(a + math.pi / 3), 8 * math.sin(a + math.pi / 3)], "id": f"g{i}"}
        for i, a in enumerate(k * math.pi / 3 for k in range(6))]
    features[0]["profiles"] = [{"outer": [f"g{i}" for i in range(6)], "inner": []}]
    assert "the sections pair equally well in two ways" in next(iter(analyze(tree)))
    pipe = load("m4_sweep_pipe")
    pipe["parts"][0]["features"][1]["path"][1]["end"] = [15, 5, 55]
    assert "not planar" in next(iter(analyze(pipe)))


def test_a_loft_closes_on_3d_points_at_the_paired_vertices(monkeypatch):
    """Live on R428 the only closing points that build a rectangle loft."""
    part = _FakePart()
    report = _builder(part, monkeypatch).build(load("m4_loft_rect_prismatoid"),
                                               check_editability=False)
    assert report["success"], report.get("error")
    (loft,) = _lofts(part)
    assert [closing[0] for _, _, closing in loft.HybridShape.sections] == ["point", "point"]


def test_no_closing_point_is_passed_as_vbscript_nothing():
    calls = []
    app = SimpleNamespace(SystemService=SimpleNamespace(
        Evaluate=lambda code, language, function, params: calls.append((code, function, params))))
    builder_module._add_loft_section(app, "shape", "section", 1, None)
    ((code, function, params),) = calls
    assert "o.AddSectionToLoft r, s, Nothing" in code
    assert function == "catia_mcp_loft_section" and params == ["shape", "section", 1]


def test_loft_sections_are_drawn_as_connected_contours(monkeypatch):
    """Live on R428 a rectangle of four lines that only touch made every loft
    fail to update: each corner is one sketch point both lines end on."""
    part = _FakePart()
    report = _builder(part, monkeypatch).build(load("m4_loft_rect_prismatoid"),
                                               check_editability=False)
    assert report["success"], report.get("error")
    (loft,) = _lofts(part)
    for sketch, _, _ in loft.HybridShape.sections:
        assert len(sketch.factory.points) == 4  # one shared point per corner
    # A pad's sketch is left as drawn.
    part = _FakePart()
    _builder(part, monkeypatch).build(load("m1_offset_block"), check_editability=False)
    assert all(not pad.sketch.factory.points for pad in part.pads if hasattr(pad, "sketch"))


# ── Human style: features straight into their body, checked by snapshots ──

# build123d's bodies after each feature (catia_mcp.tree.reference.expected),
# as the CLI computes them; written out so the tests run without build123d.
SNAPSHOTS = {
    "m3_pocket_floor_fillet": {
        "extrude1": {"Body1": {"volume_mm3": 24000.0, "cog_mm": [0.0, 0.0, 5.0]}},
        "extrude2": {"Body1": {"volume_mm3": 23000.0, "cog_mm": [-0.2174, 0.0, 4.8913]}},
    },
    "m1_holes": {
        "extrude1": {"Body1": {"volume_mm3": 24000.0, "cog_mm": [0.0, 0.0, 5.0]}},
        "hole1": {"Body1": {"volume_mm3": 23717.256661, "cog_mm": [-0.178821, 0.0, 5.0]}},
        "hole2": {"Body1": {"volume_mm3": 23666.991179, "cog_mm": [-0.147343, 0.0, 4.993628]}},
        "hole3": {"Body1": {"volume_mm3": 23465.929249,
                            "cog_mm": [-0.148606, -0.085682, 4.980721]}},
    },
    "m2_partial_revolve_cut_h_axis": {
        "extrude1": {"Body1": {"volume_mm3": 80000.0, "cog_mm": [0.0, 0.0, 0.0]}},
        "revolve1": {"Body1": {"volume_mm3": 77905.6049, "cog_mm": [-0.1204, -0.2086, 0.0]}},
    },
}


def _human_tree(name, until=None):
    tree = load(name)
    if until:
        tree = truncate(tree, tree["parts"][0]["id"], until)
    part_id = tree["parts"][0]["id"]
    snapshots = json.loads(json.dumps(SNAPSHOTS[name]))  # a copy the test may change
    final = snapshots[until or list(snapshots)[-1]]["Body1"]
    return tree, {part_id: {"volume_mm3": final["volume_mm3"], "features": snapshots}}


def test_a_cut_is_a_pocket_in_the_body_checked_against_build123d(monkeypatch):
    part = _FakePart()
    tree, expected = _human_tree("m3_pocket_floor_fillet", "extrude2")
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    _, pocket = part.pads
    assert pocket.Name.startswith("Poche") and pocket.body is part.MainBody
    assert not [f for f in part.pads if hasattr(f, "tool")]  # no tool body, no boolean
    detail = report["parts_detail"][0]
    assert detail["style"] == "human"
    assert detail["features"][3]["catia"] == [pocket.Name]


def test_a_pocket_cutting_the_wrong_way_is_flipped_once(monkeypatch):
    part = _FakePart(pockets_reversed=True)
    tree, expected = _human_tree("m3_pocket_floor_fillet", "extrude2")
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    (_, pocket) = part.pads
    assert report["parts_detail"][0]["features"][3]["notes"] == [
        f"{pocket.Name}: flipped to match the tree"]


def test_a_revolved_cut_is_a_groove_turned_the_tree_s_way(monkeypatch):
    for reversed_ in (False, True):
        part = _FakePart(turns_reversed=reversed_)
        tree, expected = _human_tree("m2_partial_revolve_cut_h_axis")
        report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
        assert report["success"], report.get("error")
        (groove,) = [f for f in part.pads if getattr(f, "kind", None) == "groove"]
        assert groove.body is part.MainBody
        notes = report["parts_detail"][0]["features"][3].get("notes", [])
        assert notes == ([f"{groove.Name}: flipped to match the tree"] if reversed_ else [])


def test_a_human_feature_that_matches_neither_way_stops_the_build(monkeypatch):
    part = _FakePart()
    tree, expected = _human_tree("m3_pocket_floor_fillet", "extrude2")
    (want,) = expected.values()
    want["features"]["extrude2"]["Body1"]["volume_mm3"] = 22900.0
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert not report["success"]
    assert report["error"]["feature"] == "extrude2"
    assert "build123d by -1100.0000 mm3" in report["error"]["message"]


def test_without_snapshots_or_in_exact_style_cuts_stay_tool_bodies(monkeypatch):
    tree, expected = _human_tree("m3_pocket_floor_fillet", "extrude2")
    for options, want in (({}, None), ({"style": "exact"}, expected)):
        part = _FakePart()
        built = _builder(part, monkeypatch)
        built.style = options.get("style", built.style)
        report = built.build(tree, want, check_editability=False)
        assert report["success"], report.get("error")
        assert report["parts_detail"][0]["style"] == "exact"
        assert [f.kind for f in part.pads if hasattr(f, "tool")] == ["remove"]


def test_reference_snapshots_every_body_after_each_feature():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    for name, snapshots in SNAPSHOTS.items():
        (props,) = expected(load(name)).values()
        for feature, bodies in snapshots.items():
            got = props["features"][feature]["Body1"]
            assert got["volume_mm3"] == pytest.approx(bodies["Body1"]["volume_mm3"], abs=1e-3)
            assert got["cog_mm"] == pytest.approx(bodies["Body1"]["cog_mm"], abs=1e-3)


def test_holes_are_hole_features_of_each_kind(monkeypatch):
    """A through hole goes up to last, a blind one keeps its depth, a
    counterbore has its head; each from a sketch holding its position."""
    part = _FakePart(through_depth=10.0)
    tree, expected = _human_tree("m1_holes")
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    holes = [f for f in part.pads if getattr(f, "kind", None) == "hole"]
    assert [(h.Type, h.BottomLimit.LimitMode, h.Diameter.Value) for h in holes] == [
        (0, 2, 6), (0, 0, 4), (2, 2, 4)]
    assert holes[1].BottomLimit.Dimension.Value == 4
    assert (holes[2].HeadDiameter.Value, holes[2].HeadDepth.Value) == (8, 2)
    assert all(h.body is part.MainBody and h.BottomType == 0 for h in holes)
    features = report["parts_detail"][0]["features"]
    assert [f["catia"] for f in features if f["type"] == "hole"] == [[h.Name] for h in holes]


def test_a_hole_drilled_into_air_is_reversed_once(monkeypatch):
    part = _FakePart(through_depth=10.0, holes_reversed=True)
    tree, expected = _human_tree("m1_holes", "hole1")
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    (hole,) = [f for f in part.pads if getattr(f, "kind", None) == "hole"]
    assert hole.flipped and report["parts_detail"][0]["features"][2]["notes"] == [
        f"{hole.Name}: flipped to match the tree"]


def test_a_hole_no_hole_feature_can_make_is_cut_as_cylinders(monkeypatch):
    """Up to last stops short of build123d's through hole (material on both
    sides of its frame): the Hole is deleted and the cylinders cut instead."""
    part = _FakePart(through_depth=4.0)
    tree, expected = _human_tree("m1_holes", "hole1")
    tree["parts"][0]["features"][2]["effective_depth"] = 10.0  # the fake has no clipping
    feature_id = "hole1"
    expected[tree["parts"][0]["id"]]["features"][feature_id]["Body1"] = {
        "volume_mm3": 24000 - math.pi * 9 * 10, "cog_mm": [-15 * math.pi * 90 / (24000 - 90 * math.pi),
                                                         0.0, 5.0]}
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    (hole,) = [f for f in part.pads if getattr(f, "kind", None) == "hole"]
    assert part.deleted == [hole]
    entry = report["parts_detail"][0]["features"][2]
    assert entry["catia"] == ["Extrusion.3", "remove.3"]
    assert "built as cut cylinders instead" in entry["notes"][0]


# ── Script parameters ──


def _script_tree():
    """A centred 60 x 40 x 8 plate (build123d Box: sketch offset by -4), with
    one through hole of radius 3, as the app's tracer gives them."""
    def param(arg, value, line, **more):
        return {"arg": arg, "kind": "literal", "value": value, "factor": 1.0, "slope": 1.0,
                "intercept": 0.0, "file": "model.py", "line": line, "col": 4, **more}

    rectangle = [(-30, -20), (30, -20), (30, 20), (-30, 20)]
    return {"version": 1, "parts": [{"id": "/Demo/Plate", "name": "Plate", "bodies": ["Body1"],
        "features": [
            {"type": "sketch", "id": "sketch1", "plane": {"origin": [0, 0, -4], "x_dir": [1, 0, 0],
                                                          "normal": [0, 0, 1]},
             "entities": [{"type": "line", "start": list(rectangle[i]),
                           "end": list(rectangle[(i + 1) % 4]), "id": f"e{i}"} for i in range(4)],
             "profiles": [{"outer": ["e0", "e1", "e2", "e3"], "inner": []}]},
            {"type": "extrude", "id": "extrude1", "name": "Extrude1", "sketch": "sketch1",
             "distance": 8, "operation": "new_body", "creates": ["Body1"],
             "params": {"distance": param("height", 8, 3, base_shift=0.5)}},
            {"type": "hole", "id": "hole1", "name": "Hole1", "kind": "simple", "diameter": 6,
             "depth": None, "effective_depth": 16.0, "positions": [[15, 0, 4]],
             "direction": [0, 0, -1], "frames": [[[1, 0, 0, 15], [0, 1, 0, 0], [0, 0, 1, 4]]],
             "bodies": ["Body1"], "params": {"diameter": param("radius", 3, 6, factor=2.0)}},
        ]}]}


def _script_expected():
    plate = 60 * 40 * 8
    hole = math.pi * 9 * 8
    return {"/Demo/Plate": {"volume_mm3": plate - hole, "features": {
        "extrude1": {"Body1": {"volume_mm3": plate, "cog_mm": [0.0, 0.0, 0.0]}},
        "hole1": {"Body1": {"volume_mm3": plate - hole,
                            "cog_mm": [-15 * hole / (plate - hole), 0.0, 0.0]}}}}}


def test_a_centred_box_is_a_symmetric_pad_on_its_middle_plane(monkeypatch):
    part = _FakePart(through_depth=8.0)
    report = _builder(part, monkeypatch).build(_script_tree(), _script_expected(),
                                               check_editability=False)
    assert report["success"], report.get("error")
    pad = part.pads[0]
    assert pad.IsSymmetric and pad.length == 4
    assert pad.sketch.ref[0] == (0.0, 0.0, 0.0)  # the XY plane itself, no offset plane
    assert _mass(part.MainBody.solids)[0] == pytest.approx(60 * 40 * 8 - math.pi * 9 * 8)


def test_script_numbers_become_user_parameters_driving_the_features(monkeypatch):
    part = _FakePart(through_depth=8.0)
    report = _builder(part, monkeypatch).build(_script_tree(), _script_expected(),
                                               check_editability=False)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    assert detail["parameters"] == [
        {"name": "Extrude1_height", "value": 8.0, "unit": "mm", "source": "model.py line 3",
         "drives": ["Extrude1_height/2"]},
        {"name": "Hole1_radius", "value": 3.0, "unit": "mm", "source": "model.py line 6",
         "drives": ["Hole1_radius*2"]}]
    assert part.Parameters.items["Hole1_radius"].text == "3mm"
    pad = part.pads[0]
    (hole,) = [f for f in part.pads if getattr(f, "kind", None) == "hole"]
    assert part.Relations.formulas == {
        "Extrude1_height_f1": (pad.FirstLimit.Dimension, "Extrude1_height/2"),
        "Hole1_radius_f1": (hole.Diameter, "Hole1_radius*2")}


def test_parameters_catia_refuses_are_removed_and_the_part_kept(monkeypatch):
    part = _FakePart(through_depth=8.0)
    part.Relations = _FakeRelations(refuse=True)
    report = _builder(part, monkeypatch).build(_script_tree(), _script_expected(),
                                               check_editability=False)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    assert detail["parameters"] == [] and "CreateFormula" in detail["parameters_note"]
    assert part.Parameters.removed == ["Extrude1_height"] and not part.Parameters.items


def test_parameter_expressions_read_as_a_designer_would_write_them():
    from catia_mcp.tree.parameters import ParameterPlan, ScriptParameter, spec_for

    p = ScriptParameter("H", 8, "mm", 1.0, 1.0, 0.0, "model.py line 3")
    assert [p.expression(), p.expression(0.5), p.expression(3)] == ["H", "H/2", "H*3"]
    q = ScriptParameter("R", 3, "mm", 2.0, 1.0, -0.5, "x")
    assert q.expression() == "(R-0.5mm)*2"
    chamfer = {"id": "chamfer1", "name": "Chamfer 1", "params": {
        "distance": {"arg": "length", "kind": "literal", "value": 0.5, "line": 7,
                     "also": ["distance2"]},
        "distance2": {"arg": "length2", "computed": "length2 is not written in the call"}}}
    assert spec_for(chamfer, "distance2")["value"] == 0.5
    plan = ParameterPlan()
    plan.drive(chamfer, "distance", "L1")
    plan.drive(chamfer, "distance2", "L2")
    (one,) = plan.parameters.values()  # one script number, one parameter
    assert one.name == "Chamfer_1_length" and [d.target for d in one.driven] == ["L1", "L2"]
    assert spec_for({"params": {"angle": {"arg": "revolution_arc", "computed": "x"}}},
                    "angle") is None


def _with_pocket(tree, expected):
    """The plate with a 10 x 6 pocket cut 3 deep from its top face, the
    script's amount being -3 (build123d's extrude(amount=-3, SUBTRACT))."""
    part = tree["parts"][0]
    rectangle = [(-15, -3), (-5, -3), (-5, 3), (-15, 3)]
    part["features"][2:2] = [
        {"type": "sketch", "id": "sketch2", "plane": {"origin": [0, 0, 4], "x_dir": [1, 0, 0],
                                                      "normal": [0, 0, 1]},
         "entities": [{"type": "line", "start": list(rectangle[i]),
                       "end": list(rectangle[(i + 1) % 4]), "id": f"r{i}"} for i in range(4)],
         "profiles": [{"outer": ["r0", "r1", "r2", "r3"], "inner": []}]},
        {"type": "extrude", "id": "extrude2", "name": "Extrude2", "sketch": "sketch2",
         "distance": -3, "operation": "cut", "bodies": ["Body1"],
         "params": {"distance": {"arg": "amount", "kind": "literal", "value": -3, "factor": 1.0,
                                 "slope": 1.0, "intercept": 0.0, "file": "model.py",
                                 "line": 8, "col": 4}}}]
    (want,) = expected.values()
    plate, pocket, hole = 60 * 40 * 8, 180.0, math.pi * 9 * 8
    after_pocket = plate - pocket
    final = after_pocket - hole
    want["features"]["extrude2"] = {"Body1": {
        "volume_mm3": after_pocket, "cog_mm": [10 * pocket / after_pocket, 0.0,
                                               -2.5 * pocket / after_pocket]}}
    want["features"]["hole1"]["Body1"] = {"volume_mm3": final, "cog_mm": [
        (10 * pocket - 15 * hole) / final, 0.0, -2.5 * pocket / final]}
    want["volume_mm3"] = final
    return tree, expected


def test_a_negative_script_amount_drives_a_positive_pocket_depth(monkeypatch):
    part = _FakePart(through_depth=8.0)
    tree, expected = _with_pocket(_script_tree(), _script_expected())
    report = _builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    (pocket,) = [f for f in part.pads if f.Name.startswith("Poche")]
    assert pocket.length == 3
    assert part.Relations.formulas["Extrude2_amount_f1"] == (pocket.FirstLimit.Dimension,
                                                            "-Extrude2_amount")


def test_undone_parameters_put_every_driven_value_back(monkeypatch):
    part = _FakePart(through_depth=8.0)
    relations = _FakeRelations()
    real = relations.CreateFormula

    def create(name, comment, target, expression):
        target.Value = -99.0  # what a wrong formula would leave behind
        if name.startswith("Hole1"):
            raise RuntimeError("La méthode CreateFormula a échoué")
        return real(name, comment, target, expression)

    relations.CreateFormula = create
    part.Relations = relations
    report = _builder(part, monkeypatch).build(_script_tree(), _script_expected(),
                                               check_editability=False)
    assert report["success"], report.get("error")
    assert part.pads[0].FirstLimit.Dimension.Value == 4  # back to the pad's length
    assert relations.removed == ["Extrude1_height_f1"]


def test_a_signed_scale_reads_as_a_minus_sign():
    from catia_mcp.tree.parameters import ScriptParameter

    p = ScriptParameter("A", -6, "mm", 1.0, 1.0, 0.0, "x")
    assert [p.expression(-1), p.expression(-0.5), p.expression(-3)] == ["-A", "-A/2", "-A*3"]


def test_the_report_is_editable_unless_a_part_failed_its_edit(monkeypatch):
    """Live sample2: parts editable True, True and two with no pad (None)."""
    for flags, want in (([True, None, None, True], True), ([True, False], False),
                        ([None], None)):
        records = iter(flags)
        monkeypatch.setattr(TreeBuilder, "_check_editability",
                            lambda self, record: setattr(record, "editable", next(records)))
        tree = load("m1_offset_block")
        tree["parts"] = [dict(tree["parts"][0], id=f"/P{i}") for i in range(len(flags))]
        part = _FakePart()
        built = _builder(part, monkeypatch)
        # Each tree part in a fresh main body, as each gets a new CATIA part.
        built.documents = SimpleNamespace(
            _new_part=lambda name, kind: setattr(part, "MainBody", _Body(name)))
        report = built.build(tree)
        assert report["success"], report.get("error")
        assert report["editable"] is want
