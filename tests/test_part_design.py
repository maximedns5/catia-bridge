"""Offline tests of drafts, extrudes up to a limit, threaded and V-bottomed
holes, and lofts to a point (catia_mcp/tree/partdesign.py, partdesign_com.py).

The fake CATIA of tests/test_dressups.py (topology, dress-ups) is extended:
a Draft turns each of its faces about the neutral plane by the first-order
wedge (exact for faces that share no edge); a Pad or Pocket with a limit adds
or removes what build123d's extrude did in the same feature when it points
the tree's way, nothing otherwise; Holes have threads and V bottoms; a
Multi-sections Solid to a point is the cone.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree import builder as builder_module
from catia_mcp.tree import partdesign_com
from catia_mcp.tree.geometry import add, cross, dot, scale, unit
from catia_mcp.tree.partdesign import (
    cone_expectation,
    draft_estimate,
    draft_plan,
    native,
    point_loft,
    right_cone,
    thread_spec,
    tip_height,
    until_spec,
)
from catia_mcp.tree.preflight import analyze, truncate
from tests.test_dressups import _of, _topo_builder, _TopoPart
from tests.test_tree import _tree_sketch

TREES = Path(__file__).parent / "fixtures" / "trees"

TAN3, TAN5 = math.tan(math.radians(3)), math.tan(math.radians(5))
# Closed forms of the m10 fixtures (mm3).
M10_MM3 = {
    # Two opposite faces turned 3 degrees about the bottom: two wedges.
    "m10_draft_two_sides": 40 * 30 * 20 - 2 * 600 * 10 * TAN3,
    # Four sides: a prismatoid h/6 (A0 + 4 Am + A1).
    "m10_draft_four_sides": 20 / 6 * (40 * 30 + 4 * (40 - 20 * TAN5) * (30 - 20 * TAN5)
                                      + (40 - 40 * TAN5) * (30 - 40 * TAN5)),
    # The C bracket (28000) and a column up to the plate (z 5 to 25).
    "m10_pad_up_to_next": 28000 + math.pi * 4**2 * 20,
    # A hole down through the plate and the base.
    "m10_pocket_up_to_last": 28000 - math.pi * 3**2 * 10,
    # 20 x 20 up to z = 18 - 0.2 y: 13 mm high on average.
    "m10_pad_up_to_plane": 60 * 40 * 5 + 20 * 20 * 13,
    "m10_pad_up_to_face": 28000 + 20 * 20 * 20,
    "m10_tapped_holes": 40 * 30 * 15 - math.pi * 2.5**2 * 12
    - math.pi * 2.5**2 * (2.5 / math.tan(math.radians(59))) / 3 - math.pi * 3.4**2 * 15,
    "m10_loft_pyramid_tip": 20 * 20 * 15 / 3,
    "m10_loft_cone_join": 30 * 30 * 10 + math.pi * 8**2 * 15 / 3,
}


def load(name: str) -> dict:
    return json.loads((TREES / f"{name}.json").read_text())


def _first(tree, kind):
    return next(f for f in tree["parts"][0]["features"] if f["type"] == kind)


def _expected(tree):
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    return expected(tree)


def _delta(expected, before_id, after_id, body="Body1"):
    """What build123d's feature `after_id` added to the body, as (signed
    volume, centre)."""
    (props,) = expected.values()
    a, b = props["features"][before_id][body], props["features"][after_id][body]
    dv = b["volume_mm3"] - a["volume_mm3"]
    centre = tuple((b["volume_mm3"] * b["cog_mm"][i] - a["volume_mm3"] * a["cog_mm"][i]) / dv
                   for i in range(3))
    return dv, centre


# ── Contract and preflight ──


def test_m10_fixtures_pass_preflight_and_match_their_closed_forms():
    for name, volume in M10_MM3.items():
        tree = load(name)
        assert not analyze(tree), name
        (props,) = _expected(tree).values()
        assert props["volume_mm3"] == pytest.approx(volume, rel=1e-9), name


def test_the_draft_estimate_is_exact_for_faces_that_share_no_edge():
    feature = _first(load("m10_draft_two_sides"), "draft")
    plan = draft_plan(feature)
    assert plan.pull == (0.0, 0.0, 1.0) and plan.angle == 3
    assert draft_estimate(plan, feature["faces"]) == pytest.approx(-2 * 600 * 10 * TAN3)
    four = _first(load("m10_draft_four_sides"), "draft")
    exact = M10_MM3["m10_draft_four_sides"] - 24000
    assert draft_estimate(draft_plan(four), four["faces"]) == pytest.approx(exact, rel=0.05)


def test_preflight_refuses_drafts_it_cannot_build():
    def refused(**change):
        tree = load("m10_draft_two_sides")
        _first(tree, "draft").update(change)
        return " ".join(analyze(tree))

    assert "draft angle" in refused(angle=0)
    assert "draft angle" in refused(angle=95)
    assert "draft has no face" in refused(faces=[])
    assert "a face anchor has no area" in refused(faces=[{"type": "face", "point": [0, 0, 0]}])
    assert "torus faces" in refused(faces=[{"type": "face", "geometry": "torus", "area": 1.0,
                                            "point": [0, 0, 0]}])
    assert "square to the pulling direction" in refused(
        faces=[{"type": "face", "geometry": "plane", "area": 1200.0, "point": [0, 0, 20],
                "normal": [0, 0, 1]}])
    assert "neutral plane is invalid" in refused(neutral_plane={"origin": [0, 0]})
    assert "pulling direction must be the neutral plane's normal" in refused(
        pull_direction=[1, 0, 0])
    assert "draft must modify exactly one known body" in refused(bodies=["Nope"])


def test_a_draft_without_faces_still_goes_to_the_step_fallback():
    bare = {"type": "draft", "id": "draft1", "bodies": ["Body1"]}
    assert not native(bare) and native(_first(load("m10_draft_two_sides"), "draft"))
    tree = load("m10_draft_two_sides")
    tree["parts"][0]["features"][-1] = bare
    assert "'draft' features are not supported yet" in next(iter(analyze(tree)))


def test_until_specs_follow_build123d_s_until():
    tree = load("m10_pocket_up_to_last")
    sketch = next(f for f in tree["parts"][0]["features"] if f["id"] == "sketch4")
    feature = tree["parts"][0]["features"][-1]
    assert until_spec(feature, sketch).sign == 1  # along its own (downward) normal
    assert until_spec(dict(feature, until="first"), sketch).sign == -1
    assert until_spec(dict(feature, direction=[0, 0, 1]), sketch).sign == -1
    plane = load("m10_pad_up_to_plane")
    sketch2, extrude2 = plane["parts"][0]["features"][2:]
    spec = until_spec(extrude2, sketch2)
    assert spec.mode == "plane" and spec.sign == 1 and spec.plane[1] == pytest.approx(
        unit((0, 0.2, 1)))
    below = dict(extrude2, until_plane={"origin": [0, 0, -3], "normal": [0, 0, 1]})
    assert until_spec(below, sketch2).sign == -1  # toward the plane


def test_preflight_refuses_extrudes_up_to_a_limit_it_cannot_build():
    def refused(**change):
        tree = load("m10_pad_up_to_plane")
        tree["parts"][0]["features"][-1].update(change)
        return " ".join(analyze(tree))

    assert "must join or cut a body" in refused(operation="new_body", creates=["Body2"])
    assert "'until' must be one of" in refused(until="halfway")
    assert "parallel to the extrude direction" in refused(
        until_plane={"origin": [0, 0, 18], "normal": [1, 0, 0]})
    assert "goes through the profile" in refused(
        until_plane={"origin": [0, 0, 5], "normal": [0, 0, 1]})
    assert "must go along its sketch normal" in refused(direction=[1, 0, 1])
    assert "tapered extrudes up to a limit" in refused(taper=5)
    assert "not a plane are not supported" in refused(
        until="face", until_face={"type": "face", "geometry": "cylinder", "area": 10.0,
                                  "point": [0, 0, 20]})
    assert "another body" in refused(until_target="Body9")
    assert not refused(until="next")  # no distance needed


def test_thread_specs_read_iso_designations():
    spec = thread_spec({"diameter": 5.0, "depth": 12, "thread": "M6"})
    assert (spec.nominal, spec.pitch, spec.depth, spec.coarse) == (6.0, 1.0, 12, True)
    fine = thread_spec({"diameter": 7.0, "depth": None, "thread": {"designation": "M8x1"}})
    assert (fine.designation, fine.pitch, fine.depth, fine.coarse) == ("M8x1", 1.0, None, False)
    by_size = thread_spec({"diameter": 8.5, "thread": {"nominal_diameter": 10, "depth": 9,
                                                       "side": "left"}})
    assert (by_size.designation, by_size.pitch, by_size.side) == ("M10", 1.5, "left")
    assert thread_spec({"diameter": 5.0}) is None
    with pytest.raises(ValueError):
        thread_spec({"diameter": 5.0, "thread": "UNC 1/4-20"})
    with pytest.raises(ValueError):
        thread_spec({"diameter": 5.0, "thread": {"nominal_diameter": 7}})  # no coarse pitch


def test_preflight_refuses_threads_and_bottoms_it_cannot_build():
    def refused(index, **change):
        tree = load("m10_tapped_holes")
        tree["parts"][0]["features"][index].update(change)
        return " ".join(analyze(tree))

    assert "smaller than the thread's nominal diameter" in refused(2, diameter=6.5)
    assert "deeper than its blind hole" in refused(2, thread={"designation": "M6", "depth": 13})
    assert "hole thread is invalid" in refused(2, thread="Tr12")
    assert "a through hole has no bottom" in refused(3, bottom={"type": "v"})
    assert "V bottom's angle" in refused(2, bottom={"type": "v", "angle": 180})
    assert "hole bottom 'round'" in refused(2, bottom={"type": "round"})


def test_point_lofts_are_cones_with_a_closed_form():
    tree = load("m10_loft_pyramid_tip")
    sketch, loft = tree["parts"][0]["features"]
    plan = point_loft(loft)
    assert plan.sketch == "sketch1" and plan.point == (3.0, 2.0, 15.0) and not plan.point_first
    want = cone_expectation(sketch, plan.point)
    assert want.volume == pytest.approx(2000) and want.cog == pytest.approx((0.75, 0.5, 3.75))
    assert right_cone(sketch, plan.point) is None
    cone_tree = load("m10_loft_cone_join")
    circle = cone_tree["parts"][0]["features"][2]
    assert right_cone(circle, (0, 0, 25)) == ((0.0, 0.0, 10.0), (0.0, 0.0, 1.0), 8.0, 15.0)
    reversed_loft = dict(loft, sections=[{"point": [0, 0, -9]}, "sketch1"])
    assert point_loft(reversed_loft).point_first
    assert not analyze({"version": 1, "parts": [
        {"id": "p", "bodies": ["Body1"], "features": [sketch, reversed_loft]}]})


def test_preflight_refuses_point_lofts_it_cannot_build():
    def refused(sections):
        tree = load("m10_loft_pyramid_tip")
        tree["parts"][0]["features"][1]["sections"] = sections
        return " ".join(analyze(tree))

    assert "exactly one sketch and one point" in refused(["sketch1", {"point": [0, 0, 1]},
                                                         {"point": [0, 0, 2]}])
    assert "exactly one sketch and one point" in refused([{"point": [0, 0, 1]},
                                                         {"point": [0, 0, 2]}])
    assert "lies on its section's plane" in refused(["sketch1", {"point": [1, 1, 0]}])
    assert "unknown sketch" in refused(["sketch9", {"point": [0, 0, 5]}])
    assert "loft to a point is invalid" in refused(["sketch1", {"point": [0, 0]}])


# ── Fake CATIA ──


class _Diameter:
    """A Hole's diameter that a thread standard drives (and locks) when the
    part says so, as CATIA's standards set the tap drill from the table."""

    def __init__(self, hole, value, part):
        self.hole, self.value, self.part = hole, value, part

    def _locked(self):
        return self.part.standard_locks_diameter and \
            getattr(self.hole, "HoleThreadStandard", "") == partdesign_com.COARSE_STANDARD

    @property
    def Value(self):
        return 4.917 if self._locked() else self.value

    @Value.setter
    def Value(self, value):
        if not self._locked():
            self.value = value


