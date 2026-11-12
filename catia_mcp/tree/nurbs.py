"""The tree's `spline` sketch entity: an exact rational NURBS, in pure Python.

A `spline` gives `degree`, 2D `poles`, `weights`, `knots` (the full knot
vector, multiplicities expanded) and `periodic`. The curve is read once into
rational Bezier spans (Boehm knot insertion in homogeneous coordinates),
which every other operation works on:

- evaluation (de Casteljau) and first derivatives, length (Gauss-Legendre);
- a polygon for areas, centroids and revolve moments (geometry.py), whose
  chords stay within SAGITTA_REL of the curve;
- conic recognition: a degree-2 rational curve whose spans are all elliptical
  arcs of one ellipse is drawn exactly in CATIA as a circle or an ellipse;
- otherwise, the points a CATIA sketch spline is drawn through, with a bound
  on the area between the curve and those points' chords.

Three knot conventions are accepted, so the reader does not depend on how the
tree was written: the textbook one (len(knots) = len(poles) + degree + 1, any
curve), and for periodic curves OpenCascade's flat knot sequence (fewer poles
than that: they wrap around) or one period of knots with multiplicities
expanded (first and last knot one period apart).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

Point = tuple[float, float]
Homogeneous = tuple[float, float, float]  # (w x, w y, w)

# Knots closer than this, relative to the knot range, are the same knot.
KNOT_TOL = 1e-12
# Area polygon: every chord's midpoint is within SAGITTA_REL x the curve's
# size (its poles' bounding-box diagonal) of the curve, after at least
# MIN_CHORDS_PER_SPAN chords per Bezier span. The polygon's area then differs
# from the curve's by about perimeter x sagitta x 2/3 (parabolic chords):
# under 1e-6 relative for any profile that is not a sliver, two orders below
# the 1e-4 part tolerance (tests check it against OpenCascade).
SAGITTA_REL = 1e-7
MIN_CHORDS_PER_SPAN = 8
MAX_SUBDIVISIONS = 30
# Conic recognition: centres, semi-axes and the implicit equation agree to
# this, relative to the major semi-axis.
CONIC_TOL = 1e-8
# Points a CATIA sketch spline is drawn through (it passes through them; see
# docs/feature-tree.md). Between two points the tangent turns by at most
# SPLINE_MAX_TURN, and each Bezier span gets at least SPLINE_MIN_SAMPLES
# intervals. Even straight chords between such points stay within area
# about turn^2 / 12 x the region they bound (2e-4 relative for a circle-like
# loop at 2 degrees, under the 1e-3 per-feature tolerance); an interpolating
# cubic through them errs as turn^4 (about 1e-8). The chord bound is what the
# build's volume checks allow on top of their usual tolerance.
SPLINE_MAX_TURN = math.radians(2.0)
SPLINE_MIN_SAMPLES = 4
# A span boundary where the tangent turns more than this is a corner: the
# CATIA spline is split there instead of rounding it.
KINK_ANGLE = math.radians(0.5)
# Points closer than this (mm) are the same point.
POINT_TOL = 1e-6

# 5-point Gauss-Legendre on [0, 1].
_GL = tuple(((1 + x) / 2, w / 2) for x, w in (
    (-0.9061798459386640, 0.2369268850561891), (-0.5384693101056831, 0.4786286704993665),
    (0.0, 0.5688888888888889), (0.5384693101056831, 0.4786286704993665),
    (0.9061798459386640, 0.2369268850561891)))


def _lerp(a: Homogeneous, b: Homogeneous, t: float) -> Homogeneous:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _casteljau(ctrl: list[Homogeneous], t: float) -> Homogeneous:
    pts = list(ctrl)
    for k in range(len(pts) - 1, 0, -1):
        pts = [_lerp(pts[i], pts[i + 1], t) for i in range(k)]
    return pts[0]


def _cartesian(h: Homogeneous) -> Point:
    return (h[0] / h[2], h[1] / h[2])


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _turn(a: Point, b: Point) -> float:
    """The unsigned angle between two directions."""
    return abs(math.atan2(_cross(a, b), a[0] * b[0] + a[1] * b[1]))


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    ab = _sub(b, a)
    length2 = ab[0] ** 2 + ab[1] ** 2
    if length2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / length2))
    return math.dist(p, (a[0] + t * ab[0], a[1] + t * ab[1]))


def _flat_knots(degree: int, n_poles: int, knots: list[float], periodic: bool) -> list[float]:
    """The textbook knot vector (len = control points + degree + 1) for the
    three accepted conventions; periodic curves may need wrapped poles."""
    count = len(knots)
    if count >= n_poles + degree + 1:
        if count > n_poles + degree + 1 and not periodic:
            raise ValueError("the spline has more knots than its poles and degree allow")
        return list(knots)
    if not periodic:
        raise ValueError("the spline has too few knots for its poles and degree")
    # One period with multiplicities expanded: extend it on both sides.
    span = knots[-1] - knots[0]
    tol = KNOT_TOL * max(1.0, abs(span))
    period = [k for k in knots if k < knots[-1] - tol]
    if span <= 0 or len(period) != n_poles:
        raise ValueError("the periodic spline's knots do not span one period of its poles")
    first = sum(1 for k in period if k <= period[0] + tol)
    before = degree + 1 - first
    if before < 0 or before > len(period):
        raise ValueError("the periodic spline's first knot is repeated more than its degree")
    head = [k - span for k in period[len(period) - before:]] if before else []
    return head + period + [k + span for k in period[:degree + 1]]


@dataclass(frozen=True)
class Span:
    """One rational Bezier piece on [u0, u1]."""

    u0: float
    u1: float
    ctrl: tuple[Homogeneous, ...]

    def point(self, t: float) -> Point:
        return _cartesian(_casteljau(list(self.ctrl), t))

    def derivative(self, t: float) -> Point:
        """dC/dt on this span (t in [0, 1])."""
        p = len(self.ctrl) - 1
        a = _casteljau(list(self.ctrl), t)
        hodograph = [tuple(p * (q1[i] - q0[i]) for i in range(3))
                     for q0, q1 in pairwise(self.ctrl)]
        da = _casteljau(hodograph, t)
        x, y = a[0] / a[2], a[1] / a[2]
        return ((da[0] - da[2] * x) / a[2], (da[1] - da[2] * y) / a[2])


@dataclass(frozen=True)
class Nurbs:
    degree: int
    spans: tuple[Span, ...]
    size: float  # the poles' bounding-box diagonal (mm)

    @classmethod
    def from_entity(cls, entity: dict[str, Any],
                    transform: Callable[[Any], Point] | None = None) -> Nurbs:
        """Read a tree `spline` entity. `transform`, an affine map of the
        sketch plane (such as the projection into a CATIA sketch frame),
        is applied to the poles. Raises ValueError when malformed."""
        try:
            degree = int(entity["degree"])
            poles = [(float(p[0]), float(p[1])) for p in entity["poles"]]
            weights = [float(w) for w in entity.get("weights") or [1.0] * len(poles)]
            knots = [float(k) for k in entity["knots"]]
        except (KeyError, TypeError, IndexError) as error:
            raise ValueError(f"the spline is malformed ({error})") from error
        periodic = bool(entity.get("periodic"))
        if degree < 1 or len(poles) < 2 or len(weights) != len(poles):
            raise ValueError("the spline needs a degree of at least 1 and one weight per pole")
        if any(not w > 0 or not math.isfinite(w) for w in weights):
            raise ValueError("the spline's weights must be positive")
        if any(b < a for a, b in pairwise(knots)) or len(knots) < 2 * (degree + 1) - 1:
            raise ValueError("the spline's knots must be a non-decreasing vector")
        if transform is not None:
            poles = [tuple(float(c) for c in transform(p)) for p in poles]
        flat = _flat_knots(degree, len(poles), knots, periodic)
        needed = len(flat) - degree - 1
        if needed > len(poles):
            if needed - len(poles) > len(poles):
                raise ValueError("the periodic spline has too few poles for its knots")
            poles += poles[:needed - len(poles)]
            weights += weights[:needed - len(weights)]
        ctrl = [(w * x, w * y, w) for (x, y), w in zip(poles, weights, strict=True)]
        xs, ys = [p[0] for p in poles], [p[1] for p in poles]
        size = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        return cls(degree, _bezier_spans(degree, flat, ctrl), size)

    # ── Evaluation ──

    @property
    def domain(self) -> tuple[float, float]:
        return self.spans[0].u0, self.spans[-1].u1

    def _locate(self, u: float) -> tuple[Span, float]:
        starts = [s.u0 for s in self.spans]
        span = self.spans[max(0, min(len(self.spans) - 1, bisect_right(starts, u) - 1))]
        return span, (u - span.u0) / (span.u1 - span.u0)

    def point(self, u: float) -> Point:
        span, t = self._locate(u)
        return span.point(t)

    def derivative(self, u: float) -> Point:
        """dC/du."""
        span, t = self._locate(u)
        dx, dy = span.derivative(t)
        return dx / (span.u1 - span.u0), dy / (span.u1 - span.u0)

    @property
    def start(self) -> Point:
        return _cartesian(self.spans[0].ctrl[0])

    @property
    def end(self) -> Point:
        return _cartesian(self.spans[-1].ctrl[-1])

    @property
    def closed(self) -> bool:
        return math.dist(self.start, self.end) <= POINT_TOL

    def length(self) -> float:
        total = 0.0
        for span in self.spans:
            for k in range(16):
                for x, w in _GL:
                    total += w / 16 * math.hypot(*span.derivative((k + x) / 16))
        return total

    # ── Polygons ──

    def span_polygon(self, span: Span) -> list[tuple[float, Point]]:
        """(t, point) along one span, chords within SAGITTA_REL x size."""
        tol = SAGITTA_REL * max(self.size, POINT_TOL)
        ts = [i / MIN_CHORDS_PER_SPAN for i in range(MIN_CHORDS_PER_SPAN + 1)]
        out = [(0.0, span.point(0.0))]
        for t0, t1 in pairwise(ts):
            stack = [(t0, t1, out[-1][1], span.point(t1), 0)]
            while stack:
                a, b, pa, pb, depth = stack.pop()
                mid = (a + b) / 2
                pm = span.point(mid)
                if depth < MAX_SUBDIVISIONS and _segment_distance(pm, pa, pb) > tol:
                    stack.append((mid, b, pm, pb, depth + 1))
                    stack.append((a, mid, pa, pm, depth + 1))
                else:
                    out.append((b, pb))
        return out

    def polygon(self) -> list[Point]:
        """The curve as a polyline from its start to its end."""
        points = [self.start]
        for span in self.spans:
            points.extend(p for _, p in self.span_polygon(span)[1:])
        return points


def _bezier_spans(degree: int, knots: list[float], ctrl: list[Homogeneous]) -> tuple[Span, ...]:
    """Insert every knot of the domain up to multiplicity `degree` (the
    domain ends included), then read off the rational Bezier spans."""
    p = degree
    lo, hi = knots[p], knots[-p - 1]
    if not hi > lo:
        raise ValueError("the spline's knots give it an empty domain")
    tol = KNOT_TOL * max(1.0, abs(knots[-1] - knots[0]))
    knots = list(knots)
    for i in range(1, len(knots)):  # snap near-equal knots together
        if knots[i] - knots[i - 1] <= tol:
            knots[i] = knots[i - 1]
    for u in sorted({k for k in knots if lo <= k <= hi}):
        if knots.count(u) > p and lo < u < hi:
            raise ValueError("the spline is discontinuous (an inner knot is repeated more "
                             "than its degree)")
        while knots.count(u) < p:
            knots, ctrl = _insert(p, knots, ctrl, u, at_end=u == hi)
    spans = []
    for i in range(p, len(knots) - p - 1):
        if knots[i] < knots[i + 1] and lo <= knots[i] and knots[i + 1] <= hi:
            spans.append(Span(knots[i], knots[i + 1], tuple(ctrl[i - p:i + 1])))
    return tuple(spans)


def _insert(p: int, knots: list[float], ctrl: list[Homogeneous], u: float, at_end: bool):
    """Boehm: insert the knot u once, in the knot interval [k, k + 1) that
    holds it, or (k, k + 1] at the domain's end."""
    k = (bisect_left(knots, u) if at_end else bisect_right(knots, u)) - 1
    new = ctrl[:k - p + 1]
    for i in range(k - p + 1, k + 1):
        alpha = (u - knots[i]) / (knots[i + p] - knots[i])
        new.append(_lerp(ctrl[i - 1], ctrl[i], alpha))
    new.extend(ctrl[k:])
    return knots[:k + 1] + [u] + knots[k + 1:], new


