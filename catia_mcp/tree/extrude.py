"""Tapered and oblique extrudes, without a CAD kernel.

build123d's `extrude(amount, dir, taper)` moves a planar profile along a
vector: `plane.normal * distance`, or the tree's `direction`, which is the
full vector (its length is how far the profile travels, as build123d measures
`amount` along `dir`). Symmetric extrudes go both ways from the profile.

A taper (degrees) moves every loop of the profile sideways in its own plane
while it travels: build123d lofts each loop to its 2D offset by
-|vector| * tan(taper) (Kind.INTERSECTION: lines meet at sharp corners, arcs
stay concentric), with the opposite offset on inner loops, then cuts the inner
lofts from the outer one. A positive taper therefore narrows the solid on
both walls (the outer loop shrinks, holes widen), whatever the extrude's
direction. Its LocOpe_DPrism shortcut, for positive tapers along the normal
without holes, gives the same solid.

This module builds that loft as loft.py's ruled LoftPlans, with each vertex
paired with its own offset, so CATIA can be given the same sections and the
volume and centre of gravity are exact (every section is the offset by a
fraction of the full one, so its area is quadratic along the extrude: the
prismatoid formula h/6 (A0 + 4 Am + A1)). Profiles whose offset would change
topology (a loop collapsing or crossing itself or another loop) are refused,
as are corners that join an arc without tangency, where OpenCascade extends
the arc in a way this module does not reproduce.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from catia_mcp.tree.geometry import (
    Frame,
    Vec,
    add,
    dot,
    norm,
    scale,
)
from catia_mcp.tree.loft import (
    LoftPlan,
    Segment,
    _section_integrals,
    loft_expectation,
    section_loop,
)

Pt = tuple[float, float]

# Below this (mm), an offset loop is degenerate.
COLLAPSE_TOL = 1e-6
# Unit tangents closer than this are tangent (G1) at a corner.
TANGENT_TOL = 1e-7
# Polyline steps per full turn when checking offset loops for crossings.
CHECK_STEPS = 256
# Tapers are in (-MAX_TAPER, MAX_TAPER) degrees.
MAX_TAPER = 89.0


def _vec(values) -> Vec:
    vector = tuple(float(c) for c in values)
    if len(vector) != 3 or not all(math.isfinite(c) for c in vector):
        raise ValueError("a direction needs three finite components")
    return vector


def extrude_vector(feature: dict[str, Any], frame: Frame) -> Vec:
    """The vector the profile travels along (one side, for a symmetric
    extrude). Raises ValueError when the extrude's direction is unusable."""
    if feature.get("direction") is None:
        distance = float(feature["distance"])
        if distance == 0:
            raise ValueError("the extrude has no distance")
        return scale(frame.normal, distance)
    vector = _vec(feature["direction"])
    length = norm(vector)
    if length < COLLAPSE_TOL:
        raise ValueError("the extrude direction has no length")
    along = dot(vector, frame.normal)
    if abs(along) < 1e-6 * length:
        raise ValueError("the extrude direction lies in the sketch plane")
    distance = feature.get("distance")
    if distance is not None:
        # `direction` is the full vector. A distance that is neither its length
        # nor its projection on the normal means the tree says something else.
        tol = max(1e-6, 1e-9 * length)
        if min(abs(abs(float(distance)) - length), abs(abs(float(distance)) - abs(along))) > tol:
            raise ValueError(f"the extrude distance {float(distance):g} mm is neither the length "
                             f"of its direction ({length:g} mm) nor its height ({abs(along):g} mm)")
    return vector


def oblique_expectation(frame: Frame, area: float, centroid: tuple[float, float], vector: Vec,
                        symmetric: bool) -> tuple[float, Vec]:
    """Volume and centre of gravity of a profile moved along `vector` (both
    ways when symmetric): a sheared prism, area times the height."""
    height = abs(dot(vector, frame.normal))
    start = frame.world(*centroid)
    if symmetric:
        return area * 2 * height, start
    return area * height, add(start, scale(vector, 0.5))


# ── 2D offsets ──


def _unit2(p: Pt) -> Pt:
    length = math.hypot(*p)
    return (p[0] / length, p[1] / length)


def _signed_area(loop: list[Segment]) -> float:
    return _section_integrals(loop, loop, 0.0)[0]