class _PlusPart(_TopoPart):
    def __init__(self, draft_reversed=False, until_solids=None, until_dir=None,
                 refused_limits=(), standard_locks_diameter=False, tip_in_depth=False,
                 point_loft_refused=False, **kwargs):
        super().__init__(**kwargs)
        self.draft_reversed = draft_reversed
        self.ShapeFactory.AddNewDraft = self._draft
        self.HybridShapeFactory.AddNewPlaneEquation = lambda a, b, c, d: ("equation", (a, b, c), d)
        # Limited pads: CatLimitMode -> (volume, centre) build123d's feature
        # added, when the pad points along `until_dir`.
        self.until_solids, self.until_dir = until_solids or {}, until_dir
        self.refused_limits = refused_limits
        self.standard_locks_diameter, self.tip_in_depth = standard_locks_diameter, tip_in_depth
        self.point_loft_refused = point_loft_refused

    # Drafts.

    def _draft(self, face, neutral, nmode, parting, x, y, z, mode, angle, msel):
        domain = SimpleNamespace(faces=[face], pull=(x, y, z),
                                 DraftAngle=SimpleNamespace(Value=angle))
        domain.AddFaceToDraft = domain.faces.append
        domain.SetPullingDirection = lambda *d: setattr(domain, "pull", d)
        feature = self._feature("draft", neutral=neutral, parting=parting,
                                DraftDomains=SimpleNamespace(Item=lambda i: domain))
        feature.domain = domain
        return feature

    def _draft_solids(self, draft):
        origin, _, _ = draft.neutral
        domain = draft.domain
        pull = unit(domain.pull)
        t = math.tan(math.radians(domain.DraftAngle.Value)) * (-1 if self.draft_reversed else 1)
        solids = []
        for face in domain.faces:
            height = dot(add(face.centroid, scale(origin, -1)), pull)
            solids.append((-face.area * height * t, add(face.centroid, scale(pull, height / 3))))
        return solids

    # Limited pads and pockets.

    def _pad(self, sketch, length, name="Extrusion", sign=1):
        pad = super()._pad(sketch, length, name, sign)
        pad.FirstLimit.LimitMode = 0
        pad.FirstLimit.LimitingElement = None
        return pad

    def _pad_cells(self, pad):
        if any(e["type"] != "line" for e in pad.sketch.factory.entities):
            return []  # the topology fake knows polygons only
        return super()._pad_cells(pad)

    def _prism(self, pad):
        mode = pad.FirstLimit.LimitMode
        if not mode:
            return super()._prism(pad)
        if mode in self.refused_limits:
            pad.base = getattr(pad, "base", list(pad.body.solids))  # deleted, it restores these
            raise RuntimeError("La mise à jour a échoué")
        _, h, v = pad.sketch.ref
        reversed_ = self.pockets_reversed if pad.sign < 0 else self.pads_reversed
        side = -1 if (pad.DirectionOrientation == 1) != reversed_ else 1
        volume, centre = self.until_solids[mode]
        if dot(scale(cross(h, v), side), self.until_dir) <= 0:
            return 0.0, centre  # nothing to stop at that way
        return abs(volume), centre

    # Holes with threads and V bottoms.

    def _hole(self, sketch, depth):
        hole = super()._hole(sketch, depth)
        hole.Diameter = _Diameter(hole, 10.0, self)
        hole.BottomAngle = SimpleNamespace(Value=None)
        hole.ThreadingMode = 1
        hole.HoleThreadStandard = ""
        hole.HoleThreadDescription = SimpleNamespace(Value="")
        for name in ("ThreadDiameter", "ThreadPitch", "ThreadDepth"):
            setattr(hole, name, SimpleNamespace(Value=None))
        hole.ThreadSide = 0
        return hole

    def _drilled(self, hole):
        volume, cog = super()._drilled(hole)
        if hole.BottomType != builder_module.HOLE_V_BOTTOM:
            return volume, cog
        origin, h, v = hole.sketch.ref
        (point,) = hole.sketch.factory.points
        normal = cross(h, v)
        at = tuple(origin[i] + point.uv[0] * h[i] + point.uv[1] * v[i] for i in range(3))
        down = -1 if hole.flipped == self.holes_reversed else 1
        r = hole.Diameter.Value / 2
        tip = r / math.tan(math.radians(hole.BottomAngle.Value) / 2)
        depth = hole.BottomLimit.Dimension.Value - (tip if self.tip_in_depth else 0.0)
        pieces = [(math.pi * r * r * depth, depth / 2),
                  (math.pi * r * r * tip / 3, depth + tip / 4)]
        total = sum(p for p, _ in pieces)
        along = sum(p * z for p, z in pieces) / total
        return total, tuple(at[i] + down * along * normal[i] for i in range(3))

    # Lofts to a point.

    def UpdateObject(self, obj):
        kind = getattr(obj, "kind", None)
        if kind == "draft":
            body = obj.body
            if not hasattr(obj, "before"):
                obj.before = obj.base = list(body.solids)
                obj.cells_before = list(body.shape_list[-1].cells)
            body.solids = obj.before + self._draft_solids(obj)
            obj.cells = [c.owned_by(obj.Name) for c in obj.cells_before]
            if all(obj is not s for s in body.shape_list):
                body.shape_list.append(obj)
            for cell in obj.cells:
                self.cells_by_id[cell.id] = cell
            return
        if kind == "loft" and any(isinstance(ref, tuple) and ref[0] == "point"
                                  for ref, _, _ in obj.HybridShape.sections):
            if self.point_loft_refused:
                raise RuntimeError("La mise à jour a échoué")
            refs = [ref for ref, _, _ in obj.HybridShape.sections]
            (apex,) = [ref[1] for ref in refs if isinstance(ref, tuple)]
            (sketch,) = [ref for ref in refs if not isinstance(ref, tuple)]
            want = cone_expectation(_tree_sketch(sketch), apex)
            obj.body.solids = [(want.volume, want.cog)]
            obj.cells = []
            return
        super().UpdateObject(obj)


