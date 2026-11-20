"""Expected curves and surfaces of the second GSD slice, built with build123d.

The independent reading of catia_mcp/tree/curves.py's contract: helices,
splines, polylines and circles as build123d makes them (Edge.make_helix,
Edge.make_spline, Wire.make_polygon, Edge.make_circle); projections
(Wire.project_to_shape along a direction, the nearest solution, or
OpenCascade's normal projection) and intersections (OpenCascade's section);
surfaces from curves (Shell.extrude, Shell.revolve, Face, Shell.sweep along
a guide); a sphere's and a cylinder's faces; moved, turned, mirrored,
scaled and affine copies (OpenCascade transformations); ruled lofts through
three or more sections (one ruled band per pair, sewn); trims (each surface
split by the other, the part holding the keep point kept, both sewn); and
extracted faces (the body's face its anchor names, as a shell's faces are
found). `record_shape` stores each one and its measures in the expected
properties: `curves` (length and centre) or `surfaces` (area and centre).
Needs build123d.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

from build123d import (
    Axis,
    Compound,
    Edge,
    Face,
    Location,
    Plane,
    Shell,
    Solid,
    Vector,
    Vertex,
    Wire,
)

from catia_mcp.tree.curves import CURVE_INPUT_OPS, MORE_OPS, band_name, helix_spec, vec
from catia_mcp.tree.surface_reference import (
    SurfaceReferenceError,
    _as_shell,
    _sewn,
    sketch_wire,
    surface_of,
    surface_properties,
)


def curve_properties(shape: Any) -> dict[str, Any]:
    """Length and centre of gravity (of the length) of a curve."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(shape.wrapped, props, True)
    centre = props.CentreOfMass()
    return {"length_mm": float(props.Mass()),
            "cog_mm": [float(centre.X()), float(centre.Y()), float(centre.Z())]}


def is_curve(shape: Any) -> bool:
    return not shape.faces() and bool(shape.edges())


def _wire(shape: Any) -> Wire:
    wires = Wire.combine(shape.edges())
    if len(wires) != 1:
        raise SurfaceReferenceError("the curve is not one connected wire")
    return wires[0]


def input_wire(name: str, sketches: dict[str, Any], shapes: dict[str, Any]) -> Wire:
    """A sketch's or a curve's wire."""
    if name in sketches:
        return sketch_wire(sketches[name])
    return _wire(shapes[name])


# ── Curves ──


def curve_of(feature: dict[str, Any], sketches: dict[str, Any], shapes: dict[str, Any]) -> Any:
    op = feature["op"]
    if op == "helix":
        spec = helix_spec(feature)
        return Edge.make_helix(float(feature["pitch"]), spec["height"], spec["radius"],
                               spec["origin"], spec["direction"], spec["taper"], spec["left"])
    if op == "spline":
        points = [vec(p) for p in feature["points"]]
        tangents = [vec(t) for t in feature["tangents"]] if feature.get("tangents") else None
        return Edge.make_spline(points, tangents, periodic=bool(feature.get("closed")))
    if op == "polyline":
        return Wire.make_polygon([vec(p) for p in feature["points"]],
                                 close=bool(feature.get("closed")))
    if op == "circle":
        return Edge.make_circle(float(feature["radius"]),
                                Plane(origin=vec(feature["center"]),
                                      z_dir=vec(feature["normal"])))
    if op == "project":
        source, target = feature["inputs"]
        return project(input_wire(source, sketches, shapes), shapes[target],
                       vec(feature["direction"]) if feature.get("direction") else None)
    if op == "intersect":
        first = shapes[feature["inputs"][0]]
        if feature.get("plane") is not None:
            return section(first, feature["plane"])
        return section(first, shapes[feature["inputs"][1]])
    raise SurfaceReferenceError(f"no reference for '{op}' curves")


def project(wire: Wire, target: Any, direction: Any) -> Any:
    """The wire projected onto the target: along a direction, the solution
    nearest the wire (CATIA's default); else normal to the target."""
    if direction is not None:
        found = wire.project_to_shape(target, Vector(*direction))
        if not found:
            raise SurfaceReferenceError("the projection misses the surface")
        centre = wire.center()
        return min(found, key=lambda w: (w.center() - centre).length)
    from OCP.BRepOffsetAPI import BRepOffsetAPI_NormalProjection

    maker = BRepOffsetAPI_NormalProjection(target.wrapped)
    maker.Add(wire.wrapped)
    maker.Build()
    if not maker.IsDone():
        raise SurfaceReferenceError("OpenCascade could not project the curve")
    return _joined_edges(Compound(maker.Shape()))


