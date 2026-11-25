"""The drawing a designer would make of a built part or assembly.

Pure and offline: no CATIA. From a part of the tree (in world coordinates,
after `place_part`) and its geometry (the build123d reference's bounding box
and planar faces, or an estimate from the features), it chooses:

- the main (front) view: the world axis the most features are seen along
  (holes drilled along it, sketches drawn normal to it), then the largest
  planar faces, then the largest projected area; the part stays upright (Z
  up) when Z is in the view, a revolution axis lies horizontal;
- the projected views needed (top, side), in first angle (ISO E, default)
  or third angle;
- a section through the hole axes or the revolution axis when the part has
  internal features, in the slot of the projection it replaces;
- a detail view when a feature is too small to read at the sheet's scale;
- an isometric view;
- the sheet (ISO A4 to A0, landscape) and the scale (ISO 5455 standard
  scales), the smallest sheet showing the part at 1:1 or larger, else at
  1:2, 1:5..., with the views placed without overlap, clear of the title
  block (ISO 7200, 180 mm wide) inside the frame (ISO 5457 margins);
- the title block's fields, the projection symbol, hole and thread callouts
  (ISO 129 symbols), centre marks and axis lines;
- an auxiliary view along an inclined face's normal, a stepped or aligned
  section through hole axes that are not on one line;
- further sheets when the views do not fit the largest allowed sheet at the
  scale the orthographic views alone reach there.

For an assembly: a front and an isometric view of the product, a bill of
materials (one row per part, with its quantity) above the title block and
one balloon per row; then one sheet per distinct part.

Sheet coordinates are millimetres of paper from the sheet's lower left
corner. A view's coordinates are model millimetres in its own plane: the
projection of a 3D point P is (P.u, P.v), and the view's origin (the
projection of the model origin) sits at its `anchor` on the sheet.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from itertools import pairwise
from typing import Any

from catia_mcp.tree.curves import extent_points
from catia_mcp.tree.geometry import Frame, Vec, add, cross, dot, norm, scale, unit

SHEETS: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0),
    "A1": (841.0, 594.0), "A0": (1189.0, 841.0),
}
SHEET_ORDER = ("A4", "A3", "A2", "A1", "A0")
# ISO 5455 scales, largest first.
SCALES = (10.0, 5.0, 2.0, 1.0, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100, 1 / 200)
# The sheet's target: the first scale some sheet reaches, smallest sheet first.
TARGETS = (1.0, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100, 1 / 200)
# Frame margins (ISO 5457): left (filing edge), bottom, right, top.
MARGINS = (20.0, 10.0, 10.0, 10.0)
TITLE_BLOCK = (180.0, 40.0)
PAD = 10.0  # paper around a view, for its dimensions
GAP = 10.0  # paper between two padded views
LABEL = 8.0  # paper under a labelled view (SECTION A-A, DETAIL B)
STEP = 5.0  # grid step when looking for room for an extra view
SMALL_ON_PAPER = 3.0  # a feature smaller than this on paper gets a detail view
DETAIL_ON_PAPER = 8.0  # ... drawn this big in the detail
BOM_ROW = 8.0
BOM_COLUMNS = (15.0, 135.0, 30.0)  # item, part, quantity: the title block's width
AXIS_TOL = 1e-6

AXES: dict[str, Vec] = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
# The viewer's side when nothing else decides: from above, from the front
# (ISO front view: X right, Z up, seen from -Y), from the right.
DEFAULT_SIGN = {"x": 1.0, "y": -1.0, "z": 1.0}


class DrawingPlanError(ValueError):
    """No drawing can be planned (no geometry, nothing fits)."""


# ── Geometry ──


@dataclass
class Face:
    """A planar face not facing a world axis: its outward unit normal, its
    area and a point on it (world)."""

    normal: Vec
    area: float
    centre: Vec


@dataclass
class PartGeometry:
    """What the plan needs of the solid: its world bounding box, the area of
    its planar faces by the axis they face, its inclined planar faces, where
    it came from."""

    bbox_min: Vec
    bbox_max: Vec
    face_area: dict[str, float] = field(default_factory=dict)
    source: str = "estimate"
    inclined: list[Face] = field(default_factory=list)

    def corners(self) -> list[Vec]:
        (x0, y0, z0), (x1, y1, z1) = self.bbox_min, self.bbox_max
        return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]

    def span(self, direction: Vec) -> tuple[float, float]:
        values = [dot(c, direction) for c in self.corners()]
        return min(values), max(values)

    def centre(self) -> Vec:
        return scale(add(self.bbox_min, self.bbox_max), 0.5)

    def size(self) -> float:
        return norm(add(self.bbox_max, scale(self.bbox_min, -1)))


def axis_key(direction: Vec) -> str | None:
    """'x', 'y' or 'z' when the direction is along a world axis."""
    try:
        d = unit(direction)
    except ValueError:
        return None
    for key, axis in AXES.items():
        if abs(abs(dot(d, axis)) - 1.0) < AXIS_TOL:
            return key
    return None


def reference_geometry(part: dict[str, Any]) -> PartGeometry:
    """The geometry of the build123d reference solid (needs build123d).
    `part` is in world coordinates."""
    from catia_mcp.tree.reference import build_part

    solid = build_part(part)
    box = solid.bounding_box()
    areas: dict[str, float] = {}
    inclined: list[Face] = []
    for face in solid.faces():
        if "PLANE" not in str(face.geom_type).upper():
            continue
        normal = face.normal_at()
        direction = (float(normal.X), float(normal.Y), float(normal.Z))
        key = axis_key(direction)
        if key:
            areas[key] = areas.get(key, 0.0) + float(face.area)
        else:
            centre = face.center()
            inclined.append(Face(unit(direction), float(face.area),
                                 (float(centre.X), float(centre.Y), float(centre.Z))))
    return PartGeometry((float(box.min.X), float(box.min.Y), float(box.min.Z)),
                        (float(box.max.X), float(box.max.Y), float(box.max.Z)),
                        areas, "build123d", inclined)


def _rotate(point: Vec, origin: Vec, axis: Vec, angle: float) -> Vec:
    """Rodrigues' rotation of a point about the line (origin, axis)."""
    k = unit(axis)
    p = add(point, scale(origin, -1))
    c, s = math.cos(angle), math.sin(angle)
    rotated = add(add(scale(p, c), scale(cross(k, p), s)), scale(k, dot(k, p) * (1 - c)))
    return add(rotated, origin)


def _pairs(value: Any) -> list[tuple[float, float]]:
    if isinstance(value, list) and value and all(
            isinstance(p, (list, tuple)) and len(p) == 2 for p in value):
        return [(float(p[0]), float(p[1])) for p in value]
    return []


def sketch_points(sketch: dict[str, Any]) -> list[Vec]:
    """World points bounding a sketch's entities (circles by their extreme
    points, arcs by their three points, splines by their poles)."""
    frame = Frame.from_tree(sketch["plane"])
    points: list[tuple[float, float]] = []
    for entity in sketch.get("entities") or []:
        for key in ("start", "end", "mid"):
            if isinstance(entity.get(key), (list, tuple)) and len(entity[key]) == 2:
                points.append((float(entity[key][0]), float(entity[key][1])))
        if entity.get("center") is not None:
            cu, cv = (float(c) for c in entity["center"])
            radii = [float(entity.get(k) or 0.0) for k in ("radius", "major_radius",
                                                             "minor_radius")]
            r = max(radii)
            points += [(cu + r, cv), (cu - r, cv), (cu, cv + r), (cu, cv - r)]
        for key in ("poles", "points", "control_points"):
            points += _pairs(entity.get(key))
    return [frame.world(u, v) for u, v in points]


def estimate_geometry(part: dict[str, Any]) -> PartGeometry:
    """A bounding box from the features alone, when build123d is missing:
    the solid-making features' sketches swept along their extrude, turned
    about their revolve axis, loft sections, sweep paths. Holes, cuts and
    dress-ups do not grow it. `part` is in world coordinates."""
    sketches = {f["id"]: f for f in part["features"] if f["type"] == "sketch"}
    points: list[Vec] = []
    for feature in part["features"]:
        if feature.get("operation") in ("cut", "intersect"):
            continue
        kind = feature["type"]
        points += extent_points(feature)  # 3D curves, spheres, cylinders (curves.py)
        if kind == "surface" and feature.get("op") in ("extrude", "revolve", "loft", "sweep", "fill"):
            # A surface spans what the solid feature of the same kind would
            # (docs/surfaces.md): its inputs are the sketches.
            inputs = feature.get("inputs") or []
            feature = {**feature, "sketch": inputs[0] if inputs else None, "sections": inputs}
            kind = "extrude" if feature["op"] == "fill" else feature["op"]
        if kind == "extrude" and feature.get("sketch") in sketches:
            sketch = sketches[feature["sketch"]]
            base = sketch_points(sketch)
            normal = Frame.from_tree(sketch["plane"]).normal
            if feature.get("direction") is not None:
                vector = tuple(float(c) for c in feature["direction"])
            else:
                vector = scale(normal, float(feature.get("distance") or 0.0))
            sides = [vector, scale(vector, -1)] if feature.get("symmetric") else [vector]
            points += base + [add(p, s) for p in base for s in sides]
        elif kind == "revolve" and feature.get("sketch") in sketches:
            axis = feature["axis"]
            origin = tuple(float(c) for c in axis["origin"])
            direction = tuple(float(c) for c in axis["direction"])
            angle = math.radians(float(feature.get("angle") or 360.0))
            steps = max(1, int(abs(angle) / math.radians(15)))
            for p in sketch_points(sketches[feature["sketch"]]):
                points += [_rotate(p, origin, direction, angle * i / steps)
                           for i in range(steps + 1)]
        elif kind == "loft":
            for name in feature.get("sections") or []:
                if isinstance(name, dict) and name.get("point") is not None:
                    points.append(tuple(float(c) for c in name["point"]))  # a loft to a point
                elif isinstance(name, str) and name in sketches:
                    points += sketch_points(sketches[name])
        elif kind == "sweep":
            path = [tuple(float(c) for c in e[k]) for e in feature.get("path") or []
                    for k in ("start", "mid", "end") if e.get(k) is not None]
            profile = sketch_points(sketches[feature["sketch"]]) if feature.get(
                "sketch") in sketches else []
            reach = 0.0
            if profile and path:
                reach = max(norm(add(p, scale(path[0], -1))) for p in profile)
            for p in path:
                points += [add(p, scale(axis, s * reach)) for axis in AXES.values()
                           for s in (-1, 1)]
    if not points:
        raise DrawingPlanError(f"part {part.get('id')}: no solid-making feature to draw")
    lo = tuple(min(p[i] for p in points) for i in range(3))
    hi = tuple(max(p[i] for p in points) for i in range(3))
    return PartGeometry(lo, hi, {}, "estimate", estimate_inclined(part, (lo, hi)))