def _plus_builder(part, monkeypatch):
    built = _topo_builder(part, monkeypatch)
    # Python cannot pass VBScript's Nothing; the fake takes None.
    monkeypatch.setattr(builder_module, "_add_loft_section",
                        lambda app, shape, ref, sense, closing:
                            shape.AddSectionToLoft(ref, sense, closing))
    return built


def _entry(report, fid):
    return next(f for f in report["parts_detail"][0]["features"] if f["id"] == fid)


# ── Drafts ──


def test_a_draft_is_one_catia_draft_on_the_matched_faces(monkeypatch):
    part = _PlusPart()
    tree = load("m10_draft_two_sides")
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    (draft,) = _of(part, "draft")
    assert draft.body is part.MainBody
    assert sorted(f.normal for f in draft.domain.faces) == [(-1, 0, 0), (1, 0, 0)]
    assert draft.neutral == ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))  # PlaneXY
    assert draft.domain.pull == (0.0, 0.0, 1.0) and draft.domain.DraftAngle.Value == 3
    entry = _entry(report, "draft1")
    assert entry["catia"] == [draft.Name] and entry["references"]["brep_name"] == 2
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        M10_MM3["m10_draft_two_sides"], abs=1e-3)


def test_a_draft_catia_turns_the_other_way_is_pulled_the_other_way(monkeypatch):
    part = _PlusPart(draft_reversed=True)
    tree = load("m10_draft_two_sides")
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    first, kept = _of(part, "draft")
    assert part.deleted == [first] and kept.domain.pull == (-0.0, -0.0, -1.0)
    assert f"{kept.Name}: pulled the other way to match the tree" in _entry(
        report, "draft1")["notes"]


