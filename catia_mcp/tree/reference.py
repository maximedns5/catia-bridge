"""Expected solids for a tree, built with build123d (OpenCascade).

This is an independent reading of the tree contract: if CATIA's result and
this one agree, the CATIA builder implements the contract. It covers the
features preflight.SUPPORTED lists; fillets, chamfers and shells find their
edges and faces with the same anchor matching as the builder
(catia_mcp/tree/anchors.py). Needs build123d (pip install -e ".[differential]").
"""

from __future__ import annotations

from typing import Any

from build123d import (
    Axis,
    CenterOf,
    Edge,
    Face,
    Keep,
    Kind,
    Plane,
    Solid,
    Transition,
    Vector,
    Wire,
    extrude,
    loft,
    revolve,
    split,
    sweep,
)

from catia_mcp.tree.anchors import (
    AnchorError,
    EdgeCandidate,
    FaceCandidate,
    match_edges,
    match_faces,
)
from catia_mcp.tree.extrude import extrude_vector
from catia_mcp.tree.geometry import (
    Frame,
    add,
    frame_axes,
    hole_profile_sketch,
    hole_segments,
    norm,
    scale,
)
from catia_mcp.tree.nurbs import Nurbs, clamped
from catia_mcp.tree.placement import place_part


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
    if kind == "spline":
        return _spline_edge(entity, frame)
    raise ReferenceError(f"no reference for sketch entity '{kind}'")


def _spline_edge(entity: dict[str, Any], frame: Frame) -> Edge:
    """The exact rational NURBS as an OpenCascade B-spline edge: the same
    curve, re-knotted clamped (nurbs.clamped), with its poles in world
    coordinates."""
    degree, poles, weights, knots = clamped(Nurbs.from_entity(entity))
    return Edge.make_bspline([frame.world(u, v) for u, v in poles], knots, degree, weights)


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


def _path_edge(entity: dict[str, Any]) -> Edge:
    """A sweep path entity, in world coordinates."""
    kind = entity["type"]
    if kind == "line":
        return Edge.make_line(entity["start"], entity["end"])
    if kind == "arc":
        return Edge.make_three_point_arc(entity["start"], entity["mid"], entity["end"])
    raise ReferenceError(f"no reference for sweep path entity '{kind}'")


def _add_solid(bodies: dict[str, Any], feature: dict[str, Any], solid: Any) -> None:
    if feature["operation"] == "new_body":
        bodies[feature["creates"][0]] = solid
    else:
        (target,) = feature["bodies"]
        bodies[target] = _combine(bodies[target], solid, feature["operation"])


def _edge_candidates(body: Any) -> list[EdgeCandidate]:
    return [EdgeCandidate(edge, float(edge.length),
                          lambda p, edge=edge: float(edge.distance_to(Vector(*p))))
            for edge in body.edges()]


def _face_candidates(body: Any) -> list[FaceCandidate]:
    candidates = []
    for face in body.faces():
        c = face.center(CenterOf.MASS)
        candidates.append(FaceCandidate(face, float(face.area), (c.X, c.Y, c.Z)))
    return candidates


def _dressup(feature: dict[str, Any], body: Any) -> Any:
    """A fillet, chamfer or shell applied to `body`, its edges and faces found
    by the same anchor matching as the CATIA builder."""
    kind = feature["type"]
    try:
        if kind == "shell":
            faces = [c.handle for c in match_faces(feature["faces"], _face_candidates(body))]
            if not faces:
                raise ReferenceError("no reference for a shell with no opening")
            join = Kind.INTERSECTION if feature.get("join", "").endswith("INTERSECTION") \
                else Kind.ARC
            return body.offset_3d(faces, float(feature["thickness"]), kind=join)
        edges = [c.handle for c in match_edges(feature["edges"], _edge_candidates(body))]
        if kind == "fillet":
            return body.fillet(float(feature["radius"]), edges)
        face = None
        if feature.get("face"):
            (match,) = match_faces([feature["face"]], _face_candidates(body))
            face = match.handle
        second = feature.get("distance2")
        return body.chamfer(float(feature["distance"]),
                            None if second is None else float(second), edges, face)
    except AnchorError as error:
        raise ReferenceError(f"{kind} {feature.get('id')}: {error}") from error