def estimate_inclined(part: dict[str, Any], box: tuple[Vec, Vec]) -> list[Face]:
    """The inclined planar faces the features make, without build123d: the
    side faces of straight extrudes swept from sketch lines, the end faces of
    extrudes sketched on a tilted plane, the cut faces of splits by a tilted
    plane. Normals point out of the material."""
    sketches = {f["id"]: f for f in part["features"] if f["type"] == "sketch"}
    faces: list[Face] = []

    def keep(normal: Vec, area: float, centre: Vec) -> None:
        if area > 1e-9 and axis_key(normal) is None:
            faces.append(Face(_snap(unit(normal)), area, centre))

    for feature in part["features"]:
        kind = feature["type"]
        if kind == "extrude" and feature.get("sketch") in sketches and not feature.get("taper"):
            sketch = sketches[feature["sketch"]]
            frame = Frame.from_tree(sketch["plane"])
            if feature.get("direction") is not None:
                vector = tuple(float(c) for c in feature["direction"])
            else:
                vector = scale(frame.normal, float(feature.get("distance") or 0.0))
            length = norm(vector) * (2 if feature.get("symmetric") else 1)
            if length < 1e-9:
                continue
            along = unit(vector)
            shift = (0.0, 0.0, 0.0) if feature.get("symmetric") else scale(vector, 0.5)
            cut = feature.get("operation") in ("cut", "intersect")
            points = sketch_points(sketch)
            middle = scale(_sum(points), 1 / len(points)) if points else frame.origin
            for entity in sketch.get("entities") or []:
                if entity.get("type") != "line":
                    continue
                a = frame.world(*(float(c) for c in entity["start"]))
                b = frame.world(*(float(c) for c in entity["end"]))
                edge = add(b, scale(a, -1))
                if norm(edge) < 1e-9 or norm(cross(edge, along)) < 1e-9:
                    continue
                normal = unit(cross(edge, along))
                mid = scale(add(a, b), 0.5)
                if dot(normal, add(mid, scale(middle, -1))) < 0:
                    normal = scale(normal, -1)
                if cut:
                    normal = scale(normal, -1)
                keep(normal, norm(cross(edge, along)) * length, add(mid, shift))
            if not cut and axis_key(frame.normal) is None and points:
                us = [dot(p, frame.x_dir) for p in points]
                vs = [dot(p, frame.y_dir) for p in points]
                area = (max(us) - min(us)) * (max(vs) - min(vs))
                end = add(middle, vector)
                keep(along, area, end)
                keep(scale(along, -1), area, middle)
        elif kind == "split" and isinstance(feature.get("plane"), dict):
            plane = feature["plane"]
            normal = unit(tuple(float(c) for c in plane["normal"]))
            spans = sorted((box[1][i] - box[0][i] for i in range(3)), reverse=True)
            outward = scale(normal, -1) if feature.get("keep", "top") == "top" else normal
            keep(outward, spans[0] * spans[1], tuple(float(c) for c in plane["origin"]))
    return faces


def _sum(points: list[Vec]) -> Vec:
    total: Vec = (0.0, 0.0, 0.0)
    for p in points:
        total = add(total, p)
    return total


def part_geometry(part: dict[str, Any]) -> PartGeometry:
    """The reference's geometry when build123d can build the part, else the
    estimate."""
    try:
        return reference_geometry(part)
    except Exception:  # no build123d, or a tree the reference cannot build
        return estimate_geometry(part)


# ── Features ──


@dataclass
class HoleCallout:
    feature: str
    kind: str
    diameter: float
    axis: Vec  # the drilling direction
    positions: list[Vec]
    text: str
    plain: str = ""  # the same callout in words, where CATIA's font lacks the symbols
    outer: float = 0.0  # the largest diameter seen: counterbore, countersink or the hole
    length: float | None = None  # the drilled length, None for a through hole


@dataclass
class Cylinder:
    """A cylindrical face made by extruding a circle: what gets a centre mark
    or an axis line."""

    feature: str
    centre: Vec  # on the axis, where the extrusion starts
    radius: float
    axis: Vec  # unit
    length: float


@dataclass
class SmallFeature:
    feature: str
    size: float  # mm: a diameter, twice a radius, a chamfer's distance
    point: Vec


@dataclass
class FeatureSummary:
    holes: list[HoleCallout] = field(default_factory=list)
    revolve_axes: list[tuple[Vec, Vec]] = field(default_factory=list)  # (origin, direction)
    profile_normals: list[Vec] = field(default_factory=list)
    small: list[SmallFeature] = field(default_factory=list)
    internal: bool = False  # holes, cuts, shells: a section shows them
    # A turned part's axis (origin, unit direction along a world axis).
    symmetry_axis: tuple[Vec, Vec] | None = None
    cylinders: list[Cylinder] = field(default_factory=list)
    chamfers: list[tuple[Vec, float]] = field(default_factory=list)  # edge point, size


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


# ISO 129-1 symbols: diameter, depth, counterbore, countersink.
DIAMETER, DEPTH, CBORE, CSK, TIMES = "\u2300", "\u21a7", "\u2334", "\u2335", "\u00d7"
INTERNAL_THREAD_CLASS = "6H"  # ISO 965-1's default for internal metric threads


def _thread_text(feature: dict[str, Any], symbols: bool) -> str | None:
    """'M6 - 6H' followed by its depth, or None when the hole has no thread
    (or one the bridge would refuse anyway)."""
    from catia_mcp.tree.partdesign import thread_spec

    try:
        spec = thread_spec(feature)
    except (TypeError, ValueError):
        return None
    if spec is None:
        return None
    raw = feature.get("thread")
    tolerance = (raw.get("tolerance") if isinstance(raw, dict) else None) or INTERNAL_THREAD_CLASS
    name = spec.designation.replace("x", TIMES if symbols else "x")
    if spec.side == "left":
        name += " LH"
    text = f"{name} - {tolerance}"
    if spec.depth is None:
        return text + " THRU"
    return text + (f" {DEPTH}{_fmt(spec.depth)}" if symbols else f" x {_fmt(spec.depth)} DEEP")


def hole_callout(feature: dict[str, Any], style: str = "symbols") -> str:
    """The ISO-style note of a hole feature. With ISO 129-1 symbols (default):
    '3\u00d7 \u23306 THRU', '\u23304 \u21a74', '\u23304 THRU \u2334\u23308 \u21a72',
    '\u23304 THRU \u2335\u23308 \u00d7 90\u00b0', 'M6 - 6H \u21a710 / \u23305 \u21a712'.
    In words (style 'text'): '3 x \u00d86 THRU', '\u00d84 x 4 DEEP',
    '\u00d84 THRU, CBORE \u00d88 x 2', '\u00d84 THRU, CSK \u00d88 x 90\u00b0',
    'M6 - 6H x 10 DEEP / \u00d85 x 12 DEEP'."""
    symbols = style == "symbols"
    dia = DIAMETER if symbols else "\u00d8"
    count = len(feature.get("positions") or [])
    depth = feature.get("depth")
    text = f"{dia}{_fmt(float(feature['diameter']))}"
    if depth is None:
        text += " THRU"
    else:
        text += f" {DEPTH}{_fmt(float(depth))}" if symbols else f" x {_fmt(float(depth))} DEEP"
    kind = feature.get("kind") or "simple"
    if kind == "counterbore":
        size = (f"{dia}{_fmt(float(feature['counterbore_diameter']))}", _fmt(
            float(feature["counterbore_depth"])))
        text += (f" {CBORE}{size[0]} {DEPTH}{size[1]}" if symbols
                 else f", CBORE {size[0]} x {size[1]}")
    elif kind == "countersink":
        size = (f"{dia}{_fmt(float(feature['countersink_diameter']))}",
                _fmt(float(feature.get("countersink_angle") or 90)))
        text += (f" {CSK}{size[0]} {TIMES} {size[1]}\u00b0" if symbols
                 else f", CSK {size[0]} x {size[1]}\u00b0")
    thread = _thread_text(feature, symbols)
    if thread is not None:
        text = f"{thread} / {text}"
    if count > 1:
        return f"{count}{TIMES} {text}" if symbols else f"{count} x {text}"
    return text


def _on_line(point: Vec, origin: Vec, direction: Vec) -> bool:
    offset = add(point, scale(origin, -1))
    return norm(cross(offset, direction)) < 1e-6


def symmetry_axis(part: dict[str, Any]) -> tuple[Vec, Vec] | None:
    """The axis of a part of revolution: every solid made by turning about
    it, or by extruding concentric circles centred on it, along a world
    axis; holes on it, along it; nothing else that breaks the symmetry
    (fillets and chamfers keep it). None otherwise."""
    sketches = {f["id"]: f for f in part["features"] if f["type"] == "sketch"}
    axes: list[tuple[Vec, Vec]] = []
    for feature in part["features"]:
        kind = feature["type"]
        if kind in ("sketch", "fillet", "chamfer"):
            continue
        if kind == "revolve":
            axis = feature["axis"]
            axes.append((tuple(float(c) for c in axis["origin"]),
                         unit(tuple(float(c) for c in axis["direction"]))))
        elif kind == "extrude" and feature.get("sketch") in sketches \
                and feature.get("direction") is None and not feature.get("taper"):
            sketch = sketches[feature["sketch"]]
            entities = sketch.get("entities") or []
            if not entities or any(e.get("type") != "circle" for e in entities):
                return None
            centres = {tuple(round(float(c), 9) for c in e["center"]) for e in entities}
            if len(centres) != 1:
                return None
            frame = Frame.from_tree(sketch["plane"])
            axes.append((frame.world(*next(iter(centres))), frame.normal))
        elif kind == "hole":
            direction = unit(tuple(float(c) for c in feature.get("direction") or (0, 0, -1)))
            axes += [(tuple(float(c) for c in p), direction)
                     for p in feature.get("positions") or []]
        else:
            return None
    if not axes:
        return None
    origin, direction = axes[0]
    key = axis_key(direction)
    if key is None:
        return None
    for point, other in axes[1:]:
        if abs(abs(dot(other, direction)) - 1) > AXIS_TOL or not _on_line(point, origin,
                                                                             direction):
            return None
    return origin, AXES[key]