def test_without_a_reference_a_draft_is_checked_by_its_first_order_estimate(monkeypatch):
    part = _PlusPart()
    report = _plus_builder(part, monkeypatch).build(load("m10_draft_two_sides"),
                                                    check_editability=False)
    assert report["success"], report.get("error")
    part = _PlusPart()
    tree = load("m10_draft_two_sides")
    _first(tree, "draft")["angle"] = 6  # the fake drafts 6 degrees, the check expects 3
    monkeypatch.setattr(partdesign_com, "draft_estimate",
                        lambda plan, faces: -2 * 600 * 10 * TAN3)
    report = _plus_builder(part, monkeypatch).build(tree, check_editability=False)
    assert not report["success"]
    assert "no Draft changed the body" in report["error"]["message"]
    assert len(part.deleted) == 3  # each way tried, none kept


def test_a_draft_angle_from_the_script_becomes_a_user_parameter(monkeypatch):
    part = _PlusPart()
    tree = load("m10_draft_two_sides")
    _first(tree, "draft")["params"] = {"angle": {"arg": "angle", "kind": "literal", "value": 3,
                                                 "file": "model.py", "line": 12}}
    expected = _expected(tree)
    part.Parameters.GetNameToUseInRelation = lambda p: p.Name
    report = _plus_builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    (draft,) = _of(part, "draft")
    parameters = report["parts_detail"][0]["parameters"]
    assert [(p["name"], p["unit"]) for p in parameters] == [("Draft1_angle", "deg")]
    ((target, expression),) = part.Relations.formulas.values()
    assert target is draft.domain.DraftAngle and expression == "Draft1_angle"


