"""3D curves and more surface features of the tree (Generative Shape Design,
second slice), without a kernel. The contract is in docs/surfaces.md
("Contract additions requested from the app"); surfaces.py holds the first
slice and calls the checks here.

- `curve`: `op` one of
  - `helix`: `axis` {origin, direction}, `pitch`, `height`, `radius`, `hand`
    (`right`, the default, or `left`), `taper` (degrees, build123d's cone
    angle, default 0): build123d's `Helix(pitch, height, radius, center,
    direction, cone_angle, lefthand)`. It starts at `origin + radius * X`,
    X being OpenCascade's reference direction for the axis (`occ_x_dir`);
  - `spline`: `points` (two or more), optional `tangents` (one per point, or
    two: start and end) and `closed`: build123d's `Spline`;
  - `polyline`: `points` (two or more), `closed`;
  - `circle`: `center`, `normal`, `radius`: a whole circle;
  - `project`: `inputs` [curve or sketch, surface], optional `direction`
    (along it, the nearest solution; else normal to the surface);
  - `intersect`: `inputs` [surface, surface], or [surface] and a `plane`
    {origin, normal};
  and `creates` [one curve name].
- `surface` ops that take curves: `extrude` (`inputs` [curve], `direction`
  the full vector), `revolve` (`inputs` [curve]), `fill` (`inputs` [a closed
  planar curve]), `sweep` (`inputs` [sketch or curve], `guide` a curve name
  instead of `path`).
- `surface` ops without inputs: `sphere` (`center`, `radius`), `cylinder`
  (`axis` {origin, direction}, `radius`, `length` from the origin along the
  direction).
- `surface` transforms of one surface (`inputs` [surface]): `translate`
  (`vector`), `rotate` (`axis`, `angle` degrees, right-handed), `mirror`
  (`plane` {origin, normal}), `scale` (`center`, `ratio`), `affinity`
  (`origin`, `ratios` [x, y, z] along the X, Y, Z axes).
- `surface` `op: loft` through three or more sections, `ruled: true`: one
  ruled band per pair of sections, joined.
- `trim_surfaces`: `inputs` [two surfaces], `keep_points` [a point on the
  part of each to keep], `creates` [name]: each surface cut by the other,
  the kept parts joined (CATIA's Trim).
- `extract`: `bodies` [one body], `face` (a face anchor, as a shell's
  faces), `creates` [name]: the face as a surface.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

from catia_mcp.tree.geometry import Vec, add, cross, dot, scale, unit

CURVE_OPS = ("helix", "spline", "polyline", "circle", "project", "intersect")
# Surface ops of this slice (surfaces.py dispatches them here).
PRIMITIVE_OPS = ("sphere", "cylinder")
TRANSFORM_OPS = ("translate", "rotate", "mirror", "scale", "affinity")
MORE_OPS = PRIMITIVE_OPS + TRANSFORM_OPS
# Surface ops whose input may be a curve.
CURVE_INPUT_OPS = ("extrude", "revolve", "fill", "sweep")
MORE_TYPES = ("curve", "trim_surfaces", "extract")
HANDS = ("right", "left")
MAX_TAPER = 80.0


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def vec(values: Any) -> Vec:
    vector = tuple(float(c) for c in values)
    if len(vector) != 3 or not all(math.isfinite(c) for c in vector):
        raise ValueError("a point or a direction needs three finite numbers")
    return vector  # type: ignore[return-value]


def occ_x_dir(normal: Vec) -> Vec:
    """The X direction OpenCascade gives an axis system built from its main
    direction alone (gp_Ax2(P, V)): where build123d's helix starts."""
    a, b, c = unit(normal)
    aa, ab, ac = abs(a), abs(b), abs(c)
    if ab <= aa and ab <= ac:
        d = (-c, 0.0, a) if aa > ac else (c, 0.0, -a)
    elif aa <= ab and aa <= ac:
        d = (0.0, -c, b) if ab > ac else (0.0, c, -b)
    else:
        d = (-b, a, 0.0) if aa > ab else (b, -a, 0.0)
    return unit(d)


def helix_spec(feature: dict[str, Any]) -> dict[str, Any]:
    """The helix's numbers, checked: origin, unit direction, start point,
    pitch along the axis (build123d's pitch runs along a cone's side), height,
    radius, taper (degrees) and whether it is left-handed."""
    axis = feature["axis"]
    origin, direction = vec(axis["origin"]), unit(vec(axis["direction"]))
    pitch, height, radius = feature.get("pitch"), feature.get("height"), feature.get("radius")
    taper = feature.get("taper", 0) or 0
    if not all(_number(x) and x > 0 for x in (pitch, height, radius)):
        raise ValueError("a helix needs a positive pitch, height and radius")
    if not _number(taper) or abs(taper) >= MAX_TAPER:
        raise ValueError(f"a helix taper must be within +-{MAX_TAPER:g} degrees")
    hand = feature.get("hand", "right")
    if hand not in HANDS:
        raise ValueError(f"helix hand '{hand}' is not 'right' or 'left'")
    start = add(origin, scale(occ_x_dir(direction), float(radius)))
    return {"origin": origin, "direction": direction, "start": start,
            "pitch": float(pitch) * math.cos(math.radians(taper)), "height": float(height),
            "radius": float(radius), "taper": float(taper), "left": hand == "left"}