def _extruded_circles(feature: dict[str, Any], sketch: dict[str, Any]) -> list[Cylinder]:
    frame = Frame.from_tree(sketch["plane"])
    if feature.get("direction") is not None:
        vector = tuple(float(c) for c in feature["direction"])
    else:
        vector = scale(frame.normal, float(feature.get("distance") or 0.0))
    if norm(vector) < 1e-9:
        return []
    start_shift = scale(vector, -1) if feature.get("symmetric") else (0.0, 0.0, 0.0)
    length = norm(vector) * (2 if feature.get("symmetric") else 1)
    out = []
    for entity in sketch.get("entities") or []:
        if entity.get("type") == "circle" and entity.get("center") is not None:
            centre = add(frame.world(*(float(c) for c in entity["center"])), start_shift)
            out.append(Cylinder(str(feature.get("id")), centre, float(entity["radius"]),
                                unit(vector), length))
    return out


def summarize_features(part: dict[str, Any], style: str = "symbols") -> FeatureSummary:
    """What the drawing must show of a part's features (world coordinates).
    `style`: hole callouts with ISO 129 symbols, or in words ('text')."""
    sketches = {f["id"]: f for f in part["features"] if f["type"] == "sketch"}
    out = FeatureSummary()
    for feature in part["features"]:
        kind = feature["type"]
        if kind in ("extrude", "revolve") and feature.get("sketch") in sketches:
            plane = sketches[feature["sketch"]]["plane"]
            out.profile_normals.append(Frame.from_tree(plane).normal)
            if feature.get("operation") == "cut":
                out.internal = True
        if kind == "extrude" and feature.get("sketch") in sketches and not feature.get("taper"):
            out.cylinders += _extruded_circles(feature, sketches[feature["sketch"]])
        if kind == "revolve":
            axis = feature["axis"]
            out.revolve_axes.append((tuple(float(c) for c in axis["origin"]),
                                     unit(tuple(float(c) for c in axis["direction"]))))
        elif kind == "hole":
            positions = [tuple(float(c) for c in p) for p in feature.get("positions") or []]
            direction = unit(tuple(float(c) for c in feature.get("direction") or (0, 0, -1)))
            outer = max(float(feature["diameter"]),
                        float(feature.get("counterbore_diameter") or 0.0),
                        float(feature.get("countersink_diameter") or 0.0))
            length = feature.get("depth")  # through: as far as the part goes
            out.holes.append(HoleCallout(str(feature.get("id")), feature.get("kind") or "simple",
                                         float(feature["diameter"]), direction, positions,
                                         hole_callout(feature, style),
                                         hole_callout(feature, "text"), outer,
                                         None if length is None else float(length)))
            out.small += [SmallFeature(str(feature.get("id")), float(feature["diameter"]), p)
                          for p in positions[:1]]
            out.internal = True
        elif kind == "shell":
            out.internal = True
        elif kind in ("fillet", "chamfer"):
            size = (2 * float(feature.get("radius") or 0.0) if kind == "fillet"
                    else float(feature.get("distance") or 0.0))
            edges = feature.get("edges") or []
            if kind == "chamfer":
                reach = max(size, float(feature.get("distance2") or 0.0))
                for edge in edges:
                    points = [edge.get("point")] + list(edge.get("samples") or [])
                    out.chamfers += [(tuple(float(c) for c in p), reach) for p in points
                                     if isinstance(p, (list, tuple)) and len(p) == 3]
            if size > 0 and edges and edges[0].get("point") is not None:
                out.small.append(SmallFeature(str(feature.get("id")), size,
                                              tuple(float(c) for c in edges[0]["point"])))
    out.symmetry_axis = symmetry_axis(part)
    return out


# ── Plan ──