# ── Extrudes up to a limit ──


def _without_wall(name):
    """The bracket fixture without its wall: the fake does not clip solids,
    and the base and the plate no longer overlap."""
    tree = load(name)
    tree["parts"][0]["features"] = [f for f in tree["parts"][0]["features"]
                                    if f["id"] not in ("sketch2", "extrude2")]
    return tree


def test_a_pad_up_to_next_is_a_pad_limited_up_to_next_in_its_body(monkeypatch):
    tree = _without_wall("m10_pad_up_to_next")
    expected = _expected(tree)
    added = _delta(expected, "extrude3", "extrude4")
    assert added[0] == pytest.approx(math.pi * 16 * 20)
    part = _PlusPart(until_solids={1: added}, until_dir=(0, 0, 1))
    report = _plus_builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    pad = part.pads[-1]
    assert pad.FirstLimit.LimitMode == 1 and pad.body is part.MainBody and pad.sign == 1
    assert "Extrusion.3: limit uptonext" in _entry(report, "extrude4")["notes"]


def test_a_pocket_up_to_last_goes_the_sketch_normal_s_way_flipping_once(monkeypatch):
    tree = _without_wall("m10_pocket_up_to_last")
    expected = _expected(tree)
    removed = _delta(expected, "extrude3", "extrude4")
    assert removed[0] == pytest.approx(-math.pi * 9 * 10)
    for reversed_ in (False, True):
        part = _PlusPart(until_solids={2: removed}, until_dir=(0, 0, -1),
                         pockets_reversed=reversed_)
        report = _plus_builder(part, monkeypatch).build(tree, expected, check_editability=False)
        assert report["success"], report.get("error")
        pocket = part.pads[-1]
        assert pocket.Name.startswith("Poche") and pocket.FirstLimit.LimitMode == 2
        notes = _entry(report, "extrude4")["notes"]
        assert (f"{pocket.Name}: flipped to match the tree" in notes) == reversed_