def section(shape: Any, other: Any) -> Any:
    """Where a surface meets another surface, or a plane {origin, normal}."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    if isinstance(other, dict):
        plane = gp_Pln(gp_Pnt(*vec(other["origin"])), gp_Dir(*vec(other["normal"])))
        maker = BRepAlgoAPI_Section(shape.wrapped, plane, True)
    else:
        maker = BRepAlgoAPI_Section(shape.wrapped, other.wrapped, True)
    maker.Build()
    found = Compound(maker.Shape())
    if not found.edges():
        raise SurfaceReferenceError("the surfaces do not meet")
    return _joined_edges(found)


def _joined_edges(found: Any) -> Any:
    wires = Wire.combine(found.edges())
    return wires[0] if len(wires) == 1 else Compound(wires)


# ── Surfaces ──


def from_curve(feature: dict[str, Any], sketches: dict[str, Any], shapes: dict[str, Any]) -> Any:
    """Extrude, revolve, fill or sweep of a curve, or a sweep along a guide."""
    op = feature["op"]
    wire = input_wire(feature["inputs"][0], sketches, shapes)
    if op == "extrude":
        vector = vec(feature["direction"])
        if feature.get("symmetric"):
            start = wire.moved(Location(Vector(*vector) * -1))
            return Shell.extrude(start, Vector(*vector) * 2)
        return Shell.extrude(wire, Vector(*vector))
    if op == "revolve":
        axis = feature["axis"]
        return Shell.revolve(wire, float(feature["angle"]),
                             Axis(vec(axis["origin"]), vec(axis["direction"])))
    if op == "fill":
        return Face(wire)
    return Shell.sweep(wire, _wire(shapes[feature["guide"]]))


def primitive(feature: dict[str, Any]) -> Any:
    if feature["op"] == "sphere":
        return Solid.make_sphere(float(feature["radius"])).moved(
            Location(Vector(*vec(feature["center"])))).faces()[0]
    axis = feature["axis"]
    direction = Vector(*vec(axis["direction"])).normalized()
    circle = Wire([Edge.make_circle(float(feature["radius"]),
                                    Plane(origin=vec(axis["origin"]), z_dir=direction))])
    return Shell.extrude(circle, direction * float(feature["length"]))


def transformed(feature: dict[str, Any], shape: Any) -> Any:
    """A moved, turned, mirrored, scaled or affine copy of a surface."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform, BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_GTrsf, gp_Mat, gp_Pnt, gp_Trsf, gp_Vec, gp_XYZ

    op = feature["op"]
    trsf = gp_Trsf()
    if op == "translate":
        trsf.SetTranslation(gp_Vec(*vec(feature["vector"])))
    elif op == "rotate":
        axis = feature["axis"]
        trsf.SetRotation(gp_Ax1(gp_Pnt(*vec(axis["origin"])), gp_Dir(*vec(axis["direction"]))),
                         math.radians(float(feature["angle"])))
    elif op == "mirror":
        plane = feature["plane"]
        trsf.SetMirror(gp_Ax2(gp_Pnt(*vec(plane["origin"])), gp_Dir(*vec(plane["normal"]))))
    elif op == "scale":
        trsf.SetScale(gp_Pnt(*vec(feature["center"])), float(feature["ratio"]))
    else:  # affinity along X, Y, Z about the origin point
        origin = vec(feature.get("origin") or (0, 0, 0))
        rx, ry, rz = vec(feature["ratios"])
        gtrsf = gp_GTrsf()
        gtrsf.SetVectorialPart(gp_Mat(rx, 0, 0, 0, ry, 0, 0, 0, rz))
        gtrsf.SetTranslationPart(gp_XYZ(*(o * (1 - r) for o, r in zip(origin, (rx, ry, rz),
                                                                        strict=True))))
        return _as_shell(BRepBuilderAPI_GTransform(shape.wrapped, gtrsf, True).Shape())
    return _as_shell(BRepBuilderAPI_Transform(shape.wrapped, trsf, True).Shape())