# ── Conics ──


@dataclass(frozen=True)
class Conic:
    """A circle or an ellipse (or an arc of one): centre, unit major axis,
    semi-axes a >= b, and for arcs the counter-clockwise eccentric angles
    from the major axis (start < end), as CATIA's CreateEllipse takes them.
    For circles the major axis is +H, so the angles are polar angles."""

    center: Point
    major: Point
    a: float
    b: float
    closed: bool
    start: float = 0.0
    end: float = 2 * math.pi

    @property
    def is_circle(self) -> bool:
        return self.a - self.b <= CONIC_TOL * self.a

    def angle(self, p: Point) -> float:
        d = _sub(p, self.center)
        e1, e2 = self.major, (-self.major[1], self.major[0])
        return math.atan2((d[0] * e2[0] + d[1] * e2[1]) / self.b,
                          (d[0] * e1[0] + d[1] * e1[1]) / self.a)

    def point(self, t: float) -> Point:
        e1, e2 = self.major, (-self.major[1], self.major[0])
        c, s = self.a * math.cos(t), self.b * math.sin(t)
        return (self.center[0] + c * e1[0] + s * e2[0], self.center[1] + c * e1[1] + s * e2[1])


def _span_ellipse(span: Span):
    """(centre, conjugate semi-diameters u and v, sweep) of a rational
    quadratic span that is an elliptical arc, else None. The span maps the
    circular arc of half-angle acos(w) (w its standard-form weight) by an
    affine map: C + u cos(phi) + v sin(phi), phi from 0 to the sweep."""
    (x0, y0, w0), (x1, y1, w1), (x2, y2, w2) = span.ctrl
    p0, p1, p2 = (x0 / w0, y0 / w0), (x1 / w1, y1 / w1), (x2 / w2, y2 / w2)
    w = w1 / math.sqrt(w0 * w2)
    if not 0 < w < 1 - 1e-9:
        return None  # a parabola (w = 1) or a hyperbola (w > 1)
    if abs(_cross(_sub(p1, p0), _sub(p2, p1))) <= 1e-12 * max(1.0, math.dist(p0, p2)) ** 2:
        return None
    m = ((p0[0] + p2[0]) / 2, (p0[1] + p2[1]) / 2)
    center = ((m[0] - w * w * p1[0]) / (1 - w * w), (m[1] - w * w * p1[1]) / (1 - w * w))
    sweep = 2 * math.acos(w)
    u = _sub(p0, center)
    rest = _sub(_sub(p2, center), (u[0] * math.cos(sweep), u[1] * math.cos(sweep)))
    v = (rest[0] / math.sin(sweep), rest[1] / math.sin(sweep))
    return center, u, v, sweep