def test_up_to_next_falls_back_to_up_through_next(monkeypatch):
    tree = _without_wall("m10_pad_up_to_next")
    expected = _expected(tree)
    added = _delta(expected, "extrude3", "extrude4")
    part = _PlusPart(until_solids={5: added}, until_dir=(0, 0, 1), refused_limits=(1,))
    report = _plus_builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    entry = _entry(report, "extrude4")
    assert part.pads[-1].FirstLimit.LimitMode == 5 and len(entry["catia"]) == 1
    assert any(n.startswith("uptonext: ") for n in entry["notes"])


def test_a_pad_up_to_a_tilted_plane_stops_at_a_construction_plane(monkeypatch):
    tree = load("m10_pad_up_to_plane")
    expected = _expected(tree)
    added = _delta(expected, "extrude1", "extrude2")
    assert added[0] == pytest.approx(5200)
    part = _PlusPart(until_solids={3: added}, until_dir=(0, 0, 1))
    report = _plus_builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    limit = part.pads[-1].FirstLimit
    kind, normal, offset = limit.LimitingElement
    assert limit.LimitMode == 3 and kind == "equation"
    assert unit(normal) == pytest.approx(unit((0, 0.2, 1))) and offset == pytest.approx(
        18 / math.sqrt(1.04))


