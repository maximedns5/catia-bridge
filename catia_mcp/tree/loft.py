"""Section pairing and expected solids for lofts, without a CAD kernel.

A loft's shape depends on which point of one section joins which point of the
next. build123d lofts through OpenCascade, whose BRepFill_CompatibleWires
pairs closed sections in one of two ways (OCCT 8.0.1):

- every section has the same number of edges and one of them has a corner:
  the second section starts at the vertex, and runs in the direction, with
  the smallest sum of distances to the first section's vertices, once both
  sections are moved onto the same barycentre (ComputeOrigin);
- otherwise (different edge counts, or every section smooth, like a circle):
  each section's vertices are carried onto the other along rays from its
  barycentre, splitting an edge where the ray crosses it unless an existing
  vertex is within 10 % of that edge, and the edges are then paired in order
  (SameNumberByPolarMethod).

This module reproduces both for two closed sections in parallel planes, so
CATIA can be given the same vertices and closing points, and computes the
volume and centre of gravity of the straight-ruled solid between them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from catia_mcp.tree.geometry import Arc, Frame, Vec, add, dot, scale

Pt = tuple[float, float]

# OpenCascade's BRepFill_CompatibleWires default for BRepOffsetAPI_ThruSections.
SNAP_PERCENT = 0.1
# Two points closer than this (mm) are the same vertex.
VERTEX_TOL = 1e-7
# Simpson intervals per edge for the section integrals.
EDGE_STEPS = 64


@dataclass(frozen=True)
class Segment:
    """A line (radius 0) or a circular arc in 2D, run from t = 0 to t = 1.

    Lines go from `a` to `b`. Arcs turn from angle `a0` to `a1` (signed sweep)
    about `center`; `a` and `b` are their end points."""

    a: Pt
    b: Pt
    center: Pt | None = None
    radius: float = 0.0
    a0: float = 0.0
    a1: float = 0.0

    def at(self, t: float) -> Pt:
        if self.center is None:
            return (self.a[0] + (self.b[0] - self.a[0]) * t, self.a[1] + (self.b[1] - self.a[1]) * t)
        angle = self.a0 + (self.a1 - self.a0) * t
        return (self.center[0] + self.radius * math.cos(angle),
                self.center[1] + self.radius * math.sin(angle))

    def tangent(self, t: float) -> Pt:
        """d(point)/dt."""
        if self.center is None:
            return (self.b[0] - self.a[0], self.b[1] - self.a[1])
        angle = self.a0 + (self.a1 - self.a0) * t
        k = self.radius * (self.a1 - self.a0)
        return (-math.sin(angle) * k, math.cos(angle) * k)

    def natural_tangent(self, t: float) -> Pt:
        """The derivative along OpenCascade's own parameter: the length for a
        line, the angle for an arc."""
        dx, dy = self.tangent(t)
        span = self.length if self.center is None else abs(self.a1 - self.a0)
        return (dx / span, dy / span)

    @property
    def length(self) -> float:
        if self.center is None:
            return math.dist(self.a, self.b)
        return abs(self.a1 - self.a0) * self.radius

    def reversed(self) -> Segment:
        if self.center is None:
            return Segment(self.b, self.a)
        return Segment(self.b, self.a, self.center, self.radius, self.a1, self.a0)

    def split(self, t: float) -> tuple[Segment, Segment]:
        p = self.at(t)
        if self.center is None:
            return Segment(self.a, p), Segment(p, self.b)
        mid = self.a0 + (self.a1 - self.a0) * t
        return (Segment(self.a, p, self.center, self.radius, self.a0, mid),
                Segment(p, self.b, self.center, self.radius, mid, self.a1))

    def entity(self, to_uv) -> dict[str, Any]:
        """The segment as a tree sketch entity, in another sketch's (u, v)."""
        if self.center is not None and abs(abs(self.a1 - self.a0) - 2 * math.pi) < 1e-12:
            rim = to_uv((self.center[0] + self.radius, self.center[1]))
            center = to_uv(self.center)
            return {"type": "circle", "center": list(center), "radius": math.dist(rim, center)}
        if self.center is None:
            return {"type": "line", "start": list(to_uv(self.a)), "end": list(to_uv(self.b))}
        return {"type": "arc", "start": list(to_uv(self.a)), "mid": list(to_uv(self.at(0.5))),
                "end": list(to_uv(self.b))}


