"""Surface features (catia_mcp/tree/surfaces.py, surface_builder.py,
surface_reference.py), offline.

The fake CATIA of test_datums.py is extended with GSD surfaces built by
build123d from what the builder drew and passed (the fake sketch's curves in
its own frame, the extrude's direction and limits, the revolve's axis line
and angles, ...), so the tests check the builder's COM arguments and its
choice among CATIA's undocumented senses, against the build123d reference.
Options reverse each sense the way an unknown CATIA convention could.
"""

from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import pytest
from test_datums import _PlanePart
from test_tree import _builder, _Sketch, _tree_sketch, load

pytest.importorskip("build123d")

from build123d import Axis, Face, Location, Shell, Solid, Vector, Wire

from catia_mcp.tree import requirements
from catia_mcp.tree.builder import TreeBuilder
from catia_mcp.tree.preflight import analyze
from catia_mcp.tree.reference import expected
from catia_mcp.tree.surface_reference import (
    _offset,
    _sewn,
    closed_solid,
    sketch_wire,
    split_by_plane,
    surface_properties,
)
from catia_mcp.tree.surfaces import chain, chain_length_centroid

SURFACE_FIXTURES = ("s1_extrude_curve_thicken", "s2_revolve_close", "s3_fill_join_close",
                    "s4_loft_split_offset_thicken", "s5_sweep_ribbon_thicken", "s6_surfaces_only")
CLOSED_FORM_MM3 = {
    "s1_extrude_curve_thicken": None,  # a thickened curved wall: build123d only
    "s2_revolve_close": math.pi * 15**2 * 25,
    "s3_fill_join_close": 20 * 10 * 15,
    "s5_sweep_ribbon_thicken": 6 * (30 + 15 * math.pi / 2) * 1,
    "s6_surfaces_only": 0.0,
}


def _wire_of(sketch):
    if hasattr(sketch, "shape"):  # a 3D curve the fake built (test_curves.py)
        return Wire.combine(sketch.shape.edges())[0]
    return sketch_wire({**_tree_sketch(sketch), "wire": True})


