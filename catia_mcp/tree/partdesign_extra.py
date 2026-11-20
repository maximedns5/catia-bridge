"""Part Design features of the second slice: the contract and its rules.

Pure geometry and preflight rules, no CATIA and no build123d, for:

- `thread`: a cosmetic thread on a shaft (CATIA's Thread on a cylindrical
  face, from a planar limit face). build123d has none, and CATIA's is
  cosmetic: the solid is unchanged;
- user patterns of holes: a hole whose frames form no grid or circle, but
  share their direction and plane, is one seed Hole and a User Pattern
  located by a sketch of points (`pattern: {"kind": "user"}` asks for it);
- `move`: a body moved in place by a rigid matrix (build123d's
  `body.moved(Location)` kept under the same name);
- `mirror_in_place`: a body replaced by its mirror image (build123d's
  `mirror(body, plane, mode=Mode.REPLACE)`).

Sweeps along a helix are in catia_mcp/tree/helix.py. Every field here is an
optional, additive addition to tree v1, listed in docs/part-design.md under
"Contract additions requested from the app".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from catia_mcp.tree.anchors import face_anchor_problem
from catia_mcp.tree.geometry import Vec, add, cross, dot, frame_axes, norm, scale, unit
from catia_mcp.tree.partdesign import ThreadSpec, thread_spec

EXTRA_TYPES = ("thread", "move", "mirror_in_place")
PATTERN_KINDS = ("user",)
ON_TOL = 1e-6


def _vec(value: Any) -> Vec:
    out = tuple(float(c) for c in value)
    if len(out) != 3:
        raise ValueError("not a 3D vector")
    return out


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _one_body(feature: dict[str, Any], bodies: set[str], refuse, where: str, kind: str) -> bool:
    if len(feature.get("bodies") or []) != 1 or feature["bodies"][0] not in bodies:
        refuse(f"{kind} must modify exactly one known body", where)
        return False
    return True


# ── Cosmetic threads on a shaft ──


def shaft_thread(feature: dict[str, Any]) -> ThreadSpec:
    """The thread of a `thread` feature: `thread` is a designation ("M10",
    "M10x1") or a mapping as a hole's, `length` its depth from the limit
    face. Raises ValueError."""
    spec = thread_spec({"thread": feature.get("thread"), "depth": feature.get("length")})
    if spec is None:
        raise ValueError("a thread feature needs its thread (an ISO designation)")
    if spec.depth is None:
        raise ValueError("a thread needs its length")
    return spec


def check_thread(feature: dict[str, Any], bodies: set[str], refuse, where: str) -> None:
    _one_body(feature, bodies, refuse, where, "thread")
    face, limit = feature.get("face"), feature.get("limit_face")
    for anchor, what in ((face, "face"), (limit, "limit_face")):
        problem = face_anchor_problem(anchor) if isinstance(anchor, dict) else \
            f"a thread needs its {what} (a face anchor)"
        if problem:
            refuse(problem, where)
            return
    if face.get("geometry") not in (None, "cylinder"):
        refuse("a thread's face must be a cylinder", where)
    if limit.get("geometry") not in (None, "plane"):
        refuse("a thread's limit face must be a plane", where)
    try:
        spec = shaft_thread(feature)
    except (TypeError, ValueError) as error:
        refuse(f"thread is invalid ({error})", where)
        return
    radius = face.get("radius")
    if radius is not None and (not _number(radius) or radius <= 0):
        refuse("a thread face's radius must be a positive number", where)
    elif radius is not None and spec.pitch >= float(radius):
        refuse("a thread's pitch must be smaller than its shaft's radius", where)


# ── User patterns of holes ──


@dataclass
class UserLayout:
    seed: int  # the seed Hole's frame
    origin: Vec  # the seed frame's origin (the positions sketch's origin)
    x_dir: Vec
    z_dir: Vec  # the drilling axis, shared by every frame
    positions: list[tuple[float, float]]  # every other frame, in (x, y) of the seed's


def user_layout(feature: dict[str, Any]) -> UserLayout | None:
    """The hole's frames as a seed and the positions of a User Pattern, when
    they share their drilling direction and their plane, else None."""
    frames = feature.get("frames") or []
    if len(frames) < 2:
        return None
    axes = [frame_axes(m) for m in frames]
    origin, x_dir, z_dir = axes[0]
    y_dir = cross(z_dir, x_dir)
    positions = []
    for at, _, z in axes[1:]:
        if abs(dot(z, z_dir) - 1) > ON_TOL:
            return None
        offset = add(at, scale(origin, -1))
        if abs(dot(offset, z_dir)) > ON_TOL:
            return None
        positions.append((dot(offset, x_dir), dot(offset, y_dir)))
    return UserLayout(0, origin, x_dir, z_dir, positions)


def wants_user_pattern(feature: dict[str, Any]) -> bool:
    pattern = feature.get("pattern")
    return isinstance(pattern, dict) and pattern.get("kind") == "user"


def positions_sketch(layout: UserLayout) -> dict[str, Any]:
    """The User Pattern's positions: a sketch of points on the seed's plane
    (the seed's own position left out: on V5 a locating point on the seed
    made the update fail)."""
    return {"plane": {"origin": list(layout.origin), "x_dir": list(layout.x_dir),
                      "normal": list(layout.z_dir)},
            "entities": [{"type": "point", "at": [u, v], "id": f"q{i}"}
                         for i, (u, v) in enumerate(layout.positions)],
            "profiles": []}


def check_hole_pattern(feature: dict[str, Any], refuse, where: str) -> None:
    pattern = feature.get("pattern")
    if pattern is None:
        return
    if not isinstance(pattern, dict) or pattern.get("kind") not in PATTERN_KINDS:
        refuse("a hole's pattern is {\"kind\": \"user\"} (grids and circles are found from "
               "the frames)", where)
        return
    try:
        layout = user_layout(feature)
    except (KeyError, TypeError, ValueError):
        return  # the frames are refused elsewhere
    if layout is None:
        refuse("a user pattern's holes must share their direction and their plane", where)
        return
    given = pattern.get("positions")
    if given is not None:
        try:
            points = [_vec(p) for p in given]
        except (TypeError, ValueError):
            refuse("a user pattern's positions are [x, y, z] points", where)
            return
        origins = [frame_axes(m)[0] for m in feature["frames"]]
        if len(points) != len(origins) or any(norm(add(p, scale(o, -1))) > ON_TOL
                                              for p, o in zip(points, origins, strict=False)):
            refuse("a user pattern's positions must be its hole frames' origins, in order",
                   where)


# ── Bodies moved or mirrored in place ──


def mirrored_point(plane: dict[str, Any], point: Vec) -> Vec:
    origin, normal = _vec(plane["origin"]), unit(_vec(plane["normal"]))
    return add(point, scale(normal, -2 * dot(add(point, scale(origin, -1)), normal)))


def moved_point(matrix: Any, point: Vec) -> Vec:
    from catia_mcp.tree.placement import placement_transform

    rotation, t = placement_transform(matrix)
    return tuple(sum(rotation[i][k] * point[k] for k in range(3)) + t[i] for i in range(3))


def check_move(feature: dict[str, Any], bodies: set[str], refuse, where: str) -> None:
    from catia_mcp.tree.placement import placement_transform

    if not _one_body(feature, bodies, refuse, where, "move"):
        return
    try:
        placement_transform(feature.get("matrix"))
    except ValueError as error:
        refuse(f"moves that are not rigid are not supported ({error}); a mirror is "
               "mirror_in_place", where)


def check_mirror_in_place(feature: dict[str, Any], bodies: set[str], refuse,
                          where: str) -> None:
    if not _one_body(feature, bodies, refuse, where, "mirror_in_place"):
        return
    plane = feature.get("plane") or {}
    try:
        _vec(plane["origin"])
        unit(_vec(plane["normal"]))
    except (KeyError, TypeError, ValueError):
        refuse("mirror_in_place plane is invalid", where)


def check_extra(feature: dict[str, Any], bodies: set[str], refuse, where: str) -> None:
    kind = feature.get("type")
    if kind == "thread":
        check_thread(feature, bodies, refuse, where)
    elif kind == "move":
        check_move(feature, bodies, refuse, where)
    elif kind == "mirror_in_place":
        check_mirror_in_place(feature, bodies, refuse, where)
