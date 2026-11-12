"""Match the tree's edge and face anchors to the cells of a solid.

Fillets, chamfers and shells name their edges and faces by geometry, not by
index: an EdgeAnchor gives the edge's length, end points and a few interior
sample points, a FaceAnchor its area, centroid and normal. The anchors are
those of the body when the feature applies. This module picks, for each
anchor, the one candidate cell of a body that fits it, with the contract's
rules:

- edge: the length agrees within max(1e-4 mm, 1e-6 L), and every sample point
  (or the anchor's `point` when it has no samples) lies within
  max(1e-4 mm, 1e-5 L) of the candidate edge; the closest fit wins;
- face: the area agrees within max(1e-3 mm2, 1e-5 A), then the nearest
  centroid wins.

Two candidates fitting equally well (within 1e-9) are an error, and so is an
anchor nothing fits. Candidates are plain geometry, so the same rules serve
CATIA (cells measured through Automation) and the build123d reference.
Nothing here touches CATIA.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from catia_mcp.tree.geometry import Vec, add, cross, distance, dot, norm, scale, unit

TIE = 1e-9
# Edge geometries whose candidates CATIA can describe exactly from three
# measured points (lines and circular arcs, closed circles included).
EDGE_GEOMETRIES = ("line", "circle")
# Not in the contract, which only says "nearest centroid": the nearest face
# must also be this close (mm, or relative to sqrt(area)) to the anchor's
# centroid, so a face missing from the candidates is not silently replaced by
# another face of the same area (the bottom of a box for its top).
FACE_CENTROID_ABS = 1e-2
FACE_CENTROID_REL = 1e-3


class AnchorError(Exception):
    """An anchor fits no candidate, or more than one equally well."""


def edge_length_tol(length: float) -> float:
    return max(1e-4, 1e-6 * length)


def edge_distance_tol(length: float) -> float:
    return max(1e-4, 1e-5 * length)


def face_area_tol(area: float) -> float:
    return max(1e-3, 1e-5 * area)


def face_centroid_tol(area: float) -> float:
    return max(FACE_CENTROID_ABS, FACE_CENTROID_REL * math.sqrt(max(area, 0.0)))


# ── Candidates ──


@dataclass
class EdgeCandidate:
    """An edge of a solid: its length, the distance from a point to it, and
    (when known) its two ends with the unit tangent there, pointing along the
    edge, for tangent-chain grouping."""

    handle: Any
    length: float
    distance: Callable[[Vec], float]
    ends: tuple[tuple[Vec, Vec], tuple[Vec, Vec]] | None = None
    label: str = ""
    radius: float | None = None


@dataclass
class FaceCandidate:
    handle: Any
    area: float
    centroid: Vec
    label: str = ""


def segment_distance(start: Vec, end: Vec) -> Callable[[Vec], float]:
    axis = add(end, scale(start, -1))
    length2 = dot(axis, axis)

    def measure(p: Vec) -> float:
        if length2 == 0:
            return distance(p, start)
        t = min(1.0, max(0.0, dot(add(p, scale(start, -1)), axis) / length2))
        return distance(p, add(start, scale(axis, t)))

    return measure


def arc_distance(center: Vec, normal: Vec, radius: float, start: Vec, sweep: float
                 ) -> Callable[[Vec], float]:
    """Distance to the arc of `radius` about `center` in the plane of `normal`,
    from `start` turning `sweep` radians counter-clockwise about `normal`."""
    n = unit(normal)
    x = unit(add(start, scale(center, -1)))
    y = cross(n, x)
    end = add(center, scale(add(scale(x, math.cos(sweep)), scale(y, math.sin(sweep))), radius))

    def measure(p: Vec) -> float:
        rel = add(p, scale(center, -1))
        height = dot(rel, n)
        u, v = dot(rel, x), dot(rel, y)
        angle = math.atan2(v, u) % (2 * math.pi)
        if math.hypot(u, v) > 0 and (angle <= sweep + 1e-12 or sweep >= 2 * math.pi - 1e-12):
            return math.hypot(height, math.hypot(u, v) - radius)
        return min(distance(p, start), distance(p, end))

    return measure


def edge_from_points(handle: Any, length: float, start: Vec, mid: Vec, end: Vec,
                     center: Vec | None = None, radius: float | None = None,
                     normal: Vec | None = None, label: str = "") -> EdgeCandidate:
    """A candidate edge from its measured length and its start, middle and end
    points (CATIA's GetPointsOnCurve): a line when the middle point is on the
    chord, else the circular arc through the three points. A closed edge
    (start = end) is a full circle: its centre and radius come from the
    measure, or from the start and the middle point, which face each other;
    without its normal the distance to it is the distance to the sphere of
    that radius, a lower bound that the length check backs up."""
    chord = distance(start, end)
    if chord <= 1e-9 * max(length, 1.0):
        if center is None:
            center = scale(add(start, mid), 0.5)
        if radius is None:
            radius = distance(start, center)
        if normal is None:
            c, r = center, radius
            return EdgeCandidate(handle, length, lambda p: abs(distance(p, c) - r),
                                 label=label, radius=radius)
        return _arc_candidate(handle, length, center, unit(normal), radius, start,
                              2 * math.pi, label)
    off_chord = segment_distance(start, end)(mid)
    if off_chord <= 1e-7 * max(chord, 1.0) and abs(length - chord) <= edge_length_tol(chord):
        direction = unit(add(end, scale(start, -1)))
        return EdgeCandidate(handle, length, segment_distance(start, end),
                             ((start, direction), (end, direction)), label)
    center, normal, radius = _circle_through(start, mid, end)
    x = unit(add(start, scale(center, -1)))
    y = cross(normal, x)
    rel = add(end, scale(center, -1))
    sweep = math.atan2(dot(rel, y), dot(rel, x)) % (2 * math.pi)
    return _arc_candidate(handle, length, center, normal, radius, start, sweep, label)


def _arc_candidate(handle, length, center, normal, radius, start, sweep, label) -> EdgeCandidate:
    x = unit(add(start, scale(center, -1)))
    y = cross(normal, x)
    end = add(center, scale(add(scale(x, math.cos(sweep)), scale(y, math.sin(sweep))), radius))
    tangent_end = add(scale(x, -math.sin(sweep)), scale(y, math.cos(sweep)))
    return EdgeCandidate(handle, length, arc_distance(center, normal, radius, start, sweep),
                         ((start, y), (end, tangent_end)), label, radius)


def _circle_through(a: Vec, b: Vec, c: Vec) -> tuple[Vec, Vec, float]:
    """Centre, unit normal (a -> b -> c turns counter-clockwise about it) and
    radius of the circle through three points."""
    ab, ac = add(b, scale(a, -1)), add(c, scale(a, -1))
    n = cross(ab, ac)
    n2 = dot(n, n)
    if n2 == 0:
        raise ValueError("the three points are collinear")
    center = add(a, scale(add(scale(cross(n, ab), dot(ac, ac)), scale(cross(ac, n), dot(ab, ab))),
                          1 / (2 * n2)))
    return center, unit(n), distance(center, a)


# ── Matching ──


def _points(anchor: dict[str, Any]) -> list[Vec]:
    samples = anchor.get("samples") or []
    if samples:
        return [tuple(float(c) for c in p) for p in samples]
    return [tuple(float(c) for c in anchor["point"])]


def describe_edge(anchor: dict[str, Any]) -> str:
    point = anchor.get("point") or (anchor.get("samples") or [[0, 0, 0]])[0]
    return (f"{anchor.get('geometry', 'edge')} edge of length {float(anchor['length']):.4f} mm "
            f"through ({', '.join(f'{float(c):.3f}' for c in point)})")


def describe_face(anchor: dict[str, Any]) -> str:
    return (f"{anchor.get('geometry', '')} face of area {float(anchor['area']):.4f} mm2 centred at "
            f"({', '.join(f'{float(c):.3f}' for c in anchor['point'])})").replace("  ", " ")


def match_edge(anchor: dict[str, Any], candidates: Sequence[EdgeCandidate]) -> EdgeCandidate:
    length = float(anchor["length"])
    points = _points(anchor)
    tol_len, tol_dist = edge_length_tol(length), edge_distance_tol(length)
    same_length = [c for c in candidates if abs(c.length - length) <= tol_len]
    fits = []
    for candidate in same_length:
        score = max(candidate.distance(p) for p in points)
        if score <= tol_dist:
            fits.append((score, candidate))
    if not fits:
        raise AnchorError(
            f"no edge of the body fits the {describe_edge(anchor)} "
            f"({len(candidates)} edge(s) searched, {len(same_length)} of that length)")
    fits.sort(key=lambda fit: fit[0])
    if len(fits) > 1 and fits[1][0] - fits[0][0] < TIE:
        raise AnchorError(f"the {describe_edge(anchor)} is ambiguous: {len(fits)} edges fit it "
                          "equally well")
    return fits[0][1]


def match_face(anchor: dict[str, Any], candidates: Sequence[FaceCandidate]) -> FaceCandidate:
    area = float(anchor["area"])
    point = tuple(float(c) for c in anchor["point"])
    same_area = sorted(((distance(c.centroid, point), c) for c in candidates
                        if abs(c.area - area) <= face_area_tol(area)), key=lambda fit: fit[0])
    if not same_area:
        raise AnchorError(f"no face of the body fits the {describe_face(anchor)} "
                          f"({len(candidates)} face(s) searched, none of that area)")
    if len(same_area) > 1 and same_area[1][0] - same_area[0][0] < TIE:
        raise AnchorError(f"the {describe_face(anchor)} is ambiguous: {len(same_area)} faces of "
                          "that area are equally close")
    gap, best = same_area[0]
    if gap > face_centroid_tol(area):
        raise AnchorError(f"no face of the body fits the {describe_face(anchor)}: the nearest "
                          f"face of that area is centred {gap:.4f} mm away")
    return best


def match_all(anchors: Sequence[dict[str, Any]], candidates: Sequence[Any],
              match: Callable[[dict[str, Any], Sequence[Any]], Any]) -> list[Any]:
    """One distinct candidate per anchor, in anchor order."""
    chosen: list[Any] = []
    errors: list[str] = []
    for index, anchor in enumerate(anchors, start=1):
        try:
            candidate = match(anchor, candidates)
        except AnchorError as error:
            errors.append(f"anchor {index}: {error}")
            continue
        if any(candidate is other for other in chosen):
            errors.append(f"anchor {index} fits the same cell as an earlier anchor")
            continue
        chosen.append(candidate)
    if errors:
        raise AnchorError("; ".join(errors))
    return chosen


def match_edges(anchors, candidates: Sequence[EdgeCandidate]) -> list[EdgeCandidate]:
    return match_all(anchors, candidates, match_edge)


def match_faces(anchors, candidates: Sequence[FaceCandidate]) -> list[FaceCandidate]:
    return match_all(anchors, candidates, match_face)


# ── Tangent chains ──


@dataclass
class Chain:
    """Edges joined end to end with the same tangent: what CATIA's (and
    OpenCascade's) tangency propagation fillets from any one of them."""

    edges: list[EdgeCandidate] = field(default_factory=list)

    @property
    def length(self) -> float:
        return sum(edge.length for edge in self.edges)


def tangent_chains(edges: Sequence[EdgeCandidate], everything: Sequence[EdgeCandidate],
                   point_tol: float = 1e-6, angle_tol: float = 1e-6) -> list[Chain]:
    """Group `edges` by the tangent chain (through any edge of `everything`)
    they belong to, in the order of their first edge. An edge without known
    ends is a chain of its own."""
    pool = list(everything) + [e for e in edges if not any(e is o for o in everything)]
    parent = list(range(len(pool)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(pool):
        for j in range(i + 1, len(pool)):
            b = pool[j]
            if a.ends is None or b.ends is None:
                continue
            if any(distance(pa, pb) <= point_tol and norm(cross(ta, tb)) <= angle_tol
                   for pa, ta in a.ends for pb, tb in b.ends):
                parent[root(i)] = root(j)

    def index(edge: EdgeCandidate) -> int:
        return next(k for k, other in enumerate(pool) if other is edge)

    chains: dict[int, Chain] = {}
    for edge in edges:
        chains.setdefault(root(index(edge)), Chain())
    for k, edge in enumerate(pool):
        if root(k) in chains:
            chains[root(k)].edges.append(edge)
    order = [root(index(edge)) for edge in edges]
    return [chains[key] for key in dict.fromkeys(order)]


def representatives(edges: Sequence[EdgeCandidate], chains: Sequence[Chain]) -> list[EdgeCandidate]:
    """The first anchored edge of each chain: with tangency propagation, one
    edge selects its whole chain, and selecting two edges of one chain is
    redundant."""
    picked = []
    for chain in chains:
        picked.append(next(e for e in edges if any(e is c for c in chain.edges)))
    return picked


# ── Anchor checks (preflight) ──


def edge_anchor_problem(anchor: Any) -> str | None:
    if not isinstance(anchor, dict) or anchor.get("type", "edge") != "edge":
        return "an edge anchor is malformed"
    try:
        length = float(anchor["length"])
        _points(anchor)
        for key in ("samples",):
            for point in anchor.get(key) or []:
                if len(point) != 3:
                    raise ValueError
    except (KeyError, TypeError, ValueError):
        return "an edge anchor has no length or no point on the edge"
    if not length > 0:
        return "an edge anchor has no length"
    if anchor.get("geometry") not in EDGE_GEOMETRIES:
        return f"fillets and chamfers on {anchor.get('geometry')!s} edges are not supported yet"
    return None


def face_anchor_problem(anchor: Any) -> str | None:
    if not isinstance(anchor, dict) or anchor.get("type", "face") != "face":
        return "a face anchor is malformed"
    try:
        area = float(anchor["area"])
        point = tuple(float(c) for c in anchor["point"])
    except (KeyError, TypeError, ValueError):
        return "a face anchor has no area or no centroid"
    if len(point) != 3 or not area > 0:
        return "a face anchor has no area or no centroid"
    return None


def plane_of(anchor: dict[str, Any]) -> tuple[Vec, Vec] | None:
    """(point, unit normal) of a planar face anchor, None otherwise."""
    if anchor.get("geometry") != "plane" or not anchor.get("normal"):
        return None
    try:
        return (tuple(float(c) for c in anchor["point"]),
                unit(tuple(float(c) for c in anchor["normal"])))
    except (TypeError, ValueError):
        return None