def helix_point(spec: dict[str, Any], t: float) -> Vec:
    """The helix at fraction t of its height."""
    x_dir = occ_x_dir(spec["direction"])
    y_dir = cross(spec["direction"], x_dir)
    z = t * spec["height"]
    turn = 2 * math.pi * z / spec["pitch"] * (-1 if spec["left"] else 1)
    r = spec["radius"] + z * math.tan(math.radians(spec["taper"]))
    return add(add(add(spec["origin"], scale(spec["direction"], z)),
                   scale(x_dir, r * math.cos(turn))), scale(y_dir, r * math.sin(turn)))


def helix_length(spec: dict[str, Any], steps: int = 4096) -> float:
    """The helix's length (Simpson on its speed), for the report and tests."""
    total, h = 0.0, 1.0 / steps
    previous = helix_point(spec, 0.0)
    for i in range(1, steps + 1):
        point = helix_point(spec, i * h)
        total += math.dist(previous, point)
        previous = point
    return total


def band_name(name: str, index: int) -> str:
    """The name of a ruled loft's band (between sections index and index + 1)."""
    return f"{name} band {index}"


def points_of(feature: dict[str, Any], least: int = 2) -> list[Vec]:
    points = [vec(p) for p in feature.get("points") or []]
    if len(points) < least:
        raise ValueError(f"needs at least {least} points")
    for a, b in pairwise(points):
        if math.dist(a, b) < 1e-9:
            raise ValueError("two consecutive points are the same")
    return points


def plane_of_points(points: list[Vec]) -> tuple[Vec, Vec] | None:
    """(point, unit normal) of the plane holding every point, or None."""
    for i in range(1, len(points)):
        for j in range(i + 1, len(points)):
            n = cross(add(points[i], scale(points[0], -1)), add(points[j], scale(points[0], -1)))
            if math.sqrt(dot(n, n)) > 1e-9:
                n = unit(n)
                if all(abs(dot(add(p, scale(points[0], -1)), n)) < 1e-6 for p in points):
                    return points[0], n
                return None
    return None