def _entity_segment(entity: dict[str, Any], to_common) -> Segment:
    kind = entity["type"]
    if kind == "line":
        return Segment(to_common(entity["start"]), to_common(entity["end"]))
    if kind == "arc":
        start, mid, end = (to_common(entity[k]) for k in ("start", "mid", "end"))
        arc = Arc.through(start, mid, end)
        a0 = math.atan2(start[1] - arc.center[1], start[0] - arc.center[0])
        sweep = arc.end - arc.start
        # Arc.through always turns counter-clockwise: keep the tree's direction.
        cross = (mid[0] - start[0]) * (end[1] - start[1]) - (mid[1] - start[1]) * (end[0] - start[0])
        sweep = sweep if cross > 0 else -sweep
        return Segment(start, end, arc.center, arc.radius, a0, a0 + sweep)
    raise ValueError(f"no loft section entity '{kind}'")


def section_loop(sketch: dict[str, Any], common: Frame) -> list[Segment]:
    """The sketch's single profile loop as segments in `common`'s (u, v), in
    the order and direction the tree gives (a circle starts on its sketch's
    x_dir, as build123d's circle edge does)."""
    frame = Frame.from_tree(sketch["plane"])

    def to_common(uv) -> Pt:
        world = frame.world(float(uv[0]), float(uv[1]))
        offset = add(world, scale(common.origin, -1))
        return (dot(offset, common.x_dir), dot(offset, common.y_dir))

    by_id = {e["id"]: e for e in sketch["entities"]}
    (profile,) = sketch["profiles"]
    entities = [by_id[i] for i in profile["outer"]]
    if len(entities) == 1 and entities[0]["type"] == "circle":
        center, r = entities[0]["center"], float(entities[0]["radius"])
        c = to_common(center)
        start = to_common((float(center[0]) + r, float(center[1])))
        a0 = math.atan2(start[1] - c[1], start[0] - c[0])
        sense = 1.0 if dot(frame.normal, common.normal) > 0 else -1.0
        return [Segment(start, start, c, r, a0, a0 + sense * 2 * math.pi)]
    pieces = [_entity_segment(e, to_common) for e in entities]
    loop = [pieces.pop(0)]
    while pieces:
        tail = loop[-1].b
        for i, piece in enumerate(pieces):
            if math.dist(piece.a, tail) <= 1e-6:
                loop.append(piece)
                break
            if math.dist(piece.b, tail) <= 1e-6:
                loop.append(piece.reversed())
                break
        else:
            raise ValueError("the section's entities do not join end to end")
        pieces.pop(i)
    if math.dist(loop[0].a, loop[-1].b) > 1e-6:
        raise ValueError("the section is not closed")
    return loop


def barycenter(loop: list[Segment]) -> Pt:
    """PlaneOfWire's location: a lone circle's centre, else the centre of mass
    of the wire as a curve (length-weighted)."""
    if len(loop) == 1 and loop[0].center is not None:
        return loop[0].center
    total = sx = sy = 0.0
    for seg in loop:
        if seg.center is None:
            mx, my = (seg.a[0] + seg.b[0]) / 2, (seg.a[1] + seg.b[1]) / 2
        else:
            half = (seg.a1 - seg.a0) / 2
            mid = seg.a0 + half
            k = seg.radius * (math.sin(half) / half if abs(half) > 1e-12 else 1.0)
            mx, my = seg.center[0] + k * math.cos(mid), seg.center[1] + k * math.sin(mid)
        sx += mx * seg.length
        sy += my * seg.length
        total += seg.length
    return (sx / total, sy / total)


def _smooth(loop: list[Segment]) -> bool:
    """C1 at every vertex in OpenCascade's sense: equal first derivatives,
    which a line meeting a tangent arc is not (only G1)."""
    for here, there in zip(loop, loop[1:] + loop[:1], strict=True):
        d0, d1 = here.natural_tangent(1.0), there.natural_tangent(0.0)
        if math.dist(d0, d1) > 1e-9 * max(1.0, math.hypot(*d0)):
            return False
    return True


