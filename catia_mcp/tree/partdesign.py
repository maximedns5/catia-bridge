"""Part Design features beyond the first milestones: the contract and its rules.

Pure geometry and preflight rules, no CATIA and no build123d, for:

- `draft`: build123d's draft(faces, neutral_plane, angle). The faces are face
  anchors (catia_mcp/tree/anchors.py), the neutral plane's normal is the
  pulling direction, and a positive angle narrows the solid along it
  (OpenCascade's BRepOffsetAPI_DraftAngle, measured with build123d 0.13).
- `extrude` with `until`: build123d's extrude(until=Until.NEXT / LAST /
  PREVIOUS / FIRST) into the body it joins or cuts, and two limits a designer
  picks in CATIA that build123d has no keyword for: up to a plane and up to a
  planar face of the body.
- threaded holes (`thread`) and V-bottomed blind holes (`bottom`): the thread
  is cosmetic (CATIA's is too), so the solid is the drilled hole.
- lofts to a point: one section a sketch, the other a single vertex (a cone
  or a pyramid tip).

Every field here is an optional, additive addition to tree v1, listed in
docs/part-design.md under "Contract additions requested from the app".
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from catia_mcp.tree.anchors import face_anchor_problem, plane_of
from catia_mcp.tree.geometry import (
    Frame,
    Vec,
    add,
    cross,
    dot,
    frame_axes,
    norm,
    profile_area_centroid,
    scale,
    unit,
)

Refuse = Callable[[str, str], None]

# build123d's Until, and the two limits CATIA adds.
UNTIL_MODES = ("next", "last", "previous", "first", "plane", "face")
# Faces build123d's draft accepts (OpenCascade's draft angle).
DRAFT_GEOMETRIES = ("plane", "cylinder", "cone")
HOLE_BOTTOMS = ("flat", "v")
THREAD_SIDES = ("right", "left")
# ISO 261 coarse pitches (mm) by nominal diameter, for a thread given only by
# its designation ("M6").
ISO_COARSE_PITCH = {
    1.0: 0.25, 1.2: 0.25, 1.4: 0.3, 1.6: 0.35, 2.0: 0.4, 2.5: 0.45, 3.0: 0.5, 3.5: 0.6,
    4.0: 0.7, 5.0: 0.8, 6.0: 1.0, 8.0: 1.25, 10.0: 1.5, 12.0: 1.75, 14.0: 2.0, 16.0: 2.0,
    18.0: 2.5, 20.0: 2.5, 22.0: 2.5, 24.0: 3.0, 27.0: 3.0, 30.0: 3.5, 36.0: 4.0, 42.0: 4.5,
    48.0: 5.0, 56.0: 5.5, 64.0: 6.0,
}
_DESIGNATION = re.compile(r"^\s*M\s*(\d+(?:\.\d+)?)\s*(?:[xX×]\s*(\d+(?:\.\d+)?))?\s*$")
ON_TOL = 1e-6


def _vec(value: Any) -> Vec:
    out = tuple(float(c) for c in value)
    if len(out) != 3:
        raise ValueError("not a 3D vector")
    return out


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def native(feature: dict[str, Any]) -> bool:
    """False for a feature of a supported type that lacks the fields its
    native rule needs: a draft without its faces and neutral plane (what the
    tree carried before drafts were native) still goes to the STEP fallback,
    or is refused as unsupported."""
    if feature.get("type") == "draft":
        return "faces" in feature and "neutral_plane" in feature
    return True


def plane_frame(plane: dict[str, Any]) -> Frame:
    """A Frame for a plane given by origin and normal (x_dir optional)."""
    normal = unit(_vec(plane["normal"]))
    x_dir = plane.get("x_dir")
    if x_dir:
        x_dir = _vec(x_dir)
    else:
        seed = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
        x_dir = unit(add(seed, scale(normal, -dot(seed, normal))))
    return Frame.from_tree({"origin": list(_vec(plane["origin"])), "x_dir": list(x_dir),
                            "normal": list(normal)})


# ── Draft ──


@dataclass
class DraftPlan:
    origin: Vec  # a point of the neutral plane
    pull: Vec  # the pulling direction (the neutral plane's normal), unit
    angle: float  # degrees, build123d's sign

    @property
    def tan(self) -> float:
        return math.tan(math.radians(self.angle))


def draft_plan(feature: dict[str, Any]) -> DraftPlan:
    plane = feature["neutral_plane"]
    pull = feature.get("pull_direction")
    normal = unit(_vec(plane["normal"]))
    if pull is not None and abs(abs(dot(unit(_vec(pull)), normal)) - 1) > 1e-9:
        raise ValueError("the pulling direction must be the neutral plane's normal (build123d "
                         "drafts along it)")
    return DraftPlan(_vec(plane["origin"]), unit(_vec(pull)) if pull is not None else normal,
                     float(feature["angle"]))


def draft_estimate(plan: DraftPlan, faces: list[dict[str, Any]]) -> float:
    """The volume change, to first order in tan(angle): each face turns
    about its line on the neutral plane, removing (positive angle) its area
    times its centroid's height along the pull times tan(angle). Exact for one
    face, or faces that share no edge; corners between drafted faces add a
    term in tan^2 (3 % on a box drafted on four sides at 5 degrees)."""
    total = 0.0
    for face in faces:
        height = dot(add(_vec(face["point"]), scale(plan.origin, -1)), plan.pull)
        total -= float(face["area"]) * height * plan.tan
    return total


def check_draft(feature: dict[str, Any], bodies: set[str], refuse: Refuse, where: str) -> None:
    if len(feature.get("bodies") or []) != 1 or feature["bodies"][0] not in bodies:
        refuse("draft must modify exactly one known body", where)
    angle = feature.get("angle")
    if not _number(angle) or not 0 < abs(angle) < 90:
        refuse("draft angle must be a non-zero number of degrees in (-90, 90)", where)
    faces = feature.get("faces")
    if not isinstance(faces, list) or not faces:
        refuse("draft has no face", where)
        return
    for problem in dict.fromkeys(face_anchor_problem(face) for face in faces):
        if problem:
            refuse(problem, where)
            return
    for face in faces:
        geometry = face.get("geometry")
        if geometry is not None and geometry not in DRAFT_GEOMETRIES:
            refuse(f"drafts on {geometry} faces are not supported (build123d drafts planes, "
                   "cylinders and cones)", where)
            return
    try:
        plan = draft_plan(feature)
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"draft neutral plane is invalid ({error})", where)
        return
    for face in faces:
        plane = plane_of(face)
        if plane is not None and abs(abs(dot(plane[1], plan.pull)) - 1) < 1e-9:
            refuse("a face square to the pulling direction cannot be drafted", where)
            return


# ── Extrude until ──


@dataclass
class UntilSpec:
    mode: str  # one of UNTIL_MODES
    sign: float  # +1 along the sketch normal, -1 against it
    plane: tuple[Vec, Vec] | None = None  # (point, unit normal) for "plane" and "face"
    face: dict[str, Any] | None = None  # the face anchor for "face"


def until_spec(feature: dict[str, Any], sketch: dict[str, Any]) -> UntilSpec:
    """How far an `until` extrude goes, read from the tree. Raises ValueError."""
    mode = feature.get("until")
    if mode not in UNTIL_MODES:
        raise ValueError(f"'until' must be one of {', '.join(UNTIL_MODES)}")
    frame = Frame.from_tree(sketch["plane"])
    sign = 1.0
    direction = feature.get("direction")
    if direction is not None:
        d = unit(_vec(direction))
        along = dot(d, frame.normal)
        if abs(abs(along) - 1) > 1e-9:
            raise ValueError("an extrude up to a limit must go along its sketch normal")
        sign = math.copysign(1.0, along)
    if mode in ("previous", "first"):
        sign = -sign  # build123d reverses the direction for these
    plane = face = None
    if mode == "plane":
        target = feature.get("until_plane") or {}
        plane = (_vec(target["origin"]), unit(_vec(target["normal"])))
    elif mode == "face":
        face = feature.get("until_face")
        problem = face_anchor_problem(face)
        if problem:
            raise ValueError(problem)
        plane = plane_of(face)
        if plane is None:
            raise ValueError("extrudes up to a face that is not a plane are not supported yet")
    if plane is not None:
        if abs(dot(plane[1], frame.normal)) < 1e-9:
            raise ValueError("the limiting plane is parallel to the extrude direction")
        if direction is None:
            # Toward the plane, from the profile's centroid.
            _, (u, v) = profile_area_centroid(sketch)
            height = dot(add(plane[0], scale(frame.world(u, v), -1)), plane[1]) \
                / dot(frame.normal, plane[1])
            if abs(height) < ON_TOL:
                raise ValueError("the limiting plane goes through the profile")
            sign = math.copysign(1.0, height)
    return UntilSpec(mode, sign, plane, face)


def check_until(feature: dict[str, Any], sketch: dict[str, Any] | None, refuse: Refuse,
                where: str) -> None:
    if feature.get("operation") not in ("join", "cut"):
        refuse("an extrude up to a limit must join or cut a body (build123d limits it by the "
               "body it changes)", where)
    if feature.get("taper"):
        refuse("tapered extrudes up to a limit are not supported yet", where)
    if feature.get("symmetric"):
        refuse("symmetric extrudes up to a limit are not supported", where)
    target = feature.get("until_target")
    if target is not None and target != (feature.get("bodies") or [None])[0]:
        refuse("extrudes limited by another body than the one they change are not supported "
               "yet", where)
    if sketch is None:
        return
    try:
        until_spec(feature, sketch)
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"extrude up to a limit is not supported ({error})", where)


# ── Holes: threads and bottoms ──


@dataclass
class ThreadSpec:
    designation: str
    nominal: float
    pitch: float
    depth: float | None  # None: the whole through hole
    side: str = "right"
    coarse: bool = True


def thread_spec(feature: dict[str, Any]) -> ThreadSpec | None:
    """The hole's thread, or None. `thread` is a designation ("M6", "M8x1")
    or a mapping {designation?, nominal_diameter?, pitch?, depth?, side?}.
    A missing pitch is ISO coarse; a missing depth is the hole's. Raises
    ValueError."""
    raw = feature.get("thread")
    if raw is None or raw is False:
        return None
    if isinstance(raw, str):
        raw = {"designation": raw}
    if not isinstance(raw, dict):
        raise TypeError("a hole thread is a designation or a mapping")
    nominal, pitch = raw.get("nominal_diameter"), raw.get("pitch")
    designation = raw.get("designation")
    if designation is not None:
        match = _DESIGNATION.match(str(designation))
        if not match:
            raise ValueError(f"thread designation {designation!r} is not ISO metric (M6, M8x1)")
        nominal = float(match.group(1)) if nominal is None else nominal
        if match.group(2) and pitch is None:
            pitch = float(match.group(2))
    if not _number(nominal) or nominal <= 0:
        raise ValueError("a thread needs a nominal diameter or a designation")
    coarse = ISO_COARSE_PITCH.get(float(nominal))
    if pitch is None:
        if coarse is None:
            raise ValueError(f"no ISO coarse pitch for M{nominal:g}: give the pitch")
        pitch = coarse
    if not _number(pitch) or pitch <= 0:
        raise ValueError("a thread pitch must be a positive number")
    # No depth: a blind hole's own, a through hole's whole length (CATIA's).
    depth = raw.get("depth", feature.get("depth"))
    if depth is not None and (not _number(depth) or depth <= 0):
        raise ValueError("a thread depth must be a positive number")
    side = raw.get("side", "right")
    if side not in THREAD_SIDES:
        raise ValueError(f"a thread side is one of {', '.join(THREAD_SIDES)}")
    name = designation or (f"M{nominal:g}" if coarse is not None and abs(pitch - coarse) < 1e-9
                           else f"M{nominal:g}x{pitch:g}")
    return ThreadSpec(str(name).replace(" ", ""), float(nominal), float(pitch),
                      None if depth is None else float(depth),
                      side, coarse is not None and abs(pitch - coarse) < 1e-9)


def bottom_angle(feature: dict[str, Any]) -> float | None:
    """The point angle (degrees) of a V-bottomed hole, None for a flat one."""
    bottom = feature.get("bottom")
    if bottom is None:
        return None
    kind = bottom.get("type", "flat") if isinstance(bottom, dict) else bottom
    if kind == "flat":
        return None
    return float(bottom.get("angle", 118.0)) if isinstance(bottom, dict) else 118.0


def tip_height(feature: dict[str, Any]) -> float:
    """The height of a V bottom's cone below the hole's depth (0 when flat)."""
    angle = bottom_angle(feature)
    if angle is None:
        return 0.0
    return float(feature["diameter"]) / 2 / math.tan(math.radians(angle) / 2)


def tip_sketch(feature: dict[str, Any], matrix) -> tuple[dict[str, Any], Vec, Vec] | None:
    """A V bottom's cone as a triangle to revolve about the frame's Z, in the
    frame's (X, Z) plane (as geometry.hole_profile_sketch), or None."""
    tip = tip_height(feature)
    if not tip:
        return None
    origin, x_dir, z_dir = frame_axes(matrix)
    r, d = float(feature["diameter"]) / 2, float(feature["depth"])
    points = [(0.0, -d), (r, -d), (0.0, -d - tip)]
    entities = [{"type": "line", "start": list(points[i]), "end": list(points[(i + 1) % 3]),
                 "id": f"t{i}"} for i in range(3)]
    sketch = {"plane": {"origin": list(origin), "x_dir": list(x_dir),
                        "normal": list(cross(x_dir, z_dir))},
              "entities": entities, "profiles": [{"outer": [e["id"] for e in entities],
                                                  "inner": []}]}
    return sketch, origin, z_dir