class MoreCheck:
    """The preflight of this slice's features for one part (used by
    surfaces.SurfaceCheck, whose `surfaces` it shares)."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        # Curve name -> {"closed": bool, "planar": bool}, None when only the
        # built curve tells (a projection, an intersection).
        self.curves: dict[str, dict[str, bool | None]] = {}

    # ── Features ──

    def check(self, feature, sketches, bodies, refuse, where) -> None:
        kind = feature.get("type")
        if kind == "curve":
            self._curve(feature, sketches, refuse, where)
        elif kind == "trim_surfaces":
            inputs = feature.get("inputs") or []
            if len(inputs) != 2 or any(name not in self.owner.surfaces for name in inputs):
                refuse("trim_surfaces must trim two known surfaces", where)
            try:
                keep = [vec(p) for p in feature.get("keep_points") or []]
                if len(keep) != 2:
                    raise ValueError
            except (TypeError, ValueError):
                refuse("trim_surfaces needs one keep point per surface", where)
            self.owner._creates(feature, refuse, where)
        elif kind == "extract":
            from catia_mcp.tree.anchors import face_anchor_problem

            targets = feature.get("bodies") or []
            if len(targets) != 1 or targets[0] not in bodies:
                refuse("extract must name exactly one known body", where)
            problem = face_anchor_problem(feature.get("face"))
            if problem:
                refuse(problem, where)
            self.owner._creates(feature, refuse, where)

    def _curve(self, feature, sketches, refuse, where) -> None:
        op = feature.get("op")
        created = feature.get("creates") or []
        if len(created) != 1:
            refuse("a curve feature must create exactly one curve", where)
        if op not in CURVE_OPS:
            refuse(f"curve op '{op}' is not supported", where)
            return
        try:
            closed, planar = self._curve_op(feature, op, sketches)
        except (KeyError, TypeError, ValueError) as error:
            refuse(f"{op} curve is not supported ({error})", where)
            closed, planar = False, False
        if len(created) == 1:
            if created[0] in self.owner.surfaces:
                refuse(f"'{created[0]}' already names a surface", where)
            self.curves[created[0]] = {"closed": closed, "planar": planar}

    def _curve_op(self, feature, op, sketches) -> tuple[bool | None, bool | None]:
        inputs = feature.get("inputs") or []
        if op == "helix":
            spec = helix_spec(feature)
            if spec["height"] / spec["pitch"] > 200:
                raise ValueError("more than 200 turns")
            return False, False
        if op in ("spline", "polyline"):
            points = points_of(feature, 2)
            closed = bool(feature.get("closed"))
            if op == "spline" and feature.get("tangents") is not None:
                tangents = [unit(vec(t)) for t in feature["tangents"]]
                if len(tangents) not in (2, len(points)):
                    raise ValueError("give two tangents (start, end) or one per point")
            if closed and len(points) < 3:
                raise ValueError("a closed curve needs three points or more")
            return closed, plane_of_points(points) is not None
        if op == "circle":
            vec(feature["center"])
            unit(vec(feature["normal"]))
            if not _number(feature.get("radius")) or feature["radius"] <= 0:
                raise ValueError("a circle needs a positive radius")
            return True, True
        if op == "project":
            if len(inputs) != 2 or inputs[1] not in self.owner.surfaces or not (
                    inputs[0] in self.curves or inputs[0] in sketches):
                raise ValueError("project a known curve or sketch onto a known surface")
            if inputs[0] in sketches:
                from catia_mcp.tree.surfaces import chain

                chain(sketches[inputs[0]])
            if feature.get("direction") is not None:
                unit(vec(feature["direction"]))
            return None, None  # known once built
        # intersect
        surfaces = self.owner.surfaces
        if feature.get("plane") is not None:
            plane = feature["plane"]
            vec(plane["origin"])
            unit(vec(plane["normal"]))
            if len(inputs) != 1 or inputs[0] not in surfaces:
                raise ValueError("intersect one known surface with the plane")
        elif len(inputs) != 2 or any(name not in surfaces for name in inputs):
            raise ValueError("intersect two known surfaces, or one and a plane")
        return None, None  # known once built

    # ── Surface ops ──

    def curve_input(self, name: str) -> dict[str, bool | None] | None:
        return self.curves.get(name)

    def surface_op(self, feature, op, inputs, refuse, where) -> None:
        """Primitives and transforms."""
        try:
            if op == "sphere":
                vec(feature["center"])
                self._positive(feature, "radius")
                if inputs:
                    raise ValueError("a sphere takes no inputs")
            elif op == "cylinder":
                vec(feature["axis"]["origin"])
                unit(vec(feature["axis"]["direction"]))
                self._positive(feature, "radius")
                if not _number(feature.get("length")) or not feature["length"]:
                    raise ValueError("a cylinder needs a non-zero length")
                if inputs:
                    raise ValueError("a cylinder takes no inputs")
            else:
                if len(inputs) != 1 or inputs[0] not in self.owner.surfaces:
                    raise ValueError(f"{op} needs one known surface as input")
                self._transform(feature, op)
        except (KeyError, TypeError, ValueError) as error:
            refuse(f"{op} surface is not supported ({error})", where)

    @staticmethod
    def _positive(feature, key) -> None:
        if not _number(feature.get(key)) or feature[key] <= 0:
            raise ValueError(f"a positive {key} is needed")

    def _transform(self, feature, op) -> None:
        if op == "translate":
            if math.dist(vec(feature["vector"]), (0, 0, 0)) < 1e-9:
                raise ValueError("the vector has no length")
        elif op == "rotate":
            vec(feature["axis"]["origin"])
            unit(vec(feature["axis"]["direction"]))
            if not _number(feature.get("angle")) or not feature["angle"]:
                raise ValueError("a rotation needs a non-zero angle")
        elif op == "mirror":
            vec(feature["plane"]["origin"])
            unit(vec(feature["plane"]["normal"]))
        elif op == "scale":
            vec(feature["center"])
            self._positive(feature, "ratio")
        else:  # affinity
            vec(feature.get("origin") or (0, 0, 0))
            ratios = vec(feature["ratios"])
            if any(r <= 0 for r in ratios):
                raise ValueError("affinity ratios must be positive")


def extent_points(feature: dict[str, Any]) -> list[Vec]:
    """Points that bound a curve or a primitive surface, for a drawing's
    bounding box when build123d is missing (drawing_plan.estimate_geometry).
    Empty for anything else, or when the feature is malformed."""
    try:
        op = feature.get("op")
        if feature.get("type") == "curve" and op == "helix":
            spec = helix_spec(feature)
            return [helix_point(spec, i / 64) for i in range(65)]
        if feature.get("type") == "curve" and op in ("spline", "polyline"):
            return [vec(p) for p in feature.get("points") or []]
        if feature.get("type") == "curve" and op == "circle":
            center, normal = vec(feature["center"]), unit(vec(feature["normal"]))
            x_dir = occ_x_dir(normal)
            r = float(feature["radius"])
            return [add(center, scale(d, s * r)) for d in (x_dir, cross(normal, x_dir))
                    for s in (-1, 1)]
        if feature.get("type") == "surface" and op == "sphere":
            center, r = vec(feature["center"]), float(feature["radius"])
            return [add(center, scale(d, s * r)) for d in ((1, 0, 0), (0, 1, 0), (0, 0, 1))
                    for s in (-1, 1)]
        if feature.get("type") == "surface" and op == "cylinder":
            origin = vec(feature["axis"]["origin"])
            d = unit(vec(feature["axis"]["direction"]))
            r = float(feature["radius"])
            ends = [origin, add(origin, scale(d, float(feature["length"])))]
            x_dir = occ_x_dir(d)
            return [add(end, scale(a, s * r)) for end in ends for a in (x_dir, cross(d, x_dir))
                    for s in (-1, 1)]
    except (KeyError, TypeError, ValueError):
        return []
    return []