@dataclass
class ViewPlan:
    name: str
    kind: str  # front, projection, section, detail, isometric, auxiliary
    u: Vec  # the view's horizontal axis, in the model
    v: Vec  # its vertical axis; u x v points to the viewer
    scale: float
    anchor: tuple[float, float]  # where the model origin sits on the sheet
    box: tuple[float, float, float, float]  # the drawn content on the sheet
    parent: str | None = None
    projection: str | None = None  # right, left, top, bottom: CATIA's CatProjViewType
    label: str | None = None  # A (section A-A), B (detail B), C (view C)
    profile: list[float] | None = None  # section or auxiliary line, in the parent's coordinates
    detail: tuple[float, float, float] | None = None  # centre and radius in the parent
    profile_type: str | None = None  # a section's: Offset (straight, stepped) or Aligned
    # An auxiliary view's arrow in its parent (tail x, y, head x, y), drawn
    # when CATIA makes no callout (the view made along the face's normal).
    arrow: tuple[float, float, float, float] | None = None
    # On a sheet other than its parent's: made on its own, along its plane.
    independent: bool = False

    def padded(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.box
        below = LABEL if self.label else 0.0
        return (x0 - PAD, y0 - PAD - below, x1 + PAD, y1 + PAD)


@dataclass
class Annotation:
    view: str
    text: str
    x: float  # view coordinates
    y: float
    leader: tuple[float, float] | None = None  # the leader's head, view coordinates
    plain: str | None = None  # the text in words, if CATIA's font lacks a symbol


@dataclass
class CentreLine:
    """A centre mark (two crossed lines on a circle's centre) or an axis line
    (a hole's, a cylinder's or a revolution axis seen side on), drawn
    dash-dot in the view, in view coordinates."""

    view: str
    kind: str  # mark, axis or circle (a bolt circle's pitch circle)
    lines: list[tuple[float, float, float, float]]
    of: str  # what it marks: a feature id or "revolution axis"
    circle: tuple[float, float, float] | None = None  # centre and radius, for a pitch circle


@dataclass
class Cell:
    x: float
    y: float
    w: float
    h: float
    label: str
    value: str


@dataclass
class TitleBlock:
    box: tuple[float, float, float, float]
    cells: list[Cell]
    frame: tuple[float, float, float, float]  # the drawing frame (ISO 5457)
    symbol_lines: list[tuple[float, float, float, float]]
    symbol_circles: list[tuple[float, float, float]]


@dataclass
class BomTable:
    anchor: tuple[float, float]  # the table's top left corner
    columns: tuple[float, ...]
    row_height: float
    rows: list[list[str]]  # header first

    def box(self) -> tuple[float, float, float, float]:
        x, y = self.anchor
        return (x, y - self.row_height * len(self.rows), x + sum(self.columns), y)


@dataclass
class Balloon:
    item: int
    view: str
    x: float  # the balloon, view coordinates
    y: float
    leader: tuple[float, float]  # what it points at, view coordinates


@dataclass
class DrawingPlan:
    name: str
    sheet: str
    width: float
    height: float
    projection: str  # first or third (angle)
    scale: float
    views: list[ViewPlan]
    title: TitleBlock
    annotations: list[Annotation] = field(default_factory=list)
    bom: BomTable | None = None
    balloons: list[Balloon] = field(default_factory=list)
    geometry_source: str = "estimate"
    notes: list[str] = field(default_factory=list)
    centre_lines: list[CentreLine] = field(default_factory=list)
    # Further sheets of the same drawing, each a plan of its own.
    more: list[DrawingPlan] = field(default_factory=list)
    # The part id this sheet projects; None: the drawing's own source.
    source: str | None = None
    # A product drawing: the part ids drawn on its sheets (1-based index).
    covers: dict[str, int] = field(default_factory=dict)

    @property
    def scale_label(self) -> str:
        return scale_label(self.scale)

    @property
    def sheets(self) -> list[DrawingPlan]:
        """This sheet, then the further ones."""
        return [self, *self.more]

    def view(self, name: str) -> ViewPlan:
        return next(v for v in self.views if v.name == name)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["scale_label"] = self.scale_label
        return out


@dataclass
class DrawingOptions:
    sheet: str = "auto"  # auto or A4..A0
    projection: str = "first"  # first (ISO E) or third (ISO A) angle
    scale: float | None = None  # a standard scale, or None to choose
    isometric: bool = True
    sections: bool = True
    details: bool = True
    date: str | None = None  # the title block's date (default today)
    max_sheet: str = "A0"  # the largest sheet allowed: beyond it, further sheets
    auxiliary: bool = True
    centre_lines: bool = True
    callouts: str = "symbols"  # ISO 129 symbols, or 'text' (words)
    assembly_sheets: bool = True  # an assembly: one sheet per distinct part after the product

    def __post_init__(self) -> None:
        if self.sheet != "auto" and self.sheet not in SHEETS:
            raise ValueError(f"sheet is auto or one of {', '.join(SHEET_ORDER)}")
        if self.max_sheet not in SHEETS:
            raise ValueError(f"max_sheet is one of {', '.join(SHEET_ORDER)}")
        if self.callouts not in ("symbols", "text"):
            raise ValueError("callouts are 'symbols' or 'text'")
        if self.projection not in ("first", "third"):
            raise ValueError("projection is 'first' or 'third' (angle)")


def scale_label(value: float) -> str:
    if value >= 1:
        return f"{_fmt(value)}:1"
    return f"1:{_fmt(1 / value)}"


def view_point(view_u: Vec, view_v: Vec, point: Vec) -> tuple[float, float]:
    """A model point in a view's coordinates."""
    return dot(point, view_u), dot(point, view_v)


def _neg(a: Vec) -> Vec:
    return scale(a, -1)


def _snap(a: Vec) -> Vec:
    return tuple(0.0 if abs(c) < 1e-12 else c for c in a)  # type: ignore[return-value]


def _sign_toward_viewer(key: str, features: FeatureSummary) -> float:
    """+1 when the viewer looks from the axis' positive side: the side holes
    are drilled from, else the default."""
    axis = AXES[key]
    drilled = sum(-dot(h.axis, axis) * len(h.positions) for h in features.holes
                  if axis_key(h.axis) == key)
    if abs(drilled) > 0:
        return math.copysign(1.0, drilled)
    return DEFAULT_SIGN[key]


def choose_front(geometry: PartGeometry, features: FeatureSummary,
                 assembly: bool = False) -> tuple[Vec, Vec, Vec, str]:
    """The front view's (u, v, n) and why: n points to the viewer."""
    scores = {k: 0.0 for k in AXES}
    for hole in features.holes:
        key = axis_key(hole.axis)
        if key:
            scores[key] += 2 * len(hole.positions)
    for normal in features.profile_normals:
        key = axis_key(normal)
        if key:
            scores[key] += 3
    areas = {}
    for key in AXES:
        others = [a for k, a in AXES.items() if k != key]
        lo0, hi0 = geometry.span(others[0])
        lo1, hi1 = geometry.span(others[1])
        areas[key] = (hi0 - lo0) * (hi1 - lo1)
    if assembly:  # the product upright, seen from the front or the side
        key = max(("y", "x"), key=lambda k: areas[k])
        reason = "assembly: upright, the larger of the front and side views"
    elif features.symmetry_axis is not None:  # a turned part: its axis horizontal
        along = axis_key(features.symmetry_axis[1])
        key = next(k for k in ("y", "x", "z") if k != along)
        reason = f"across the axis of revolution ({along}), the axis horizontal"
    else:
        key = max(AXES, key=lambda k: (scores[k], geometry.face_area.get(k, 0.0), areas[k],
                                       k == "z"))
        reason = (f"seen along {key}: feature score {scores[key]:g}, planar faces "
                  f"{geometry.face_area.get(key, 0.0):.1f} mm2, projected area "
                  f"{areas[key]:.1f} mm2")
    n = scale(AXES[key], _sign_toward_viewer(key, features))
    in_plane = [k for k in AXES if k != key]
    turned = [features.symmetry_axis] if features.symmetry_axis else features.revolve_axes
    revolve_in_plane = [axis_key(d) for _, d in turned if axis_key(d) in in_plane]
    if revolve_in_plane and not assembly:
        u = AXES[revolve_in_plane[0]]
        v = cross(n, u)
    elif key != "z":
        v = AXES["z"]
        u = cross(v, n)
    else:
        spans = {k: geometry.span(AXES[k]) for k in in_plane}
        longest = max(in_plane, key=lambda k: spans[k][1] - spans[k][0])
        u = AXES[longest]
        v = cross(n, u)
    return _snap(u), _snap(v), _snap(n), reason


@dataclass
class _Box:
    """A view's content extents in its own coordinates (model mm)."""

    x0: float
    x1: float
    y0: float
    y1: float

    @classmethod
    def of(cls, geometry: PartGeometry, u: Vec, v: Vec) -> _Box:
        (x0, x1), (y0, y1) = geometry.span(u), geometry.span(v)
        return cls(x0, x1, y0, y1)

    def size(self, s: float) -> tuple[float, float]:
        return (self.x1 - self.x0) * s, (self.y1 - self.y0) * s

    def centre(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


def _place(name: str, kind: str, u: Vec, v: Vec, extents: _Box, s: float,
           centre: tuple[float, float], **extra: Any) -> ViewPlan:
    w, h = extents.size(s)
    cx, cy = extents.centre()
    anchor = (centre[0] - s * cx, centre[1] - s * cy)
    box = (centre[0] - w / 2, centre[1] - h / 2, centre[0] + w / 2, centre[1] + h / 2)
    return ViewPlan(name, kind, _snap(u), _snap(v), s, anchor, box, **extra)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(a, area) -> bool:
    return a[0] >= area[0] - 1e-9 and a[1] >= area[1] - 1e-9 and a[2] <= area[2] + 1e-9 \
        and a[3] <= area[3] + 1e-9


def _free_spot(size: tuple[float, float], area, taken) -> tuple[float, float] | None:
    """The centre of the first free padded rectangle of `size` in `area`,
    scanning rows from the top, left to right."""
    w, h = size
    y = area[3] - h
    while y >= area[1] - 1e-9:
        x = area[0]
        while x + w <= area[2] + 1e-9:
            rect = (x, y, x + w, y + h)
            if not any(_overlap(rect, t) for t in taken):
                return x + w / 2, y + h / 2
            x += STEP
        y -= STEP
    return None


def _drawing_area(width: float, height: float) -> tuple[float, float, float, float]:
    left, bottom, right, top = MARGINS
    return (left, bottom, width - right, height - top)


def _title_box(width: float) -> tuple[float, float, float, float]:
    _, bottom, right, _ = MARGINS
    w, h = TITLE_BLOCK
    return (width - right - w, bottom, width - right, bottom + h)


@dataclass
class _Section:
    profile: list[float]
    horizontal: bool  # the cutting line (its first segment) runs along u
    cut: list[str]  # the features it cuts through
    profile_type: str = "Offset"  # CATIA's: Offset (straight or stepped) or Aligned
    kind: str = "straight"  # straight, stepped, aligned
    span: tuple[float, float] | None = None  # aligned: the unfolded extent along u


def _seen_end_on(direction: Vec, n: Vec) -> bool:
    return abs(abs(dot(direction, n)) - 1) < AXIS_TOL


def _section_centre(features: FeatureSummary, u: Vec, v: Vec, n: Vec
                    ) -> tuple[tuple[float, float], str | None] | None:
    """The centre a hole pattern turns about in the front view, and the
    feature on it: an axis of revolution seen end on, or a single hole at the
    centre of a pattern of at least three holes."""
    axes = ([features.symmetry_axis] if features.symmetry_axis else []) + features.revolve_axes
    for origin, direction in axes:
        if _seen_end_on(direction, n):
            return view_point(u, v, origin), "revolve axis"
    patterns = [h for h in features.holes if _seen_end_on(h.axis, n) and len(h.positions) >= 3]
    for single in features.holes:
        if not _seen_end_on(single.axis, n) or len(single.positions) != 1:
            continue
        cx, cy = view_point(u, v, single.positions[0])
        for pattern in patterns:
            radii = [math.hypot(px - cx, py - cy)
                     for px, py in (view_point(u, v, p) for p in pattern.positions)]
            if min(radii) > 1e-6 and max(radii) - min(radii) < 1e-6:
                return (cx, cy), single.feature
    return None


def _aligned_section(targets: list[tuple[str, tuple[float, float]]],
                     centre: tuple[tuple[float, float], str | None],
                     front: _Box, mx: float) -> _Section | None:
    """Two cutting lines from the centre through holes, at an angle: when no
    straight line through the centre finds holes on both sides (a bolt circle
    of three or five holes)."""
    (cx, cy), on_centre = centre
    rays: dict[float, list[str]] = {}
    for feature, (x, y) in targets:
        if math.hypot(x - cx, y - cy) < 1e-6:
            continue
        rays.setdefault(round(math.atan2(y - cy, x - cx), 6), []).append(feature)
    if len(rays) < 2:
        return None
    for angle in rays:
        opposite = math.atan2(math.sin(angle + math.pi), math.cos(angle + math.pi))
        if any(abs(other - opposite) < 1e-5 for other in rays):
            return None  # a straight section through the centre shows both sides

    def score(pair):
        a, b = pair
        between = abs(math.atan2(math.sin(a - b), math.cos(a - b)))
        return (len(set(rays[a] + rays[b])), len(rays[a]) + len(rays[b]), between)

    pairs = [(a, b) for a in rays for b in rays if a < b]
    a, b = max(pairs, key=score)
    if abs(math.sin(a)) > abs(math.sin(b)):  # the more horizontal ray first
        a, b = b, a
    reach = max(math.hypot(x - cx, y - cy) for x in (front.x0, front.x1)
                for y in (front.y0, front.y1)) + mx
    profile = [cx + reach * math.cos(a), cy + reach * math.sin(a), cx, cy,
               cx + reach * math.cos(b), cy + reach * math.sin(b)]
    cut = set(rays[a] + rays[b]) | ({on_centre} if on_centre else set())
    return _Section([round(c, 9) for c in profile], abs(math.cos(a)) >= abs(math.sin(a)),
                    sorted(cut), "Aligned", "aligned", (cx - reach, cx + reach))


def _stepped_section(targets: list[tuple[str, tuple[float, float]]], horizontal: bool,
                     at: float, front: _Box, mx: float) -> _Section | None:
    """A cutting line stepped at right angles through one hole of each hole
    feature (the one nearest the best straight line), along u or v."""
    by_feature: dict[str, list[tuple[float, float]]] = {}
    for feature, point in targets:
        by_feature.setdefault(feature, []).append(point)
    cross_index = 1 if horizontal else 0
    reps = [(feature, min(points, key=lambda p: (abs(p[cross_index] - at), p)))
            for feature, points in by_feature.items()]
    for along_u in (horizontal, not horizontal):
        i, j = (0, 1) if along_u else (1, 0)
        ordered = sorted(reps, key=lambda r: (r[1][i], r[1][j]))
        if any(abs(p[1][i] - q[1][i]) < 1e-6 and abs(p[1][j] - q[1][j]) > 1e-6
               for p, q in pairwise(ordered)):
            continue  # two holes one behind the other: no step between them
        lo, hi = (front.x0, front.x1) if along_u else (front.y0, front.y1)
        current = ordered[0][1][j]
        points = [(lo - mx, current)]
        for (_, p), (_, q) in pairwise(ordered):
            if abs(q[j] - current) > 1e-6:
                middle = (p[i] + q[i]) / 2
                points += [(middle, current), (middle, q[j])]
                current = q[j]
        points.append((hi + mx, current))
        flat = [c for a, b in points for c in ((a, b) if along_u else (b, a))]
        return _Section([round(c, 9) for c in flat], along_u, sorted(by_feature), "Offset",
                        "stepped")
    return None


def plan_section(features: FeatureSummary, u: Vec, v: Vec, n: Vec,
                 front: _Box) -> _Section | None:
    """A cutting line in the front view through the most hole axes (or the
    revolution axis), straight across the part; stepped through one hole of
    each hole feature when no straight line meets them all; aligned (two
    lines from the centre at an angle) through a bolt circle no straight line
    through its centre shows on both sides."""
    lines: dict[tuple[bool, float], list[str]] = {}

    def vote(horizontal: bool, at: float, what: str) -> None:
        lines.setdefault((horizontal, round(at, 6)), []).append(what)

    targets: list[tuple[str, tuple[float, float]]] = []
    for hole in features.holes:
        along = axis_key(hole.axis)
        for p in hole.positions:
            pu, pv = view_point(u, v, p)
            if along and abs(abs(dot(hole.axis, n)) - 1) < AXIS_TOL:
                vote(True, pv, hole.feature)
                vote(False, pu, hole.feature)
                targets.append((hole.feature, (pu, pv)))
            elif along and abs(abs(dot(hole.axis, u)) - 1) < AXIS_TOL:
                vote(True, pv, hole.feature)
            elif along and abs(abs(dot(hole.axis, v)) - 1) < AXIS_TOL:
                vote(False, pu, hole.feature)
    for origin, direction in features.revolve_axes:
        pu, pv = view_point(u, v, origin)
        if abs(abs(dot(direction, u)) - 1) < AXIS_TOL:
            vote(True, pv, "revolve axis")
        elif abs(abs(dot(direction, v)) - 1) < AXIS_TOL:
            vote(False, pu, "revolve axis")
        elif abs(abs(dot(direction, n)) - 1) < AXIS_TOL:
            vote(True, pv, "revolve axis")
    if not features.internal:
        return None  # a section of a solid part shows nothing more
    mx = 0.05 * max(front.x1 - front.x0, front.y1 - front.y0, 1.0)
    centre = _section_centre(features, u, v, n)
    if centre is not None and targets:
        aligned = _aligned_section(targets, centre, front, mx)
        if aligned is not None:
            return aligned
    if not lines:
        _, cy = front.centre()
        lines[(True, round(cy, 6))] = ["the part's middle"]
    (horizontal, at), cut = max(lines.items(), key=lambda kv: (len(set(kv[1])), len(kv[1]),
                                                               kv[0][0]))
    if {f for f, _ in targets} - set(cut):
        stepped = _stepped_section(targets, horizontal, at, front, mx)
        if stepped is not None:
            return stepped
    if horizontal:
        profile = [front.x0 - mx, at, front.x1 + mx, at]
    else:
        profile = [at, front.y0 - mx, at, front.y1 + mx]
    return _Section(profile, horizontal, sorted(set(cut)))


def _detail_scale(main: float, size: float) -> float:
    for value in sorted(SCALES):
        if value >= 2 * main - 1e-12 and size * value >= DETAIL_ON_PAPER:
            return value
    return max(SCALES)


def _title(name: str, number: str, plan_scale: float, sheet: str, projection: str,
           width: float, height: float, material: str | None, when: str,
           ) -> TitleBlock:
    x0, y0, x1, y1 = _title_box(width)
    row = TITLE_BLOCK[1] / 4
    cells = [
        Cell(x0, y0 + 3 * row, 180.0, row, "Title", name),
        Cell(x0, y0 + 2 * row, 180.0, row, "Number", number),
        Cell(x0, y0 + row, 45.0, row, "Scale", scale_label(plan_scale)),
        Cell(x0 + 45.0, y0 + row, 45.0, row, "Sheet", f"{sheet} 1/1"),
        Cell(x0 + 90.0, y0 + row, 90.0, row, "Projection",
             "ISO E (first angle)" if projection == "first" else "ISO A (third angle)"),
        Cell(x0, y0, 90.0, row, "Material", material or "-"),
        Cell(x0 + 90.0, y0, 45.0, row, "Date", when),
        Cell(x0 + 135.0, y0, 45.0, row, "Units", "mm"),
    ]
    # ISO 5456-2 symbol in the projection cell's right part: a truncated
    # cone (small end left) and its end view, right of it in first angle,
    # left of it in third angle.
    cx, cy = x0 + 90.0 + 70.0, y0 + row + row / 2
    big, small, length = 3.0, 1.5, 6.0
    cone_x = cx - 8.0 if projection == "first" else cx + 2.0
    circle_x = cx + 6.0 if projection == "first" else cx - 6.0
    lines = [(cone_x, cy - small, cone_x, cy + small),
             (cone_x, cy + small, cone_x + length, cy + big),
             (cone_x + length, cy + big, cone_x + length, cy - big),
             (cone_x + length, cy - big, cone_x, cy - small)]
    circles = [(circle_x, cy, big), (circle_x, cy, small)]
    frame = _drawing_area(width, height)
    return TitleBlock((x0, y0, x1, y1), cells, frame, lines, circles)


def _sheets(options: DrawingOptions) -> tuple[str, ...]:
    if options.sheet != "auto":
        return (options.sheet,)
    return SHEET_ORDER[:SHEET_ORDER.index(options.max_sheet) + 1]


def _choose(layout, options: DrawingOptions):
    """The first (target scale, sheet) whose layout fits, smallest sheet first."""
    sheets = _sheets(options)
    if options.scale is not None:
        for sheet in sheets:
            plan = layout(sheet, float(options.scale))
            if plan is not None:
                return plan
        raise DrawingPlanError(f"the views do not fit any sheet at {scale_label(options.scale)}")
    best: dict[str, DrawingPlan | None] = {}

    def largest(sheet: str) -> DrawingPlan | None:
        if sheet not in best:
            best[sheet] = next((p for p in (layout(sheet, s) for s in SCALES) if p), None)
        return best[sheet]

    for target in TARGETS:
        for sheet in sheets:
            plan = largest(sheet)
            if plan is not None and plan.scale >= target - 1e-12:
                return plan
    fitted = [p for p in (largest(sheet) for sheet in sheets) if p]
    if fitted:
        return fitted[-1]
    raise DrawingPlanError("the views do not fit any sheet at any standard scale")


def plan_part_drawing(part: dict[str, Any], geometry: PartGeometry | None = None,
                      options: DrawingOptions | None = None) -> DrawingPlan:
    """The drawing of one part. `part` is in world coordinates (the tree's
    part after `place_part`)."""
    options = options or DrawingOptions()
    geometry = geometry or part_geometry(part)
    features = summarize_features(part, options.callouts)
    u, v, n, reason = choose_front(geometry, features)
    front = _Box.of(geometry, u, v)
    top_u, top_v = u, _neg(n)  # seen from +v: u right, the viewer's side down
    if options.projection == "first":  # the left view, placed on the right
        side_u, side_v, side_type = n, v, "left"
    else:  # the right view, placed on the right
        side_u, side_v, side_type = _neg(n), v, "right"
    top = _Box.of(geometry, top_u, top_v)
    side = _Box.of(geometry, side_u, side_v)
    notes = [f"front view {reason}"]

    seen_along = {axis_key(h.axis) for h in features.holes} | {
        axis_key(x) for x in features.profile_normals}
    need_side = axis_key(u) in seen_along
    need_top = axis_key(v) in seen_along
    depth = top.y1 - top.y0
    thin = depth <= 0.25 * min(front.x1 - front.x0, front.y1 - front.y0)
    turned = features.symmetry_axis is not None and abs(
        abs(dot(features.symmetry_axis[1], u)) - 1) < AXIS_TOL
    if turned:
        slots = ["side"]
        notes.append("a part of revolution: the front view and the end view")
    elif thin and not (need_side and need_top):
        slots = ["side"] if need_side and not need_top else ["top"]
        notes.append(f"a thin part ({_fmt(depth)} mm): one view for its thickness")
    else:
        slots = ["top", "side"]
    section = plan_section(features, u, v, n, front) if options.sections else None
    letters = iter("ABCDEFGHJKLMNP")
    if section is not None:
        slot = "top" if section.horizontal else "side"
        if slot not in slots:
            slots.append(slot)
        label = {"straight": "section", "stepped": "stepped section",
                 "aligned": "aligned section"}[section.kind]
        notes.append(f"{label} {next(letters)}-A through {', '.join(section.cut)}")
    section_slot = None if section is None else ("top" if section.horizontal else "side")
    if section is not None and section.span is not None and section.horizontal:
        top = _Box(min(top.x0, section.span[0]), max(top.x1, section.span[1]), top.y0, top.y1)
    elif section is not None and section.span is not None:
        side = _Box(side.x0 - (section.span[1] - section.span[0]) / 2,
                    side.x1 + (section.span[1] - section.span[0]) / 2, side.y0, side.y1)
    side_name = f"{side_type.capitalize()} view"
    parents = [("Front view", u, v, n, front)]
    if "top" in slots and section_slot != "top":
        parents.append(("Top view", top_u, top_v, cross(top_u, top_v), top))
    if "side" in slots and section_slot != "side":
        parents.append((side_name, side_u, side_v, cross(side_u, side_v), side))
    aux = choose_auxiliary(geometry, parents, features) if options.auxiliary else None
    if isinstance(aux, str):
        notes.append(aux)
        aux = None
    aux_label = next(letters) if aux is not None else None
    if aux is not None:
        notes.append(f"auxiliary view {aux_label} along the inclined face's normal "
                     f"({', '.join(_fmt(c) for c in aux.normal)}), from the {aux.parent}")
    detail_label = next(letters)
    name = str(part.get("name") or part.get("id"))
    when = options.date or _date.today().isoformat()
    deferred_of: dict[int, list[ViewPlan]] = {}

    def layout(sheet: str, s: float, mode: str = "full") -> DrawingPlan | None:
        """full: every view on this sheet; main: the orthographic views here,
        the isometric and auxiliary views on further sheets if there is no
        room; front: the front view (and its section) here, the rest on
        further sheets."""
        extras = mode == "full"
        width, height = SHEETS[sheet]
        area = _drawing_area(width, height)
        title = _title_box(width)
        fw, fh = front.size(s)
        dx = max(fw, (top.x1 - top.x0) * s if "top" in slots else 0.0) / 2 + 2 * PAD + GAP + (
            side.x1 - side.x0) * s / 2
        dy = fh / 2 + 2 * PAD + GAP + (top.y1 - top.y0) * s / 2
        below = options.projection == "first"
        if section is not None and section.horizontal and not below:
            dy += LABEL  # the section's label sits between it and the front view
        # The group's corner: the front view's padded top left at the area's.
        top_above = "top" in slots and not below
        tw = (top.x1 - top.x0) * s if "top" in slots else 0.0  # an aligned section is wider
        fx = area[0] + PAD + max(fw, tw) / 2
        fy = area[3] - PAD - fh / 2
        if top_above:
            fy -= dy - fh / 2 + (top.y1 - top.y0) * s / 2
        views = [_place("Front view", "front", u, v, front, s, (fx, fy))]
        elsewhere: list[ViewPlan] = []
        for slot in slots:
            is_section = section is not None and slot == section_slot
            if mode == "front" and not is_section:
                box, pu, pv, kind = ((top, top_u, top_v, "top") if slot == "top"
                                     else (side, side_u, side_v, side_type))
                elsewhere.append(_place("Top view" if slot == "top" else side_name,
                                        "projection", pu, pv, box, s, (0.0, 0.0),
                                        parent="Front view", projection=kind, independent=True))
                continue
            if slot == "top":
                centre = (fx, fy - dy if below else fy + dy)
                if is_section:
                    views.append(_place("Section A-A", "section", top_u, top_v, top, s, centre,
                                        parent="Front view", label="A",
                                        profile=section.profile,
                                        profile_type=section.profile_type))
                else:
                    views.append(_place("Top view", "projection", top_u, top_v, top, s, centre,
                                        parent="Front view", projection="top"))
            else:
                centre = (fx + dx, fy)
                if is_section:
                    views.append(_place("Section A-A", "section", side_u, side_v, side, s,
                                        centre, parent="Front view", label="A",
                                        profile=section.profile,
                                        profile_type=section.profile_type))
                else:
                    views.append(_place(side_name, "projection", side_u, side_v, side, s,
                                        centre, parent="Front view", projection=side_type))
        taken = [title] + [view.padded() for view in views]
        if not all(_inside(view.padded(), area) for view in views) or any(
                _overlap(view.padded(), title) for view in views):
            return None
        extra_notes: list[str] = []
        deferred: list[ViewPlan] = list(elsewhere)
        if aux is not None:
            parent_view = next(view for view in views if view.name == aux.parent)
            made = _aux_view(aux, aux_label, parent_view, geometry, s, area, taken)
            if made is None and extras:
                return None
            if made is None:
                deferred.append(_aux_view(aux, aux_label, parent_view, geometry, s, None, []))
                extra_notes.append(f"no room for view {aux_label} on this sheet")
            else:
                views.append(made)
                taken.append(made.padded())
        if options.isometric:
            iso = _iso_view(geometry, u, v, n, s, area, taken) if extras else None
            if iso is None and extras:
                return None
            if iso is None:
                deferred.append(_iso_view(geometry, u, v, n, s, None, []))
            else:
                views.append(iso)
                taken.append(iso.padded())
        if options.details:
            detail = _detail_view(features, u, v, n, s, area, taken, front, detail_label)
            if isinstance(detail, ViewPlan):
                views.append(detail)
                taken.append(detail.padded())
            elif detail:
                extra_notes.append(detail)
        block = _title(name, str(part.get("id")), s, sheet, options.projection, width, height,
                       part.get("material"), when)
        plan = DrawingPlan(name, sheet, width, height, options.projection, s, views, block,
                           _hole_annotations(features, views), geometry_source=geometry.source,
                           notes=notes + extra_notes)
        if options.centre_lines:
            plan.centre_lines = plan_centre_lines(views, features, geometry)
        deferred_of[id(plan)] = deferred
        return plan

    def attempt(mode: str) -> DrawingPlan | None:
        try:
            return _choose(lambda sheet, s: layout(sheet, s, mode), options)
        except DrawingPlanError:
            return None

    plan = attempt("full")
    largest = _sheets(options)[-1]
    if plan is None or ((options.isometric or aux is not None) and plan.sheet == largest):
        # Further sheets when the views do not fit the largest sheet allowed,
        # or fit it only at a smaller scale than the orthographic views alone.
        main = attempt("main")
        if plan is None or (main is not None and main.scale > plan.scale + 1e-12):
            plan = main or attempt("front")
            if plan is None:
                raise DrawingPlanError(
                    "the front view does not fit any sheet" + (
                        f" at {scale_label(options.scale)}" if options.scale else ""))
            plan.notes.append(f"the views do not fit one {largest} sheet at "
                              f"{plan.scale_label}: further sheets")
    deferred = deferred_of.get(id(plan), [])
    if deferred:
        plan.more = _extra_sheets(deferred, plan, features, geometry, options, name,
                                  str(part.get("id")), part.get("material"), when)
    number_sheets(plan)
    return plan


# ── Auxiliary views ──

AUX_MIN_SHARE = 0.1  # of the part's largest projected area
AUX_MIN_SLOPE = 15.0  # degrees off every main axis: a draft is no slope worth a view


@dataclass
class _Auxiliary:
    parent: str  # the parent view's name
    normal: Vec  # the inclined face's outward normal: the viewer's side
    u: Vec
    v: Vec
    centre: Vec  # a point of the face
    parent_box: _Box


def choose_auxiliary(geometry: PartGeometry, parents: list[tuple[str, Vec, Vec, Vec, _Box]],
                     features: FeatureSummary | None = None) -> _Auxiliary | str | None:
    """The inclined planar face whose true shape the main views miss, seen
    edge on in a main view: the auxiliary view looks along its normal from
    that view. A face holes are drilled into first, else the largest, at
    least a tenth of the part's largest projected area (not a chamfer), and
    only when the part has one or two such slopes (a hexagonal prism's sides
    need none). A note when the face is oblique to every main view."""
    merged: dict[tuple[float, ...], Face] = {}
    for face in geometry.inclined:
        key = tuple(round(c, 5) for c in face.normal)
        if key in merged:
            old = merged[key]
            merged[key] = Face(old.normal, old.area + face.area,
                               old.centre if old.area >= face.area else face.centre)
        else:
            merged[key] = face
    largest = 0.0
    for key in AXES:
        others = [a for k, a in AXES.items() if k != key]
        lo0, hi0 = geometry.span(others[0])
        lo1, hi1 = geometry.span(others[1])
        largest = max(largest, (hi0 - lo0) * (hi1 - lo1))
    drilled = [h.axis for h in (features.holes if features else [])]

    def holed(face: Face) -> bool:
        return any(abs(dot(axis, face.normal) + 1) < 1e-6 for axis in drilled)

    def chamfer(face: Face) -> bool:
        return any(norm(add(face.centre, scale(point, -1))) <= 2 * size + 1e-6
                   for point, size in (features.chamfers if features else []))

    steep = math.cos(math.radians(AUX_MIN_SLOPE))
    candidates = [f for f in merged.values()
                  if max(abs(c) for c in f.normal) <= steep + 1e-9 and not chamfer(f)]
    faces = sorted((f for f in candidates if f.area >= AUX_MIN_SHARE * largest or holed(f)),
                   key=lambda f: (not holed(f), -f.area, f.normal))
    if not faces or (len(faces) > 2 and not holed(faces[0])):
        return None
    for face in faces:
        for name, pu, pv, pn, box in parents:
            if abs(dot(face.normal, pn)) < 1e-6:
                au = unit(cross(pn, face.normal))
                return _Auxiliary(name, face.normal, _snap(au), _snap(cross(face.normal, au)),
                                  face.centre, box)
    return (f"an inclined face (normal {', '.join(_fmt(c) for c in faces[0].normal)}) is "
            "oblique to every main view: no auxiliary view (a secondary auxiliary view is "
            "not done)")


def _aux_view(aux: _Auxiliary, label: str, parent: ViewPlan, geometry: PartGeometry,
              s: float, area, taken) -> ViewPlan | None:
    """View `label` along the face's normal at the sheet's scale, its cutting
    line and arrow in the parent: next to the parent in the arrow's
    direction, else anywhere there is room, else None. Without an area, a
    view to place on another sheet."""
    extents = _Box.of(geometry, aux.u, aux.v)
    w, h = extents.size(s)
    nu, nv = dot(aux.normal, parent.u), dot(aux.normal, parent.v)
    length = math.hypot(nu, nv) or 1.0
    nu, nv = nu / length, nv / length
    cx, cy = view_point(parent.u, parent.v, aux.centre)
    tu, tv = dot(aux.u, parent.u), dot(aux.u, parent.v)
    lo, hi = geometry.span(aux.u)
    at = dot(aux.centre, aux.u)
    profile = [cx + tu * (lo - at), cy + tv * (lo - at), cx + tu * (hi - at), cy + tv * (hi - at)]
    box = aux.parent_box
    edge = max(nu * (x - cx) + nv * (y - cy) for x in (box.x0, box.x1) for y in (box.y0, box.y1))
    head, tail = edge + 4.0 / s, edge + 16.0 / s
    arrow = (cx + nu * tail, cy + nv * tail, cx + nu * head, cy + nv * head)
    extra = {"parent": parent.name, "label": label, "profile": [round(c, 9) for c in profile],
             "arrow": tuple(round(c, 9) for c in arrow)}
    name = f"View {label}"
    if area is None:
        return _place(name, "auxiliary", aux.u, aux.v, extents, s, (0.0, 0.0), independent=True,
                      **extra)
    pw, ph = parent.box[2] - parent.box[0], parent.box[3] - parent.box[1]
    reach = (abs(nu) * pw + abs(nv) * ph) / 2 + 2 * PAD + GAP + LABEL + (
        abs(nu) * w + abs(nv) * h) / 2
    px, py = (parent.box[0] + parent.box[2]) / 2, (parent.box[1] + parent.box[3]) / 2
    view = _place(name, "auxiliary", aux.u, aux.v, extents, s,
                  (px + nu * reach, py + nv * reach), **extra)
    if _inside(view.padded(), area) and not any(_overlap(view.padded(), t) for t in taken):
        return view
    spot = _free_spot((w + 2 * PAD, h + 2 * PAD + LABEL), area, taken)
    if spot is None:
        return None
    return _place(name, "auxiliary", aux.u, aux.v, extents, s, (spot[0], spot[1] + LABEL / 2),
                  **extra)


# ── Further sheets ──


def _extra_sheets(deferred: list[ViewPlan], first: DrawingPlan, features: FeatureSummary,
                  geometry: PartGeometry, options: DrawingOptions, name: str, number: str,
                  material: str | None, when: str) -> list[DrawingPlan]:
    """The views that found no room on the first sheet, on further sheets:
    all on the smallest allowed sheet that takes them at the first sheet's
    scale, else one per sheet, at the largest scale that fits."""
    out: list[DrawingPlan] = []
    remaining = list(deferred)
    while remaining:
        packed = None
        for sheet in _sheets(options):  # all of them at the first sheet's scale
            views = _pack(remaining, sheet, first.scale)
            if views is not None:
                packed = (sheet, first.scale, views, len(remaining))
                break
        for scale_value in [x for x in SCALES if x <= first.scale + 1e-12]:
            if packed:
                break
            for sheet in _sheets(options):  # else the next one alone, as large as it fits
                views = _pack(remaining[:1], sheet, scale_value)
                if views is not None:
                    packed = (sheet, scale_value, views, 1)
                    break
        if packed is None:
            first.notes.append(f"no room for {remaining[0].name} on any allowed sheet")
            remaining.pop(0)
            continue
        sheet, scale_value, views, count = packed
        remaining = remaining[count:]
        width, height = SHEETS[sheet]
        block = _title(name, number, scale_value, sheet, options.projection, width, height,
                       material, when)
        plan = DrawingPlan(name, sheet, width, height, options.projection, scale_value, views,
                           block, _hole_annotations(features, views),
                           geometry_source=geometry.source,
                           notes=[", ".join(v.name for v in views)
                                  + ": no room on the first sheet"])
        if options.centre_lines:
            plan.centre_lines = plan_centre_lines(views, features, geometry)
        out.append(plan)
    return out


def _pack(group: list[ViewPlan], sheet: str, s: float) -> list[ViewPlan] | None:
    width, height = SHEETS[sheet]
    area = _drawing_area(width, height)
    taken = [_title_box(width)]
    views = []
    for view in group:
        ratio = s / view.scale
        w = (view.box[2] - view.box[0]) * ratio
        h = (view.box[3] - view.box[1]) * ratio
        below = LABEL if view.label else 0.0
        spot = _free_spot((w + 2 * PAD, h + 2 * PAD + below), area, taken)
        if spot is None:
            return None
        centre = (spot[0], spot[1] + below / 2)
        cx, cy = (view.box[0] + view.box[2]) / 2, (view.box[1] + view.box[3]) / 2
        # The model point at the box's centre stays there: anchor moves with it.
        mx, my = (cx - view.anchor[0]) / view.scale, (cy - view.anchor[1]) / view.scale
        anchor = (centre[0] - s * mx, centre[1] - s * my)
        placed = ViewPlan(**{**{k: getattr(view, k) for k in view.__dataclass_fields__},
                             "scale": s, "anchor": anchor,
                             "box": (centre[0] - w / 2, centre[1] - h / 2,
                                     centre[0] + w / 2, centre[1] + h / 2),
                             "independent": True})
        views.append(placed)
        taken.append(placed.padded())
    return views


def number_sheets(plan: DrawingPlan) -> None:
    """Each sheet's title block says which sheet of how many it is."""
    sheets = plan.sheets
    for index, sheet in enumerate(sheets, start=1):
        for cell in sheet.title.cells:
            if cell.label == "Sheet":
                cell.value = f"{sheet.sheet} {index}/{len(sheets)}"


def _iso_view(geometry: PartGeometry, u: Vec, v: Vec, n: Vec, s: float, area, taken,
              name: str = "Isometric view") -> ViewPlan | None:
    """An isometric view seen from the front, the right and above, at the
    sheet's scale, or smaller (down to a quarter) where there is room.
    Without an area, a view at the sheet's scale to place on another sheet."""
    d = unit(add(add(n, u), v))
    iso_v = unit(add(v, scale(d, -dot(v, d))))
    iso_u = cross(iso_v, d)
    extents = _Box.of(geometry, iso_u, iso_v)
    if area is None:
        return _place(name, "isometric", iso_u, iso_v, extents, s, (0.0, 0.0), independent=True)
    for k in (1.0, 0.5, 0.4, 0.25):
        candidates = [x for x in SCALES if x <= s * k + 1e-12]
        if not candidates:
            continue
        si = max(candidates)
        w, h = extents.size(si)
        spot = _free_spot((w + 2 * PAD, h + 2 * PAD), area, taken)
        if spot is not None:
            return _place(name, "isometric", iso_u, iso_v, extents, si, spot)
    return None


def _detail_view(features: FeatureSummary, u: Vec, v: Vec, n: Vec, s: float, area, taken,
                 front: _Box, label: str = "B") -> ViewPlan | str | None:
    """A circular detail of the smallest feature seen in the front view, when
    it is too small on paper; None when none is needed, a note when there is
    no room."""
    seen = [f for f in features.small
            if front.x0 - 1e-6 <= dot(f.point, u) <= front.x1 + 1e-6
            and front.y0 - 1e-6 <= dot(f.point, v) <= front.y1 + 1e-6]
    if not seen:
        return None
    small = min(seen, key=lambda f: f.size)
    if small.size * s >= SMALL_ON_PAPER:
        return None
    cx, cy = view_point(u, v, small.point)
    radius = 1.5 * small.size
    best = _detail_scale(s, small.size)
    for ds in [x for x in SCALES if 2 * s - 1e-12 <= x <= best]:  # largest first
        size = 2 * radius * ds
        spot = _free_spot((size + 2 * PAD, size + 2 * PAD + LABEL), area, taken)
        if spot is None:
            continue
        box = _Box(cx - radius, cx + radius, cy - radius, cy + radius)
        centre = (spot[0], spot[1] + LABEL / 2)
        return _place(f"Detail {label}", "detail", u, v, box, ds, centre, parent="Front view",
                      label=label, detail=(cx, cy, radius))
    return f"no room for a detail view of {small.feature} ({_fmt(small.size)} mm)"


CALLOUT_VIEWS = ("front", "projection", "auxiliary")


def _hole_annotations(features: FeatureSummary, views: list[ViewPlan]) -> list[Annotation]:
    """One callout per hole feature, in the first view that shows its holes
    as circles, next to its first hole, with a leader to the hole's edge."""
    out = []
    for hole in features.holes:
        if not hole.positions:
            continue
        for view in views:
            if view.kind not in CALLOUT_VIEWS or not _seen_end_on(hole.axis,
                                                                    cross(view.u, view.v)):
                continue
            hx, hy = view_point(view.u, view.v, hole.positions[0])
            r = hole.diameter / 2
            lead = (hx + r * math.sqrt(0.5), hy + r * math.sqrt(0.5))
            off = 8.0 / view.scale  # 8 mm of paper away
            out.append(Annotation(view.name, hole.text, lead[0] + off, lead[1] + off, lead,
                                  hole.plain if hole.plain != hole.text else None))
            break
    return out


# ── Centre lines ──

CENTRE_OVERSHOOT = 3.0  # mm of paper beyond the outline (ISO 128-24: a short distance)
CENTRE_VIEWS = ("front", "projection", "section", "auxiliary")


def _on_polyline(point: tuple[float, float], profile: list[float], tol: float = 1e-3) -> bool:
    px, py = point
    pts = list(zip(profile[::2], profile[1::2]))
    for (ax, ay), (bx, by) in pairwise(pts):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 < 1e-18 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy)
                                                     / length2))
        if math.hypot(px - ax - t * dx, py - ay - t * dy) < tol:
            return True
    return False


