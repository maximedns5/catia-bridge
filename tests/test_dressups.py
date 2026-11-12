"""Offline tests of fillets, chamfers and shells in the tree replay (milestone 3).

The fake CATIA of tests/test_tree.py is extended with topology: every pad
gets the cells of its prism, a boolean's cells come from the test, and the
topology search from a body returns, as CATIA's ",sel" scope does, the cells
of the body's result *and* of the tool bodies nested under its booleans (the
"tool body cells after Remove" bug). Each cell's DisplayName names the
feature result it belongs to, as CATIA's do ("...;Remove.1_ResultOUT;...").
Fillets, chamfers and shells change the fake's volume and centre of gravity
by closed forms, and a fillet or chamfer given two edges of one tangent chain
fails, as CATIA's may.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree import dressup as dressup_module
from catia_mcp.tree.anchors import (
    AnchorError,
    EdgeCandidate,
    FaceCandidate,
    edge_from_points,
    match_edge,
    match_edges,
    match_face,
    representatives,
    tangent_chains,
)
from catia_mcp.tree.dressup import DressUps, canonical_brep_name, context_of, length_mm
from catia_mcp.tree.geometry import add, cross, dot, norm, scale, unit
from catia_mcp.tree.preflight import analyze
from tests.test_tree import _builder, _FakePart, _mass

TREES = Path(__file__).parent / "fixtures" / "trees"

# Volumes of the m3 fixtures: closed forms where there is one, else
# build123d's (OpenCascade) result.
M3_MM3 = {
    "m3_fillet_corners": (60 * 40 - (4 - math.pi) * 3**2) * 10,
    # Vertical fillet r 4, then a 0.5 chamfer around the top loop: straight
    # parts d^2/2 * l, arcs d^2/2 * theta * (R - d/3).
    "m3_chamfer_top_loop": (60 * 40 - (4 - math.pi) * 4**2) * 8
    - 0.5**2 / 2 * (2 * 52 + 2 * 32) - 0.5**2 / 2 * 2 * math.pi * (4 - 0.5 / 3),
    "m3_pocket_floor_fillet": 23012.492629,  # build123d: corner patches have no closed form
    "m3_asymmetric_chamfer": 40 * 30 * 10 - 2 * (3 * 1 / 2 * 40),
    "m3_shell_open_top": 40 * 30 * 20 - 36 * 26 * 18,
    "m3_shell_two_openings": 50 * 20 * 20 - 50 * 15 * 15,
}


def load(name: str) -> dict:
    return json.loads((TREES / f"{name}.json").read_text())


def sub(a, b):
    return add(a, scale(b, -1))


# ── Anchor matching (pure) ──


def _line(handle, start, end):
    length = norm(sub(end, start))
    return edge_from_points(handle, length, start, scale(add(start, end), 0.5), end)


def _anchor_line(start, end):
    s = [add(start, scale(sub(end, start), t)) for t in (0.2, 0.45, 0.7)]
    return {"type": "edge", "geometry": "line", "start": list(start), "end": list(end),
            "point": list(scale(add(start, end), 0.5)), "samples": [list(p) for p in s],
            "length": norm(sub(end, start))}


def test_an_edge_anchor_matches_by_length_then_samples_whatever_its_direction():
    a = _line("a", (0, 0, 0), (10, 0, 0))
    b = _line("b", (0, 5, 0), (10, 5, 0))  # same length, elsewhere
    c = _line("c", (0, 0, 0), (0, 12, 0))
    assert match_edge(_anchor_line((10, 0, 0), (0, 0, 0)), [b, c, a]) is a
    shifted = _anchor_line((0, 0, 2e-4), (10, 0, 2e-4))
    with pytest.raises(AnchorError, match="no edge of the body fits"):
        match_edge(shifted, [a, b, c])
    longer = _anchor_line((0, 0, 0), (10.001, 0, 0))
    with pytest.raises(AnchorError, match="1 of that length|0 of that length"):
        match_edge(longer, [a, b, c])


def test_two_equal_edges_are_ambiguous():
    a, twin = _line("a", (0, 0, 0), (10, 0, 0)), _line("twin", (0, 0, 0), (10, 0, 0))
    with pytest.raises(AnchorError, match="ambiguous: 2 edges"):
        match_edge(_anchor_line((0, 0, 0), (10, 0, 0)), [a, twin])
    with pytest.raises(AnchorError, match="same cell as an earlier anchor"):
        match_edges([_anchor_line((0, 0, 0), (10, 0, 0))] * 2, [a])


def test_arcs_and_closed_circles_are_rebuilt_from_three_measured_points():
    r, c = 4.0, (-26.0, -16.0, 4.0)
    at = lambda angle: (c[0] + r * math.cos(angle), c[1] + r * math.sin(angle), c[2])
    arc = edge_from_points("arc", math.pi * r / 2, at(-math.pi / 2), at(-3 * math.pi / 4), at(-math.pi))
    assert arc.radius == pytest.approx(r)
    anchor = {"type": "edge", "geometry": "circle", "length": math.pi * r / 2,
              "samples": [list(at(-math.pi / 2 - t * math.pi / 2)) for t in (0.2, 0.45, 0.7)]}
    assert match_edge(anchor, [arc]) is arc
    beyond = dict(anchor, samples=[list(at(0.3))])  # on the circle, off the arc
    with pytest.raises(AnchorError):
        match_edge(beyond, [arc])
    full = edge_from_points("full", 2 * math.pi * 3, (3, 0, 0), (-3, 0, 0), (3, 0, 0))
    assert full.distance((0, 3, 0)) == pytest.approx(0) and full.ends is None
    with pytest.raises(ValueError):
        length_mm(1.0, (0, 0, 0), (0, 0, 0), (0, 0, 0))
    assert length_mm(0.01, (0, 0, 0), (5, 0, 0), (10, 0, 0)) == pytest.approx(10)  # m -> mm
    assert length_mm(10.0, (0, 0, 0), (5, 0, 0), (10, 0, 0)) == pytest.approx(10)


def test_faces_match_by_area_then_nearest_centroid():
    top = FaceCandidate("top", 900.0, (0, 0, 10))
    bottom = FaceCandidate("bottom", 900.0, (0, 0, -10))
    side = FaceCandidate("side", 600.0, (15, 0, 0))
    anchor = {"type": "face", "geometry": "plane", "point": [0, 0, 10], "normal": [0, 0, 1],
              "area": 900.0}
    assert match_face(anchor, [side, bottom, top]) is top
    with pytest.raises(AnchorError, match="centred 20.0000 mm away"):
        match_face(anchor, [side, bottom])  # the top face is missing: do not take the bottom
    with pytest.raises(AnchorError, match="ambiguous"):
        match_face(dict(anchor, point=[0, 0, 0]), [top, bottom])
    with pytest.raises(AnchorError, match="none of that area"):
        match_face(dict(anchor, area=901.0), [top, bottom, side])


def test_tangent_chains_group_edges_through_unanchored_neighbours():
    # A rounded rectangle's loop: lines and quarter arcs, one chain.
    loop = _rounded_loop(60, 40, 4, 4.0, "o")
    edges = [_measure(cell) for cell in loop]
    anchored = [edges[0], edges[4]]  # two lines of the loop, not adjacent
    chains = tangent_chains(anchored, edges)
    assert len(chains) == 1 and len(chains[0].edges) == 8
    assert representatives(anchored, chains) == [edges[0]]
    square = [_line(i, p, q) for i, (p, q) in enumerate(
        [((0, 0, 0), (1, 0, 0)), ((1, 0, 0), (1, 1, 0)), ((1, 1, 0), (0, 1, 0))])]
    assert len(tangent_chains(square, square)) == 3


def test_brep_names_from_display_names():
    name = ("Selection_REdge:(Edge:(Face:(Brp:(Pad.1;2);None:();Cf11:());Face:(Brp:(Pad.1;0:"
            "(Brp:(Sketch.1;3)));None:();Cf11:());None:(Limits1:();Limits2:());Cf11:());"
            "Retrait.1_ResultOUT;Z0;G8782)")
    assert context_of(name) == "Retrait.1"
    assert canonical_brep_name(name) == (
        "REdge:(Edge:(Face:(Brp:(Pad.1;2);None:();Cf11:());Face:(Brp:(Pad.1;0:(Brp:(Sketch.1;3)));"
        "None:();Cf11:());None:(Limits1:();Limits2:());Cf11:());WithPermanentBody;"
        "WithoutBuildError;WithSelectingFeatureSupport;MFBRepVersion_CXR15)")
    assert context_of("Face.3") is None and canonical_brep_name("Face.3") is None


# ── Preflight ──


def test_m3_fixtures_pass_preflight():
    for name in M3_MM3:
        assert not analyze(load(name)), name


def _first(tree, kind):
    return next(f for f in tree["parts"][0]["features"] if f["type"] == kind)


def test_preflight_refuses_dressups_it_cannot_build():
    cases = []
    tree = load("m3_fillet_corners")
    _first(tree, "fillet")["radius"] = [1, 2]
    cases.append((tree, "variable-radius fillets are not supported yet"))
    tree = load("m3_fillet_corners")
    _first(tree, "fillet")["edges"][0]["geometry"] = "bspline"
    cases.append((tree, "fillets and chamfers on bspline edges are not supported yet"))
    tree = load("m3_fillet_corners")
    _first(tree, "fillet")["bodies"] = ["Body9"]
    cases.append((tree, "fillet must modify exactly one known body"))
    tree = load("m3_asymmetric_chamfer")
    del _first(tree, "chamfer")["face"]
    cases.append((tree, "an asymmetric chamfer needs the face its first distance is measured on"))
    tree = load("m3_asymmetric_chamfer")
    _first(tree, "chamfer")["face"]["point"][2] = 0.0  # the bottom: not holding the edges
    cases.append((tree, "an asymmetric chamfer's edges must all lie on its reference face"))
    tree = load("m3_asymmetric_chamfer")
    chamfer = _first(tree, "chamfer")
    chamfer.pop("distance2")
    chamfer["angle"] = 30
    cases.append((tree, "chamfers given by an angle are not supported yet"))
    tree = load("m3_shell_open_top")
    _first(tree, "shell")["faces"] = []
    cases.append((tree, "shells with no removed face (offset solids) are not supported yet"))
    tree = load("m3_shell_open_top")
    _first(tree, "shell")["join"] = "Kind.TANGENT"
    cases.append((tree, "shell join 'Kind.TANGENT' is not supported"))
    for tree, reason in cases:
        assert reason in analyze(tree), (reason, list(analyze(tree)))


def test_preflight_refuses_asymmetric_chamfers_at_a_three_edge_corner():
    tree = load("m3_asymmetric_chamfer")
    chamfer = _first(tree, "chamfer")
    corner = chamfer["edges"][0]["start"]
    for end in ([corner[0], corner[1] + 30 * (1 if corner[1] < 0 else -1), corner[2]],
                [corner[0], corner[1], corner[2] - 10]):
        chamfer["edges"].append(dict(_anchor_line(tuple(corner), tuple(end)), geometry="line"))
    chamfer["edges"][-1]["start"] = corner
    assert any("three edges meeting at a corner" in reason for reason in analyze(tree))


# ── Reference (build123d) ──


def test_reference_reproduces_the_m3_fixtures():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected

    for name, volume in M3_MM3.items():
        (props,) = expected(load(name)).values()
        assert props["volume_mm3"] == pytest.approx(volume, rel=1e-9), name


def test_the_chamfer_orientation_rule_holds_in_opencascade():
    """distance2 < distance on the face: the tree's way round puts the centre
    of gravity lower along the face normal than the swapped one."""
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import build_part

    tree = load("m3_asymmetric_chamfer")
    part = tree["parts"][0]
    chamfer = _first(tree, "chamfer")
    right = build_part(part).center().Z
    chamfer["distance"], chamfer["distance2"] = chamfer["distance2"], chamfer["distance"]
    swapped = build_part(part).center().Z
    assert right < swapped


def test_reference_refuses_an_anchor_it_cannot_find():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import ReferenceError, expected

    tree = load("m3_fillet_corners")
    _first(tree, "fillet")["edges"][0]["length"] = 9.0
    with pytest.raises(ReferenceError, match="fillet fillet1: anchor 1: no edge"):
        expected(tree)


# ── Fake CATIA with topology ──


class _Cell:
    """A fake topology cell (also its own Reference)."""

    serial = 0

    def __init__(self, kind, owner, names=True, **geometry):
        _Cell.serial += 1
        self.kind, self.owner, self.id = kind, owner, _Cell.serial
        self.__dict__.update(geometry)
        self.DisplayName = (f"Selection_R{'Edge' if kind == 'Edge' else 'Sur'}:(Brp:(c{self.id}));"
                            f"{owner}_ResultOUT;Z0;G1)") if names else f"{kind}.{self.id}"

    def owned_by(self, owner):
        geometry = {k: v for k, v in self.__dict__.items()
                    if k not in ("kind", "owner", "id", "DisplayName")}
        return _Cell(self.kind, owner, "_ResultOUT" in self.DisplayName, **geometry)


def _edge_cell(owner, start, end, faces, convex=1, chain=None, names=True):
    return _Cell("Edge", owner, names, start=tuple(start), end=tuple(end),
                 mid=scale(add(start, end), 0.5), length=norm(sub(end, start)), center=None,
                 faces=faces, convex=convex, chain=chain, arc=None)


def _arc_cell(owner, center, radius, a0, a1, z, chain, names=True):
    at = lambda a: (center[0] + radius * math.cos(a), center[1] + radius * math.sin(a), z)
    return _Cell("Edge", owner, names, start=at(a0), end=at(a1), mid=at((a0 + a1) / 2),
                 length=radius * abs(a1 - a0), center=None, faces=None, convex=1, chain=chain,
                 arc=(radius, abs(a1 - a0)))


def _face_cell(owner, area, centroid, normal, names=True):
    return _Cell("Face", owner, names, area=area, centroid=tuple(centroid), normal=tuple(normal))


def _measure(cell):
    return edge_from_points(cell, cell.length, cell.start, cell.mid, cell.end)


def _prism_cells(points, n, low, high, owner, names=True):
    """Cells of a straight prism: polygon `points` (counter-clockwise about
    n) swept from `low` to `high` along n."""
    area = sum(dot(cross(points[i], points[(i + 1) % len(points)]), n)
               for i in range(len(points))) / 2
    if area < 0:
        points, area = points[::-1], -area
    centroid = scale(add(points[0], points[0]), 0)
    for p in points:
        centroid = add(centroid, scale(p, 1 / len(points)))
    lo = [add(p, scale(n, low)) for p in points]
    hi = [add(p, scale(n, high)) for p in points]
    height = high - low
    cells = [_face_cell(owner, area, add(centroid, scale(n, low)), scale(n, -1), names),
             _face_cell(owner, area, add(centroid, scale(n, high)), n, names)]
    for i in range(len(points)):
        j = (i + 1) % len(points)
        out = unit(cross(sub(points[j], points[i]), n))
        cells.append(_face_cell(owner, norm(sub(points[j], points[i])) * height,
                                add(scale(add(lo[i], hi[j]), 0.5), (0, 0, 0)), out, names))
        cells.append(_edge_cell(owner, lo[i], lo[j], (scale(n, -1), out), names=names))
        cells.append(_edge_cell(owner, hi[i], hi[j], (n, out), names=names))
        prev = unit(cross(sub(points[i], points[i - 1]), n))
        cells.append(_edge_cell(owner, lo[i], hi[i], (prev, out), names=names))
    return cells


def _rounded_loop(w, h, r, z, chain, owner="o", names=True):
    """A rounded rectangle's loop at height z: 4 lines and 4 quarter arcs."""
    x, y = w / 2, h / 2
    cap = (0.0, 0.0, math.copysign(1.0, z))

    def line(start, end, out):
        return _edge_cell(owner, start, end, (cap, out), chain=chain, names=names)

    return [
        line((-x + r, -y, z), (x - r, -y, z), (0, -1, 0)),
        _arc_cell(owner, (x - r, -y + r), r, -math.pi / 2, 0, z, chain, names),
        line((x, -y + r, z), (x, y - r, z), (1, 0, 0)),
        _arc_cell(owner, (x - r, y - r), r, 0, math.pi / 2, z, chain, names),
        line((x - r, y, z), (-x + r, y, z), (0, 1, 0)),
        _arc_cell(owner, (-x + r, y - r), r, math.pi / 2, math.pi, z, chain, names),
        line((-x, y - r, z), (-x, -y + r, z), (-1, 0, 0)),
        _arc_cell(owner, (-x + r, -y + r), r, math.pi, 3 * math.pi / 2, z, chain, names),
    ]


