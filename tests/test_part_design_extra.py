"""Offline tests of the second Part Design slice: sweeps along a helix,
cosmetic threads on a shaft, user patterns of holes, bodies moved or
mirrored in place (catia_mcp/tree/helix.py, helix_com.py,
partdesign_extra.py, partdesign_extra_com.py).

The fake CATIA of tests/test_part_design.py is extended: a GSD Helix is the
helix its axis, start point, pitch, height, hand and taper describe (its
distance to a point measured on it); a Rib or Slot along it adds or removes
the screw motion's closed form (OpenCascade's sweep for a tapered helix); a
Thread changes nothing but reads back its values; a User Pattern copies the
seed Hole's cut onto its sketch's points; Rotate, Translate and Symmetry
move the body in work.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree import helix_com, partdesign_extra_com
from catia_mcp.tree.anchors import FaceCandidate
from catia_mcp.tree.dressup import DressUps
from catia_mcp.tree.geometry import add, cross, dot, norm, scale, unit
from catia_mcp.tree.helix import (
    HelixSpec,
    helix_spec,
    occ_x_direction,
    path_helix,
    screw_sweep,
)
from catia_mcp.tree.partdesign_extra import (
    mirrored_point,
    moved_point,
    positions_sketch,
    shaft_thread,
    user_layout,
)
from catia_mcp.tree.preflight import analyze
from catia_mcp.tree.requirements import WIREFRAME, feature_needs
from tests.test_part_design import _plus_builder, _PlusPart
from tests.test_tree import _FakeRelations, _tree_sketch

TREES = Path(__file__).parent / "fixtures" / "trees"
T30 = math.tan(math.radians(30))


def load(name: str) -> dict:
    return json.loads((TREES / f"{name}.json").read_text())


def _features(tree):
    return tree["parts"][0]["features"]


def _first(tree, kind):
    return next(f for f in _features(tree) if f["type"] == kind)


def _by_id(tree, fid):
    return next(f for f in _features(tree) if f.get("id") == fid)


def _expected(tree):
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    return expected(tree)


def _clipped_groove():
    """The thread groove's profile inside the 12 mm shaft: the V cut at the
    shaft's surface (u = 0), a triangle 0.8 deep and 2 x 0.8 tan 30 wide."""
    tree = load("m11_helix_thread_cut")
    sketch = dict(_by_id(tree, "sketch2"))
    half = 0.8 * T30
    points = [(-0.8, 0.0), (0.0, -half), (0.0, half)]
    sketch["entities"] = [{"type": "line", "start": list(points[i]),
                           "end": list(points[(i + 1) % 3]), "id": f"g{i}"} for i in range(3)]
    sketch["profiles"] = [{"outer": ["g0", "g1", "g2"], "inner": []}]
    return sketch, path_helix(_by_id(tree, "sweep1")["path"])


# The L block of the move and mirror fixtures: 450 mm2, 8 mm high.
L_COG = ((300 * 15 + 150 * 5) / 450, (300 * 5 + 150 * 17.5) / 450, 4.0)


def _closed_forms():
    groove, groove_helix = _clipped_groove()
    spring = load("m11_helix_spring")
    spring_spec = path_helix(_first(spring, "sweep")["path"])
    moved = moved_point(_first(load("m11_move_in_place"), "move")["matrix"], L_COG)
    hole = 32 * math.pi
    return {
        # A 2 mm wire, 2.5 turns of radius 10: pi a^2 x 2 pi R x turns.
        "m11_helix_spring": (math.pi * 2 * math.pi * 10 * 2.5,
                             screw_sweep(_first(spring, "sketch"), spring_spec).cog),
        "m11_helix_thread_cut": (math.pi * 36 * 30 - screw_sweep(groove, groove_helix).volume,
                                 None),
        "m11_shaft_thread": (math.pi * (64 * 10 + 25 * 30), None),  # cosmetic: unchanged
        "m11_user_pattern_holes": (60 * 40 * 10 - 4 * math.pi * 9 * 10, None),
        "m11_move_in_place": (3600 - hole, tuple((3600 * moved[i] - hole * (30, 5, 4)[i])
                                                 / (3600 - hole) for i in range(3))),
        "m11_mirror_in_place": (3600, mirrored_point({"origin": [40, 0, 0],
                                                      "normal": [1, 1, 0]}, L_COG)),
    }


# ── Contract and preflight ──


def test_m11_fixtures_pass_preflight_and_match_their_closed_forms():
    forms = _closed_forms()
    for name in ("m11_helix_spring", "m11_helix_thread_cut", "m11_helix_tapered_spring",
                 "m11_shaft_thread", "m11_user_pattern_holes", "m11_move_in_place",
                 "m11_mirror_in_place"):
        tree = load(name)
        assert not analyze(tree), name
        (props,) = _expected(tree).values()
        if name not in forms:
            continue
        volume, cog = forms[name]
        assert props["volume_mm3"] == pytest.approx(volume, rel=1e-5), name
        if cog is not None:
            assert props["cog_mm"] == pytest.approx(list(cog), abs=1e-4), name