def _pitch_circle(hole: HoleCallout, u: Vec, v: Vec) -> tuple[float, float, float] | None:
    """The circle three or more holes of one feature sit on, about their
    centroid, in view coordinates."""
    if len(hole.positions) < 3:
        return None
    points = [view_point(u, v, p) for p in hole.positions]
    cx = sum(x for x, _ in points) / len(points)
    cy = sum(y for _, y in points) / len(points)
    radii = [math.hypot(x - cx, y - cy) for x, y in points]
    if min(radii) < 1e-6 or max(radii) - min(radii) > 1e-6:
        return None
    return (round(cx, 6), round(cy, 6), round(radii[0], 6))


def plan_centre_lines(views: list[ViewPlan], features: FeatureSummary,
                      geometry: PartGeometry) -> list[CentreLine]:
    """Centre marks on every hole and cylinder seen end on, axis lines on
    every hole, cylinder and revolution axis seen side on, in each
    orthographic view, and the pitch circle of holes on a bolt circle; a
    section only shows those its cutting line meets (an aligned section
    unfolded). Lines overshoot the outline by 3 mm of paper; collinear axes
    merge."""
    by_name = {view.name: view for view in views}
    out: list[CentreLine] = []
    for view in views:
        if view.kind in CENTRE_VIEWS:
            parent = by_name.get(view.parent) if view.kind == "section" else None
            out += _view_centre_lines(view, parent, features, geometry)
    return out