class _Shapes:
    def __init__(self, body):
        self.body = body

    @property
    def Count(self):
        return len(self.body.shape_list)

    def Item(self, i):
        return self.body.shape_list[i - 1]


class _Selection:
    """Selection.Search over the fake topology, French queries only, ",sel"
    scope including the tool bodies nested under booleans."""

    def __init__(self, part):
        self.part, self.items, self.queries = part, [], []

    def Clear(self):
        self.items = []

    def Add(self, obj):
        self.items.append(obj)

    def Delete(self):
        for feature in self.items:
            self.part.delete(feature)

    def Search(self, query):
        self.queries.append(query)
        match = re.fullmatch(r"Topologie\.(Face|Arête),sel", query)
        if not match:
            raise RuntimeError("La méthode Search a échoué")
        kind = "Face" if match.group(1) == "Face" else "Edge"
        (root,) = self.items
        self.items = [SimpleNamespace(Reference=cell) for cell in _visible(root) if cell.kind == kind]

    @property
    def Count(self):
        return len(self.items)

    def Item(self, i):
        return self.items[i - 1]


def _visible(body):
    cells = list(body.shape_list[-1].cells) if body.shape_list else []
    for shape in body.shape_list:
        if getattr(shape, "tool", None) is not None:
            cells += _visible(shape.tool)
    return cells


