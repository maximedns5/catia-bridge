"""Sweep paths and expected solids, without a CAD kernel.

A tree sweep moves a sketch profile along a 3D path of lines and arcs.
build123d keeps the profile at a constant angle to the path (corrected
Frenet), as CATIA's Rib does with its default "keep angle". This module
supports paths that are planar and tangent-continuous, with the profile
perpendicular to the path where it starts: the path is then drawn as a
sketch in its own plane for CATIA, and the solid's volume is the profile's
area times the length its centroid travels (Pappus).
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

from catia_mcp.tree.geometry import (
    Frame,
    Vec,
    add,
    cross,
    dot,
    norm,
    profile_area_centroid,
    profile_loops,
    scale,
    unit,
)
from catia_mcp.tree.loft import Segment, _entity_segment

PATH_TYPES = ("line", "arc")
# Points closer than this are the same point (mm); directions closer than
# this (1 - cosine) are parallel.
JOIN_TOL = 1e-6
ANGLE_TOL = 1e-9


def _vec(p) -> Vec:
    return tuple(float(c) for c in p)


def _sub(a: Vec, b: Vec) -> Vec:
    return add(a, scale(b, -1))


def path_plane(path: list[dict[str, Any]], fallback_normal: Vec) -> Frame:
    """The plane of a path: origin at its start, x_dir along its start
    tangent. A straight path takes `fallback_normal`, made perpendicular to
    it. Raises ValueError when the path is not planar."""
    if not path:
        raise ValueError("the sweep path is empty")
    for entity in path:
        if entity.get("type") not in PATH_TYPES:
            raise ValueError(f"'{entity.get('type')}' path entities are not supported yet")
    points = [_vec(e[k]) for e in path for k in ("start", "mid", "end") if k in e]
    start = points[0]
    normal = None
    for entity in path:
        if entity["type"] == "arc":
            n = cross(_sub(_vec(entity["mid"]), _vec(entity["start"])),
                      _sub(_vec(entity["end"]), _vec(entity["start"])))
            if norm(n) > 1e-9:
                normal = unit(n)
                break
    if normal is None:
        directions = [unit(_sub(_vec(e["end"]), _vec(e["start"]))) for e in path]
        for d in directions[1:]:
            n = cross(directions[0], d)
            if norm(n) > 1e-9:
                normal = unit(n)
                break
    if normal is None:  # one straight line
        d = unit(_sub(_vec(path[0]["end"]), _vec(path[0]["start"])))
        n = _sub(fallback_normal, scale(d, dot(fallback_normal, d)))
        if norm(n) < 1e-9:
            seed = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
            n = _sub(seed, scale(d, dot(seed, d)))
        normal = unit(n)
    for p in points:
        if abs(dot(_sub(p, start), normal)) > JOIN_TOL:
            raise ValueError("sweeps along a path that is not planar are not supported yet")
    first = path[0]
    if first["type"] == "line":
        tangent = unit(_sub(_vec(first["end"]), _vec(first["start"])))
    else:
        seg = _entity_segment(_to_2d(first, start, normal), lambda uv: tuple(uv))
        tangent = _from_2d(seg.tangent(0.0), start, normal, direction=True)
    return Frame.from_tree({"origin": list(start), "x_dir": list(unit(tangent)),
                            "normal": list(normal)})


def _basis(normal: Vec) -> tuple[Vec, Vec]:
    seed = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
    x = unit(_sub(seed, scale(normal, dot(seed, normal))))
    return x, cross(normal, x)


def _to_2d(entity: dict[str, Any], origin: Vec, normal: Vec) -> dict[str, Any]:
    x, y = _basis(normal)
    out = {"type": entity["type"]}
    for key in ("start", "mid", "end"):
        if key in entity:
            offset = _sub(_vec(entity[key]), origin)
            out[key] = (dot(offset, x), dot(offset, y))
    return out


def _from_2d(uv, origin: Vec, normal: Vec, direction: bool = False) -> Vec:
    x, y = _basis(normal)
    vec = add(scale(x, uv[0]), scale(y, uv[1]))
    return vec if direction else add(origin, vec)


def path_segments(path: list[dict[str, Any]], frame: Frame) -> list[Segment]:
    """The path in its plane's (u, v), checked to be connected and
    tangent-continuous (a corner would need a transition rule)."""

    def to_uv(p) -> tuple[float, float]:
        offset = _sub(_vec(p), frame.origin)
        return (dot(offset, frame.x_dir), dot(offset, frame.y_dir))

    segments = []
    for entity in path:
        local = {k: to_uv(entity[k]) for k in ("start", "mid", "end") if k in entity}
        segments.append(_entity_segment({"type": entity["type"], **local}, lambda uv: tuple(uv)))
    for here, there in pairwise(segments):
        if math.dist(here.b, there.a) > JOIN_TOL:
            raise ValueError("the sweep path is not connected end to end")
        t0, t1 = here.tangent(1.0), there.tangent(0.0)
        cosine = (t0[0] * t1[0] + t0[1] * t1[1]) / (math.hypot(*t0) * math.hypot(*t1))
        if cosine < 1 - ANGLE_TOL:
            raise ValueError("sweep paths with corners are not supported yet")
    return segments


def path_sketch(path: list[dict[str, Any]], frame: Frame) -> dict[str, Any]:
    """The path as a tree sketch on its own plane, for CATIA's Rib."""
    entities = [dict(seg.entity(lambda uv: uv), id=f"p{i}")
                for i, seg in enumerate(path_segments(path, frame))]
    return {"plane": {"origin": list(frame.origin), "x_dir": list(frame.x_dir),
                      "normal": list(frame.normal)},
            "entities": entities, "profiles": []}


def sweep_expectation(sketch: dict[str, Any], path: list[dict[str, Any]]) -> tuple[Frame, float]:
    """The path plane and the expected volume of sweeping the sketch's
    profile along the path. Raises ValueError when the sweep is outside what
    this builder supports."""
    profile = Frame.from_tree(sketch["plane"])
    frame = path_plane(path, profile.x_dir)
    segments = path_segments(path, frame)
    if abs(abs(dot(profile.normal, frame.x_dir)) - 1) > 1e-6:
        raise ValueError("sweep profiles that are not perpendicular to the path where it "
                         "starts are not supported yet")
    if abs(dot(_sub(frame.origin, profile.origin), profile.normal)) > JOIN_TOL:
        raise ValueError("the sweep profile's plane does not contain the start of the path")
    area, (cu, cv) = profile_area_centroid(sketch)
    # The profile rides on the path in the frame (tangent, inward, binormal):
    # its centroid keeps its offset along `inward` = binormal x tangent.
    inward = cross(frame.normal, frame.x_dir)
    centroid_offset = dot(_sub(profile.world(cu, cv), frame.origin), inward)
    reach = [dot(_sub(profile.world(u, v), frame.origin), inward)
             for sign, loop in profile_loops(sketch) if sign > 0 for u, v in loop]
    travelled = 0.0
    for seg in segments:
        if seg.center is None:
            travelled += seg.length
            continue
        # The arc turns toward +inward (left) when its sweep is positive.
        side = 1.0 if seg.a1 > seg.a0 else -1.0
        if min(seg.radius - side * r for r in reach) <= JOIN_TOL:
            raise ValueError("the sweep profile is wider than a path arc's radius, so the "
                             "solid would cross itself")
        travelled += abs(seg.a1 - seg.a0) * (seg.radius - side * centroid_offset)
    return frame, area * travelled