def _view_centre_lines(view: ViewPlan, parent: ViewPlan | None, features: FeatureSummary,
                       geometry: PartGeometry) -> list[CentreLine]:
    out: list[CentreLine] = []
    revolution = ([features.symmetry_axis] if features.symmetry_axis else []) + list(
        features.revolve_axes)
    n = cross(view.u, view.v)
    e = CENTRE_OVERSHOOT / view.scale
    marks: dict[tuple[float, float], list[Any]] = {}
    axes: dict[tuple[float, ...], list[Any]] = {}

    def cut(point: Vec) -> bool:
        if view.kind != "section":
            return True
        if parent is None or not view.profile:
            return False
        return _on_polyline(view_point(parent.u, parent.v, point), view.profile)

    unfold = (view.kind == "section" and view.profile_type == "Aligned" and parent is not None
              and len(view.profile or []) == 6 and view.u == parent.u)

    def project(point: Vec) -> tuple[float, float]:
        """The point in the view; in an aligned section, the second cutting
        line turned into the first one's plane, about the centre."""
        x, y = view_point(view.u, view.v, point)
        if not unfold:
            return x, y
        px, py = view_point(parent.u, parent.v, point)
        ax, _, cx, cy, bx, by = view.profile
        distance = math.hypot(px - cx, py - cy)
        side = math.copysign(1.0, ax - cx)
        if _on_polyline((px, py), [cx, cy, bx, by]) and distance > 1e-9:
            side = -side
        return cx + side * distance, y

    def mark(point: Vec, radius: float, what: str) -> None:
        x, y = project(point)
        key = (round(x, 4), round(y, 4))
        if key not in marks or marks[key][2] < radius:
            marks[key] = [x, y, radius, what]

    def axis(point: Vec, direction: Vec, t0: float, t1: float, what: str) -> None:
        x0, y0 = project(add(point, scale(direction, t0 - e)))
        x1, y1 = project(add(point, scale(direction, t1 + e)))
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-9:
            return
        dx, dy = (x1 - x0) / length, (y1 - y0) / length
        if dx < -1e-9 or (abs(dx) < 1e-9 and dy < 0):
            dx, dy = -dx, -dy
        key = (round(dx, 5), round(dy, 5), round(x0 * dy - y0 * dx, 4))
        a, b = sorted((x0 * dx + y0 * dy, x1 * dx + y1 * dy))
        if key in axes:
            axes[key][0] = min(axes[key][0], a)
            axes[key][1] = max(axes[key][1], b)
        else:
            axes[key] = [a, b, x0, y0, dx, dy, what]

    def span_from(point: Vec, direction: Vec) -> tuple[float, float]:
        lo, hi = geometry.span(direction)
        at = dot(point, direction)
        return lo - at, hi - at

    for hole in features.holes:
        pitch = _pitch_circle(hole, view.u, view.v)
        if pitch is not None and view.kind != "section" and _seen_end_on(hole.axis, n):
            out.append(CentreLine(view.name, "circle", [], hole.feature, pitch))
        for p in hole.positions:
            if not cut(p):
                continue
            if _seen_end_on(hole.axis, n):
                mark(p, hole.outer / 2, hole.feature)
            elif abs(dot(hole.axis, n)) < 1e-6:
                length = hole.length if hole.length is not None else max(
                    span_from(p, hole.axis)[1], 0.0)
                axis(p, hole.axis, 0.0, length, hole.feature)
    for cylinder in features.cylinders:
        if not cut(cylinder.centre):
            continue
        if _seen_end_on(cylinder.axis, n):
            mark(cylinder.centre, cylinder.radius, cylinder.feature)
        elif abs(dot(cylinder.axis, n)) < 1e-6:
            axis(cylinder.centre, cylinder.axis, 0.0, cylinder.length, cylinder.feature)
    for origin, direction in revolution:
        if not cut(origin):
            continue
        if _seen_end_on(direction, n):
            half = max(view.box[2] - view.box[0], view.box[3] - view.box[1]) / 2 / view.scale
            mark(origin, half, "revolution axis")
        elif abs(dot(direction, n)) < 1e-6:
            lo, hi = span_from(origin, direction)
            axis(origin, direction, lo, hi, "revolution axis")
    for x, y, radius, what in marks.values():
        reach = radius + e
        out.append(CentreLine(view.name, "mark", [
            (round(x - reach, 6), round(y, 6), round(x + reach, 6), round(y, 6)),
            (round(x, 6), round(y - reach, 6), round(x, 6), round(y + reach, 6))], what))
    for a, b, x0, y0, dx, dy, what in axes.values():
        # The point of the line nearest the origin, then along it.
        t0 = x0 * dx + y0 * dy
        bx, by = x0 - t0 * dx, y0 - t0 * dy
        out.append(CentreLine(view.name, "axis", [
            (round(bx + a * dx, 6), round(by + a * dy, 6), round(bx + b * dx, 6),
             round(by + b * dy, 6))], what))
    return out