class _TopoPart(_FakePart):
    """The fake part of test_tree with topology, dress-ups and BRep names."""

    def __init__(self, booleans=None, after_dressup=None, names=True, native_first_face=0,
                 brep_names=True, **kwargs):
        super().__init__(**kwargs)
        self.booleans, self.after_dressup = booleans or {}, after_dressup or {}
        self.names, self.native_first_face, self.brep_names = names, native_first_face, brep_names
        self.cells_by_id = {}
        self._wrap(self.MainBody)
        add_body = self.Bodies.Add
        self.Bodies.Add = lambda: self._wrap(add_body())
        self.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius = self._fillet
        self.ShapeFactory.AddNewChamfer = self._chamfer
        self.ShapeFactory.AddNewShell = self._shell
        self.brep_calls = []

    @staticmethod
    def _wrap(body):
        body.shape_list = []
        body.Shapes = _Shapes(body)
        return body

    def CreateReferenceFromBRepName(self, label, context):
        self.brep_calls.append((label, context))
        if not self.brep_names:
            raise RuntimeError("CreateReferenceFromBRepName a échoué")
        assert label.endswith(dressup_module.BREP_FLAGS) and "_ResultOUT" not in label
        return self.cells_by_id[int(re.search(r"Brp:\(c(\d+)\)", label).group(1))]

    # Dress-up features: the edges or faces are kept and applied on update.

    def _fillet(self, ref, propagation, radius):
        assert propagation == 1  # catTangencyFilletEdgePropagation
        feature = self._feature("fillet", refs=[ref], radius=radius)
        feature.AddObjectToFillet = feature.refs.append
        return feature

    def _chamfer(self, ref, propagation, mode, orientation, first, second):
        assert (propagation, mode) == (0, 0)  # tangency, two lengths
        feature = self._feature("chamfer", refs=[ref], Orientation=orientation, lengths=(first, second))
        feature.AddElementToChamfer = feature.refs.append
        return feature

    def _shell(self, ref, inside, outside):
        feature = self._feature("shell", refs=[ref], thickness=(inside, outside))
        feature.AddFaceToRemove = feature.refs.append
        return feature

    def UpdateObject(self, obj):
        kind = getattr(obj, "kind", None)
        body = getattr(obj, "body", None) or getattr(obj, "target", None)
        if kind in ("fillet", "chamfer", "shell"):
            if not hasattr(obj, "before"):
                obj.before = list(body.solids)
                obj.cells_before = list(body.shape_list[-1].cells)
            body.solids = obj.before + getattr(self, f"_{kind}_solids")(obj)
            cells = self.after_dressup.get(kind, lambda cells, owner: [c.owned_by(owner) for c in cells])
            obj.cells = cells(obj.cells_before, obj.Name)
        else:
            super().UpdateObject(obj)
            if body is None and not hasattr(obj, "length"):
                return  # sketches, planes, points
            if hasattr(obj, "length"):
                obj.cells = self._pad_cells(obj)
            elif hasattr(obj, "tool"):
                rule = self.booleans.get(kind, lambda target, tool, owner: [
                    c.owned_by(owner) for c in target.shape_list[-1].cells])
                obj.cells = rule(obj.target, obj.tool, obj.Name)
            else:
                obj.cells = [c.owned_by(obj.Name) for c in body.shape_list[-1].cells] \
                    if body is not None and body.shape_list else []
        if body is not None and all(obj is not s for s in body.shape_list):
            body.shape_list.append(obj)
        for cell in obj.cells:
            self.cells_by_id[cell.id] = cell

    def _pad_cells(self, pad):
        origin, h, v = pad.sketch.ref
        n = cross(h, v)
        points = [add(origin, add(scale(h, e["start"][0]), scale(v, e["start"][1])))
                  for e in pad.sketch.factory.entities]
        side = -1 if (pad.DirectionOrientation == 1) != self.pads_reversed else 1
        low, high = (-pad.length, pad.length) if pad.IsSymmetric else sorted((0, side * pad.length))
        # Points relative to the origin, swept from the sketch plane.
        return _prism_cells(points, n, low, high, pad.Name, self.names)

    def _chains(self, refs):
        """Tangency propagation: each given edge brings its chain; two edges of
        one chain are refused, as CATIA may refuse redundant edges."""
        chains = [r.chain or r.id for r in refs]
        if len(set(chains)) != len(chains):
            raise RuntimeError("Arête redondante")
        cells = self.cells_by_id.values()
        return [c for c in cells if c.kind == "Edge" and c.chain is not None and c.chain in chains
                and c.owner == refs[0].owner] + [r for r in refs if r.chain is None]

    def _fillet_solids(self, fillet):
        r = fillet.radius
        return [(-edge.convex * (1 - math.pi / 4) * r * r * edge.length, edge.mid)
                for edge in self._chains(fillet.refs)]

    def _chamfer_solids(self, chamfer):
        solids = []
        for edge in self._chains(chamfer.refs):
            d1, d2 = chamfer.lengths
            if edge.arc:
                radius, angle = edge.arc
                solids.append((-d1 * d2 / 2 * angle * (radius - d1 / 3), edge.mid))
                continue
            # Length1 on the edge's native first face, or the other with
            # Orientation reversed; prism centroid inset by d/3 across each face.
            on_first = (chamfer.Orientation == 0) == (self.native_first_face == 0)
            first, second = edge.faces if on_first else edge.faces[::-1]
            centroid = add(edge.mid, add(scale(second, -d1 / 3), scale(first, -d2 / 3)))
            solids.append((-edge.convex * d1 * d2 / 2 * edge.length, centroid))
        return solids

    def _shell_solids(self, shell):
        """Axis-aligned box bodies only: the cavity is the box shrunk by the
        thickness on every face kept."""
        inside, _ = shell.thickness
        faces = [c for c in shell.cells_before if c.kind == "Face"]
        lo = [min(c.centroid[i] for c in faces) for i in range(3)]
        hi = [max(c.centroid[i] for c in faces) for i in range(3)]
        open_normals = [r.normal for r in shell.refs]
        box_lo, box_hi = list(lo), list(hi)
        for i in range(3):
            axis = tuple(1.0 if k == i else 0.0 for k in range(3))
            if not any(dot(axis, nrm) > 0.99 for nrm in open_normals):
                box_hi[i] -= inside
            if not any(dot(axis, nrm) < -0.99 for nrm in open_normals):
                box_lo[i] += inside
        volume = math.prod(b - a for a, b in zip(box_lo, box_hi, strict=True))
        return [(-volume, tuple((a + b) / 2 for a, b in zip(box_lo, box_hi, strict=True)))]