class _SurfacePart(_PlanePart):
    """GSD surfaces and Part Design solids from surfaces, computed with
    build123d. `extrude_reversed`, `revolve_reversed`, `offset_reversed`,
    `split_reversed_gsd` and `thick_reversed` make CATIA's sense the other
    one; `refuse` lists factory methods that raise."""

    def __init__(self, extrude_reversed=False, revolve_reversed=False, offset_reversed=False,
                 split_reversed_gsd=False, thick_reversed=False, refuse=(), no_set_sketches=False,
                 **kwargs):
        super().__init__(**kwargs)
        self.extrude_reversed, self.revolve_reversed = extrude_reversed, revolve_reversed
        self.offset_reversed, self.split_reversed_gsd = offset_reversed, split_reversed_gsd
        self.thick_reversed, self.refuse = thick_reversed, refuse
        self.gsd, self.geosets, self.set_sketches = [], [], []
        sets = self.geosets

        def add_set():
            geoset = SimpleNamespace(Name="", AppendHybridShape=lambda s: None)
            if not no_set_sketches:
                geoset.HybridSketches = SimpleNamespace(Add=self._set_sketch)
            sets.append(geoset)
            return geoset

        self.HybridBodies = SimpleNamespace(Count=0, Item=lambda i: sets[i - 1], Add=add_set)
        factory = self.HybridShapeFactory
        factory.AddNewDirectionByCoord = lambda x, y, z: ("direction", (x, y, z))
        factory.AddNewExtrude = lambda ref, l1, l2, d: self._gsd("extrude", ref=ref, l1=l1, l2=l2,
                                                                 direction=d[1])
        factory.AddNewRevol = lambda ref, a1, a2, axis: self._gsd("revol", ref=ref, a1=a1, a2=a2,
                                                                  axis=axis)
        factory.AddNewFill = lambda: self._fill()
        factory.AddNewOffset = lambda ref, d, flag, precision: self._gsd(
            "offset", ref=ref, value=d, flag=flag)
        factory.AddNewJoin = lambda r1, r2: self._join(r1, r2)
        factory.AddNewHybridSplit = lambda ref, cutter, o: self._gsd("hsplit", ref=ref,
                                                                     cutter=cutter, o=o)
        factory.AddNewLoft = lambda: self._gsd_loft()
        factory.AddNewSweepExplicit = lambda p, g: self._gsd("sweep", ref=p, guide=g)
        self.ShapeFactory.AddNewThickSurface = lambda ref, o, top, bottom: self._feature(
            "thicksurface", ref=ref, o=o, top=top, bottom=bottom)
        self.ShapeFactory.AddNewCloseSurface = lambda ref: self._feature("closesurface", ref=ref)

    def _set_sketch(self, ref):
        sketch = _Sketch(ref)
        self.set_sketches.append(sketch)
        return sketch

    def _gsd(self, kind, **fields):
        name = {"extrude": "AddNewExtrude", "revol": "AddNewRevol", "offset": "AddNewOffset",
                "hsplit": "AddNewHybridSplit", "sweep": "AddNewSweepExplicit"}.get(kind, kind)
        if name in self.refuse:
            raise RuntimeError(f"La méthode {name} a échoué")
        shape = SimpleNamespace(Name=f"{kind}.{len(self.gsd) + 1}", kind="gsd", op=kind, **fields)
        if kind == "extrude":
            shape.BeginOffset = SimpleNamespace(Value=fields["l1"])
            shape.EndOffset = SimpleNamespace(Value=fields["l2"])
        self.gsd.append(shape)
        return shape

    def _fill(self):
        fill = self._gsd("fill", bounds=[])
        fill.AddBound = fill.bounds.append
        return fill

    def _join(self, r1, r2):
        join = self._gsd("join", refs=[r1, r2])
        join.AddElement = join.refs.append
        return join

    def _gsd_loft(self):
        loft = self._gsd("loft", sections=[], SectionCoupling=1)
        loft.AddSectionToLoft = lambda ref, sense, closing: loft.sections.append(ref)
        return loft

    def _make(self, obj):
        op = obj.op
        if op == "extrude":
            d = tuple(c * (-1 if self.extrude_reversed else 1) for c in obj.direction)
            start = _wire_of(obj.ref).moved(Location(Vector(*(-obj.l2 * c for c in d))))
            return Shell.extrude(start, Vector(*((obj.l1 + obj.l2) * c for c in d)))
        if op == "revol":
            _, origin, direction = obj.axis
            sign = -1 if self.revolve_reversed else 1
            axis = Axis(origin, direction)
            wire = _wire_of(obj.ref).rotate(axis, -sign * obj.a2)
            return Shell.revolve(wire, sign * (obj.a1 + obj.a2), axis) if sign > 0 else \
                Shell.revolve(_wire_of(obj.ref).rotate(axis, obj.a2), obj.a1 + obj.a2,
                              Axis(origin, tuple(-c for c in direction)))
        if op == "fill":
            return Face(_wire_of(obj.bounds[0]))
        if op == "offset":
            flip = (obj.flag is True) != self.offset_reversed
            return _offset(obj.ref.shape, -obj.value if flip else obj.value)
        if op == "join":
            return _sewn([f for ref in obj.refs for f in ref.shape.faces()])
        if op == "hsplit":
            from build123d import Plane

            origin, h, v = obj.cutter
            normal = tuple(h[(i + 1) % 3] * v[(i + 2) % 3] - h[(i + 2) % 3] * v[(i + 1) % 3]
                           for i in range(3))
            keep_top = (obj.o == 1) != self.split_reversed_gsd
            return split_by_plane(obj.ref.shape, Plane(origin=origin, z_dir=normal), keep_top)
        if op == "loft":
            return Shell.make_loft([_wire_of(s) for s in obj.sections])
        if op == "sweep":
            return Shell.sweep(_wire_of(obj.ref), _wire_of(obj.guide))
        raise AssertionError(op)

    def UpdateObject(self, obj):
        kind = getattr(obj, "kind", None)
        if kind == "gsd":
            obj.shape = self._make(obj)
        elif kind == "thicksurface":
            sign = (1 if obj.o == 0 else -1) * (-1 if self.thick_reversed else 1)
            obj.body.solids = [_mass_of(Solid.thicken(obj.ref.shape, sign * obj.top))]
        elif kind == "closesurface":
            obj.body.solids = [_mass_of(closed_solid(obj.ref.shape))]
        else:
            super().UpdateObject(obj)

    def delete(self, feature):
        kind = getattr(feature, "kind", None)
        if kind == "gsd":
            self.gsd.remove(feature)
            return
        if kind in ("thicksurface", "closesurface"):
            self.deleted.append(feature)
            feature.body.solids = []
            return
        super().delete(feature)


def _mass_of(solid):
    centre = solid.center()
    return float(solid.volume), (centre.X, centre.Y, centre.Z)


def _build(name, part=None, monkeypatch=None, tree=None):
    part = part or _SurfacePart()
    tree = tree or load(name)
    built = _builder(part, monkeypatch)
    monkeypatch.setattr(TreeBuilder, "_surface_area",
                        lambda self, shape: surface_properties(shape.shape)["area_mm2"])
    monkeypatch.setattr(TreeBuilder, "_surface_cog",
                        lambda self, shape: tuple(surface_properties(shape.shape)["cog_mm"]))
    report = built.build(tree, expected(tree), check_editability=False)
    return part, report