def test_a_pad_up_to_a_face_is_limited_by_the_matched_face(monkeypatch):
    tree = _without_wall("m10_pad_up_to_face")
    feature = tree["parts"][0]["features"][-1]
    feature["until_face"] = {"type": "face", "geometry": "plane", "point": [0, 0, 25],
                             "normal": [0, 0, -1], "area": 2400.0}
    expected = _expected(tree)
    added = _delta(expected, "extrude3", "extrude4")
    assert added[0] == pytest.approx(8000)
    part = _PlusPart(until_solids={4: added}, until_dir=(0, 0, 1))
    report = _plus_builder(part, monkeypatch).build(tree, expected, check_editability=False)
    assert report["success"], report.get("error")
    limit = part.pads[-1].FirstLimit
    assert limit.LimitMode == 4 and limit.LimitingElement.normal == (0, 0, -1)
    assert limit.LimitingElement.centroid == (0, 0, 25)


def test_without_a_reference_an_extrude_up_to_a_limit_is_checked_by_its_sign(monkeypatch):
    tree = _without_wall("m10_pad_up_to_next")
    part = _PlusPart(until_solids={1: (1005.3, (-10, 0, 15))}, until_dir=(0, 0, 1))
    report = _plus_builder(part, monkeypatch).build(tree, check_editability=False)
    assert report["success"], report.get("error")
    assert any("sign of the volume change only" in n for n in _entry(report, "extrude4")["notes"])
    part = _PlusPart(until_solids={1: (1005.3, (-10, 0, 15)), 5: (1005.3, (-10, 0, 15))},
                     until_dir=(0, 0, -1))  # never reaches anything
    report = _plus_builder(part, monkeypatch).build(tree, check_editability=False)
    assert not report["success"] and "no Pad or Pocket up to next" in report["error"]["message"]


# ── Threaded and V-bottomed holes ──


def _holes(part):
    return [f for f in part.pads if getattr(f, "kind", None) == "hole"]


def test_tapped_holes_are_threaded_holes_drilled_at_the_tap_drill(monkeypatch):
    tree = load("m10_tapped_holes")
    part = _PlusPart(through_depth=15.0)
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    blind, through = _holes(part)
    assert (blind.Diameter.Value, blind.BottomType, blind.BottomAngle.Value) == (5.0, 1, 118)
    assert blind.BottomLimit.Dimension.Value == 12
    assert (blind.ThreadingMode, blind.HoleThreadStandard, blind.HoleThreadDescription.Value,
            blind.ThreadDepth.Value) == (0, "Metric Thick Pitch", "M6", 10)
    assert (through.Diameter.Value, through.BottomType, through.BottomLimit.LimitMode) == \
        (6.8, 0, 2)
    assert through.HoleThreadDescription.Value == "M8" and through.ThreadDepth.Value is None
    thread = _entry(report, "hole1")["thread"]
    assert thread["threaded"] and thread["standard"] == "Metric Thick Pitch"
    assert "unverified" in thread["threading_mode"]


def test_a_standard_that_changes_the_tap_drill_gives_way_to_no_standard(monkeypatch):
    tree = load("m10_tapped_holes")
    part = _PlusPart(through_depth=15.0, standard_locks_diameter=True)
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    blind, _ = _holes(part)
    assert blind.HoleThreadStandard == "No Standard" and blind.Diameter.Value == 5.0
    assert (blind.ThreadDiameter.Value, blind.ThreadPitch.Value) == (6.0, 1.0)
    thread = _entry(report, "hole1")["thread"]
    assert thread["standard"] == "No Standard" and "Metric Thick Pitch" in thread["tried"][0]