def _topo_builder(part, monkeypatch, **dressups):
    built = _builder(part, monkeypatch)
    built.conn.hso = _Selection(part)
    built.dressups = DressUps(built, **dressups)
    monkeypatch.setattr(DressUps, "_edge_geometry",
                        lambda self, ref: (ref.length, ref.start, ref.mid, ref.end, ref.center))
    monkeypatch.setattr(DressUps, "_face_geometry", lambda self, ref: (ref.area, ref.centroid))
    monkeypatch.setattr(DressUps, "_area", lambda self, body: sum(
        c.area for c in body.shape_list[-1].cells if c.kind == "Face"))
    return built


def _build(name, part, monkeypatch, expect=True, **dressups):
    tree = load(name)
    want = {tree["parts"][0]["id"]: {"volume_mm3": M3_MM3[name]}} if expect else None
    return _topo_builder(part, monkeypatch, **dressups).build(tree, want, check_editability=False)


def _entry(report, kind):
    return next(f for f in report["parts_detail"][0]["features"] if f["type"] == kind)


def _of(part, kind):
    return [f for f in part.pads if getattr(f, "kind", None) == kind]


# ── Builder ──


def test_a_fillet_is_one_catia_feature_on_the_matched_edges(monkeypatch):
    part = _TopoPart()
    report = _build("m3_fillet_corners", part, monkeypatch)
    assert report["success"], report.get("error")
    (fillet,) = _of(part, "fillet")
    assert len(fillet.refs) == 4 and all(abs(r.start[2] - r.end[2]) == 10 for r in fillet.refs)
    entry = _entry(report, "fillet")
    assert entry["catia"] == [fillet.Name]
    assert entry["references"] == {"brep_name": 4, "selection": 0}
    (census,) = entry["topology"]
    assert census["found"] == census["kept"] == 12 and census["contexts"] == {"Extrusion.1": 12}
    # Every reference was resolved in the context of the body's last feature.
    assert {context.Name for _, context in part.brep_calls} == {"Extrusion.1"}
    assert part.MainBody.shape_list[-1] is fillet


