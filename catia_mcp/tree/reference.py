"""Expected solids for a tree, built with build123d (OpenCascade).

This is an independent reading of the tree contract: if CATIA's result and
this one agree, the CATIA builder implements the contract. It covers the
features preflight.SUPPORTED lists. Needs build123d
(pip install -e ".[differential]").
"""

from __future__ import annotations

from typing import Any

from build123d import CenterOf, Edge, Face, Plane, Solid, Wire, extrude

from catia_mcp.tree.geometry import Frame, add, scale


class ReferenceError(Exception):
    """The reference cannot build this tree."""


def _plane(frame: Frame, origin=None) -> Plane:
    return Plane(origin=origin or frame.origin, x_dir=frame.x_dir, z_dir=frame.normal)


def _edge(entity: dict[str, Any], frame: Frame) -> Edge:
    kind = entity["type"]
    if kind == "line":
        return Edge.make_line(frame.world(*entity["start"]), frame.world(*entity["end"]))
    if kind == "arc":
        return Edge.make_three_point_arc(
            frame.world(*entity["start"]), frame.world(*entity["mid"]), frame.world(*entity["end"])
        )
    if kind == "circle":
        return Edge.make_circle(entity["radius"], _plane(frame, frame.world(*entity["center"])))
    raise ReferenceError(f"no reference for sketch entity '{kind}'")


def _wire(ids: list[str], by_id: dict[str, Any], frame: Frame) -> Wire:
    wires = Wire.combine([_edge(by_id[i], frame) for i in ids])
    if len(wires) != 1:
        raise ReferenceError("a profile loop does not form one wire")
    return wires[0]


def sketch_faces(sketch: dict[str, Any]) -> list[Face]:
    frame = Frame.from_tree(sketch["plane"])
    by_id = {e["id"]: e for e in sketch["entities"]}
    return [
        Face(_wire(profile["outer"], by_id, frame),
             [_wire(inner, by_id, frame) for inner in profile.get("inner", [])])
        for profile in sketch["profiles"]
    ]


def _combine(target: Any, tool: Any, operation: str) -> Any:
    if operation == "join":
        return target + tool
    if operation == "cut":
        return target - tool
    if operation == "intersect":
        return target & tool
    raise ReferenceError(f"unknown operation '{operation}'")


def hole_segments(feature: dict[str, Any]) -> list[tuple[float, float, float]]:
    """(radius, top, bottom) along the hole frame's Z, as the contract's
    revolved profiles give them (d = effective depth)."""
    r = feature["diameter"] / 2
    d = feature.get("effective_depth") or feature["depth"]
    if feature["kind"] == "simple":
        return [(r, d / 2, -d / 2)] if feature.get("depth") is None else [(r, 0.0, -d)]
    if feature["kind"] == "counterbore":
        h = feature["counterbore_depth"]
        return [(feature["counterbore_diameter"] / 2, d, -h), (r, -h, -d)]
    raise ReferenceError(f"no reference for '{feature['kind']}' holes")


def frame_axes(matrix: list[list[float]]):
    """Origin, X and Z of a 3x4 hole frame whose columns are X, Y, Z, origin."""
    def column(j: int):
        return tuple(float(matrix[i][j]) for i in range(3))

    return column(3), column(0), column(2)


def build_part(part: dict[str, Any]) -> Any:
    sketches: dict[str, dict[str, Any]] = {}
    bodies: dict[str, Any] = {}
    for feature in part.get("features") or []:
        kind = feature["type"]
        if kind == "sketch":
            sketches[feature["id"]] = feature
        elif kind == "extrude":
            sketch = sketches[feature["sketch"]]
            normal = Frame.from_tree(sketch["plane"]).normal
            distance = float(feature["distance"])
            direction = normal if distance > 0 else scale(normal, -1)
            solid = extrude(sketch_faces(sketch), amount=abs(distance), dir=direction,
                            both=bool(feature.get("symmetric")))
            if feature["operation"] == "new_body":
                bodies[feature["creates"][0]] = solid
            else:
                (target,) = feature["bodies"]
                bodies[target] = _combine(bodies[target], solid, feature["operation"])
        elif kind == "combine":
            (target,) = feature["targets"]
            for tool in feature["tools"]:
                bodies[target] = _combine(bodies[target], bodies.pop(tool), feature["operation"])
        elif kind == "hole":
            (target,) = feature["bodies"]
            for matrix in feature["frames"]:
                origin, x_dir, z_dir = frame_axes(matrix)
                for radius, top, bottom in hole_segments(feature):
                    plane = Plane(origin=add(origin, scale(z_dir, bottom)), x_dir=x_dir, z_dir=z_dir)
                    bodies[target] = bodies[target] - Solid.make_cylinder(radius, top - bottom, plane)
        else:
            raise ReferenceError(f"no reference for '{kind}' features")
    final = [bodies[name] for name in (part.get("bodies") or bodies)]
    result = final[0]
    for other in final[1:]:
        result = result + other
    return result


def properties(solid: Any) -> dict[str, Any]:
    cog = solid.center(CenterOf.MASS)
    return {"volume_mm3": float(solid.volume), "area_mm2": float(solid.area),
            "cog_mm": [float(cog.X), float(cog.Y), float(cog.Z)]}


def expected(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-part expected properties, keyed by part id."""
    return {part["id"]: properties(build_part(part)) for part in tree["parts"]}
