"""Surface features of the tree (Generative Shape Design), without a kernel.

Surfaces are optional tree features (a contract addition, docs/surfaces.md).
They make named surfaces, kept apart from the bodies, that later surface
features use and that `thicken` or `close_surface` turn into a body:

- `surface`: `op` one of
  - `extrude`: `inputs` [sketch], `distance` or `direction` (as an extrude's),
    `symmetric`;
  - `revolve`: `inputs` [sketch], `axis` {origin, direction}, `angle` (0, 360];
  - `loft`: `inputs` [two sketches with one closed profile each], `ruled`;
  - `sweep`: `inputs` [sketch], `path` (as a sweep's: planar, tangent
    continuous lines and arcs, the sketch perpendicular to it at its start);
  - `fill`: `inputs` [sketch with one closed chain]: the planar face inside it;
  - `offset`: `inputs` [surface], `distance` (along the surface's normal as
    build123d orients it);
  and `creates` [one surface name].
- `join_surfaces`: `inputs` [two or more surfaces], `creates` [name].
- `split_surface`: `inputs` [surface], `plane` {origin, normal}, `keep`
  `top` (the side the normal points to) or `bottom`, `creates` [name].
- `thicken`: `inputs` [surface], `thickness` (signed, along the surface's
  normal), `operation` and `creates` / `bodies` as an extrude's.
- `close_surface`: `inputs` [surface], `operation` and `creates` / `bodies`:
  the solid inside the surface, its planar openings closed.

A sketch drawn only to give a curve sets `wire: true` and needs no closed
profile. Surface inputs are one chain of lines, arcs and circles (open or
closed); spline entities are refused for now.
"""

from __future__ import annotations

import math
from typing import Any

from catia_mcp.tree.extrude import extrude_vector
from catia_mcp.tree.geometry import Arc, Frame, dot, unit
from catia_mcp.tree.loft import plan_loft
from catia_mcp.tree.sweep import JOIN_TOL, path_plane, path_segments

SURFACE_TYPES = ("surface", "join_surfaces", "split_surface", "thicken", "close_surface")
SURFACE_OPS = ("extrude", "revolve", "loft", "sweep", "fill", "offset")
# Features that make a body from a surface.
SOLID_FROM_SURFACE = ("thicken", "close_surface")
CHAIN_TYPES = ("line", "arc", "circle")
SPLIT_SIDES = ("top", "bottom")
OPERATIONS = ("new_body", "join", "cut", "intersect")
POINT_TOL = 1e-6