def _principal(u: Point, v: Point) -> tuple[Point, float, float]:
    """Unit major axis and semi-axes a >= b of C + u cos + v sin."""
    sxx, syy = u[0] ** 2 + v[0] ** 2, u[1] ** 2 + v[1] ** 2
    sxy = u[0] * u[1] + v[0] * v[1]
    mean, radius = (sxx + syy) / 2, math.hypot((sxx - syy) / 2, sxy)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return (math.cos(theta), math.sin(theta)), math.sqrt(mean + radius), \
        math.sqrt(max(mean - radius, 0.0))


def conic(curve: Nurbs) -> Conic | None:
    """The circle or ellipse a curve exactly is, or None. Every span must be
    an elliptical arc of the same ellipse, turning the same way, and the
    whole sweep at most one turn."""
    if curve.degree != 2:
        return None
    pieces = [_span_ellipse(span) for span in curve.spans]
    if any(piece is None for piece in pieces):
        return None
    center, u, v, _ = pieces[0]
    major, a, b = _principal(u, v)
    if b <= CONIC_TOL * a:
        return None
    ccw = _cross(u, v) > 0
    tol = CONIC_TOL * a
    for c, pu, pv, _ in pieces:
        if math.dist(c, center) > tol or (_cross(pu, pv) > 0) != ccw:
            return None
        m2, a2, b2 = _principal(pu, pv)
        if abs(a2 - a) > tol or abs(b2 - b) > tol:
            return None
        if a - b > tol and abs(_cross(m2, major)) > CONIC_TOL * 10:
            return None
    if a - b <= tol:
        major, b = (1.0, 0.0), a
    sweep = sum(piece[3] for piece in pieces)
    if sweep > 2 * math.pi + 1e-9:
        return None
    shape = Conic(center, major, a, b, closed=False)
    # The implicit equation holds along every span, not only at its ends.
    e1, e2 = major, (-major[1], major[0])
    for span in curve.spans:
        for t in (0.25, 0.5, 0.75):
            d = _sub(span.point(t), center)
            x, y = (d[0] * e1[0] + d[1] * e1[1]) / a, (d[0] * e2[0] + d[1] * e2[1]) / b
            if abs(x * x + y * y - 1) > CONIC_TOL * 10:
                return None
    if abs(sweep - 2 * math.pi) <= 1e-9 and curve.closed:
        return Conic(center, major, a, b, closed=True)
    start, end = shape.angle(curve.start), shape.angle(curve.end)
    if not ccw:
        start, end = end, start
    while end <= start:
        end += 2 * math.pi
    return Conic(center, major, a, b, False, start, end)