def _ray_hits(origin: Pt, through: Pt, loop: list[Segment]) -> list[tuple[int, float, Pt]]:
    """Where the ray from `origin` through `through` crosses the loop:
    (segment index, t on the segment, point)."""
    dx, dy = through[0] - origin[0], through[1] - origin[1]
    norm = math.hypot(dx, dy)
    dx, dy = dx / norm, dy / norm
    hits = []
    for index, seg in enumerate(loop):
        if seg.center is None:
            ex, ey = seg.b[0] - seg.a[0], seg.b[1] - seg.a[1]
            det = dx * (-ey) - dy * (-ex)
            if abs(det) < 1e-14:
                continue
            rx, ry = seg.a[0] - origin[0], seg.a[1] - origin[1]
            s = (rx * (-ey) - ry * (-ex)) / det
            t = (dx * ry - dy * rx) / det
            if s >= -1e-12 and -1e-12 <= t <= 1 + 1e-12:
                hits.append((index, min(max(t, 0.0), 1.0), seg.at(min(max(t, 0.0), 1.0))))
            continue
        fx, fy = origin[0] - seg.center[0], origin[1] - seg.center[1]
        b = fx * dx + fy * dy
        disc = b * b - (fx * fx + fy * fy - seg.radius ** 2)
        if disc < 0:
            continue
        for s in (-b - math.sqrt(disc), -b + math.sqrt(disc)):
            if s < -1e-12:
                continue
            px, py = origin[0] + s * dx, origin[1] + s * dy
            angle = math.atan2(py - seg.center[1], px - seg.center[0])
            sweep = seg.a1 - seg.a0
            rel = (angle - seg.a0) if sweep > 0 else (seg.a0 - angle)
            rel %= 2 * math.pi
            if abs(rel - 2 * math.pi) < 1e-12:
                rel = 0.0
            t = rel / abs(sweep)
            if abs(abs(sweep) - 2 * math.pi) < 1e-12 and t > 1 - 1e-12:
                t = 0.0
            if t <= 1 + 1e-12:
                hits.append((index, min(t, 1.0), seg.at(min(t, 1.0))))
    return hits


class AmbiguousPairing(ValueError):
    """build123d's pairing of these sections depends on the order OpenCascade
    visits their vertices in, which the tree does not give."""


def _hit(origin: Pt, through: Pt, loop: list[Segment]) -> tuple[int, float]:
    """Where the ray from `origin` through `through` meets the loop, as
    (segment, t): the crossing closest to `through`, as BRepExtrema gives it."""
    hits = _ray_hits(origin, through, loop)
    if not hits:
        raise ValueError("a section vertex has no counterpart on the other section "
                         "(the section is not star-shaped about its barycentre)")
    index, t, _ = min(hits, key=lambda h: math.dist(h[2], through))
    return index, t


def _vertex_at(loop: list[Segment], index: int, t: float) -> int | None:
    """The loop vertex a hit lands exactly on, if any."""
    if t <= 1e-9:
        return index
    if t >= 1 - 1e-9:
        return (index + 1) % len(loop)
    return None


def _resolve_snaps(loop: list[Segment], hits: list[tuple[int, float]]) -> list[int | None]:
    """For each hit on `loop`'s original edges, the original vertex it ends on
    (exact or snapped), or None for a new vertex, in a way that holds for every
    order OpenCascade may visit the hits in. Snapping needs the hit within 10 %
    of the edge piece it lands on from a vertex no earlier hit took; earlier
    hits on the same edge shorten that piece. Raises AmbiguousPairing when the
    outcome depends on the order."""
    n = len(loop)
    landed: list[int | None] = []
    candidates: dict[int, list[int]] = {}
    for k, (index, t) in enumerate(hits):
        exact = _vertex_at(loop, index, t)
        landed.append(exact)
        if exact is not None:
            candidates.setdefault(exact, []).append(k)
            continue
        others = [u for i, u in hits if i == index and u != t]
        if t < SNAP_PERCENT:
            below, above = [u for u in others if u < t], [u for u in others if u > t]
            certain = not below and t < SNAP_PERCENT * min(above + [1.0])
            target = index
        elif t > 1 - SNAP_PERCENT:
            below, above = [u for u in others if u < t], [u for u in others if u > t]
            certain = not above and (1 - t) < SNAP_PERCENT * (1 - max(below + [0.0]))
            target = (index + 1) % n
        else:
            continue
        if not certain:
            raise AmbiguousPairing("a section vertex lands near a corner of the other "
                                   "section, where build123d's pairing depends on its "
                                   "internal vertex order")
        landed[k] = target
        candidates.setdefault(target, []).append(k)
    if any(len(ks) > 1 for ks in candidates.values()):
        raise AmbiguousPairing("two section vertices land on the same corner of the other "
                               "section")
    if len({(i, round(t, 9)) for i, t in hits}) != len(hits):
        raise AmbiguousPairing("two section vertices line up on the same ray")
    return landed