def offset_loop(loop: list[Segment], delta: float) -> list[Segment]:
    """The loop moved `delta` outward (inward when negative), segment by
    segment, keeping the vertex count, the order and the pairing: segment i of
    the result is the offset of segment i. Raises ValueError when the offset
    collapses a segment or needs a corner this module does not reproduce."""
    orient = 1.0 if _signed_area(loop) > 0 else -1.0

    def outward(direction: Pt) -> Pt:
        tx, ty = _unit2(direction)
        return (orient * ty, -orient * tx)

    n = len(loop)
    corners: list[Pt] = []
    for i, seg in enumerate(loop):
        before = loop[i - 1]
        t_in, t_out = _unit2(before.tangent(1.0)), _unit2(seg.tangent(0.0))
        sine = t_in[0] * t_out[1] - t_in[1] * t_out[0]
        cosine = t_in[0] * t_out[0] + t_in[1] * t_out[1]
        if abs(sine) <= TANGENT_TOL and cosine > 0:
            nx, ny = outward(t_out)
            corners.append((seg.a[0] + delta * nx, seg.a[1] + delta * ny))
            continue
        if before.center is not None or seg.center is not None:
            raise ValueError("a corner of the tapered profile joins an arc without tangency")
        if cosine < -1 + TANGENT_TOL:
            raise ValueError("the tapered profile turns back on itself")
        (ax, ay), (bx, by) = outward(t_in), outward(t_out)
        k = delta / (1 + ax * bx + ay * by)
        corners.append((seg.a[0] + k * (ax + bx), seg.a[1] + k * (ay + by)))
    result = []
    for i, seg in enumerate(loop):
        a, b = corners[i], corners[(i + 1) % n]
        if seg.center is None:
            new = Segment(a, b)
            old = (seg.b[0] - seg.a[0], seg.b[1] - seg.a[1])
            if new.length < COLLAPSE_TOL or (b[0] - a[0]) * old[0] + (b[1] - a[1]) * old[1] <= 0:
                raise ValueError("the taper collapses an edge of the profile (its offset "
                                 "changes the profile's topology)")
        else:
            turn = 1.0 if seg.a1 > seg.a0 else -1.0
            radius = seg.radius + delta * orient * turn
            if radius < COLLAPSE_TOL:
                raise ValueError("the taper collapses an arc of the profile (its offset "
                                 "changes the profile's topology)")
            if n == 1:  # a full circle starts and ends on its start angle
                a = b = (seg.center[0] + radius * math.cos(seg.a0),
                         seg.center[1] + radius * math.sin(seg.a0))
            new = Segment(a, b, seg.center, radius, seg.a0, seg.a1)
        result.append(new)
    return result


def _polyline(loop: list[Segment]) -> list[Pt]:
    points: list[Pt] = []
    for seg in loop:
        steps = 1 if seg.center is None else max(
            4, math.ceil(CHECK_STEPS * abs(seg.a1 - seg.a0) / (2 * math.pi)))
        points.extend(seg.at(k / steps) for k in range(steps))
    return points