def test_a_chamfer_on_a_tangent_loop_passes_one_edge_per_chain(monkeypatch):
    def rounded(cells, owner):
        """The block after its vertical fillet: rounded loops at top and
        bottom (its vertical edges are left out)."""
        loops = _rounded_loop(60, 40, 4, 4.0, "top", owner) + \
            _rounded_loop(60, 40, 4, -4.0, "bottom", owner)
        return loops + [c.owned_by(owner) for c in cells if c.kind == "Face"]

    part = _TopoPart(after_dressup={"fillet": rounded})
    report = _build("m3_chamfer_top_loop", part, monkeypatch)
    assert report["success"], report.get("error")
    (chamfer,) = _of(part, "chamfer")
    assert len(chamfer.refs) == 1
    entry = _entry(report, "chamfer")
    assert "8 anchored edge(s) in 1 tangent chain(s) of 8 edge(s)" in entry["notes"][0]
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(M3_MM3["m3_chamfer_top_loop"])


def _pocket(target, tool, owner):
    """Block minus a pocket tool inside it: the block's cells and the
    pocket's walls, floor and edges (the floor edges concave)."""
    cells = [c.owned_by(owner) for c in target.shape_list[-1].cells]
    for cell in tool.shape_list[-1].cells:
        if cell.kind == "Face" and cell.centroid[2] > 9.999:
            continue  # the tool's top is open in the block
        mine = cell.owned_by(owner)
        if cell.kind == "Edge" and cell.start[2] < 5.001 and cell.end[2] < 5.001:
            mine.convex = -1
        cells.append(mine)
    return cells