def _insert(loop: list[Segment], new_points: list[tuple[int, float, object]]):
    """The loop with new vertices inserted, and a key for every vertex: the
    original index, or the key given with the new point."""
    by_edge: dict[int, list[tuple[float, object]]] = {}
    for index, t, key in new_points:
        by_edge.setdefault(index, []).append((t, key))
    segments: list[Segment] = []
    keys: list[object] = []
    for index, seg in enumerate(loop):
        keys.append(("v", index))
        rest, done = seg, 0.0
        for t, key in sorted(by_edge.get(index, []), key=lambda x: x[0]):
            first, rest = rest.split((t - done) / (1 - done))
            segments.append(first)
            keys.append(key)
            done = t
        segments.append(rest)
    return segments, keys


def _polar_pairing(loop1: list[Segment], loop2: list[Segment]) -> tuple[list[Segment], list[Segment]]:
    g1, g2 = barycenter(loop1), barycenter(loop2)

    def carried(p: Pt, src: Pt, dst: Pt) -> Pt:
        return (p[0] - src[0] + dst[0], p[1] - src[1] + dst[1])

    # Backward: each vertex of section 2 onto section 1.
    sources2 = [k for k, seg in enumerate(loop2)
                if math.dist(carried(seg.a, g2, g1), g1) > VERTEX_TOL]
    hits1 = [_hit(g1, carried(loop2[k].a, g2, g1), loop1) for k in sources2]
    landed1 = _resolve_snaps(loop1, hits1)
    match: dict[object, object] = {}          # section 1 vertex key -> section 2 vertex key
    new1 = []
    for k, (index, t), vertex in zip(sources2, hits1, landed1, strict=True):
        key1 = ("v", vertex) if vertex is not None else ("b", k)
        if vertex is None:
            new1.append((index, t, key1))
        match[key1] = ("v", k)
    # Forward: each original vertex of section 1 still unpaired onto section 2.
    # Every section 2 vertex is paired by then, so nothing snaps; landing
    # exactly on one would pair it twice.
    new2 = []
    for k, seg in enumerate(loop1):
        if ("v", k) in match:
            continue
        p = carried(seg.a, g1, g2)
        if math.dist(p, g2) <= VERTEX_TOL:
            raise ValueError("a section vertex sits on its barycentre")
        index, t = _hit(g2, p, loop2)
        if _vertex_at(loop2, index, t) is not None:
            raise AmbiguousPairing("two section vertices land on the same corner of the "
                                   "other section")
        new2.append((index, t, ("f", k)))
        match[("v", k)] = ("f", k)
    if len({(i, round(t, 9)) for i, t, _ in new2}) != len(new2):
        raise AmbiguousPairing("two section vertices line up on the same ray")
    segs1, keys1 = _insert(loop1, new1)
    segs2, keys2 = _insert(loop2, new2)
    if len(segs1) != len(segs2) or set(match) != set(keys1) or set(match.values()) != set(keys2):
        raise ValueError("the sections could not be given matching vertices")
    # Section 2 starts at the counterpart of section 1's first vertex and runs
    # the way section 1 does.
    order2 = [match[key] for key in keys1]
    start = keys2.index(order2[0])
    n = len(keys2)
    if [keys2[(start + k) % n] for k in range(n)] == order2:
        return segs1, [segs2[(start + k) % n] for k in range(n)]
    if [keys2[(start - k) % n] for k in range(n)] != order2:
        raise ValueError("the sections' vertices do not pair in one order")
    return segs1, [segs2[(start - 1 - k) % n].reversed() for k in range(n)]


