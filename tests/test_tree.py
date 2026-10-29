"""Offline tests of the feature tree package (catia_mcp/tree).

The builder is exercised against a small fake CATIA whose pads extrude the
profile drawn in the fake sketch's own frame, so the tests check the builder's
projection into CATIA's sketch frame, its direction choice and flip, and its
failure reporting. Real CATIA behaviour is checked live (tree CLI).
"""

from __future__ import annotations

import json
import math
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
from catia_mcp.tree.preflight import analyze, refusal, truncate

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
        {"type": "fillet", "id": "fillet1", "source": {"line": 7}},
        {"type": "fillet", "id": "fillet2", "source": {"line": 9}},
        {"type": "base_feature", "id": "base1", "creates": ["Body2"]},
    ]
    reasons = analyze(tree)
    text = refusal(reasons)
    assert "'fillet' features are not supported yet (2x): /M1/OffsetBlock model.py line 7, " \
           "/M1/OffsetBlock model.py line 9" in text
    assert "base_feature" in text and text.endswith("the STEP download is not affected.")


def test_preflight_does_not_cascade_from_an_unsupported_feature():
    tree = {"version": 1, "parts": [{"id": "/P", "features": [
        {"type": "loft", "id": "loft1", "creates": ["Body1"]},
        {"type": "copy", "id": "copy1", "bodies": ["Body1"], "creates": ["Body2"]},
        {"type": "combine", "id": "combine1", "operation": "join",
         "targets": ["Body1"], "tools": ["Body2"]},
    ]}]}
    assert list(analyze(tree)) == ["'loft' features are not supported yet",
                                   "'copy' features are not supported yet"]


def test_fixture_trees_pass_preflight():
    for path in TREES.glob("*.json"):
        assert not analyze(json.loads(path.read_text())), path.name


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
    def __init__(self):
        self.entities = []

    def CreateLine(self, u0, v0, u1, v1):
        self.entities.append({"type": "line", "start": [u0, v0], "end": [u1, v1]})

    def CreateClosedCircle(self, cu, cv, r):
        self.entities.append({"type": "circle", "center": [cu, cv], "radius": r})

    def CreateCircle(self, cu, cv, r, a0, a1):
        point = lambda a: [cu + r * math.cos(a), cv + r * math.sin(a)]
        self.entities.append({"type": "arc", "start": point(a0), "mid": point((a0 + a1) / 2),
                              "end": point(a1)})


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
        return sketch


class _FakePart:
    """Pads extrude the drawn profile along the fake sketch's normal (or the
    opposite when `pads_reversed`); booleans assume disjoint or nested solids."""

    def __init__(self, pads_reversed=False, turns_reversed=False, split_reversed=False):
        self.Name, self.pads_reversed, self.pads = "Part1", pads_reversed, []
        self.turns_reversed, self.split_reversed = turns_reversed, split_reversed
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
            AddNewMirror=lambda ref: self._feature("mirror", ref=ref),
            AddNewSplit=lambda ref, side: self._feature("split", ref=ref, SplitSide=side),
            AddNewAdd=lambda t: self._boolean("add", t),
            AddNewRemove=lambda t: self._boolean("remove", t),
            AddNewIntersect=lambda t: self._boolean("intersect", t))
        self.InWorkObject = None

    def CreateReferenceFromObject(self, obj):
        if isinstance(obj, str):
            return ((0.0, 0.0, 0.0), *builder_module.ORIGIN_PLANES[obj])
        return obj

    @staticmethod
    def _offset_plane(ref, offset, reverse):
        origin, h, v = ref
        normal = cross(h, v)
        return (tuple(origin[i] + offset * normal[i] for i in range(3)), h, v)

    def _pad(self, sketch, length):
        pad = SimpleNamespace(Name=f"Extrusion.{len(self.pads) + 1}", sketch=sketch, length=length,
                              IsSymmetric=False, DirectionOrientation=0, body=self.InWorkObject)
        self.pads.append(pad)
        return pad

    def _shaft(self, sketch):
        return self._feature("shaft", sketch=sketch, RevoluteAxis=None, FirstAngle=0.0, SecondAngle=0.0)

    def _feature(self, kind, **fields):
        feature = SimpleNamespace(Name=f"{kind}.{len(self.pads) + 1}", kind=kind,
                                  body=self.InWorkObject, **fields)
        self.pads.append(feature)
        return feature

    def _boolean(self, kind, tool):
        target = self.InWorkObject
        feature = SimpleNamespace(Name=f"{kind}.{len(self.pads)}", kind=kind, tool=tool, target=target)
        self.pads.append(feature)
        return feature

    def UpdateObject(self, obj):
        kind = getattr(obj, "kind", None)
        if kind == "shaft":
            obj.body.solids = [self._revolved(obj)]
        elif kind == "mirror":
            origin, h, v = obj.ref
            n = cross(h, v)
            volume, cog = _mass(obj.body.solids)
            off = dot(tuple(c - o for c, o in zip(cog, origin)), n)
            obj.body.solids = [(2 * volume, tuple(c - off * n[i] for i, c in enumerate(cog)))]
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
            obj.body.solids = [self._prism(obj)]
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
        side = -1 if (pad.DirectionOrientation == 1) != self.pads_reversed else 1
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


def _mass(solids):
    volume = sum(v for v, _ in solids)
    if not volume:
        return 0.0, (0.0, 0.0, 0.0)
    return volume, tuple(sum(v * c[i] for v, c in solids) / volume for i in range(3))


def _builder(part, monkeypatch, active=None):
    conn = SimpleNamespace(is_3dx=True, app=None,
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
    tree["parts"][0]["features"].append({"type": "shell", "id": "shell1"})
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
    refused["parts"][0]["features"].append({"type": "sweep", "id": "sweep1"})
    path = tmp_path / "refused.json"
    path.write_text(json.dumps(refused))
    assert main(["check", str(path)]) == 1
    assert "'sweep' features are not supported yet" in capsys.readouterr().out


# ── Milestone 2: revolve, mirror, split ──


def _shafts(part):
    return [f for f in part.pads if getattr(f, "kind", None) == "shaft"]


def test_revolve_about_an_axis_off_the_sketch_origin_uses_a_3d_line(monkeypatch):
    """Like Fab's sample 1 Shaft: a -XZ sketch revolved about a vertical axis
    that is not the CATIA sketch's own axis."""
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