def test_the_ownership_filter_drops_the_tool_body_cells_a_remove_leaves_in_the_search(monkeypatch):
    """The pocket's floor edges are also the tool body's own edges, which the
    body search returns after the Remove: without the filter every floor edge
    matches twice."""
    part = _TopoPart(booleans={"remove": _pocket})
    report = _build("m3_pocket_floor_fillet", part, monkeypatch, expect=False)
    assert report["success"], report.get("error")
    edges = next(c for c in _entry(report, "fillet")["topology"] if c["kind"] == "Edge")
    assert edges["found"] == 36 and edges["kept"] == 24
    assert edges["contexts"] == {"remove.2": 24, "Extrusion.2": 12}
    (fillet,) = _of(part, "fillet")
    assert {r.owner for r in fillet.refs} == {"remove.2"} and len(fillet.refs) == 4
    # Concave floor edges: the fillet adds (1 - pi/4) r^2 per mm.
    assert report["parts_detail"][0]["volume_mm3"] == pytest.approx(
        23000 + (1 - math.pi / 4) * 60, rel=1e-6)

    part = _TopoPart(booleans={"remove": _pocket})
    report = _build("m3_pocket_floor_fillet", part, monkeypatch, expect=False, ownership="off")
    assert not report["success"]
    assert "ambiguous: 2 edges fit it equally well" in report["error"]["message"]
    assert report["error"]["feature"] == "fillet1"