# ── Contract and reference ──


def test_surface_fixtures_pass_preflight_and_their_reference_has_the_closed_form():
    for name in SURFACE_FIXTURES:
        tree = load(name)
        assert not analyze(tree), name
        props = expected(tree)[tree["parts"][0]["id"]]
        want = CLOSED_FORM_MM3.get(name)
        if want is not None:
            assert props["volume_mm3"] == pytest.approx(want, rel=1e-9, abs=1e-9), name


def test_reference_surface_areas_are_the_closed_forms():
    s1 = expected(load("s1_extrude_curve_thicken"))["/S1/Wall"]["surfaces"]["Wall"]
    assert s1["area_mm2"] == pytest.approx((20 + 10 * math.pi) * 30)
    s3 = expected(load("s3_fill_join_close"))["/S3/Box"]["surfaces"]
    assert s3["Tube"]["area_mm2"] == pytest.approx(60 * 15)
    assert s3["Floor"]["area_mm2"] == pytest.approx(200)
    assert s3["Skin"]["area_mm2"] == pytest.approx(1300)
    s6 = expected(load("s6_surfaces_only"))["/S6/Skins"]["surfaces"]
    assert s6["Quarter"]["area_mm2"] == pytest.approx(math.pi / 2 * 10 * 20)
    assert s6["Vault"]["area_mm2"] == pytest.approx(10 * math.pi * 16)
    assert s6["Vault"]["cog_mm"][2] == pytest.approx(20)
    s4 = expected(load("s4_loft_split_offset_thicken"))["/S4/Frustum"]["surfaces"]
    slant = math.hypot(4, 20)
    assert s4["Skin"]["area_mm2"] == pytest.approx(math.pi * 16 * slant)
    assert s4["Lower"]["area_mm2"] == pytest.approx(math.pi * 18 * slant / 2)


def test_a_curve_chain_is_ordered_and_measured():
    entities = [{"type": "line", "start": [10, 0], "end": [10, 5], "id": "b"},
                {"type": "line", "start": [0, 0], "end": [10, 0], "id": "a"}]
    ordered, closed = chain({"entities": entities})
    assert [e["id"] for e in ordered] == ["a", "b"] and not closed
    length, centroid = chain_length_centroid({"entities": entities})
    assert length == pytest.approx(15) and centroid == pytest.approx((20 / 3, 2.5 / 3))
    with pytest.raises(ValueError, match="one connected chain"):
        chain({"entities": entities + [{"type": "line", "start": [50, 50], "end": [60, 50]}]})


def test_preflight_refuses_malformed_surface_features():
    def refusals(edit):
        tree = copy.deepcopy(load("s3_fill_join_close"))
        edit(tree["parts"][0]["features"])
        return " | ".join(analyze(tree))

    assert "a fill needs a closed curve" in refusals(
        lambda f: f[0]["entities"].pop() and f[0].update(wire=True, profiles=[]))
    assert "join_surfaces must join two or more known surfaces" in refusals(
        lambda f: f[5].update(inputs=["Tube", "Nothing"]))
    assert "close_surface needs one known surface" in refusals(
        lambda f: f[6].update(inputs=["Nothing"]))
    assert "surface op 'blend' is not supported" in refusals(lambda f: f[3].update(op="blend"))
    assert "thicken needs a non-zero thickness" in refusals(
        lambda f: f.append({"type": "thicken", "id": "t", "inputs": ["Skin"], "thickness": 0,
                            "operation": "new_body", "creates": ["B2"]}))
    tree = load("s1_extrude_curve_thicken")
    tree["parts"][0]["features"].insert(1, {"type": "extrude", "id": "x", "sketch": "sketch1",
                                            "distance": 3, "operation": "new_body",
                                            "creates": ["B9"]})
    assert "a wire sketch (no closed profile) cannot make a solid" in " | ".join(analyze(tree))


def test_surfaces_need_gsd_surface():
    tree = load("s2_revolve_close")
    features = tree["parts"][0]["features"]
    sketches = {"sketch1": features[0]}
    assert requirements.SURFACE in requirements.feature_needs(features[1], sketches)
    assert requirements.SURFACE in requirements.feature_needs(features[2], sketches)


# ── Builder against the fake CATIA ──


@pytest.mark.parametrize("name", SURFACE_FIXTURES)
def test_surface_fixtures_build_and_match_build123d(monkeypatch, name):
    _, report = _build(name, monkeypatch=monkeypatch)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    assert detail["surfaces"], name
    for row in detail["surfaces"]:
        assert row["area_mm2"] == pytest.approx(row["expected_area_mm2"], rel=1e-3), row


