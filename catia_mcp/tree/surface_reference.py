"""Expected surfaces for the tree's surface features, built with build123d.

The independent reading of catia_mcp/tree/surfaces.py's contract: each
surface is made as build123d makes it (Shell.extrude, Shell.revolve,
Shell.make_loft, Shell.sweep, a planar Face, an OpenCascade offset, a sewn
shell, a split), and `thicken` / `close_surface` give the solid
(Solid.thicken, the shell closed by planar faces on its openings). The CATIA
builder checks each surface's area and centre against these, and each solid
like any other body. Needs build123d.
"""

from __future__ import annotations

from typing import Any

from build123d import (
    Axis,
    Box,
    Compound,
    Edge,
    Face,
    Location,
    Plane,
    Shell,
    Solid,
    Transition,
    Vector,
    Wire,
)

from catia_mcp.tree.extrude import extrude_vector
from catia_mcp.tree.geometry import Frame, scale
from catia_mcp.tree.surfaces import chain

# Faces whose area OpenCascade integrates exactly; the others (B-splines,
# extrusions and revolutions of them, offsets) are integrated in
# DIVIDE_PIECES pieces: its default Gauss rule is off by up to 0.4 % on one
# whole approximated face (an affine image of a sphere), and its adaptive
# rule fails on others (a projected curve extruded).
EXACT_FACES = ("PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS")
DIVIDE_PIECES = 64


class SurfaceReferenceError(Exception):
    """The reference cannot build this surface."""


def _plane(frame: Frame, origin=None) -> Plane:
    return Plane(origin=origin or frame.origin, x_dir=frame.x_dir, z_dir=frame.normal)


def _edge(entity: dict[str, Any], frame: Frame) -> Edge:
    kind = entity["type"]
    if kind == "line":
        return Edge.make_line(frame.world(*entity["start"]), frame.world(*entity["end"]))
    if kind == "arc":
        return Edge.make_three_point_arc(frame.world(*entity["start"]),
                                         frame.world(*entity["mid"]), frame.world(*entity["end"]))
    return Edge.make_circle(float(entity["radius"]), _plane(frame, frame.world(*entity["center"])))


def sketch_wire(sketch: dict[str, Any]) -> Wire:
    """The sketch's curve, one wire in world coordinates."""
    frame = Frame.from_tree(sketch["plane"])
    entities, _ = chain(sketch)
    wires = Wire.combine([_edge(entity, frame) for entity in entities])
    if len(wires) != 1:
        raise SurfaceReferenceError("the sketch's curve does not form one wire")
    return wires[0]


def _path_wire(path: list[dict[str, Any]]) -> Wire:
    edges = []
    for entity in path:
        if entity["type"] == "line":
            edges.append(Edge.make_line(entity["start"], entity["end"]))
        else:
            edges.append(Edge.make_three_point_arc(entity["start"], entity["mid"], entity["end"]))
    return Wire(edges)


def _offset(shape: Any, distance: float) -> Any:
    """An OpenCascade offset of a face or shell along its normal."""
    from OCP.BRepOffset import BRepOffset_Skin
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape
    from OCP.GeomAbs import GeomAbs_Arc

    maker = BRepOffsetAPI_MakeOffsetShape()
    maker.PerformByJoin(shape.wrapped, distance, 1e-6, BRepOffset_Skin, False, False, GeomAbs_Arc)
    if not maker.IsDone():
        raise SurfaceReferenceError("OpenCascade could not offset the surface")
    return _as_shell(maker.Shape())


def _as_shell(wrapped: Any) -> Any:
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
    from OCP.TopoDS import TopoDS

    if wrapped.ShapeType() == TopAbs_FACE:
        return Face(TopoDS.Face(wrapped))
    if wrapped.ShapeType() == TopAbs_SHELL:
        return Shell(TopoDS.Shell(wrapped))
    faces = Compound(wrapped).faces()
    return faces[0] if len(faces) == 1 else _sewn(faces)