def test_uninformative_names_skip_the_filter_unless_strict(monkeypatch):
    part = _TopoPart(names=False)
    report = _build("m3_fillet_corners", part, monkeypatch)
    assert report["success"], report.get("error")
    entry = _entry(report, "fillet")
    assert "filter not applied" in entry["topology"][0]["note"]
    assert entry["references"] == {"brep_name": 0, "selection": 4}
    part = _TopoPart(names=False)
    report = _build("m3_fillet_corners", part, monkeypatch, ownership="strict")
    assert "the topology ownership filter kept nothing" in report["error"]["message"]


def test_a_brep_name_catia_refuses_falls_back_to_the_selection_reference(monkeypatch):
    part = _TopoPart(brep_names=False)
    report = _build("m3_fillet_corners", part, monkeypatch)
    assert report["success"], report.get("error")
    entry = _entry(report, "fillet")
    assert entry["references"] == {"brep_name": 0, "selection": 4}
    assert all("selection reference" in note for note in entry["notes"])


@pytest.mark.parametrize("native_first_face", [0, 1])
def test_an_asymmetric_chamfer_keeps_the_orientation_its_centre_of_gravity_shows(
        monkeypatch, native_first_face):
    """3 mm on the top face, 1 mm down the sides. Not reversed, the fake
    puts Length1 on the top (0) or down the sides (1)."""
    part = _TopoPart(native_first_face=native_first_face)
    tree = load("m3_asymmetric_chamfer")
    want = {"/M3/AsymmetricChamfer": {"volume_mm3": M3_MM3["m3_asymmetric_chamfer"],
                                      "cog_mm": [0.0, 0.0, 4.952862]}}
    report = _topo_builder(part, monkeypatch).build(tree, want, check_editability=False)
    assert report["success"], report.get("error")
    (chamfer,) = _of(part, "chamfer")
    assert chamfer.lengths == (3.0, 1.0)
    assert _mass(part.MainBody.solids)[1][2] == pytest.approx(4.952862, abs=1e-6)
    reversed_ = chamfer.Orientation == 1
    assert reversed_ == (native_first_face == 1)
    assert any("orientation reversed" in n for n in _entry(report, "chamfer").get("notes", [])) \
        == reversed_