def check_hole_extras(feature: dict[str, Any], refuse: Refuse, where: str) -> None:
    try:
        thread = thread_spec(feature)
    except (TypeError, ValueError) as error:
        refuse(f"hole thread is invalid ({error})", where)
        thread = None
    if thread is not None:
        diameter = feature.get("diameter") or 0
        if thread.nominal <= diameter:
            refuse("a threaded hole's diameter is its tap drill, smaller than the thread's "
                   "nominal diameter", where)
        depth = feature.get("depth")
        if depth is not None and thread.depth is not None and thread.depth > float(depth) + 1e-9:
            refuse("a thread cannot be deeper than its blind hole", where)
    bottom = feature.get("bottom")
    if bottom is None:
        return
    kind = bottom.get("type", "flat") if isinstance(bottom, dict) else bottom
    if kind not in HOLE_BOTTOMS:
        refuse(f"hole bottom '{kind}' is not supported", where)
        return
    if kind == "v":
        angle = bottom.get("angle", 118.0) if isinstance(bottom, dict) else 118.0
        if not _number(angle) or not 0 < angle < 180:
            refuse("a V bottom's angle must be in (0, 180) degrees", where)
        if feature.get("depth") is None:
            refuse("a through hole has no bottom to shape", where)


# ── Lofts to a point ──