def test_an_extruded_curve_is_a_named_surface_thickened_into_a_body(monkeypatch):
    part, report = _build("s1_extrude_curve_thicken", monkeypatch=monkeypatch)
    assert report["success"], report.get("error")
    (wall,) = part.gsd
    assert wall.Name == "Wall" and wall.direction == (0, 0, 1) and wall.l1 == 30
    assert part.geosets[-1].Name == "Surfaces" and part.set_sketches  # sketch in the set
    detail = report["parts_detail"][0]
    assert detail["features"][2]["catia"] == ["thicksurface.1"]
    # The script's amount drives the extrude's length.
    assert detail["parameters"][0]["name"] == "Extrude1_amount"
    assert part.Relations.formulas["Extrude1_amount_f1"] == (wall.BeginOffset, "Extrude1_amount")


@pytest.mark.parametrize("option", ["extrude_reversed", "thick_reversed"])
def test_catia_s_other_sense_is_found_by_the_centre(monkeypatch, option):
    _, report = _build("s1_extrude_curve_thicken", _SurfacePart(**{option: True}),
                          monkeypatch)
    assert report["success"], report.get("error")
    notes = " ".join(n for f in report["parts_detail"][0]["features"] for n in f.get("notes", []))
    assert ("against the direction" if option == "extrude_reversed" else "orientation 1") in notes


def test_a_partial_revolve_turns_the_tree_s_way(monkeypatch):
    part, report = _build("s6_surfaces_only", _SurfacePart(revolve_reversed=True), monkeypatch)
    assert report["success"], report.get("error")
    quarter = next(s for s in part.gsd if s.Name == "Quarter")
    assert (quarter.a1, quarter.a2) == (0.0, 90.0)
    assert report["parts_detail"][0]["volume_mm3"] == 0
    assert {r["name"] for r in report["parts_detail"][0]["construction"]} >= {"Axis Z"}


@pytest.mark.parametrize("option", ["offset_reversed", "split_reversed_gsd"])
def test_offsets_and_splits_keep_the_side_build123d_keeps(monkeypatch, option):
    _, report = _build("s4_loft_split_offset_thicken", _SurfacePart(**{option: True}),
                          monkeypatch)
    assert report["success"], report.get("error")
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        expected(load("s4_loft_split_offset_thicken"))["/S4/Frustum"]["volume_mm3"], rel=1e-6)


def test_sketches_go_in_the_main_body_when_the_set_takes_none(monkeypatch):
    part, report = _build("s3_fill_join_close", _SurfacePart(no_set_sketches=True), monkeypatch)
    assert report["success"], report.get("error")
    assert not part.set_sketches and part.MainBody.sketch_list
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(3000)


def test_a_surface_no_variant_builds_stops_the_build(monkeypatch):
    _, report = _build("s1_extrude_curve_thicken", _SurfacePart(refuse=("AddNewExtrude",)),
                          monkeypatch)
    assert not report["success"]
    assert report["error"]["feature"] == "surface1"
    assert "no way of building surface 'Wall'" in report["error"]["message"]


def test_without_a_reference_the_first_variant_is_kept_and_noted(monkeypatch):
    part = _SurfacePart()
    tree = load("s2_revolve_close")
    built = _builder(part, monkeypatch)
    monkeypatch.setattr(TreeBuilder, "_surface_area",
                        lambda self, shape: surface_properties(shape.shape)["area_mm2"])
    monkeypatch.setattr(TreeBuilder, "_surface_cog", lambda self, shape: None)
    report = built.build(tree, None, check_editability=False)
    assert report["success"], report.get("error")
    notes = [n for f in report["parts_detail"][0]["features"] for n in f.get("notes", [])]
    assert any("no build123d reference" in n for n in notes)
    assert report["parts_detail"][0]["surfaces"][0]["expected_area_mm2"] is None


def test_reference_planes_fixture_builds_with_one_plane_per_plane(monkeypatch):
    tree = load("m10_reference_planes")
    _, report = _build("m10_reference_planes", _SurfacePart(), monkeypatch, tree)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    planes = [r for r in detail["construction"] if r["type"] == "plane"]
    assert [p["name"] for p in planes] == ["Plane Z+20", "Plane XY +30deg about X"]
    assert planes[0]["used_by"] == ["extrude1", "extrude2"]
    assert planes[0]["parameter"] == "Sketch1_amount" and planes[1]["verified"] is True
    assert detail["volume_mm3"] == pytest.approx(expected(tree)["/M10/Planes"]["volume_mm3"],
                                                 rel=1e-6)


def test_wire_helpers_round_trip_through_the_fake():
    wire = Wire.make_polygon([(0, 0, 0), (10, 0, 0), (10, 5, 0)], close=False)
    assert Shell.extrude(wire, Vector(0, 0, 2)).area == pytest.approx(30)