# ── Assemblies ──


def _transform(rows: list[list[float]], p: Vec) -> Vec:
    return tuple(rows[i][0] * p[0] + rows[i][1] * p[1] + rows[i][2] * p[2] + rows[i][3]
                 for i in range(3))  # type: ignore[return-value]


def assembly_geometry(instances: list[tuple[list[list[float]], PartGeometry]]) -> PartGeometry:
    """The world bounding box of every instance (its part's box moved by the
    instance's world rows)."""
    points = [_transform(rows, c) for rows, g in instances for c in g.corners()]
    if not points:
        raise DrawingPlanError("an assembly without instances")
    lo = tuple(min(p[i] for p in points) for i in range(3))
    hi = tuple(max(p[i] for p in points) for i in range(3))
    sources = {g.source for _, g in instances}
    return PartGeometry(lo, hi, {}, sources.pop() if len(sources) == 1 else "mixed")


@dataclass
class BomItem:
    item: int
    name: str
    quantity: int
    prototype: str
    anchor: Vec  # where the balloon points: the first instance's centre (world)


def bom_items(instances: list[Any], geometries: dict[str, PartGeometry]) -> list[BomItem]:
    """One row per part, in the order parts first appear, with its instance
    count. `instances` are the assembly plan's Placed rows. Parts are counted
    by name: without the STEP file, two tree parts named alike (two bolts)
    are two prototypes of one part."""
    items: dict[str, BomItem] = {}
    for placed in instances:
        if placed.name in items:
            items[placed.name].quantity += 1
            continue
        geometry = geometries.get(placed.prototype)
        local = geometry.centre() if geometry else (0.0, 0.0, 0.0)
        items[placed.name] = BomItem(len(items) + 1, placed.name, 1, placed.prototype,
                                     _transform(placed.world, local))
    return list(items.values())