def test_the_tapered_spring_is_close_to_its_mean_radius_screw():
    # No closed form: the conical helix is not a screw motion. Pappus at the
    # mean radius over the generatrix length is within half a percent.
    (props,) = _expected(load("m11_helix_tapered_spring")).values()
    spec = path_helix(_first(load("m11_helix_tapered_spring"), "sweep")["path"])
    mean = (spec.radius + spec.end_radius) / 2
    assert spec.end_radius == pytest.approx(10 + 12 * math.tan(math.radians(8)))
    assert props["volume_mm3"] == pytest.approx(
        math.pi * 0.64 * 2 * math.pi * mean * spec.turns, rel=5e-3)


def test_the_helix_starts_where_build123d_starts_it():
    build123d = pytest.importorskip("build123d")
    for direction in ((0, 0, 1), (1, 0, 0), (0, 1, 0), (1, 2, 3), (-1, 0.2, 0.1), (0, 0, -1)):
        spec = HelixSpec((1.0, 2.0, 3.0), unit(direction), 5.0, 3.0, 6.0)
        edge = build123d.Helix(3, 6, 5, center=(1, 2, 3), direction=direction)
        assert tuple(edge.position_at(0)) == pytest.approx(spec.start, abs=1e-9), direction
        assert tuple(edge.position_at(1)) == pytest.approx(spec.end, abs=1e-6), direction
    assert occ_x_direction((0, 0, 1)) == (1.0, 0.0, 0.0)
    left = HelixSpec((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 10.0, 4.0, 4.0, lefthand=True)
    assert left.point(0.25) == pytest.approx((0, -10, 1))


def test_the_screw_closed_form_matches_build123d_off_axis_and_left_handed():
    build123d = pytest.importorskip("build123d")
    from catia_mcp.tree.reference import sketch_faces

    for lefthand, turns in ((False, 2.5), (True, 1.3)):
        spec = helix_spec({"type": "helix", "pitch": 4, "turns": turns, "radius": 10,
                           "center": [1, -2, 0.5], "lefthand": lefthand})
        quad = [(-0.5, -1), (1.5, -1), (1.5, 1.2), (-0.5, 1)]
        sketch = {"plane": {"origin": list(spec.start), "x_dir": [1, 0, 0],
                            "normal": [0, -1, 0]},
                  "entities": [{"type": "line", "start": list(quad[i]),
                                "end": list(quad[(i + 1) % 4]), "id": f"e{i}"}
                               for i in range(4)],
                  "profiles": [{"outer": ["e0", "e1", "e2", "e3"], "inner": []}]}
        edge = build123d.Helix(4, spec.height, 10, center=spec.center, lefthand=lefthand)
        solid = build123d.sweep(sketch_faces(sketch), path=build123d.Wire([edge]),
                                is_frenet=True)
        want = screw_sweep(sketch, spec)
        cog = solid.center(build123d.CenterOf.MASS)
        assert solid.volume == pytest.approx(want.volume, rel=1e-5)
        assert (cog.X, cog.Y, cog.Z) == pytest.approx(want.cog, abs=1e-4)


def test_helix_specs_read_turns_or_height_and_taper():
    spec = helix_spec({"type": "helix", "pitch": 4, "turns": 2.5, "radius": 10})
    assert (spec.height, spec.turns, spec.direction, spec.lefthand) == (10.0, 2.5,
                                                                         (0.0, 0.0, 1.0), False)
    tapered = helix_spec({"type": "helix", "pitch": 4, "height": 12, "radius": 10,
                          "cone_angle": 8})
    assert tapered.axial_pitch == pytest.approx(4 * math.cos(math.radians(8)))
    assert tapered.turns == pytest.approx(12 / tapered.axial_pitch) and tapered.tapered
    with pytest.raises(ValueError, match="turns and height disagree"):
        helix_spec({"type": "helix", "pitch": 4, "turns": 2, "height": 10, "radius": 10})
    with pytest.raises(ValueError, match="'start'"):
        helix_spec({"type": "helix", "pitch": 4, "turns": 2, "radius": 10, "start": [0, 10, 0]})
    assert helix_spec({"type": "helix", "pitch": 4, "turns": 2, "radius": 10,
                       "start": [10, 0, 0]}).start == (10.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="pitch"):
        helix_spec({"type": "helix", "pitch": 0, "turns": 2, "radius": 10})
    with pytest.raises(ValueError, match="closes onto its axis"):
        helix_spec({"type": "helix", "pitch": 4, "height": 40, "radius": 2, "cone_angle": -40})


def test_preflight_refuses_helix_sweeps_it_cannot_build():
    def refused(sweep=None, helix=None, entities=None):
        tree = load("m11_helix_spring")
        feature = _first(tree, "sweep")
        feature.update(sweep or {})
        feature["path"][0].update(helix or {})
        if entities is not None:
            _first(tree, "sketch")["entities"] = entities
        return " ".join(analyze(tree))

    assert "must be Frenet sweeps" in refused(sweep={"frenet": False})
    assert "one helix entity" in refused(sweep={"path": [
        {"type": "helix", "pitch": 4, "height": 10, "radius": 10},
        {"type": "line", "start": [0, 0, 10], "end": [0, 0, 20]}]})
    assert "helix sweep is invalid" in refused(helix={"radius": -1})
    assert "taller than the pitch" in refused(helix={"pitch": 1.5})
    assert "must not reach the helix axis" in refused(
        entities=[{"type": "circle", "center": [-10, 0], "radius": 1, "id": "c"}])
    assert not refused(helix={"turns": 2.5})  # height and turns agree


def test_preflight_refuses_threads_moves_and_mirrors_it_cannot_build():
    def refused(name, kind, **change):
        tree = load(name)
        _first(tree, kind).update(change)
        return " ".join(analyze(tree))

    assert "needs its limit_face" in refused("m11_shaft_thread", "thread", limit_face=None)
    assert "must be a cylinder" in refused("m11_shaft_thread", "thread", face={
        "type": "face", "geometry": "plane", "point": [0, 0, 25], "area": 10.0})
    assert "thread is invalid" in refused("m11_shaft_thread", "thread", thread="UNC 1/4-20")
    assert "needs its length" in refused("m11_shaft_thread", "thread", length=None)
    assert "thread must modify exactly one known body" in refused(
        "m11_shaft_thread", "thread", bodies=["Body9"])
    assert "moves that are not rigid" in refused(
        "m11_move_in_place", "move", matrix=[[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
    assert "mirror_in_place plane is invalid" in refused(
        "m11_mirror_in_place", "mirror_in_place", plane={"origin": [0, 0, 0]})


def test_preflight_refuses_user_patterns_it_cannot_build():
    def refused(**change):
        tree = load("m11_user_pattern_holes")
        _first(tree, "hole").update(change)
        return " ".join(analyze(tree))

    hole = _first(load("m11_user_pattern_holes"), "hole")
    tilted = [list(map(list, frame)) for frame in hole["frames"]]
    tilted[2][2][3] = 9  # one hole a millimetre lower
    assert "share their direction and their plane" in refused(frames=tilted)
    assert "a hole's pattern is" in refused(pattern={"kind": "rect"})
    assert "must be its hole frames' origins" in refused(
        pattern={"kind": "user", "positions": [[0, 0, 10]] * 4})
    assert not refused(pattern={"kind": "user", "positions": hole["positions"]})


def test_user_layouts_and_their_positions_sketch():
    hole = _first(load("m11_user_pattern_holes"), "hole")
    layout = user_layout(hole)
    assert layout.origin == (-20.0, -10.0, 10.0) and layout.z_dir == (0.0, 0.0, 1.0)
    assert layout.positions == [(25.0, -2.0), (38.0, 18.0), (12.0, 22.0)]
    sketch = positions_sketch(layout)
    assert [e["at"] for e in sketch["entities"]] == [[25.0, -2.0], [38.0, 18.0], [12.0, 22.0]]
    assert shaft_thread(_first(load("m11_shaft_thread"), "thread")).depth == 15


def test_helix_sweeps_and_moves_need_construction_elements():
    tree = load("m11_helix_spring")
    sketches = {"sketch1": _first(tree, "sketch")}
    assert WIREFRAME in feature_needs(_first(tree, "sweep"), sketches)
    assert WIREFRAME in feature_needs(_first(load("m11_move_in_place"), "move"), {})


# ── Fake CATIA ──


class _Helix(SimpleNamespace):
    """A GSD helix: about its axis line, from its start point."""

    def geometry(self, axis_reversed: bool, clockwise_is_right: bool):
        _, origin, d = self.axis
        direction = scale(unit(d), -1 if self.invert != axis_reversed else 1)
        start = self.start[1]
        rel = add(start, scale(origin, -1))
        center = add(origin, scale(unit(d), dot(rel, unit(d))))
        radius = norm(add(start, scale(center, -1)))
        cone = self.taper * (1 if self.outward else -1)
        lefthand = self.clockwise != clockwise_is_right
        spec = HelixSpec(center, direction, radius,
                         self.pitch / math.cos(math.radians(cone)), self.height, lefthand, cone)
        return spec, unit(add(start, scale(center, -1)))

    def point(self, spec: HelixSpec, e: tuple, turns: float):
        f = cross(spec.direction, e)
        v = turns * spec.pitch
        a = math.radians(spec.cone_angle)
        phi = spec.sense * 2 * math.pi * turns
        radial = add(scale(e, math.cos(phi)), scale(f, math.sin(phi)))
        return add(add(spec.center, scale(radial, spec.radius + v * math.sin(a))),
                   scale(spec.direction, v * math.cos(a)))


class _Rib:
    """A Rib or Slot; a part may refuse its pulling direction."""

    def __init__(self, part, **fields):
        object.__setattr__(self, "part", part)
        self.__dict__.update(fields, PullingDirElement=None)

    def __setattr__(self, name, value):
        if name == "PullingDirElement" and self.part.pulling_refused:
            raise RuntimeError("La propriété PullingDirElement ne peut pas être définie")
        self.__dict__[name] = value


class _TableRelations(_FakeRelations):
    """The part's relations, with the thread tables listed by name."""

    def __init__(self, tables):
        super().__init__()
        self.tables = tables

    @property
    def Count(self):
        return len(self.tables)

    def Item(self, i):
        return SimpleNamespace(Name=self.tables[i - 1])

    def Remove(self, name):
        if name in self.tables:
            self.tables.remove(name)
        else:
            super().Remove(name)


class _ExtraPart(_PlusPart):
    def __init__(self, clockwise_is_right=False, axis_reversed=False, refused_ribs=(),
                 pulling_refused=False, keep_angle_twists=False, user_pattern=None,
                 symmetry_needs_later=False, thread_table=None, thread_refused=(),
                 thread_runs_off=False, **kwargs):
        super().__init__(**kwargs)
        self.clockwise_is_right, self.axis_reversed = clockwise_is_right, axis_reversed
        self.refused_ribs, self.pulling_refused = refused_ribs, pulling_refused
        self.keep_angle_twists = keep_angle_twists
        # None: a user pattern works; "anchor": only anchored; "refused": never.
        self.user_pattern = user_pattern
        self.symmetry_needs_later = symmetry_needs_later
        # None: no table; "fits": the ISO table sets the tree's values;
        # "locks": it sets another pitch and locks it.
        self.thread_table, self.thread_refused = thread_table, thread_refused
        self.thread_runs_off = thread_runs_off
        self.tables: list[str] = []
        self.helices: list[_Helix] = []
        self.HybridShapeFactory.AddNewHelix = self._helix
        self.HybridShapeFactory.AddNewDirectionByCoord = lambda x, y, z: (x, y, z)
        factory = self.ShapeFactory
        factory.AddNewRibFromRef = lambda p, h: self._rib("AddNewRibFromRef", p, h, 1)
        factory.AddNewRib = lambda p, h: self._rib("AddNewRib", p, h, 1)
        factory.AddNewSlotFromRef = lambda p, h: self._rib("AddNewSlotFromRef", p, h, -1)
        factory.AddNewSlot = lambda p, h: self._rib("AddNewSlot", p, h, -1)
        factory.AddNewThreadWithRef = lambda lateral, limit: self._thread(
            "AddNewThreadWithRef", lateral, limit)
        factory.AddNewThreadWithOutRef = lambda: self._thread("AddNewThreadWithOutRef")
        factory.AddNewUserPattern = self._user_pattern
        factory.AddNewSymmetry2 = self._symmetry
        factory.AddNewRotate2 = lambda axis, angle: self._feature(
            "rotate", axis=axis, HybridShape=SimpleNamespace(AngleValue=angle))
        factory.AddNewTranslate2 = lambda distance: self._feature(
            "translate", HybridShape=SimpleNamespace(VectorType=0, Direction=None,
                                                     Distance=SimpleNamespace(Value=distance)))
        self.Relations = _TableRelations(self.tables)

    # Helices and ribs.

    def _helix(self, axis, invert, start, pitch, height, clockwise, angle, taper, outward):
        helix = _Helix(Name=f"Hélice.{len(self.helices) + 1}", kind="helix", axis=axis,
                       invert=invert, start=start, pitch=pitch, height=height,
                       clockwise=clockwise, angle=angle, taper=taper, outward=outward)
        self.helices.append(helix)
        return helix

    def helix_distance(self, helix, point):
        spec, e = helix.geometry(self.axis_reversed, self.clockwise_is_right)
        q = point[1]
        n = max(8, int(400 * spec.turns))
        ts = [spec.turns * i / n for i in range(n + 1)]
        best = min(ts, key=lambda t: math.dist(helix.point(spec, e, t), q))
        lo, hi = max(0.0, best - spec.turns / n), min(spec.turns, best + spec.turns / n)
        for _ in range(60):
            m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
            if math.dist(helix.point(spec, e, m1), q) < math.dist(helix.point(spec, e, m2), q):
                hi = m2
            else:
                lo = m1
        return math.dist(helix.point(spec, e, (lo + hi) / 2), q)

    def _rib(self, route, profile, helix, sign):
        if route in self.refused_ribs:
            raise RuntimeError(f"La méthode {route} a échoué")
        rib = _Rib(self, Name=f"{'Nervure' if sign > 0 else 'Rainure'}.{len(self.pads) + 1}",
                   kind="helixrib", route=route, sketch=profile, helix=helix, sign=sign,
                   body=self.InWorkObject)
        self.pads.append(rib)
        return rib

    def _ribbed_along_helix(self, rib):
        spec, _ = rib.helix.geometry(self.axis_reversed, self.clockwise_is_right)
        sketch = _tree_sketch(rib.sketch)
        if spec.tapered:
            import build123d

            from catia_mcp.tree.reference import sketch_faces

            edge = build123d.Edge.make_helix(spec.pitch, spec.height, spec.radius, spec.center,
                                             spec.direction, spec.cone_angle, spec.lefthand)
            solid = build123d.sweep(sketch_faces(sketch), path=build123d.Wire([edge]),
                                    is_frenet=True)
            cog = solid.center(build123d.CenterOf.MASS)
            volume, centre = solid.volume, (cog.X, cog.Y, cog.Z)
        else:
            swept = screw_sweep(sketch, spec)
            volume, centre = swept.volume, swept.cog
        if self.keep_angle_twists and rib.PullingDirElement is None:
            centre = add(centre, (0.0, 0.0, 0.5))  # another frame: elsewhere
        return volume, centre

    # Threads.

    def _thread(self, route, lateral=None, limit=None):
        if route in self.thread_refused:
            raise RuntimeError(f"La méthode {route} a échoué")
        value = lambda: SimpleNamespace(Value=0.0)
        thread = self._feature("thread", route=route, LateralFaceElement=lateral,
                               LimitFaceElement=limit, Side=0, Diameter=value(),
                               Depth=value(), Pitch=value(), reversed=False, locked=False)
        thread.ReverseDirection = lambda: setattr(thread, "reversed", not thread.reversed)
        thread.CreateStandardThreadDesignTable = lambda kind: self._table(thread, kind)
        return thread

    def _table(self, thread, kind):
        if self.thread_table is None:
            raise RuntimeError("La méthode CreateStandardThreadDesignTable a échoué")
        thread.table = kind
        self.tables.append(f"DesignTable.{len(self.tables) + 1}")
        if self.thread_table == "fits":
            thread.Diameter.Value, thread.Pitch.Value = 10.0, 1.5
        else:  # another pitch, locked while the table is there
            name, tables, free = self.tables[-1], self.tables, thread.Pitch

            class Locked:
                @property
                def Value(self):
                    return 1.25 if name in tables else free.Value

                @Value.setter
                def Value(self, value):
                    if name not in tables:
                        free.Value = value

            thread.Pitch = Locked()

    # User patterns and symmetries.

    def _user_pattern(self, seed, count):
        pattern = self._feature("userpattern", seed=seed, count=count, located=[],
                                AnchorPoint=None)
        pattern.AddFeatureToLocatePositions = pattern.located.append
        return pattern

    def _symmetry(self, reference):
        if self.symmetry_needs_later and reference != ("name", ""):
            raise RuntimeError("La méthode AddNewSymmetry2 a échoué")
        return self._feature("symmetry", HybridShape=SimpleNamespace(Reference=reference))

    @staticmethod
    def _world_points(sketch):
        origin, h, v = sketch.ref
        return [tuple(origin[i] + p.uv[0] * h[i] + p.uv[1] * v[i] for i in range(3))
                for p in sketch.factory.points]

    def _patterned_user(self, pattern):
        if self.user_pattern == "refused" or (self.user_pattern == "anchor"
                                              and pattern.AnchorPoint is None):
            raise RuntimeError("La mise à jour a échoué")
        (sketch,) = pattern.located
        points = self._world_points(sketch)
        if pattern.count != len(points):
            raise RuntimeError("La mise à jour a échoué")
        volume, cog = self._drilled(pattern.seed)
        (seed_at,) = self._world_points(pattern.seed.sketch)
        return [(-volume, add(cog, add(p, scale(seed_at, -1)))) for p in points]

    @staticmethod
    def _plane_of(reference):
        if reference[0] == "equation":
            _, normal, d = reference
            n = unit(normal)
            return scale(n, d / norm(normal)), n
        origin, h, v = reference
        return origin, cross(h, v)

    # Updates.

    def UpdateObject(self, obj):
        kind = getattr(obj, "kind", None)
        if kind == "helix":
            return
        if kind == "helixrib":
            self._onto(obj, self._ribbed_along_helix(obj), obj.sign)
            return
        if kind == "thread":
            if self.thread_runs_off and not obj.reversed:
                raise RuntimeError("La mise à jour a échoué : le filetage sort de la face")
            return
        if kind == "userpattern":
            if not hasattr(obj, "base"):
                obj.base = list(obj.body.solids)
            obj.body.solids = obj.base + self._patterned_user(obj)
            return
        if kind in ("symmetry", "rotate", "translate"):
            if not hasattr(obj, "base"):
                obj.base = list(obj.body.solids)
            obj.body.solids = [(v, self._moved(obj, c)) for v, c in obj.base]
            return
        super().UpdateObject(obj)

    def _moved(self, obj, c):
        if obj.kind == "symmetry":
            origin, n = self._plane_of(obj.HybridShape.Reference)
            return add(c, scale(n, -2 * dot(add(c, scale(origin, -1)), n)))
        if obj.kind == "rotate":
            _, origin, axis = obj.axis
            a, k = math.radians(obj.HybridShape.AngleValue), unit(axis)
            r = add(c, scale(origin, -1))
            return tuple(origin[i] + r[i] * math.cos(a) + cross(k, r)[i] * math.sin(a)
                         + k[i] * dot(k, r) * (1 - math.cos(a)) for i in range(3))
        d = unit(obj.HybridShape.Direction)
        return add(c, scale(d, obj.HybridShape.Distance.Value))

    def delete(self, feature):
        if getattr(feature, "kind", None) == "helix":
            self.deleted.append(feature)
            return
        super().delete(feature)


def _extra_builder(part, monkeypatch):
    built = _plus_builder(part, monkeypatch)
    monkeypatch.setattr(helix_com.HelixSweeps, "_distance",
                        lambda self, helix, point: part.helix_distance(helix, point))
    return built


def _entry(report, fid):
    return next(f for f in report["parts_detail"][0]["features"] if f["id"] == fid)


def _of(part, kind):
    return [f for f in part.pads if getattr(f, "kind", None) == kind]


# ── Sweeps along a helix ──


def test_a_spring_is_a_rib_along_a_gsd_helix_pulled_along_its_axis(monkeypatch):
    tree = load("m11_helix_spring")
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (helix,) = part.helices
    assert (helix.pitch, helix.height, helix.clockwise, helix.invert, helix.taper) == \
        (4.0, 10.0, False, False, 0.0)
    assert helix.start == ("point", (10.0, 0.0, 0.0)) and helix.axis[2] == (0.0, 0.0, 1.0)
    (rib,) = _of(part, "helixrib")
    assert rib.route == "AddNewRibFromRef" and rib.PullingDirElement is helix.axis
    assert rib.body is part.MainBody and rib.helix is helix
    entry = _entry(report, "sweep1")
    assert entry["catia"] == [helix.Name, rib.Name]
    assert entry["helix"]["built"]["measured"] and "on the tree's helix" in entry["notes"][0]
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        50 * math.pi ** 2, rel=1e-5)


def test_without_a_reference_a_spring_is_checked_by_its_closed_form(monkeypatch):
    tree = load("m11_helix_spring")
    part = _ExtraPart()
    want = {"/M11/HelixSpring": {"volume_mm3": 50 * math.pi ** 2}}
    report = _extra_builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    # A part whose helix comes out left-handed: the closed form's centre of
    # gravity (2.5 turns) tells it from the tree's.
    part = _ExtraPart(clockwise_is_right=True)
    built = _extra_builder(part, monkeypatch)
    monkeypatch.setattr(helix_com.HelixSweeps, "_distance",
                        lambda self, helix, point: (_ for _ in ()).throw(
                            RuntimeError("DistanceMinTo a échoué")))
    report = built.build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    first, kept = part.helices
    assert part.deleted[-1] is first and (kept.clockwise, kept.invert) == (True, False)
    entry = _entry(report, "sweep1")
    assert "not measured" in entry["notes"][0] and not entry["helix"]["built"]["measured"]
    assert any("added its solid around" in n for n in entry["notes"])


def test_a_helix_catia_turns_the_other_way_is_rebuilt_clockwise(monkeypatch):
    tree = load("m11_helix_spring")
    part = _ExtraPart(clockwise_is_right=True)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    first, kept = part.helices
    assert first in part.deleted and kept.clockwise and not kept.invert
    assert len(_of(part, "helixrib")) == 1  # the wrong helix never got a rib
    notes = _entry(report, "sweep1")["notes"]
    assert "misses a quarter turn in" in notes[-1]


def test_an_inverted_axis_is_found_by_the_helix_s_end(monkeypatch):
    tree = load("m11_helix_spring")
    part = _ExtraPart(axis_reversed=True)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    assert [(h.clockwise, h.invert) for h in part.helices] == \
        [(False, False), (True, False), (False, True)]


def test_a_rib_falls_back_to_the_objects_and_to_keep_angle(monkeypatch):
    tree = load("m11_helix_spring")
    part = _ExtraPart(refused_ribs={"AddNewRibFromRef"}, pulling_refused=True)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (rib,) = [r for r in _of(part, "helixrib") if r not in part.deleted]
    assert rib.route == "AddNewRib" and rib.PullingDirElement is None
    assert f"{rib.Name}: AddNewRib, keep angle" in _entry(report, "sweep1")["notes"]


def test_keep_angle_is_not_taken_when_it_moves_the_solid(monkeypatch):
    tree = load("m11_helix_spring")
    part = _ExtraPart(keep_angle_twists=True)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (rib,) = _of(part, "helixrib")
    assert rib.PullingDirElement is not None


def _inside_groove(tree):
    """The groove's V entirely inside the shaft: the fake does not clip."""
    sketch = _by_id(tree, "sketch2")
    half = 0.75 * T30
    points = [(-0.8, 0.0), (-0.05, -half), (-0.05, half)]
    sketch["entities"] = [{"type": "line", "start": list(points[i]),
                           "end": list(points[(i + 1) % 3]), "id": f"e{i}"} for i in range(3)]
    return tree


def test_a_modelled_thread_is_a_left_hand_slot_into_the_shaft(monkeypatch):
    tree = _inside_groove(load("m11_helix_thread_cut"))
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (helix,) = [h for h in part.helices if h not in part.deleted]
    assert helix.clockwise and helix.pitch == 1.5 and helix.height == pytest.approx(14.25)
    (slot,) = _of(part, "helixrib")
    assert slot.route == "AddNewSlotFromRef" and slot.body is part.MainBody
    groove = screw_sweep(_tree_sketch(slot.sketch), path_helix(
        _by_id(tree, "sweep1")["path"]))
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        math.pi * 36 * 30 - groove.volume, abs=1e-3)


def test_in_exact_style_a_helix_cut_is_a_checked_tool_body(monkeypatch):
    tree = _inside_groove(load("m11_helix_thread_cut"))
    groove = screw_sweep(_by_id(tree, "sketch2"), path_helix(_by_id(tree, "sweep1")["path"]))
    want = {"/M11/HelixThreadCut": {"volume_mm3": math.pi * 36 * 30 - groove.volume}}
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    (rib,) = _of(part, "helixrib")
    assert rib.route == "AddNewRibFromRef" and rib.body is not part.MainBody
    assert _entry(report, "sweep1")["catia"][-1].startswith("remove")


def test_a_tapered_spring_is_a_tapered_helix_checked_against_build123d(monkeypatch):
    tree = load("m11_helix_tapered_spring")
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (helix,) = part.helices
    assert helix.taper == 8 and helix.outward
    assert helix.pitch == pytest.approx(4 * math.cos(math.radians(8)))
    # Without build123d's body nothing checks the Rib but the part's volume.
    part = _ExtraPart()
    (props,) = _expected(tree).values()
    report = _extra_builder(part, monkeypatch).build(
        tree, {"/M11/HelixTaperedSpring": {"volume_mm3": props["volume_mm3"]}},
        check_editability=False)
    assert report["success"], report.get("error")
    assert any("no closed form" in n for n in _entry(report, "sweep1")["notes"])


def test_a_rib_catia_refuses_every_way_stops_the_build(monkeypatch):
    part = _ExtraPart(refused_ribs={"AddNewRibFromRef", "AddNewRib"})
    report = _extra_builder(part, monkeypatch).build(load("m11_helix_spring"),
                                                     check_editability=False)
    assert not report["success"]
    assert "no Rib along a CATIA helix" in report["error"]["message"]


# ── Cosmetic threads ──


def _shaft_faces(monkeypatch):
    """The stepped shaft's faces, as the topology census would measure them."""
    faces = [("shank", 2 * math.pi * 5 * 30, (0, 0, 25)), ("end", math.pi * 25, (0, 0, 40)),
             ("step", math.pi * 39, (0, 0, 10)), ("head", 2 * math.pi * 8 * 10, (0, 0, 5)),
             ("base", math.pi * 64, (0, 0, 0))]
    candidates = [FaceCandidate(SimpleNamespace(name=n), a, c) for n, a, c in faces]
    monkeypatch.setattr(DressUps, "face_candidates", lambda self, body, entry: candidates)
    monkeypatch.setattr(DressUps, "_face_reference",
                        lambda self, candidate, anchor, body, entry: candidate.handle)


def test_a_shaft_thread_is_one_cosmetic_thread_on_the_matched_faces(monkeypatch):
    _shaft_faces(monkeypatch)
    tree = load("m11_shaft_thread")
    part = _ExtraPart(thread_table="fits")
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (thread,) = _of(part, "thread")
    assert thread.route == "AddNewThreadWithRef" and thread.table == 1
    assert (thread.LateralFaceElement.name, thread.LimitFaceElement.name) == ("shank", "end")
    assert (thread.Diameter.Value, thread.Depth.Value, thread.Pitch.Value, thread.Side) == \
        (10.0, 15.0, 1.5, 0)
    record = _entry(report, "thread1")["thread"]
    assert record["standard"] == "Metric Thick Pitch" and record["route"] == "AddNewThreadWithRef"
    assert record["read_back"] == {"diameter": 10.0, "depth": 15.0, "pitch": 1.5, "side": 0}
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(1390 * math.pi, abs=1e-3)


def test_a_thread_table_that_changes_the_pitch_is_removed_for_no_standard(monkeypatch):
    _shaft_faces(monkeypatch)
    tree = load("m11_shaft_thread")
    part = _ExtraPart(thread_table="locks", thread_refused={"AddNewThreadWithRef"})
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (kept,) = _of(part, "thread")
    assert kept.route == "AddNewThreadWithOutRef" and kept.table == 1
    record = _entry(report, "thread1")["thread"]
    assert record["standard"] == "No Standard" and not part.tables
    assert record["tried"][0].startswith("AddNewThreadWithRef: La méthode")
    assert any(t.startswith("Metric Thick Pitch") and "1.25" in t for t in record["tried"])
    assert kept.Pitch.Value == 1.5 and kept.LateralFaceElement.name == "shank"


def test_a_thread_that_runs_off_its_face_is_reversed_once(monkeypatch):
    _shaft_faces(monkeypatch)
    tree = load("m11_shaft_thread")
    part = _ExtraPart(thread_runs_off=True)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (thread,) = _of(part, "thread")
    assert thread.reversed
    assert _entry(report, "thread1")["thread"]["standard"] == "No Standard"


def test_a_left_hand_fine_thread_sets_its_side_and_pitch(monkeypatch):
    _shaft_faces(monkeypatch)
    tree = load("m11_shaft_thread")
    _first(tree, "thread")["thread"] = {"designation": "M10x1", "side": "left"}
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (thread,) = _of(part, "thread")
    assert (thread.Side, thread.Pitch.Value) == (1, 1.0)


# ── User patterns of holes ──


def test_holes_at_no_grid_are_one_hole_and_a_user_pattern(monkeypatch):
    tree = load("m11_user_pattern_holes")
    part = _ExtraPart(through_depth=10.0)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (seed,) = _of(part, "hole")
    (pattern,) = _of(part, "userpattern")
    assert pattern.seed is seed and pattern.count == 3 and pattern.AnchorPoint is None
    # In the sketch's frame: the plane z = 10 offset from the XY plane.
    assert [p.uv for p in pattern.located[0].factory.points] == \
        [(5.0, -12.0), (18.0, 8.0), (-8.0, 12.0)]
    entry = _entry(report, "hole1")
    assert entry["catia"] == [seed.Name, pattern.Name]
    assert "one Hole and a user pattern on 3 sketch points" in entry["notes"]
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        24000 - 360 * math.pi, abs=1e-3)


def test_the_user_pattern_is_found_without_the_tree_asking(monkeypatch):
    tree = load("m11_user_pattern_holes")
    del _first(tree, "hole")["pattern"]
    part = _ExtraPart(through_depth=10.0)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    assert len(_of(part, "userpattern")) == 1


def test_a_user_pattern_catia_needs_anchored_gets_the_seed_as_anchor(monkeypatch):
    tree = load("m11_user_pattern_holes")
    part = _ExtraPart(through_depth=10.0, user_pattern="anchor")
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    first, kept = _of(part, "userpattern")
    assert first in part.deleted and kept.AnchorPoint == ("point", (-20.0, -10.0, 10.0))
    assert any("anchored at the seed" in n for n in _entry(report, "hole1")["notes"])


def test_a_user_pattern_catia_refuses_falls_back_to_one_hole_each(monkeypatch):
    # Live on R428 a user pattern of a Hole was refused on update.
    tree = load("m11_user_pattern_holes")
    part = _ExtraPart(through_depth=10.0, user_pattern="refused")
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    holes = [h for h in _of(part, "hole") if h not in part.deleted]
    assert len(holes) == 4 and len(_of(part, "userpattern")) == 3
    notes = _entry(report, "hole1")["notes"]
    assert any(n.startswith("as a Hole and a pattern: no User Pattern") for n in notes)


# ── Bodies moved or mirrored in place ──


def test_a_body_moved_in_place_is_a_rotate_and_a_translate_of_itself(monkeypatch):
    tree = load("m11_move_in_place")
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (rotate,) = _of(part, "rotate")
    (translate,) = _of(part, "translate")
    assert rotate.body is translate.body is part.MainBody
    assert rotate.HybridShape.AngleValue == pytest.approx(90)
    assert translate.HybridShape.Direction == (50.0, 0.0, 0.0)
    assert _entry(report, "move1")["catia"] == [rotate.Name, translate.Name]
    volume, _ = _closed_forms()["m11_move_in_place"]
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(volume, abs=1e-3)


def test_a_body_mirrored_in_place_is_a_symmetry_about_a_construction_plane(monkeypatch):
    tree = load("m11_mirror_in_place")
    part = _ExtraPart()
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (symmetry,) = _of(part, "symmetry")
    kind, normal, offset = symmetry.HybridShape.Reference
    assert kind == "equation" and unit(normal) == pytest.approx(unit((1, 1, 0)))
    assert offset == pytest.approx(40 / math.sqrt(2))
    (volume, cog) = _closed_forms()["m11_mirror_in_place"]
    mass = [s for s in part.MainBody.solids]
    total = sum(v for v, _ in mass)
    centre = tuple(sum(v * c[i] for v, c in mass) / total for i in range(3))
    assert total == pytest.approx(volume) and centre == pytest.approx(cog)


def test_a_symmetry_catia_wants_set_after_gets_its_plane_set_after(monkeypatch):
    tree = load("m11_mirror_in_place")
    part = _ExtraPart(symmetry_needs_later=True)
    report = _extra_builder(part, monkeypatch).build(tree, _expected(tree),
                                                     check_editability=False)
    assert report["success"], report.get("error")
    (symmetry,) = _of(part, "symmetry")
    assert symmetry.HybridShape.Reference[0] == "equation"
    assert "its plane set after" in _entry(report, "mirror1")["notes"][0]


def test_a_mirror_that_lands_elsewhere_stops_the_build(monkeypatch):
    tree = load("m11_mirror_in_place")
    _first(tree, "mirror_in_place")["plane"]["origin"] = [45, 0, 0]
    part = _ExtraPart()
    want = {"/M11/MirrorInPlace": {"volume_mm3": 3600.0}}
    monkeypatch.setattr(partdesign_extra_com, "mirrored_point",
                        lambda plane, point: (0.0, 0.0, 0.0))
    report = _extra_builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert not report["success"]
    assert "did not mirror the body in place" in report["error"]["message"]
