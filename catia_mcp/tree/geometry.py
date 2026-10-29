"""Pure geometry for the tree: planes, 3-point arcs and sketch profiles.

No CATIA here, so everything can be tested offline. Lengths are mm, sketch
coordinates are (u, v) on a plane, and world = origin + u*x_dir + v*y_dir with
y_dir = normal x x_dir (the tree's convention, which CATIA's H/V matches).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

Vec = tuple[float, float, float]

# Endpoints closer than this (mm) are the same sketch point.
POINT_TOL = 1e-6
# Arc discretization for areas and centroids: segments per full turn.
ARC_SEGMENTS = 1440


def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec) -> Vec:
    length = norm(a)
    if length == 0:
        raise ValueError("zero-length vector")
    return scale(a, 1 / length)


def distance(a: Vec, b: Vec) -> float:
    return norm(add(a, scale(b, -1)))


@dataclass(frozen=True)
class Frame:
    """A sketch plane: origin, H (x_dir), V (y_dir) and normal = H x V."""

    origin: Vec
    x_dir: Vec
    y_dir: Vec
    normal: Vec

    @classmethod
    def from_tree(cls, plane: dict[str, Any]) -> Frame:
        origin = tuple(float(c) for c in plane["origin"])
        normal = unit(tuple(float(c) for c in plane["normal"]))
        x_dir = unit(tuple(float(c) for c in plane["x_dir"]))
        if abs(dot(x_dir, normal)) > 1e-9:
            raise ValueError("plane x_dir is not perpendicular to its normal")
        return cls(origin, x_dir, cross(normal, x_dir), normal)

    def world(self, u: float, v: float, w: float = 0.0) -> Vec:
        return add(add(add(self.origin, scale(self.x_dir, u)), scale(self.y_dir, v)),
                   scale(self.normal, w))


@dataclass(frozen=True)
class Arc:
    """A circular arc through three points, as CATIA's CreateCircle takes it:
    centre, radius and a counter-clockwise sweep from start to end (radians,
    end > start)."""

    center: tuple[float, float]
    radius: float
    start: float
    end: float

    @classmethod
    def through(cls, p0, p1, p2) -> Arc:
        (ax, ay), (bx, by), (cx, cy) = p0, p1, p2
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            raise ValueError("the three arc points are collinear")
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
        uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
        radius = math.hypot(ax - ux, ay - uy)

        def angle(px, py):
            return math.atan2(py - uy, px - ux)

        start, mid, end = angle(ax, ay), angle(bx, by), angle(cx, cy)
        ccw = ((mid - start) % (2 * math.pi)) < ((end - start) % (2 * math.pi))
        if not ccw:
            start, end = end, start
        while end <= start:
            end += 2 * math.pi
        return cls((ux, uy), radius, start, end)

    def points(self, reverse: bool = False) -> list[tuple[float, float]]:
        steps = max(8, math.ceil(ARC_SEGMENTS * (self.end - self.start) / (2 * math.pi)))
        cu, cv = self.center
        pts = [
            (cu + self.radius * math.cos(self.start + (self.end - self.start) * i / steps),
             cv + self.radius * math.sin(self.start + (self.end - self.start) * i / steps))
            for i in range(steps + 1)
        ]
        return pts[::-1] if reverse else pts


def _polyline(entity: dict[str, Any]) -> list[tuple[float, float]]:
    kind = entity["type"]
    if kind == "line":
        return [tuple(entity["start"]), tuple(entity["end"])]
    if kind == "arc":
        arc = Arc.through(entity["start"], entity["mid"], entity["end"])
        pts = arc.points()
        # Orient the polyline from the tree's start to its end.
        if math.dist(pts[0], entity["start"]) > math.dist(pts[-1], entity["start"]):
            pts.reverse()
        return pts
    raise ValueError(f"no polyline for entity type '{kind}'")


def _loop_points(entities: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """One closed loop of entities as a closed polygon (first point = last);
    arcs and circles are discretized."""
    if len(entities) == 1 and entities[0]["type"] == "circle":
        (cu, cv), r = entities[0]["center"], float(entities[0]["radius"])
        return Arc((cu, cv), r, 0.0, 2 * math.pi).points()
    pieces = [_polyline(e) for e in entities]
    chain = list(pieces.pop(0))
    while pieces:
        tail = chain[-1]
        for i, piece in enumerate(pieces):
            if math.dist(piece[0], tail) <= POINT_TOL:
                chain.extend(piece[1:])
                break
            if math.dist(piece[-1], tail) <= POINT_TOL:
                chain.extend(piece[::-1][1:])
                break
        else:
            raise ValueError("the loop's entities do not join end to end")
        pieces.pop(i)
    if math.dist(chain[0], chain[-1]) > POINT_TOL:
        raise ValueError("the loop is not closed")
    return chain


def _loop_area_centroid(entities: list[dict[str, Any]]) -> tuple[float, tuple[float, float]]:
    """Unsigned area and centroid of one closed loop of entities."""
    if len(entities) == 1 and entities[0]["type"] == "circle":
        r = float(entities[0]["radius"])
        return math.pi * r * r, tuple(entities[0]["center"])
    area2 = cx = cy = 0.0
    for (x0, y0), (x1, y1) in pairwise(_loop_points(entities)):
        c = x0 * y1 - x1 * y0
        area2 += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(area2) < 1e-12:
        raise ValueError("the loop encloses no area")
    return abs(area2) / 2, (cx / (3 * area2), cy / (3 * area2))


def profile_loops(sketch: dict[str, Any]) -> list[tuple[int, list[tuple[float, float]]]]:
    """(+1 outer / -1 inner, closed polygon) for every loop of the profiles."""
    by_id = {e["id"]: e for e in sketch["entities"]}
    loops = []
    for profile in sketch["profiles"]:
        loops.append((1, _loop_points([by_id[i] for i in profile["outer"]])))
        loops.extend((-1, _loop_points([by_id[i] for i in inner]))
                     for inner in profile.get("inner", []))
    return loops


def profile_area_centroid(sketch: dict[str, Any], profiles: list[dict[str, Any]] | None = None):
    """Total area (mm2) and centroid (u, v) of a sketch's profiles: each outer
    loop minus its inner loops."""
    by_id = {e["id"]: e for e in sketch["entities"]}
    total = mu = mv = 0.0
    for profile in profiles if profiles is not None else sketch["profiles"]:
        loops = [(1, profile["outer"])] + [(-1, inner) for inner in profile.get("inner", [])]
        for sign, ids in loops:
            area, (u, v) = _loop_area_centroid([by_id[i] for i in ids])
            total += sign * area
            mu += sign * area * u
            mv += sign * area * v
    if total <= 0:
        raise ValueError("the profiles enclose no area")
    return total, (mu / total, mv / total)


def prism_expectation(frame: Frame, area: float, centroid: tuple[float, float],
                      distance: float, symmetric: bool) -> tuple[float, Vec]:
    """Volume and centre of gravity of a straight extrude of a planar profile:
    `distance` signed along the normal, or on each side when symmetric."""
    if symmetric:
        return area * 2 * abs(distance), frame.world(*centroid)
    return area * abs(distance), frame.world(centroid[0], centroid[1], distance / 2)


def _polygon_moments(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Unsigned A, Sx = int x dA, Ixx = int x^2 dA and Ixy = int x y dA of a
    closed polygon (Green's theorem), whatever its orientation."""
    a = sx = ixx = ixy = 0.0
    for (x0, y0), (x1, y1) in pairwise(points):
        c = x0 * y1 - x1 * y0
        a += c / 2
        sx += (x0 + x1) * c / 6
        ixx += (x0 * x0 + x0 * x1 + x1 * x1) * c / 12
        ixy += (x0 * y1 + 2 * x0 * y0 + 2 * x1 * y1 + x1 * y0) * c / 24
    sign = 1.0 if a >= 0 else -1.0
    return sign * a, sign * sx, sign * ixx, sign * ixy