def ruled_bands(sketches: list[dict[str, Any]]) -> list[Any]:
    """A ruled loft through N sections, one band per pair (as OpenCascade's
    ruled ThruSections builds it)."""
    wires = [sketch_wire(s) for s in sketches]
    return [Shell.make_loft([a, b], ruled=True) for a, b in pairwise(wires)]


def split_parts(shape: Any, tool: Any) -> list[list[Any]]:
    """The faces of `shape` split by `tool`, grouped into the parts the cut
    separates (faces sharing an edge that is not on the tool)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.collections import List_TopoDS_Shape

    arguments, tools = List_TopoDS_Shape(), List_TopoDS_Shape()
    arguments.Append(shape.wrapped)
    tools.Append(tool.wrapped)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    faces = list(Compound(splitter.Shape()).faces())
    parent = list(range(len(faces)))

    def root(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    for i, first in enumerate(faces):
        for j in range(i + 1, len(faces)):
            if root(i) == root(j):
                continue
            for edge in first.edges():
                if any(edge.is_same(other) for other in faces[j].edges()) \
                        and edge.distance_to(tool) > 1e-6:
                    parent[root(i)] = root(j)
                    break
    groups: dict[int, list[Any]] = {}
    for i, face in enumerate(faces):
        groups.setdefault(root(i), []).append(face)
    return list(groups.values())


def trimmed(first: Any, second: Any, keep_points: list[Any]) -> Any:
    """Each surface cut by the other, the part nearest its keep point kept,
    the two parts sewn (CATIA's Trim)."""
    kept = []
    for shape, tool, point in ((first, second, keep_points[0]), (second, first, keep_points[1])):
        parts = split_parts(shape, tool)
        if len(parts) < 2:
            raise SurfaceReferenceError("the surfaces do not cut each other")
        at = Vertex(*vec(point))
        kept += min(parts, key=lambda faces: min(face.distance_to(at) for face in faces))
    return _sewn(kept)


def extracted(feature: dict[str, Any], bodies: dict[str, Any]) -> Any:
    from catia_mcp.tree.anchors import match_faces
    from catia_mcp.tree.reference import _face_candidates

    (target,) = feature["bodies"]
    (match,) = match_faces([feature["face"]], _face_candidates(bodies[target]))
    return match.handle


def shape_of(feature: dict[str, Any], sketches: dict[str, Any], shapes: dict[str, Any],
             bodies: dict[str, Any], out: dict[str, Any]) -> Any:
    kind = feature["type"]
    if kind == "curve":
        return curve_of(feature, sketches, shapes)
    if kind == "trim_surfaces":
        return trimmed(*(shapes[name] for name in feature["inputs"]), feature["keep_points"])
    if kind == "extract":
        return extracted(feature, bodies)
    op = feature.get("op")
    inputs = feature.get("inputs") or []
    if kind == "surface" and op in MORE_OPS:
        return primitive(feature) if op in ("sphere", "cylinder") else \
            transformed(feature, shapes[inputs[0]])
    if kind == "surface" and op in CURVE_INPUT_OPS and (
            (inputs and inputs[0] in shapes) or feature.get("guide")):
        return from_curve(feature, sketches, shapes)
    if kind == "surface" and op == "loft" and len(inputs) > 2:
        bands = ruled_bands([sketches[name] for name in inputs])
        (name,) = feature["creates"]
        for i, band in enumerate(bands, 1):
            out.setdefault("surfaces", {})[band_name(name, i)] = surface_properties(band)
        return _sewn([face for band in bands for face in band.faces()])
    return surface_of(feature, sketches, shapes)


def record_shape(feature: dict[str, Any], sketches: dict[str, Any], shapes: dict[str, Any],
                 bodies: dict[str, Any], out: dict[str, Any]) -> None:
    """Build the feature's curve or surface, keep it in `shapes` and its
    measures in `out` (`curves` or `surfaces`)."""
    (name,) = feature["creates"]
    shape = shape_of(feature, sketches, shapes, bodies, out)
    shapes[name] = shape
    if is_curve(shape):
        out.setdefault("curves", {})[name] = curve_properties(shape)
    else:
        out.setdefault("surfaces", {})[name] = surface_properties(shape)