def _origin_pairing(loop1: list[Segment], loop2: list[Segment],
                    height: float) -> tuple[list[Segment], list[Segment]]:
    g1, g2 = barycenter(loop1), barycenter(loop2)
    off = (g1[0] - g2[0], g1[1] - g2[1])
    n = len(loop1)
    samples = 4 if n <= 2 else 0

    def moved(p: Pt) -> Pt:
        return (p[0] + off[0], p[1] + off[1])

    def gap(p1: Pt, p2: Pt) -> float:
        # A 3D distance: the sections stay `height` apart.
        return math.hypot(math.dist(p1, moved(p2)), height)

    def cost(candidate: list[Segment]) -> float:
        total = 0.0
        for s1, s2 in zip(loop1, candidate, strict=True):
            total += gap(s1.a, s2.a)
            for k in range(1, samples):
                total += gap(s1.at(k / samples), s2.at(k / samples))
        return total

    best: tuple[float, list[Segment]] | None = None
    for j in range(n):
        forward = [loop2[(j + k) % n] for k in range(n)]
        backward = [loop2[(j - 1 - k) % n].reversed() for k in range(n)]
        for candidate in (forward, backward):
            c = cost(candidate)
            if best is None or c < best[0]:
                best = (c, candidate)
    return list(loop1), best[1]


def _pair_once(loop1: list[Segment], loop2: list[Segment],
               height: float) -> tuple[list[Segment], list[Segment]]:
    if len(loop1) == len(loop2) and not (_smooth(loop1) and _smooth(loop2)):
        return _origin_pairing(loop1, loop2, height)
    return _polar_pairing(loop1, loop2)


def _visiting_orders(loop: list[Segment]) -> list[list[Segment]]:
    """The loop from each of its vertices, both ways round."""
    backward = [seg.reversed() for seg in reversed(loop)]
    n = len(loop)
    return [way[k:] + way[:k] for way in (loop, backward) for k in range(n)]


def _same_pairing(a, b) -> bool:
    pairs_a = list(zip((s.a for s in a[0]), (s.a for s in a[1]), strict=True))
    pairs_b = list(zip((s.a for s in b[0]), (s.a for s in b[1]), strict=True))
    return len(pairs_a) == len(pairs_b) and all(
        any(math.dist(p, r) < 1e-6 and math.dist(q, s) < 1e-6 for r, s in pairs_b)
        for p, q in pairs_a)


def pair_sections(loop1: list[Segment], loop2: list[Segment],
                  height: float) -> tuple[list[Segment], list[Segment]]:
    """Both loops cut into the same number of segments, paired in order, as
    build123d's loft pairs them. `height` is how far apart the section planes
    are.

    OpenCascade visits each section's vertices in an order the tree does not
    give. The polar rule is resolved so that it holds for every order, or
    refused; the same-count rule only depends on the order through ties, so it
    is computed from every start and direction of both sections and refused
    unless they all agree. Refusals raise AmbiguousPairing."""
    if len(loop1) * len(loop2) > 400:
        raise ValueError("the loft sections have too many edges to pair reliably")
    result = _pair_once(loop1, loop2, height)
    if len(loop1) == len(loop2) and not (_smooth(loop1) and _smooth(loop2)):
        for order1 in _visiting_orders(loop1):
            for order2 in _visiting_orders(loop2):
                if not _same_pairing(result, _pair_once(order1, order2, height)):
                    raise AmbiguousPairing("the sections pair equally well in two ways "
                                           "(they are symmetric), and build123d's choice "
                                           "depends on its internal vertex order")
    return result