@dataclass(frozen=True)
class RevolveExpectation:
    volume: float
    cog: Vec          # revolving by +angle, right-handed about the axis direction
    cog_reversed: Vec  # revolving the other way
    min_radius: float  # < 0 when the profile crosses the axis


def revolve_expectation(frame: Frame, sketch: dict[str, Any], axis_origin: Vec,
                        axis_direction: Vec, angle_deg: float) -> RevolveExpectation:
    """Volume (Pappus) and centre of gravity of a profile revolved about an
    axis lying in its plane. Lengths mm, angle degrees."""
    d = unit(axis_direction)
    _, centroid = profile_area_centroid(sketch)
    offset = add(frame.world(*centroid), scale(axis_origin, -1))
    radial = add(offset, scale(d, -dot(offset, d)))
    if norm(radial) < POINT_TOL:
        raise ValueError("the profile's centroid lies on the revolve axis")
    e_r = unit(radial)
    area = s1 = s2 = st = 0.0
    min_radius = math.inf
    for sign, polygon in profile_loops(sketch):
        local = []
        for u, v in polygon:
            rel = add(frame.world(u, v), scale(axis_origin, -1))
            local.append((dot(rel, e_r), dot(rel, d)))
            min_radius = min(min_radius, local[-1][0])
        a, sx, ixx, ixy = _polygon_moments(local)
        area += sign * a
        s1 += sign * sx
        s2 += sign * ixx
        st += sign * ixy
    theta = math.radians(angle_deg)
    volume = theta * s1
    along = scale(d, st / s1)
    half = theta / 2
    distance_from_axis = 0.0 if abs(angle_deg - 360) < 1e-9 else (s2 / s1) * math.sin(half) / half
    tangent = cross(d, e_r)

    def at(phi: float) -> Vec:
        swing = add(scale(e_r, math.cos(phi)), scale(tangent, math.sin(phi)))
        return add(add(axis_origin, along), scale(swing, distance_from_axis))

    return RevolveExpectation(volume, at(half), at(-half), min_radius)