# ── Drawing a curve in CATIA ──


@dataclass(frozen=True)
class Drawing:
    """How one spline is drawn: exact items (lines, circles, ellipses) or
    sketch splines through points, with a bound on the area between the
    curve and the chords of those points (0 when exact)."""

    items: tuple[tuple, ...]
    exact: bool
    deviation_area: float = 0.0


def _spline_samples(curve: Nurbs, spans: list[Span]) -> tuple[list[Point], float]:
    """Points along consecutive spans for a CATIA spline, and a bound on the
    area between the curve and their chords: fan triangles over the fine
    polygon (so overlaps and inflections are counted, not cancelled), plus
    the fine polygon's own distance to the curve."""
    samples = [_cartesian(spans[0].ctrl[0])]
    deviation = 0.0
    for span in spans:
        fine = [p for _, p in curve.span_polygon(span)]
        turn = [0.0]
        length = [0.0]
        for i in range(1, len(fine)):
            bend = _turn(_sub(fine[i - 1], fine[i - 2]), _sub(fine[i], fine[i - 1])) if i > 1 else 0
            turn.append(turn[-1] + bend)
            length.append(length[-1] + math.dist(fine[i - 1], fine[i]))
        if length[-1] <= 0:
            continue
        # The fine polygon's own chords are within the sagitta of the curve.
        deviation += SAGITTA_REL * max(curve.size, POINT_TOL) * length[-1]
        measure = [t / SPLINE_MAX_TURN + s * SPLINE_MIN_SAMPLES / length[-1]
                   for t, s in zip(turn, length, strict=True)]
        count = max(SPLINE_MIN_SAMPLES, math.ceil(measure[-1] - 1e-9))
        picks, k = [0], 1
        for i in range(1, len(fine) - 1):
            if measure[i] >= k * measure[-1] / count:
                picks.append(i)
                k += 1
        picks.append(len(fine) - 1)
        for i0, i1 in pairwise(picks):
            anchor = fine[i0]
            deviation += sum(abs(_cross(_sub(fine[j], anchor), _sub(fine[j + 1], anchor))) / 2
                             for j in range(i0 + 1, i1))
            samples.append(fine[i1])
    return samples, deviation