def _ends(entity: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if entity["type"] == "circle":
        return None
    return tuple(map(float, entity["start"])), tuple(map(float, entity["end"]))


def chain(sketch: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The sketch's entities as one chain, in order, and whether it is
    closed. Raises ValueError when they are not one chain of lines, arcs and
    circles."""
    entities = list(sketch.get("entities") or [])
    if not entities:
        raise ValueError("the sketch has no curve")
    for entity in entities:
        if entity.get("type") not in CHAIN_TYPES:
            raise ValueError(f"'{entity.get('type')}' entities in a surface's curve are not "
                             "supported yet")
    circles = [e for e in entities if e["type"] == "circle"]
    if circles:
        if len(entities) != 1:
            raise ValueError("a surface's curve is one chain: a circle must be alone")
        return entities, True
    left = entities[1:]
    ordered = [entities[0]]
    head, tail = _ends(entities[0])
    while left:
        for i, entity in enumerate(left):
            a, b = _ends(entity)
            if math.dist(a, tail) <= POINT_TOL:
                ordered.append(entity)
                tail = b
            elif math.dist(b, tail) <= POINT_TOL:
                ordered.append(entity)
                tail = a
            elif math.dist(b, head) <= POINT_TOL:
                ordered.insert(0, entity)
                head = a
            elif math.dist(a, head) <= POINT_TOL:
                ordered.insert(0, entity)
                head = b
            else:
                continue
            left.pop(i)
            break
        else:
            raise ValueError("a surface's curve must be one connected chain")
    return ordered, math.dist(head, tail) <= POINT_TOL


def chain_length_centroid(sketch: dict[str, Any]) -> tuple[float, tuple[float, float]]:
    """Length and (u, v) centroid of the sketch's curve."""
    total, mu, mv = 0.0, 0.0, 0.0
    for entity in chain(sketch)[0]:
        if entity["type"] == "line":
            (u0, v0), (u1, v1) = entity["start"], entity["end"]
            length = math.dist((u0, v0), (u1, v1))
            cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
        else:
            if entity["type"] == "circle":
                (cu, cv), r, sweep = entity["center"], float(entity["radius"]), 2 * math.pi
                mid = 0.0
            else:
                arc = Arc.through(entity["start"], entity["mid"], entity["end"])
                (cu, cv), r = arc.center, arc.radius
                sweep, mid = arc.end - arc.start, (arc.start + arc.end) / 2
            length = r * sweep
            # The centroid of an arc lies at r sin(s/2) / (s/2) along its middle.
            reach = r * math.sin(sweep / 2) / (sweep / 2) if sweep < 2 * math.pi - 1e-12 else 0.0
            cu, cv = cu + reach * math.cos(mid), cv + reach * math.sin(mid)
        total += length
        mu += length * cu
        mv += length * cv
    return total, (mu / total, mv / total)


def sweep_frame(sketch: dict[str, Any], path: list[dict[str, Any]]) -> Frame:
    """The path's plane for a surface sweep: the path planar and tangent
    continuous, the sketch perpendicular to it where it starts."""
    profile = Frame.from_tree(sketch["plane"])
    frame = path_plane(path, profile.x_dir)
    path_segments(path, frame)
    if abs(abs(dot(profile.normal, frame.x_dir)) - 1) > 1e-6:
        raise ValueError("sweep profiles that are not perpendicular to the path where it "
                         "starts are not supported yet")
    offset = tuple(a - b for a, b in zip(frame.origin, profile.origin, strict=True))
    if abs(dot(offset, profile.normal)) > JOIN_TOL:
        raise ValueError("the sweep profile's plane does not contain the start of the path")
    return frame


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SurfaceCheck:
    """The preflight of surface features for one part: which sketches are
    wires, which surfaces exist."""

    def __init__(self) -> None:
        self.surfaces: set[str] = set()

    def check(self, feature: dict[str, Any], sketches: dict[str, dict[str, Any]],
              bodies: set[str], refuse, where: str) -> None:
        kind = feature.get("type")
        inputs = feature.get("inputs") or []
        if kind == "surface":
            self._surface(feature, inputs, sketches, refuse, where)
        elif kind == "join_surfaces":
            if len(inputs) < 2 or any(name not in self.surfaces for name in inputs):
                refuse("join_surfaces must join two or more known surfaces", where)
            self._creates(feature, refuse, where)
        elif kind == "split_surface":
            self._one_surface(inputs, refuse, where, kind)
            plane = feature.get("plane") or {}
            try:
                tuple(float(c) for c in plane["origin"])
                unit(tuple(float(c) for c in plane["normal"]))
            except (KeyError, TypeError, ValueError):
                refuse("split_surface plane is invalid", where)
            if feature.get("keep") not in SPLIT_SIDES:
                refuse(f"split_surface keep '{feature.get('keep')}' is not supported", where)
            self._creates(feature, refuse, where)
        elif kind in SOLID_FROM_SURFACE:
            self._one_surface(inputs, refuse, where, kind)
            if kind == "thicken":
                thickness = feature.get("thickness")
                if not _number(thickness) or not thickness:
                    refuse("thicken needs a non-zero thickness", where)
            self._operation(feature, bodies, refuse, where, kind)

    def _surface(self, feature, inputs, sketches, refuse, where) -> None:
        op = feature.get("op")
        if op not in SURFACE_OPS:
            refuse(f"surface op '{op}' is not supported", where)
            self._creates(feature, refuse, where)
            return
        if op == "offset":
            self._one_surface(inputs, refuse, where, "an offset surface")
            if not _number(feature.get("distance")) or not feature["distance"]:
                refuse("an offset surface needs a non-zero distance", where)
            self._creates(feature, refuse, where)
            return
        wanted = 2 if op == "loft" else 1
        if len(inputs) != wanted or any(name not in sketches for name in inputs):
            refuse(f"a {op} surface needs {'two sketches' if wanted == 2 else 'one sketch'} "
                   "as inputs", where)
            self._creates(feature, refuse, where)
            return
        found = [sketches[name] for name in inputs]
        try:
            self._op(feature, op, found)
        except (KeyError, TypeError, ValueError) as error:
            refuse(f"{op} surface is not supported ({error})", where)
        self._creates(feature, refuse, where)

    @staticmethod
    def _op(feature, op, found) -> None:
        sketch = found[0]
        frame = Frame.from_tree(sketch["plane"])
        if op == "loft":
            if any(s.get("wire") or len(s.get("profiles") or []) != 1 for s in found):
                raise ValueError("loft sections must be sketches with one closed profile")
            for s in found:
                chain(s)
            plan_loft(*found)
            return
        _, closed = chain(sketch)
        if op == "extrude":
            extrude_vector(feature, frame)
        elif op == "revolve":
            angle = feature.get("angle")
            if not _number(angle) or not 0 < angle <= 360:
                raise ValueError("the angle must be in (0, 360] degrees")
            axis = feature["axis"]
            origin = tuple(float(c) for c in axis["origin"])
            direction = unit(tuple(float(c) for c in axis["direction"]))
            offset = tuple(o - p for o, p in zip(origin, frame.origin, strict=True))
            if abs(dot(direction, frame.normal)) > 1e-6 or abs(dot(offset, frame.normal)) > 1e-6:
                raise ValueError("the axis does not lie in the sketch plane")
        elif op == "sweep":
            sweep_frame(sketch, feature.get("path") or [])
        elif op == "fill" and not closed:
            raise ValueError("a fill needs a closed curve")

    def _one_surface(self, inputs, refuse, where, kind) -> None:
        if len(inputs) != 1 or inputs[0] not in self.surfaces:
            refuse(f"{kind} needs one known surface as input", where)

    def _creates(self, feature, refuse, where) -> None:
        created = feature.get("creates") or []
        if len(created) != 1:
            refuse(f"a {feature.get('type')} feature must create exactly one surface", where)
        self.surfaces.update(created)

    @staticmethod
    def _operation(feature, bodies, refuse, where, kind) -> None:
        operation = feature.get("operation")
        if operation not in OPERATIONS:
            refuse(f"{kind} operation '{operation}' is not supported", where)
        elif operation == "new_body":
            created = feature.get("creates") or []
            if len(created) != 1:
                refuse(f"a new-body {kind} must create exactly one body", where)
            bodies.update(created)
        elif len(feature.get("bodies") or []) != 1 or feature["bodies"][0] not in bodies:
            refuse(f"{kind} must modify exactly one known body", where)


def check_wire_sketch(feature: dict[str, Any], refuse, where: str) -> None:
    """A `wire: true` sketch: a valid plane and one chain of curves."""
    try:
        Frame.from_tree(feature["plane"])
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"sketch plane is invalid ({error})", where)
        return
    try:
        chain(feature)
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"wire sketch is not supported ({error})", where)