def _crosses(p: Pt, q: Pt, r: Pt, s: Pt) -> bool:
    """Whether segments pq and rs touch or cross (shared end points excluded
    by the caller)."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2, d3, d4 = side(r, s, p), side(r, s, q), side(p, q, r), side(p, q, s)
    eps = 1e-12
    return d1 * d2 <= eps and d3 * d4 <= eps and not (
        abs(d1) <= eps and abs(d2) <= eps and abs(d3) <= eps and abs(d4) <= eps
        and (max(p[0], q[0]) < min(r[0], s[0]) - eps or max(r[0], s[0]) < min(p[0], q[0]) - eps
             or max(p[1], q[1]) < min(r[1], s[1]) - eps or max(r[1], s[1]) < min(p[1], q[1]) - eps))


def _inside(point: Pt, polygon: list[Pt]) -> bool:
    x, y = point
    inside = False
    for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _check_topology(loops: list[list[Segment]], original: list[list[Segment]]) -> None:
    """Refuse offset loops that cross themselves or each other, or that no
    longer nest as the original loops did."""
    polygons = [_polyline(loop) for loop in loops]
    edges = [(k, i, poly[i], poly[(i + 1) % len(poly)])
             for k, poly in enumerate(polygons) for i in range(len(poly))]
    for x, (k1, i1, p, q) in enumerate(edges):
        for k2, i2, r, s in edges[x + 1:]:
            if k1 == k2:
                count = len(polygons[k1])
                if abs(i1 - i2) in (0, 1, count - 1):
                    continue
            if _crosses(p, q, r, s):
                raise ValueError("the taper makes the profile cross itself (its offset changes "
                                 "the profile's topology)")
    before = [_polyline(loop) for loop in original]
    for a in range(len(loops)):
        for b in range(len(loops)):
            if a != b and _inside(before[a][0], before[b]) != _inside(polygons[a][0], polygons[b]):
                raise ValueError("the taper changes how the profile's loops nest")


# ── Tapered extrude plans ──


@dataclass(frozen=True)
class TaperPiece:
    """One outer loop's loft and its inner loops' lofts (cut from it), with
    the planes their sections are drawn on."""

    outer: LoftPlan
    inners: list[LoftPlan]
    bottom: dict[str, Any]
    top: dict[str, Any]


@dataclass(frozen=True)
class TaperPlan:
    pieces: list[TaperPiece]
    volume: float
    cog: Vec


def _loop_sketch(sketch: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    return {"plane": sketch["plane"], "entities": sketch["entities"],
            "profiles": [{"outer": list(ids), "inner": []}]}


def _shifted_plane(plane: dict[str, Any], vector: Vec) -> dict[str, Any]:
    origin = add(tuple(float(c) for c in plane["origin"]), vector)
    return {"origin": list(origin), "x_dir": list(plane["x_dir"]), "normal": list(plane["normal"])}


def taper_plan(sketch: dict[str, Any], feature: dict[str, Any]) -> TaperPlan:
    """The lofts of a tapered extrude, with its volume and centre of gravity.
    Raises ValueError with the reason when it is not supported."""
    taper = float(feature["taper"])
    if not -MAX_TAPER < taper < MAX_TAPER:
        raise ValueError(f"the taper must be within ±{MAX_TAPER:g} degrees")
    frame = Frame.from_tree(sketch["plane"])
    vector = extrude_vector(feature, frame)
    delta = -norm(vector) * math.tan(math.radians(taper))
    by_profile = []
    originals, offsets = [], []
    for profile in sketch["profiles"]:
        loops = [section_loop(_loop_sketch(sketch, profile["outer"]), frame)]
        loops += [section_loop(_loop_sketch(sketch, inner), frame)
                  for inner in profile.get("inner", [])]
        moved = [offset_loop(loop, delta if k == 0 else -delta) for k, loop in enumerate(loops)]
        by_profile.append((loops, moved))
        originals += loops
        offsets += moved
    _check_topology(offsets, originals)
    # Halfway, too: a loop moving monotonically can only cross another once.
    _check_topology([offset_loop(loop, (delta if k == 0 else -delta) / 2)
                     for loops, _ in by_profile for k, loop in enumerate(loops)], originals)
    sides = (vector, scale(vector, -1)) if feature.get("symmetric") else (vector,)
    pieces: list[TaperPiece] = []
    volume = mx = my = mz = 0.0
    for side in sides:
        shift = (dot(side, frame.x_dir), dot(side, frame.y_dir))
        height = dot(side, frame.normal)
        top = _shifted_plane(sketch["plane"], side)
        for loops, moved in by_profile:
            plans = []
            for loop, offset in zip(loops, moved, strict=True):
                second = [Segment((s.a[0] + shift[0], s.a[1] + shift[1]),
                                  (s.b[0] + shift[0], s.b[1] + shift[1]),
                                  None if s.center is None
                                  else (s.center[0] + shift[0], s.center[1] + shift[1]),
                                  s.radius, s.a0, s.a1) for s in offset]
                plans.append(LoftPlan(frame, height, loop, second,
                                      loft_expectation(frame, height, loop, second)))
            pieces.append(TaperPiece(plans[0], plans[1:], sketch["plane"], top))
            for k, plan in enumerate(plans):
                sign = 1.0 if k == 0 else -1.0
                v, c = plan.expectation.volume, plan.expectation.cog
                volume += sign * v
                mx, my, mz = mx + sign * v * c[0], my + sign * v * c[1], mz + sign * v * c[2]
    if volume <= 0:
        raise ValueError("the tapered extrude encloses no volume")
    return TaperPlan(pieces, volume, (mx / volume, my / volume, mz / volume))