def drawing(curve: Nurbs) -> Drawing:
    """How to draw a curve in a CATIA sketch: degree 1 as lines, an exact
    circle or ellipse natively, anything else as sketch splines through
    sampled points, split at corners and, when closed, in two halves (a
    sketch spline is never closed on itself)."""
    if curve.degree == 1:
        lines = tuple(("line", _cartesian(s.ctrl[0]), _cartesian(s.ctrl[1])) for s in curve.spans
                      if math.dist(_cartesian(s.ctrl[0]), _cartesian(s.ctrl[1])) > POINT_TOL)
        return Drawing(lines, exact=True)
    shape = conic(curve)
    if shape is not None:
        if shape.is_circle:
            item = ("circle", shape.center, shape.a) if shape.closed else \
                ("arc", shape.center, shape.a, shape.start, shape.end)
        else:
            item = ("ellipse", shape.center, shape.major, shape.a, shape.b) if shape.closed else \
                ("elliptic_arc", shape.center, shape.major, shape.a, shape.b, shape.start,
                 shape.end)
        return Drawing((item,), exact=True)
    groups: list[list[Span]] = [[curve.spans[0]]]
    for before, after in pairwise(curve.spans):
        if _turn(before.derivative(1.0), after.derivative(0.0)) > KINK_ANGLE:
            groups.append([])
        groups[-1].append(after)
    if len(groups) == 1 and curve.closed:
        spans = groups[0]
        if len(spans) > 1:
            groups = [spans[:len(spans) // 2], spans[len(spans) // 2:]]
        else:
            groups = [[s] for s in _halve(spans[0])]
    items, deviation = [], 0.0
    for spans in groups:
        points, area = _spline_samples(curve, spans)
        items.append(("spline", tuple(points)))
        deviation += area
    return Drawing(tuple(items), exact=False, deviation_area=deviation)


def _halve(span: Span) -> tuple[Span, Span]:
    """Split a Bezier span at its middle (de Casteljau)."""
    left, right, pts = [], [], list(span.ctrl)
    while pts:
        left.append(pts[0])
        right.append(pts[-1])
        pts = [_lerp(pts[i], pts[i + 1], 0.5) for i in range(len(pts) - 1)]
    mid = (span.u0 + span.u1) / 2
    return Span(span.u0, mid, tuple(left)), Span(mid, span.u1, tuple(reversed(right)))


def clamped(curve: Nurbs) -> tuple[int, list[Point], list[float], list[float]]:
    """The curve as one clamped, non-periodic NURBS: degree, poles, weights
    and the expanded knot vector. Its inner knots have multiplicity `degree`,
    so its spans are independent Bezier pieces whatever the knot values: the
    knots are 0, 1, 2... per span, which no kernel merges as too close."""
    p = curve.degree
    homogeneous = list(curve.spans[0].ctrl)
    for span in curve.spans[1:]:
        homogeneous.extend(span.ctrl[1:])
    count = len(curve.spans)
    knots = [0.0] * (p + 1) + [float(i) for i in range(1, count) for _ in range(p)] \
        + [float(count)] * (p + 1)
    return p, [_cartesian(h) for h in homogeneous], [h[2] for h in homogeneous], knots
