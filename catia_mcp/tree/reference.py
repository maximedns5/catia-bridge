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
    Location,
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
from catia_mcp.tree.curve_reference import record_shape
from catia_mcp.tree.extrude import extrude_vector
from catia_mcp.tree.geometry import (
    Frame,
    add,
    chamfer_second,
    frame_axes,
    hole_profile_sketch,
    hole_segments,
    norm,
    scale,
)
from catia_mcp.tree.nurbs import Nurbs, clamped
from catia_mcp.tree.partdesign_extra import EXTRA_TYPES
from catia_mcp.tree.placement import place_part
from catia_mcp.tree.surface_reference import solid_of
from catia_mcp.tree.surfaces import SOLID_FROM_SURFACE, SURFACE_TYPES


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
    if kind == "helix":  # catia_mcp/tree/reference_extra.py
        from catia_mcp.tree.reference_extra import helix_edge

        return helix_edge(entity)
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
            # No opening: OpenCascade offsets the whole solid (build123d's
            # offset() without openings).
            faces = [c.handle for c in match_faces(feature["faces"], _face_candidates(body))] \
                if feature["faces"] else []
            join = Kind.INTERSECTION if feature.get("join", "").endswith("INTERSECTION") \
                else Kind.ARC
            result = body.offset_3d(faces, float(feature["thickness"]), kind=join)
            return _closed_solid(result) if not faces else result
        edges = [c.handle for c in match_edges(feature["edges"], _edge_candidates(body))]
        if kind == "fillet":
            return body.fillet(float(feature["radius"]), edges)
        face = None
        if feature.get("face"):
            (match,) = match_faces([feature["face"]], _face_candidates(body))
            face = match.handle
        second = chamfer_second(feature)
        return body.chamfer(float(feature["distance"]), second, edges, face)
    except AnchorError as error:
        raise ReferenceError(f"{kind} {feature.get('id')}: {error}") from error


def _closed_solid(shape: Any) -> Any:
    """The solid inside the closed shell an offset without openings gives."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopoDS import TopoDS

    if shape.wrapped.ShapeType() != TopAbs_SHELL:
        return shape
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell(shape.wrapped)).Solid()
    # The outward offset's shell comes inside out (a negative volume).
    return Solid(solid) if _volume(Solid(solid)) >= 0 else Solid(solid.Reversed())


def _location(matrix: Any) -> Location:
    """A build123d Location for a 3x4 rigid matrix [R|t]."""
    from OCP.gp import gp_Trsf

    trsf = gp_Trsf()
    trsf.SetValues(*[float(c) for row in matrix for c in row])
    return Location(trsf)


def _scaled(shape: Any, feature: dict[str, Any]) -> Any:
    """A shape scaled along its frame's axes about its origin (a general
    affine map: BRepBuilderAPI_GTransform, NURBS-converting when needed)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ

    from catia_mcp.tree.geometry import scale_frame, scaled_point

    _, *axes = scale_frame(feature)
    factors = [float(f) for f in feature["factors"]]
    m = [[sum(f * a[i] * a[j] for f, a in zip(factors, axes, strict=True)) for j in range(3)]
         for i in range(3)]
    shift = [scaled_point(feature, (0.0, 0.0, 0.0))[i] for i in range(3)]
    gtrsf = gp_GTrsf()
    gtrsf.SetVectorialPart(gp_Mat(*[m[i][j] for i in range(3) for j in range(3)]))
    gtrsf.SetTranslationPart(gp_XYZ(*shift))
    maker = BRepBuilderAPI_GTransform(shape.wrapped, gtrsf, True)
    return type(shape)(maker.Shape()) if hasattr(shape, "wrapped") else maker.Shape()


def build_part(part: dict[str, Any], external: Any = None, on_feature: Any = None,
               surface_out: dict[str, Any] | None = None) -> Any:
    """The part's solid (None when it has no body, only surfaces).
    `external(part, feature, bodies)` builds the features this reference has
    no rule for, by updating `bodies` (the STEP fallback). `on_feature(feature,
    bodies)` sees the live bodies after each feature. `surface_out` receives
    each surface's area and centre (`surfaces`) and the solid each thicken or
    close_surface makes (`solids`, by feature id)."""
    part = place_part(part)
    sketches: dict[str, dict[str, Any]] = {}
    bodies: dict[str, Any] = {}
    surfaces: dict[str, Any] = {}
    out = surface_out if surface_out is not None else {}
    for feature in part.get("features") or []:
        kind = feature["type"]
        if kind == "sketch":
            sketches[feature["id"]] = feature
        elif kind == "extrude" and feature.get("until") is not None:
            _plus(feature, bodies, sketches)
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
        elif kind == "loft" and any(not isinstance(s, str) for s in feature["sections"]):
            _plus(feature, bodies, sketches)
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
            _plus(feature, bodies, sketches)  # a V bottom's tip
        elif kind == "copy":
            (source,), (created,) = feature["bodies"], feature["creates"]
            bodies[created] = bodies[source].moved(_location(feature["matrix"]))
        elif kind == "scale":
            (target,) = feature["bodies"]
            bodies[target] = _scaled(bodies[target], feature)
        elif kind == "remove":
            for name in feature["bodies"]:
                bodies.pop(name, None)
        elif kind in ("fillet", "chamfer", "shell"):
            (target,) = feature["bodies"]
            bodies[target] = _dressup(feature, bodies[target])
        elif kind == "draft" and "faces" in feature and "neutral_plane" in feature:
            _plus(feature, bodies, sketches)
        elif kind in EXTRA_TYPES:  # thread, move, mirror_in_place
            from catia_mcp.tree.reference_extra import apply

            apply(feature, bodies, _location)
        elif kind in SOLID_FROM_SURFACE:
            solid = solid_of(feature, surfaces)
            out.setdefault("solids", {})[str(feature.get("id"))] = mass(solid)
            _add_solid(bodies, feature, solid)
        elif kind in SURFACE_TYPES:  # surfaces and 3D curves (curve_reference.py)
            record_shape(feature, sketches, surfaces, bodies, out)
        elif external is not None:
            external(part, feature, bodies)
        else:
            raise ReferenceError(f"no reference for '{kind}' features")
        if on_feature is not None and kind != "sketch":
            on_feature(feature, bodies)
    final = [bodies[name] for name in (part.get("bodies") or bodies)]
    if not final:
        return None
    result = final[0]
    for other in final[1:]:
        result = result + other
    return result