def test_a_thread_catia_refuses_leaves_a_smooth_hole_and_says_so(monkeypatch):
    tree = truncate(load("m10_tapped_holes"), None, "hole1")
    part = _PlusPart()
    original = part._hole

    def refusing(sketch, depth):
        hole = original(sketch, depth)
        del hole.HoleThreadDescription, hole.ThreadDiameter  # this release has neither
        return hole

    part.ShapeFactory.AddNewHoleFromSketch = refusing
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    (hole,) = _holes(part)
    entry = _entry(report, "hole1")
    assert entry["catia"] == [hole.Name] and not entry["thread"]["threaded"]
    assert len(entry["thread"]["tried"]) == 2
    assert entry["notes"][-1].startswith(f"{hole.Name}: left smooth")


def test_a_v_bottom_depth_catia_measures_to_the_tip_is_given_the_tip(monkeypatch):
    tree = truncate(load("m10_tapped_holes"), None, "hole1")
    part = _PlusPart(tip_in_depth=True)
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    first, kept = _holes(part)
    tip = tip_height(_first(tree, "hole"))
    assert part.deleted[0] is first
    assert kept.BottomLimit.Dimension.Value == pytest.approx(12 + tip)
    assert "the V-bottomed hole's depth is measured to its tip" in _entry(report, "hole1")["notes"]


def test_without_a_reference_a_v_bottom_is_cut_as_a_turned_cone(monkeypatch):
    tree = truncate(load("m10_tapped_holes"), None, "hole1")
    part = _PlusPart()
    want = {tree["parts"][0]["id"]: {"volume_mm3": 18000 - math.pi * 6.25 * 12
                                     - math.pi * 6.25 * tip_height(_first(tree, "hole")) / 3}}
    report = _plus_builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    assert [f.kind for f in part.pads if getattr(f, "kind", None) == "shaft"] == ["shaft"]
    assert "the thread is not modelled" in _entry(report, "hole1")["notes"][0]


# ── Lofts to a point ──


def test_a_loft_to_a_point_is_a_multi_sections_solid_to_a_construction_point(monkeypatch):
    tree = load("m10_loft_pyramid_tip")
    part = _PlusPart()
    report = _plus_builder(part, monkeypatch).build(
        tree, {"/M10/LoftPyramidTip": {"volume_mm3": 2000.0}}, check_editability=False)
    assert report["success"], report.get("error")
    (loft,) = _of(part, "loft")
    (section, _, _), (apex, _, _) = loft.HybridShape.sections
    assert apex == ("point", (3.0, 2.0, 15.0)) and section is part.MainBody.last_sketch
    assert "Multi-sections Solid to a point" in _entry(report, "loft1")["notes"][-1]


def test_a_cone_catia_will_not_loft_to_its_point_is_a_shaft(monkeypatch):
    tree = load("m10_loft_cone_join")
    part = _PlusPart(point_loft_refused=True)
    report = _plus_builder(part, monkeypatch).build(tree, _expected(tree), check_editability=False)
    assert report["success"], report.get("error")
    (shaft,) = [f for f in part.pads if getattr(f, "kind", None) == "shaft"]
    assert shaft.FirstAngle == 360
    entry = _entry(report, "loft1")
    assert "built as a Shaft of its triangle" in entry["notes"][-1]
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        M10_MM3["m10_loft_cone_join"], abs=1e-3)


def test_a_pyramid_catia_will_not_loft_stops_the_build(monkeypatch):
    part = _PlusPart(point_loft_refused=True)
    report = _plus_builder(part, monkeypatch).build(load("m10_loft_pyramid_tip"),
                                                    check_editability=False)
    assert not report["success"]
    assert "no Multi-sections Solid to the point" in report["error"]["message"]


def test_the_probe_script_cases_pass_preflight():
    import importlib.util

    path = Path(__file__).parent.parent / "scripts" / "probe_part_design.py"
    spec = importlib.util.spec_from_file_location("probe_part_design", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert set(probe.FIXTURES) == set(M10_MM3)
    for name in probe.FIXTURES:
        assert not analyze(load(name)), name