def plan_assembly_drawing(name: str, items: list[BomItem], geometry: PartGeometry,
                          options: DrawingOptions | None = None) -> DrawingPlan:
    """A front and an isometric view of the product, the bill of materials
    above the title block, one balloon per row in the front view."""
    options = options or DrawingOptions()
    u, v, n, reason = choose_front(geometry, FeatureSummary(), assembly=True)
    front = _Box.of(geometry, u, v)
    rows = [["Item", "Part", "Qty"]] + [[str(i.item), i.name, str(i.quantity)] for i in items]

    def layout(sheet: str, s: float) -> DrawingPlan | None:
        width, height = SHEETS[sheet]
        area = _drawing_area(width, height)
        title = _title_box(width)
        bom = BomTable((title[0], title[3] + BOM_ROW * len(rows)), BOM_COLUMNS, BOM_ROW, rows)
        fw, fh = front.size(s)
        balloon_room = 15.0  # balloons sit around the front view
        centre = (area[0] + PAD + balloon_room + fw / 2, area[3] - PAD - balloon_room - fh / 2)
        view = _place("Front view", "front", u, v, front, s, centre)
        padded = view.padded()
        padded = (padded[0] - balloon_room, padded[1] - balloon_room,
                  padded[2] + balloon_room, padded[3] + balloon_room)
        taken = [title, bom.box(), padded]
        if not _inside(padded, area) or _overlap(padded, title) or _overlap(padded, bom.box()):
            return None
        if bom.box()[3] > area[3]:
            return None
        views = [view]
        if options.isometric:
            iso = _iso_view(geometry, u, v, n, s, area, taken)
            if iso is None:
                return None
            views.append(iso)
        balloons = []
        cx, cy = front.centre()
        for item in items:
            px, py = view_point(u, v, item.anchor)
            dx, dy = px - cx, py - cy
            length = math.hypot(dx, dy) or 1.0
            if math.hypot(dx, dy) < 1e-9:
                dx, dy = 1.0, 1.0
                length = math.sqrt(2)
            # Out of the view's box, 10 mm of paper beyond its edge.
            reach = math.hypot((front.x1 - front.x0) / 2, (front.y1 - front.y0) / 2) + 10.0 / s
            balloons.append(Balloon(item.item, "Front view", cx + dx / length * reach,
                                    cy + dy / length * reach, (px, py)))
        block = _title(name, name, s, sheet, options.projection, width, height, None,
                       options.date or _date.today().isoformat())
        return DrawingPlan(name, sheet, width, height, options.projection, s, views, block,
                           bom=bom, balloons=balloons, geometry_source=geometry.source,
                           notes=[f"front view {reason}"])

    return _choose(layout, options)


# ── Every drawing of a tree ──


def plan_drawings(tree: dict[str, Any], assembly: Any = None,
                  options: Any = None) -> tuple[dict[str, Any], Any, list[str]]:
    """The drawing plan of every built part (by part id) and of the
    assembly, if any, with the reasons a plan could not be made. Offline."""
    import copy

    from catia_mcp.tree.placement import place_part

    options = options or DrawingOptions()
    plans: dict[str, Any] = {}
    geometries: dict[str, Any] = {}
    problems: list[str] = []
    for part in tree["parts"]:
        try:
            placed = place_part(part)
            geometries[part["id"]] = part_geometry(placed)
            plans[part["id"]] = plan_part_drawing(placed, geometries[part["id"]], options)
        except Exception as error:  # reported with the part
            problems.append(f"{part['id']}: {error}")
    product = None
    if assembly is not None:
        try:
            items = bom_items(assembly.instances, geometries)
            world = assembly_geometry([(i.world, geometries[i.prototype])
                                       for i in assembly.instances
                                       if i.prototype in geometries])
            product = plan_assembly_drawing(assembly.root.name, items, world, options)
        except Exception as error:
            problems.append(f"assembly {assembly.root.name}: {error}")
    if product is not None and options.assembly_sheets:
        # One sheet (or more) per distinct part after the product's, in the
        # bill of materials' order; every prototype of that part points to it.
        for item in items:
            part_plan = plans.get(item.prototype)
            if part_plan is None:
                continue
            index = len(product.sheets) + 1
            for sheet in part_plan.sheets:
                copied = copy.deepcopy(sheet)
                copied.more, copied.source = [], item.prototype
                product.more.append(copied)
            for placed in assembly.instances:
                if placed.name == item.name and placed.prototype in plans:
                    product.covers[placed.prototype] = index
        number_sheets(product)
    return plans, product, problems


def main(argv: list[str] | None = None) -> int:
    """python -m catia_mcp.tree.drawing_plan TREE.json [--step MODEL.step]:
    the drawings --drawing auto would make, offline, as JSON."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m catia_mcp.tree.drawing_plan",
                                     description="The drawings --drawing auto would make, "
                                                 "offline, as JSON.")
    parser.add_argument("tree", help="feature tree JSON (version 1)")
    parser.add_argument("--step", help="the model's STEP file: its assembly, for the product's "
                        "drawing (needs OCP)")
    parser.add_argument("--assembly", action="store_true",
                        help="also plan the product's drawing without a STEP file")
    parser.add_argument("--projection", default="first", choices=["first", "third"])
    parser.add_argument("--sheet", default="auto", choices=["auto", *SHEET_ORDER])
    parser.add_argument("--max-sheet", default="A0", choices=SHEET_ORDER,
                        help="the largest sheet allowed; beyond it, further sheets")
    parser.add_argument("--callouts", default="symbols", choices=["symbols", "text"],
                        help="hole callouts with ISO 129 symbols or in words")
    parser.add_argument("--report", default="drawing_plan.json")
    args = parser.parse_args(argv)
    with open(args.tree, encoding="utf-8") as handle:
        tree = json.load(handle)
    assembly = None
    if args.step or args.assembly:
        from catia_mcp.tree.assembly import plan_assembly

        structure = None
        if args.step:
            from catia_mcp.tree.step_structure import read

            structure = read(args.step)
        assembly, problems = plan_assembly(tree, structure)
        if problems:
            print("The STEP file does not match the tree:\n  " + "\n  ".join(problems))
            return 1
        tree = assembly.tree
    options = DrawingOptions(sheet=args.sheet, projection=args.projection,
                             max_sheet=args.max_sheet, callouts=args.callouts)
    plans, product, problems = plan_drawings(tree, assembly, options)
    out = {"parts": {k: p.to_dict() for k, p in plans.items()},
           "assembly": product.to_dict() if product else None, "problems": problems}
    for label, plan in [*plans.items(), ("assembly", product)]:
        if plan is not None:
            print(f"{label}: " + "; ".join(
                f"{p.sheet} {p.scale_label}, {', '.join(v.name for v in p.views)}"
                for p in plan.sheets))
    for problem in problems:
        print(f"no drawing: {problem}")
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2, ensure_ascii=False)
    print(f"Report: {args.report}")
    return 1 if problems else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