@dataclass
class PointLoft:
    sketch: str  # the section sketch's id
    point: Vec  # the apex
    point_first: bool  # the apex is the first section


def point_loft(feature: dict[str, Any]) -> PointLoft | None:
    """The loft's sketch and apex when one section is a point, else None.
    A point section is {"point": [x, y, z]} (or a bare [x, y, z])."""
    sections = feature.get("sections") or []
    if all(isinstance(section, str) for section in sections):
        return None
    if len(sections) != 2:
        raise ValueError("a loft to a point has exactly one sketch and one point")
    points = [i for i, s in enumerate(sections) if not isinstance(s, str)]
    if len(points) != 1:
        raise ValueError("a loft to a point has exactly one sketch and one point")
    raw = sections[points[0]]
    raw = raw.get("point") if isinstance(raw, dict) else raw
    return PointLoft(sections[1 - points[0]], _vec(raw), points[0] == 0)


@dataclass
class ConeExpectation:
    volume: float
    cog: Vec
    height: float  # the apex's height above the section plane (signed, along its normal)


def cone_expectation(sketch: dict[str, Any], apex: Vec) -> ConeExpectation:
    """A generalised cone from a planar section to an apex: volume A h / 3,
    centre of gravity a quarter of the way from the section's centroid to
    the apex (exact for any planar section)."""
    frame = Frame.from_tree(sketch["plane"])
    area, (u, v) = profile_area_centroid(sketch)
    base = frame.world(u, v)
    height = dot(add(apex, scale(frame.origin, -1)), frame.normal)
    cog = add(base, scale(add(apex, scale(base, -1)), 0.25))
    return ConeExpectation(area * abs(height) / 3, cog, height)