def test_the_part_centre_of_gravity_is_checked_after_an_asymmetric_chamfer(monkeypatch):
    part = _TopoPart()
    want = {"/M3/AsymmetricChamfer": {"volume_mm3": M3_MM3["m3_asymmetric_chamfer"],
                                      "cog_mm": [0.0, 0.0, 4.9]}}
    report = _topo_builder(part, monkeypatch).build(load("m3_asymmetric_chamfer"), want,
                                                   check_editability=False)
    assert "centre of gravity is at" in report["error"]["message"]


@pytest.mark.parametrize("name", ["m3_shell_open_top", "m3_shell_two_openings"])
def test_shells_remove_the_matched_faces_with_the_wall_inside(monkeypatch, name):
    part = _TopoPart()
    report = _build(name, part, monkeypatch)
    assert report["success"], report.get("error")
    (shell,) = _of(part, "shell")
    thickness = -_first(load(name), "shell")["thickness"]
    assert shell.thickness == (thickness, 0.0)
    assert [r.normal for r in shell.refs] in ([(0, 0, 1)], [(1, 0, 0), (-1, 0, 0)],
                                              [(-1, 0, 0), (1, 0, 0)])


def test_a_shell_outside_its_bounds_stops_the_build(monkeypatch):
    part = _TopoPart()
    tree = load("m3_shell_open_top")
    _first(tree, "shell")["thickness"] = -30  # the fake's cavity turns inside out
    report = _topo_builder(part, monkeypatch).build(tree, check_editability=False)
    assert not report["success"]
    assert "expected between 0 and" in report["error"]["message"]


def test_a_missing_edge_is_reported_with_its_anchor(monkeypatch):
    part = _TopoPart()
    tree = load("m3_fillet_corners")
    _first(tree, "fillet")["edges"][2]["length"] = 11.0
    report = _topo_builder(part, monkeypatch).build(tree, check_editability=False)
    message = report["error"]["message"]
    assert report["error"]["feature"] == "fillet1"
    assert message.startswith("anchor 3: no edge of the body fits the line edge of length 11.0000")
    assert "topology census" in message
    assert not _of(part, "fillet")


def test_dressups_use_the_type_library_enums_when_readable(monkeypatch):
    values = {"CatChamferMode": {"catTwoLengthChamfer": 3, "catLengthAngleChamfer": 4},
              "CatChamferOrientation": {"catNoReverseChamfer": 7, "catReverseChamfer": 8}}
    monkeypatch.setattr(dressup_module, "find_enum", lambda objs, name: values.get(name))
    built = DressUps(SimpleNamespace(part=SimpleNamespace(ShapeFactory=None),
                                     conn=SimpleNamespace(app=None)))
    assert built._enum("CatChamferMode", "twolength") == 3
    assert built._enum("CatChamferOrientation", "reverse") == 8
    assert built._enum("CatChamferOrientation", "noreverse") == 7
    assert built._enum("CatFilletEdgePropagation", "tangency") == 1  # declared order
    with pytest.raises(ValueError):
        DressUps(None, ownership="sometimes")


def test_unused_candidate_types_are_plain_geometry():
    candidate = EdgeCandidate("x", 1.0, lambda p: 0.0)
    assert candidate.ends is None and candidate.radius is None


# ── Live probe script (offline parts) ──


def _probe_module():
    import importlib.util

    path = Path(__file__).parent.parent / "scripts" / "probe_topology_ownership.py"
    spec = importlib.util.spec_from_file_location("probe_topology_ownership", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_cases_pass_preflight_and_compare_with_the_reference():
    probe = _probe_module()
    trees = probe.case_trees()
    assert set(trees) == {"block", "boolean_remove", "boolean_remove_pocket", "boolean_add"}
    for tree in trees.values():
        assert not analyze(tree)
    pytest.importorskip("build123d")
    # The U-notched block's 24 edges, plus 2 foreign ones (a tool body's).
    tree = trees["boolean_remove"]
    from catia_mcp.tree.reference import build_part

    solid = build_part(tree["parts"][0])
    cells = [SimpleNamespace(index=i, context="Remove.1") for i in range(len(solid.edges()))]
    candidates = [_line(cell, tuple(e.position_at(0)), tuple(e.position_at(1)))
                  for cell, e in zip(cells, solid.edges(), strict=True)]
    candidates += [_line(SimpleNamespace(index=100 + i, context="Pad.2"), (40, -10, z), (40, 10, z))
                   for i, z in enumerate((-15, 35))]
    result = probe.compare_with_reference(tree, candidates)
    assert result["reference_edges"] == 24 and len(result["matched"]) == 24
    assert [f["index"] for f in result["foreign"]] == [100, 101] and not result["missing"]