def _plus(feature: dict[str, Any], bodies: dict[str, Any],
          sketches: dict[str, dict[str, Any]]) -> None:
    """Drafts, extrudes up to a limit, V-bottomed holes and lofts to a point
    (catia_mcp/tree/reference_plus.py)."""
    from catia_mcp.tree import reference_plus as plus

    kind = feature["type"]
    try:
        if kind == "draft":
            (target,) = feature["bodies"]
            bodies[target] = plus.draft(feature, bodies[target], _face_candidates)
        elif kind == "extrude":
            (target,) = feature["bodies"]
            sketch = sketches[feature["sketch"]]
            tool = plus.until_solid(feature, sketch, sketch_faces(sketch), bodies[target])
            bodies[target] = _combine(bodies[target], tool, feature["operation"])
        elif kind == "loft":
            plan = plus.point_loft(feature)
            _add_solid(bodies, feature,
                       plus.point_loft_solid(feature, sketch_faces(sketches[plan.sketch])))
        elif kind == "hole":
            (target,) = feature["bodies"]
            for matrix in feature["frames"]:
                tip = plus.hole_tip(feature, matrix, sketch_faces)
                if tip is not None:
                    bodies[target] = bodies[target] - tip
    except plus.PlusReferenceError as error:
        raise ReferenceError(str(error)) from error


def properties(solid: Any) -> dict[str, Any]:
    if solid is None:  # a part of surfaces only
        return {"volume_mm3": 0.0, "area_mm2": 0.0, "cog_mm": [0.0, 0.0, 0.0]}
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
    if _ADAPTIVE_VOLUME:
        # Helix sweeps: Gauss-Kronrod at 1e-9 can run for minutes on their
        # faces; the adaptive Gauss rule agrees with the closed form to 2e-6.
        BRepGProp.VolumeProperties_s(solid.wrapped, props, 1e-9)
    else:
        BRepGProp.VolumePropertiesGK_s(solid.wrapped, props, 1e-9)
    return float(props.Mass())


# Set by expected() while it measures a part with a helix sweep.
_ADAPTIVE_VOLUME = False


def mass(solid: Any) -> dict[str, Any]:
    """Volume and centre of gravity, the per-feature check's two numbers."""
    volume = _volume(solid)
    if volume <= 0:
        return {"volume_mm3": 0.0, "cog_mm": [0.0, 0.0, 0.0]}
    cog = solid.center(CenterOf.MASS)
    return {"volume_mm3": volume, "cog_mm": [float(cog.X), float(cog.Y), float(cog.Z)]}


def expected(tree: dict[str, Any], external: Any = None,
             features: bool = True) -> dict[str, dict[str, Any]]:
    """Per-part expected properties, keyed by part id. With `features`, each
    part also gets `features`: {feature id: {body: {volume_mm3, cog_mm}}},
    every live body after that feature. The builder checks features that have
    no closed form (a Pocket into a body, a Hole, a Groove) against them."""
    out: dict[str, dict[str, Any]] = {}
    for part in tree["parts"]:
        snapshots: dict[str, dict[str, Any]] = {}

        def record(feature: dict[str, Any], bodies: dict[str, Any],
                   snapshots: dict[str, dict[str, Any]] = snapshots) -> None:
            snapshots[str(feature.get("id"))] = {name: mass(body) for name, body in bodies.items()}

        surface_out: dict[str, Any] = {}
        global _ADAPTIVE_VOLUME
        from catia_mcp.tree.reference_extra import has_helix

        _ADAPTIVE_VOLUME = has_helix(part)
        try:
            props = properties(build_part(part, external, record if features else None,
                                          surface_out))
        finally:
            _ADAPTIVE_VOLUME = False
        if features:
            props["features"] = snapshots
        props.update(surface_out)
        out[part["id"]] = props
    return out