def _sewn(faces: list[Any]) -> Any:
    """Faces sewn along their shared edges into one shell (CATIA's Join)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    sewing = BRepBuilderAPI_Sewing(1e-6)
    for face in faces:
        sewing.Add(face.wrapped)
    sewing.Perform()
    return _as_shell(sewing.SewedShape())


def _faces(shape: Any) -> list[Any]:
    return list(shape.faces())


def closed_solid(shape: Any) -> Any:
    """The solid inside a surface whose openings are planar, each opening
    closed by the planar face it bounds (CATIA's Close Surface)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    free = ShapeAnalysis_FreeBounds(shape.wrapped, 1e-6, False, False)
    caps = []
    explorer = TopExp_Explorer(free.GetClosedWires(), TopAbs_WIRE)
    while explorer.More():
        wire = Wire(TopoDS.Wire(explorer.Current()))
        try:
            caps.append(Face(wire))
        except Exception as error:  # an opening that is not planar
            raise SurfaceReferenceError(f"an opening of the surface is not planar ({error})") \
                from error
        explorer.Next()
    shell = _sewn(_faces(shape) + caps)
    if shell.wrapped.ShapeType() != TopAbs_SHELL:
        raise SurfaceReferenceError("the surface and its caps do not close")
    solid = Solid(BRepBuilderAPI_MakeSolid(TopoDS.Shell(shell.wrapped)).Solid())
    if solid.volume < 0:
        solid = Solid(solid.wrapped.Reversed())
    return solid


def split_by_plane(shape: Any, plane: Plane, keep_top: bool) -> Any:
    """The part of a surface on one side of a plane (build123d's split
    classifies a shell as a whole): the shell cut by the half-space box on
    the other side."""
    size = 4 * (shape.bounding_box().diagonal + plane.origin.length) + 1.0
    away = -size / 2 if keep_top else size / 2
    box = Box(size, size, size).located(Location(plane) * Location((0, 0, away)))
    faces = _faces(shape.cut(box))
    if not faces:
        raise SurfaceReferenceError("the split keeps nothing of the surface")
    return faces[0] if len(faces) == 1 else _sewn(faces)


def surface_of(feature: dict[str, Any], sketches: dict[str, dict[str, Any]],
               surfaces: dict[str, Any]) -> Any:
    """The surface a `surface`, `join_surfaces` or `split_surface` makes."""
    kind, inputs = feature["type"], feature.get("inputs") or []
    if kind == "join_surfaces":
        return _sewn([face for name in inputs for face in _faces(surfaces[name])])
    if kind == "split_surface":
        plane = feature["plane"]
        return split_by_plane(surfaces[inputs[0]], Plane(origin=plane["origin"],
                                                         z_dir=plane["normal"]),
                              feature["keep"] == "top")
    op = feature["op"]
    if op == "offset":
        return _offset(surfaces[inputs[0]], float(feature["distance"]))
    if op == "loft":
        wires = [sketch_wire(sketches[name]) for name in inputs]
        return Shell.make_loft(wires, ruled=bool(feature.get("ruled")))
    sketch = sketches[inputs[0]]
    wire = sketch_wire(sketch)
    if op == "extrude":
        vector = extrude_vector(feature, Frame.from_tree(sketch["plane"]))
        if feature.get("symmetric"):
            start = wire.moved(Location(Vector(*scale(vector, -1))))
            return Shell.extrude(start, Vector(*scale(vector, 2)))
        return Shell.extrude(wire, Vector(*vector))
    if op == "revolve":
        axis = feature["axis"]
        return Shell.revolve(wire, float(feature["angle"]),
                             Axis(tuple(axis["origin"]), tuple(axis["direction"])))
    if op == "sweep":
        return Shell.sweep(wire, _path_wire(feature["path"]), transition=Transition.TRANSFORMED)
    if op == "fill":
        return Face(wire)
    raise SurfaceReferenceError(f"no reference for '{op}' surfaces")


def solid_of(feature: dict[str, Any], surfaces: dict[str, Any]) -> Any:
    """The solid a `thicken` or `close_surface` makes."""
    surface = surfaces[feature["inputs"][0]]
    if feature["type"] == "thicken":
        return Solid.thicken(surface, float(feature["thickness"]))
    return closed_solid(surface)


def surface_properties(shape: Any) -> dict[str, Any]:
    """Area and centre of gravity (of the area) of a face or shell."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    for face in shape.faces():
        part = GProp_GProps()
        BRepGProp.SurfaceProperties_s(_divided(face), part)
        props.Add(part)
    centre = props.CentreOfMass()
    return {"area_mm2": float(props.Mass()),
            "cog_mm": [float(centre.X()), float(centre.Y()), float(centre.Z())]}


def _divided(face: Any) -> Any:
    """The face to integrate: itself when its geometry is exact, else cut
    into about DIVIDE_PIECES pieces of equal area."""
    if face.geom_type.name in EXACT_FACES:
        return face.wrapped
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideArea

    divider = ShapeUpgrade_ShapeDivideArea(face.wrapped)
    divider.MaxArea = face.area / DIVIDE_PIECES
    divider.Perform()
    return divider.Result()