def build_part(part: dict[str, Any], external: Any = None) -> Any:
    """The part's solid. `external(part, feature, bodies)` builds the features
    this reference has no rule for, by updating `bodies` (the STEP fallback)."""
    part = place_part(part)
    sketches: dict[str, dict[str, Any]] = {}
    bodies: dict[str, Any] = {}
    for feature in part.get("features") or []:
        kind = feature["type"]
        if kind == "sketch":
            sketches[feature["id"]] = feature
        elif kind == "extrude":
            sketch = sketches[feature["sketch"]]
            # The full vector the profile travels along, as build123d's
            # extrude(amount, dir) moves it (amount along dir).
            vector = extrude_vector(feature, Frame.from_tree(sketch["plane"]))
            solid = extrude(sketch_faces(sketch), amount=norm(vector), dir=vector,
                            both=bool(feature.get("symmetric")),
                            taper=float(feature.get("taper") or 0.0))
            if feature["operation"] == "new_body":
                bodies[feature["creates"][0]] = solid
            else:
                (target,) = feature["bodies"]
                bodies[target] = _combine(bodies[target], solid, feature["operation"])
        elif kind == "revolve":
            sketch = sketches[feature["sketch"]]
            axis = feature["axis"]
            solid = revolve(sketch_faces(sketch), axis=Axis(axis["origin"], axis["direction"]),
                            revolution_arc=feature["angle"])
            if feature["operation"] == "new_body":
                bodies[feature["creates"][0]] = solid
            else:
                (target,) = feature["bodies"]
                bodies[target] = _combine(bodies[target], solid, feature["operation"])
        elif kind == "loft":
            sections = [sketch_faces(sketches[name])[0] for name in feature["sections"]]
            _add_solid(bodies, feature, loft(sections, ruled=bool(feature.get("ruled"))))
        elif kind == "sweep":
            path = Wire([_path_edge(entity) for entity in feature["path"]])
            transition = getattr(Transition, feature.get("transition", "Transition.TRANSFORMED")
                                 .split(".")[-1])
            solid = sweep(sketch_faces(sketches[feature["sketch"]]), path=path,
                          transition=transition, is_frenet=bool(feature.get("frenet")))
            _add_solid(bodies, feature, solid)
        elif kind == "mirror":
            (source,), (created,) = feature["bodies"], feature["creates"]
            plane = feature["plane"]
            bodies[created] = bodies[source].mirror(Plane(origin=plane["origin"], z_dir=plane["normal"]))
        elif kind == "split":
            (target,) = feature["bodies"]
            plane = feature["plane"]
            cutter = Plane(origin=plane["origin"], z_dir=plane["normal"])
            keep = Keep.TOP if feature["keep"] == "top" else Keep.BOTTOM
            bodies[target] = split(bodies[target], bisect_by=cutter, keep=keep)
        elif kind == "combine":
            (target,) = feature["targets"]
            for tool in feature["tools"]:
                bodies[target] = _combine(bodies[target], bodies.pop(tool), feature["operation"])
        elif kind == "hole":
            (target,) = feature["bodies"]
            for matrix in feature["frames"]:
                if feature["kind"] == "countersink":
                    sketch, origin, z_dir = hole_profile_sketch(feature, matrix)
                    tool = revolve(sketch_faces(sketch), axis=Axis(origin, z_dir))
                    bodies[target] = bodies[target] - tool
                    continue
                origin, x_dir, z_dir = frame_axes(matrix)
                for radius, top, bottom in hole_segments(feature):
                    plane = Plane(origin=add(origin, scale(z_dir, bottom)), x_dir=x_dir, z_dir=z_dir)
                    bodies[target] = bodies[target] - Solid.make_cylinder(radius, top - bottom, plane)
        elif kind in ("fillet", "chamfer", "shell"):
            (target,) = feature["bodies"]
            bodies[target] = _dressup(feature, bodies[target])
        elif external is not None:
            external(part, feature, bodies)
        else:
            raise ReferenceError(f"no reference for '{kind}' features")
    final = [bodies[name] for name in (part.get("bodies") or bodies)]
    result = final[0]
    for other in final[1:]:
        result = result + other
    return result


def properties(solid: Any) -> dict[str, Any]:
    cog = solid.center(CenterOf.MASS)
    return {"volume_mm3": _volume(solid), "area_mm2": float(solid.area),
            "cog_mm": [float(cog.X), float(cog.Y), float(cog.Z)]}


def _volume(solid: Any) -> float:
    """The volume by adaptive Gauss-Kronrod integration. OpenCascade's
    default integration is exact on planes, cylinders and cones but not on
    B-spline faces (spline sketches): 1.7e-3 off on an extruded ellipse, where
    this agrees with the closed form to 1e-10."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumePropertiesGK_s(solid.wrapped, props, 1e-9)
    return float(props.Mass())


def expected(tree: dict[str, Any], external: Any = None) -> dict[str, dict[str, Any]]:
    """Per-part expected properties, keyed by part id."""
    return {part["id"]: properties(build_part(part, external)) for part in tree["parts"]}