def _section_integrals(p: list[Segment], q: list[Segment], s: float) -> tuple[float, float, float]:
    """Signed area and first moments (int x dA, int y dA) of the section at
    fraction s between paired loops p and q, by Green's theorem with Simpson
    along each pair of segments."""
    area = mx = my = 0.0
    for sp, sq in zip(p, q, strict=True):
        h = 1.0 / EDGE_STEPS
        for i in range(EDGE_STEPS + 1):
            t = i * h
            w = (1 if i in (0, EDGE_STEPS) else 4 if i % 2 else 2) * h / 3
            (px, py), (qx, qy) = sp.at(t), sq.at(t)
            (dpx, dpy), (dqx, dqy) = sp.tangent(t), sq.tangent(t)
            x, y = (1 - s) * px + s * qx, (1 - s) * py + s * qy
            dx, dy = (1 - s) * dpx + s * dqx, (1 - s) * dpy + s * dqy
            area += w * (x * dy - y * dx) / 2
            mx += w * (x * x * dy) / 2
            my += w * (-y * y * dx) / 2
    return area, mx, my


@dataclass(frozen=True)
class LoftExpectation:
    volume: float
    cog: Vec


def loft_expectation(common: Frame, height: float, p: list[Segment],
                     q: list[Segment]) -> LoftExpectation:
    """Volume and centre of gravity of the ruled solid joining paired loops p
    (in `common`'s plane) and q (`height` along its normal). Areas are
    quadratic and moments cubic in s, so Simpson on s = 0, 1/2, 1 is exact."""
    values = [_section_integrals(p, q, s) for s in (0.0, 0.5, 1.0)]
    weights = (1 / 6, 4 / 6, 1 / 6)
    area = sum(w * v[0] for w, v in zip(weights, values, strict=True))
    if abs(area) < 1e-12:
        raise ValueError("the loft encloses no volume")
    mx = sum(w * v[1] for w, v in zip(weights, values, strict=True))
    my = sum(w * v[2] for w, v in zip(weights, values, strict=True))
    mz = sum(w * s * v[0] for w, s, v in zip(weights, (0.0, 0.5, 1.0), values, strict=True))
    cog = common.world(mx / area, my / area, height * mz / area)
    return LoftExpectation(abs(area * height), cog)


@dataclass(frozen=True)
class LoftPlan:
    """Two sections paired as build123d pairs them: `first` in the common
    (first section's) plane, `second` `height` along its normal."""

    common: Frame
    height: float
    first: list[Segment]
    second: list[Segment]
    expectation: LoftExpectation


def plan_loft(sketch1: dict[str, Any], sketch2: dict[str, Any]) -> LoftPlan:
    """Check two loft sections and pair them. Raises ValueError (or
    AmbiguousPairing) with the reason when they are not supported."""
    for sketch in (sketch1, sketch2):
        profiles = sketch.get("profiles") or []
        if len(profiles) != 1:
            raise ValueError("each loft section must be a sketch with exactly one profile")
        if profiles[0].get("inner"):
            raise ValueError("loft sections with holes are not supported yet")
    common, other = Frame.from_tree(sketch1["plane"]), Frame.from_tree(sketch2["plane"])
    if abs(abs(dot(common.normal, other.normal)) - 1) > 1e-9:
        raise ValueError("lofts between sections in planes that are not parallel are not "
                         "supported yet")
    height = dot(add(other.origin, scale(common.origin, -1)), common.normal)
    if abs(height) < 1e-6:
        raise ValueError("the loft sections lie in the same plane")
    first, second = pair_sections(section_loop(sketch1, common), section_loop(sketch2, common),
                                  height)
    return LoftPlan(common, height, first, second,
                    loft_expectation(common, height, first, second))


def section_sketch(sketch: dict[str, Any], segments: list[Segment], common: Frame,
                   height: float) -> dict[str, Any]:
    """The section redrawn on its own plane with the paired vertices: its
    entities split where the pairing put vertices, in pairing order, so the
    first vertex is the closing point."""
    frame = Frame.from_tree(sketch["plane"])

    def to_uv(pt: Pt) -> Pt:
        offset = add(common.world(pt[0], pt[1], height), scale(frame.origin, -1))
        return (dot(offset, frame.x_dir), dot(offset, frame.y_dir))

    entities = [dict(seg.entity(to_uv), id=f"s{i}") for i, seg in enumerate(segments)]
    return {"plane": sketch["plane"], "entities": entities,
            "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}