def right_cone(sketch: dict[str, Any], apex: Vec) -> tuple[Vec, Vec, float, float] | None:
    """(centre, unit axis toward the apex, radius, height) when the section is
    one circle and the apex is on its axis: a cone a Shaft can turn."""
    entities = sketch.get("entities") or []
    if len(entities) != 1 or entities[0].get("type") != "circle":
        return None
    frame = Frame.from_tree(sketch["plane"])
    centre = frame.world(*entities[0]["center"])
    offset = add(apex, scale(centre, -1))
    height = dot(offset, frame.normal)
    if abs(height) < ON_TOL or norm(add(offset, scale(frame.normal, -height))) > ON_TOL:
        return None
    return centre, scale(frame.normal, math.copysign(1.0, height)), \
        float(entities[0]["radius"]), abs(height)


def cone_profile(centre: Vec, axis: Vec, radius: float, height: float
                 ) -> tuple[dict[str, Any], Vec, Vec]:
    """The right triangle a Shaft turns into a cone, in a plane holding the
    axis, with the axis as the sketch's V direction."""
    seed = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    x_dir = unit(add(seed, scale(axis, -dot(seed, axis))))
    points = [(0.0, 0.0), (radius, 0.0), (0.0, height)]
    entities = [{"type": "line", "start": list(points[i]), "end": list(points[(i + 1) % 3]),
                 "id": f"k{i}"} for i in range(3)]
    sketch = {"plane": {"origin": list(centre), "x_dir": list(x_dir),
                        "normal": list(cross(x_dir, axis))},
              "entities": entities, "profiles": [{"outer": [e["id"] for e in entities],
                                                  "inner": []}]}
    return sketch, centre, axis


def check_point_loft(feature: dict[str, Any], sketches: dict[str, dict[str, Any]],
                     refuse: Refuse, where: str) -> None:
    try:
        plan = point_loft(feature)
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"loft to a point is invalid ({error})", where)
        return
    sketch = sketches.get(plan.sketch)
    if sketch is None:
        refuse("loft refers to an unknown sketch", where)
        return
    profiles = sketch.get("profiles") or []
    if len(profiles) != 1 or profiles[0].get("inner"):
        refuse("a loft to a point needs a section of one profile without holes", where)
        return
    if any(entity.get("type") == "spline" for entity in sketch.get("entities") or []):
        refuse("loft sections with spline entities are not supported yet", where)
        return
    try:
        want = cone_expectation(sketch, plan.point)
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"loft to a point is invalid ({error})", where)
        return
    if abs(want.height) < ON_TOL:
        refuse("a loft's point lies on its section's plane", where)
