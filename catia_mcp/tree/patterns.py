"""Repeated holes as one Hole and a pattern, as a designer would model them.

build123d's GridLocations and PolarLocations reach the tree only as the
hole's list of frames: the layout is not given, so it is recognised here
from the frames' origins, in the plane of the first frame (its X and Y):

- a grid: the origins are every combination of nx equally spaced X values
  and ny equally spaced Y values (nx * ny = N, a single row when one is 1);
- a circle: N >= 3 origins equally far from a centre, at equal angular steps
  (a full crown or an arc).

Anything else is not a pattern: one Hole per position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from catia_mcp.tree.geometry import Vec, add, cross, dot, frame_axes, scale, unit

TOL = 1e-6


@dataclass
class GridPattern:
    seed: int  # index of the seed frame (the corner with the lowest X and Y)
    counts: tuple[int, int]
    steps: tuple[float, float]  # mm, along the directions below
    directions: tuple[Vec, Vec]  # unit vectors in world coordinates


@dataclass
class PolarPattern:
    seed: int  # index of the seed frame (the start of the run)
    count: int
    step_deg: float  # counter-clockwise about `axis`
    centre: Vec
    axis: Vec


def detect(frames: list) -> GridPattern | PolarPattern | None:
    if len(frames) < 2:
        return None
    axes = [frame_axes(m) for m in frames]
    origin0, x_dir, z_dir = axes[0]
    if any(abs(dot(z, z_dir) - 1) > TOL for _, _, z in axes):
        return None  # not all drilled the same way
    y_dir = cross(z_dir, x_dir)
    points = []
    for origin, _, _ in axes:
        d = add(origin, scale(origin0, -1))
        if abs(dot(d, z_dir)) > TOL:
            return None  # not on one plane
        points.append((dot(d, x_dir), dot(d, y_dir)))
    grid = _grid(points)
    if grid is not None:
        seed, counts, steps = grid
        return GridPattern(seed, counts, steps, (x_dir, y_dir))
    polar = _polar(points)
    if polar is not None:
        seed, count, step, (cu, cv) = polar
        centre = add(origin0, add(scale(x_dir, cu), scale(y_dir, cv)))
        return PolarPattern(seed, count, step, centre, z_dir)
    return None


def _levels(values: list[float]) -> list[float]:
    levels: list[float] = []
    for value in sorted(values):
        if not levels or value - levels[-1] > TOL:
            levels.append(value)
    return levels


def _even(levels: list[float]) -> float | None:
    if len(levels) == 1:
        return 0.0
    step = levels[1] - levels[0]
    if all(abs(b - a - step) <= TOL for a, b in zip(levels, levels[1:], strict=False)):
        return step
    return None


def _grid(points):
    us, vs = _levels([p[0] for p in points]), _levels([p[1] for p in points])
    if len(us) * len(vs) != len(points):
        return None
    du, dv = _even(us), _even(vs)
    if du is None or dv is None:
        return None
    wanted = {(round(u / TOL), round(v / TOL)) for u in us for v in vs}
    got = {(round(u / TOL), round(v / TOL)) for u, v in points}
    if wanted != got or len(got) != len(points):
        return None
    seed = min(range(len(points)), key=lambda i: (round(points[i][0] / TOL), round(points[i][1] / TOL)))
    return seed, (len(us), len(vs)), (du, dv)


def _circumcentre(a, b, c):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < TOL:
        return None
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    return ux, uy


def _polar(points):
    if len(points) < 3:
        return None
    centre = _circumcentre(*points[:3])
    if centre is None:
        return None
    radius = math.dist(points[0], centre)
    if radius < TOL or any(abs(math.dist(p, centre) - radius) > TOL * max(1.0, radius)
                           for p in points):
        return None
    angles = [math.atan2(p[1] - centre[1], p[0] - centre[0]) % (2 * math.pi) for p in points]
    order = sorted(range(len(points)), key=lambda i: angles[i])
    gaps = [(angles[order[(k + 1) % len(order)]] - angles[order[k]]) % (2 * math.pi)
            for k in range(len(order))]
    # The run starts after the largest gap; a full crown starts at the
    # first frame, as the script listed it.
    if max(gaps) - min(gaps) <= TOL:
        start = order.index(0)
    else:
        start = (max(range(len(gaps)), key=lambda k: gaps[k]) + 1) % len(order)
    run = order[start:] + order[:start]
    steps = [(angles[b] - angles[a]) % (2 * math.pi) for a, b in zip(run, run[1:], strict=False)]
    step = steps[0]
    if step < TOL or any(abs(s - step) > TOL for s in steps):
        return None
    return run[0], len(points), math.degrees(step), centre


def instance_origins(pattern: GridPattern | PolarPattern, seed_origin: Vec) -> list[Vec]:
    """Every instance's origin, the seed's included (for checks and tests)."""
    if isinstance(pattern, GridPattern):
        (n1, n2), (s1, s2), (d1, d2) = pattern.counts, pattern.steps, pattern.directions
        return [add(seed_origin, add(scale(d1, i * s1), scale(d2, j * s2)))
                for i in range(n1) for j in range(n2)]
    out = []
    axis = unit(pattern.axis)
    r = add(seed_origin, scale(pattern.centre, -1))
    for k in range(pattern.count):
        a = math.radians(k * pattern.step_deg)
        # Rodrigues' rotation of r about the axis.
        rotated = add(add(scale(r, math.cos(a)), scale(cross(axis, r), math.sin(a))),
                      scale(axis, dot(axis, r) * (1 - math.cos(a))))
        out.append(add(pattern.centre, rotated))
    return out
