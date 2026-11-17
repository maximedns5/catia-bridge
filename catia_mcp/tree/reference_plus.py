"""build123d references for drafts, extrudes up to a limit, V-bottomed holes
and lofts to a point (catia_mcp/tree/partdesign.py). Called by
catia_mcp/tree/reference.py; needs build123d."""

from __future__ import annotations

from typing import Any

from build123d import Axis, Keep, Plane, Solid, Until, Vertex, extrude, loft, revolve, split

from catia_mcp.tree.anchors import AnchorError, match_faces
from catia_mcp.tree.geometry import Frame, add, dot, scale
from catia_mcp.tree.partdesign import (
    UntilSpec,
    draft_plan,
    point_loft,
    tip_sketch,
    until_spec,
)

UNTIL = {"next": Until.NEXT, "last": Until.LAST, "previous": Until.NEXT, "first": Until.LAST}


class PlusReferenceError(Exception):
    pass


def draft(feature: dict[str, Any], body: Any, face_candidates) -> Any:
    """build123d's draft of the anchored faces about the neutral plane."""
    try:
        faces = [c.handle for c in match_faces(feature["faces"], face_candidates(body))]
    except AnchorError as error:
        raise PlusReferenceError(f"draft {feature.get('id')}: {error}") from error
    plan = draft_plan(feature)
    solid = body.solids()[0] if hasattr(body, "solids") and len(body.solids()) == 1 else body
    return solid.draft(faces, Plane(origin=plan.origin, z_dir=plan.pull), plan.angle)


def until_solid(feature: dict[str, Any], sketch: dict[str, Any], faces: list[Any],
                body: Any) -> Any:
    """The solid an `until` extrude adds or removes: build123d's
    extrude_until into the body for next / last / previous / first; for a
    plane or a planar face, the profile extruded toward it and cut there."""
    spec: UntilSpec = until_spec(feature, sketch)
    frame = Frame.from_tree(sketch["plane"])
    direction = scale(frame.normal, spec.sign)
    if spec.mode in UNTIL:
        pieces = [Solid.extrude_until(face, target=body, direction=direction,
                                      until=UNTIL[spec.mode]) for face in faces]
    else:
        point, normal = spec.plane
        bbox = body.bounding_box()
        reach = 2 * (bbox.diagonal + abs(dot(add(point, scale(frame.origin, -1)), normal))) + 1
        long = extrude(faces, amount=reach, dir=direction)
        # Keep the side of the plane the profile is on.
        side = dot(add(frame.origin, scale(point, -1)), normal)
        keep = Keep.TOP if side > 0 else Keep.BOTTOM
        cut = split(long, bisect_by=Plane(origin=point, z_dir=normal), keep=keep)
        pieces = [cut]
    result = pieces[0]
    for piece in pieces[1:]:
        result = result + piece
    return result


def point_loft_solid(feature: dict[str, Any], sketch_faces) -> Any:
    plan = point_loft(feature)
    face = sketch_faces[0]
    apex = Vertex(*plan.point)
    return loft([apex, face] if plan.point_first else [face, apex])


def hole_tip(feature: dict[str, Any], matrix, sketch_faces_of) -> Any | None:
    """A V bottom's cone below the hole's flat end, or None."""
    made = tip_sketch(feature, matrix)
    if made is None:
        return None
    sketch, origin, z_dir = made
    return revolve(sketch_faces_of(sketch), axis=Axis(origin, z_dir))
