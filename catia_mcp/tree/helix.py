"""Sweeps along a helix (springs, modelled threads): the contract and its
closed form.

A `sweep` whose path is one `helix` entity is build123d's
sweep(profile, Helix(pitch, height, radius, center, direction, cone_angle,
lefthand), is_frenet=True). Pure geometry, no CATIA and no build123d.

Where the helix starts is OpenCascade's rule (Edge.make_helix wraps a line
around a cylinder or cone whose X direction gp_Ax3 picks from the axis): the
start is `center + radius * x_dir`, x_dir as `occ_x_direction` gives it.

Along a cylindrical helix the Frenet frame moves rigidly by a screw motion
(a turn about the axis and the matching advance along it), so the solid has
a closed form for any planar profile, wherever it lies: the volume is
Theta * A * w(c), w the flux of the screw motion's velocity through the
profile's plane (linear in the point), and the centre of gravity integrates
the screw over the profile's second moments. Measured against build123d
0.13: 1e-7 relative on the volume. A tapered helix (cone_angle) is not a
screw motion: only build123d's body checks it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from catia_mcp.tree.geometry import (
    Frame,
    Vec,
    _polygon_moments,
    add,
    cross,
    dot,
    norm,
    profile_loops,
    scale,
    unit,
)

Refuse = Any
HELIX_KEYS = ("pitch", "radius", "height", "turns", "center", "direction", "cone_angle",
              "lefthand", "start", "type")
START_TOL = 1e-6  # mm: a tree's `start` must be OpenCascade's


def _vec(value: Any) -> Vec:
    out = tuple(float(c) for c in value)
    if len(out) != 3:
        raise ValueError("not a 3D vector")
    return out


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def occ_x_direction(direction: Vec) -> Vec:
    """The X direction gp_Ax3(point, direction) picks (gp_Ax2's constructor
    from a main direction), where OpenCascade's helix starts."""
    a, b, c = unit(direction)
    aa, ab, ac = abs(a), abs(b), abs(c)
    if ab <= aa and ab <= ac:
        x = (-c, 0.0, a) if aa > ac else (c, 0.0, -a)
    elif aa <= ab and aa <= ac:
        x = (0.0, -c, b) if ab > ac else (0.0, c, -b)
    else:
        x = (-b, a, 0.0) if aa > ab else (b, -a, 0.0)
    return unit(x)


@dataclass(frozen=True)
class HelixSpec:
    center: Vec  # on the axis, at the helix's start height
    direction: Vec  # unit, the way the helix advances
    radius: float  # at the start
    pitch: float  # build123d's: along the cone's generatrix per turn when tapered
    height: float  # along the axis
    lefthand: bool = False
    cone_angle: float = 0.0  # degrees, > 0 widens along the direction

    @property
    def x_dir(self) -> Vec:
        return occ_x_direction(self.direction)

    @property
    def y_dir(self) -> Vec:
        return cross(self.direction, self.x_dir)

    @property
    def sense(self) -> float:
        """+1 turning right-handed about the direction, -1 left-handed."""
        return -1.0 if self.lefthand else 1.0

    @property
    def axial_pitch(self) -> float:
        """The advance along the axis per turn (CATIA's pitch)."""
        return self.pitch * math.cos(math.radians(self.cone_angle))

    @property
    def turns(self) -> float:
        return self.height / self.axial_pitch

    @property
    def tapered(self) -> bool:
        return abs(self.cone_angle) > 1e-12

    def point(self, turns: float) -> Vec:
        """The helix point after `turns` turns (0 at the start)."""
        v = turns * self.pitch  # along the generatrix
        a = math.radians(self.cone_angle)
        r = self.radius + v * math.sin(a)
        phi = self.sense * 2 * math.pi * turns
        radial = add(scale(self.x_dir, math.cos(phi)), scale(self.y_dir, math.sin(phi)))
        return add(add(self.center, scale(radial, r)), scale(self.direction, v * math.cos(a)))

    @property
    def start(self) -> Vec:
        return self.point(0.0)

    @property
    def end(self) -> Vec:
        return self.point(self.turns)

    @property
    def end_radius(self) -> float:
        return self.radius + self.height * math.tan(math.radians(self.cone_angle))

    def tangent(self, turns: float = 0.0) -> Vec:
        h = 1e-6
        return unit(add(self.point(turns + h), scale(self.point(turns - h), -1)))


def is_helix_path(path: Any) -> bool:
    return isinstance(path, list) and any(isinstance(e, dict) and e.get("type") == "helix"
                                          for e in path)


def helix_spec(entity: dict[str, Any]) -> HelixSpec:
    """The helix of a path entity. Raises ValueError."""
    pitch, radius = entity.get("pitch"), entity.get("radius")
    if not _number(pitch) or pitch <= 0:
        raise ValueError("a helix pitch must be a positive number")
    if not _number(radius) or radius <= 0:
        raise ValueError("a helix radius must be a positive number")
    angle = entity.get("cone_angle", 0.0) or 0.0
    if not _number(angle) or not -60 < angle < 60:
        raise ValueError("a helix cone_angle must be in (-60, 60) degrees")
    height, turns = entity.get("height"), entity.get("turns")
    axial = pitch * math.cos(math.radians(angle))
    if height is None and turns is not None:
        if not _number(turns) or turns <= 0:
            raise ValueError("helix turns must be a positive number")
        height = turns * axial
    if not _number(height) or height <= 0:
        raise ValueError("a helix needs a positive height or a number of turns")
    if turns is not None and abs(float(turns) * axial - float(height)) > 1e-6 * max(1.0, height):
        raise ValueError("a helix's turns and height disagree (height = turns x pitch)")
    lefthand = entity.get("lefthand", False)
    if not isinstance(lefthand, bool):
        raise ValueError("helix lefthand must be true or false")
    spec = HelixSpec(_vec(entity.get("center", (0, 0, 0))),
                     unit(_vec(entity.get("direction", (0, 0, 1)))),
                     float(radius), float(pitch), float(height), lefthand, float(angle))
    if spec.end_radius <= 0:
        raise ValueError("the tapered helix closes onto its axis")
    if entity.get("start") is not None and \
            norm(add(_vec(entity["start"]), scale(spec.start, -1))) > START_TOL:
        raise ValueError("the helix 'start' is not where build123d's Helix starts "
                         f"({', '.join(f'{c:g}' for c in spec.start)})")
    return spec


def path_helix(path: list[dict[str, Any]]) -> HelixSpec:
    if len(path) != 1 or path[0].get("type") != "helix":
        raise ValueError("a helix path is one helix entity (lines or arcs cannot follow it "
                         "yet)")
    return helix_spec(path[0])


# ── The screw motion's closed form ──


@dataclass(frozen=True)
class ScrewSweep:
    volume: float
    cog: Vec
    flux_range: tuple[float, float]  # the flux w over the profile's corners


def _profile_moments(sketch: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    """A, Su, Sv, Iuu, Iuv, Ivv of the profiles in the sketch's (u, v)."""
    a = su = sv = iuu = iuv = ivv = 0.0
    for sign, polygon in profile_loops(sketch):
        area, sx, ixx, ixy = _polygon_moments(polygon)
        _, sy, iyy, _ = _polygon_moments([(v, u) for u, v in polygon])
        a, su, sv = a + sign * area, su + sign * sx, sv + sign * sy
        iuu, iuv, ivv = iuu + sign * ixx, iuv + sign * ixy, ivv + sign * iyy
    if a <= 0:
        raise ValueError("the profile encloses no area")
    return a, su, sv, iuu, iuv, ivv


def screw_sweep(sketch: dict[str, Any], spec: HelixSpec) -> ScrewSweep:
    """Volume and centre of gravity of the profile swept along the
    cylindrical helix with build123d's Frenet frame: the screw motion
    S(t) q = O + R(s t) (q - O) + (p t / 2 pi) d, t in [0, 2 pi turns]."""
    if spec.tapered:
        raise ValueError("a tapered helix has no closed form")
    frame = Frame.from_tree(sketch["plane"])
    d, o = spec.direction, spec.center
    s, k = spec.sense, spec.pitch / (2 * math.pi)
    n, e1, e2 = frame.normal, frame.x_dir, frame.y_dir
    # w(q) = s (d x r).n + k d.n = a.r + b, r = q - O.
    a_vec, b = scale(cross(n, d), s), k * dot(d, n)
    area, su, sv, iuu, iuv, ivv = _profile_moments(sketch)
    r0 = add(frame.origin, scale(o, -1))
    beta = dot(a_vec, r0) + b
    a1, a2 = dot(a_vec, e1), dot(a_vec, e2)
    flux = beta * area + a1 * su + a2 * sv  # integral of w over the profile
    if abs(flux) < 1e-12:
        raise ValueError("the profile does not sweep any volume along the helix")
    # m = integral of r w(q) dA.
    m = add(add(scale(r0, flux),
                scale(e1, beta * su + a1 * iuu + a2 * iuv)),
            scale(e2, beta * sv + a1 * iuv + a2 * ivv))
    theta = 2 * math.pi * spec.turns
    m_par = dot(m, d)
    m_perp = add(m, scale(d, -m_par))
    turned = add(add(scale(m_perp, math.sin(theta)),
                     scale(cross(d, m_perp), s * (1 - math.cos(theta)))),
                 scale(d, m_par * theta))
    moment = add(add(scale(o, flux * theta), turned), scale(d, k * theta * theta / 2 * flux))
    corners = [beta + a1 * u + a2 * v for _, loop in profile_loops(sketch) for u, v in loop]
    return ScrewSweep(abs(flux) * theta, scale(moment, 1 / (flux * theta)),
                      (min(corners), max(corners)))


def check_helix_sweep(feature: dict[str, Any], sketch: dict[str, Any] | None,
                      refuse, where: str) -> None:
    """Preflight of a sweep along a helix (the sketch and operation are
    checked by the caller)."""
    try:
        spec = path_helix(feature.get("path") or [])
    except (TypeError, ValueError) as error:
        refuse(f"helix sweep is invalid ({error})", where)
        return
    if not feature.get("frenet"):
        # build123d's default frame (corrected Frenet) twists the profile
        # along a helix: 0.8 % off the Frenet solid for a 2 x 2 square.
        refuse("sweeps along a helix must be Frenet sweeps (is_frenet=True), as a spring "
               "or a thread is modelled", where)
    if sketch is None:
        return
    try:
        frame = Frame.from_tree(sketch["plane"])
        loops = profile_loops(sketch)
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"helix sweep profile is invalid ({error})", where)
        return
    d, o = spec.direction, spec.center
    corners = [frame.world(u, v) for _, loop in loops for u, v in loop]
    radial = [norm(add(add(p, scale(o, -1)), scale(d, -dot(add(p, scale(o, -1)), d))))
              for p in corners]
    if min(radial) <= 1e-6:
        refuse("a helix sweep profile must not reach the helix axis", where)
        return
    along = [dot(add(p, scale(o, -1)), d) for p in corners]
    if spec.turns > 1 and max(along) - min(along) >= spec.axial_pitch - 1e-9:
        refuse("the helix sweep profile is taller than the pitch, so the turns would "
               "overlap", where)
        return
    if spec.tapered:
        return
    try:
        swept = screw_sweep(sketch, spec)
    except (KeyError, ValueError) as error:
        refuse(f"helix sweep is not supported ({error})", where)
        return
    low, high = swept.flux_range
    if low * high <= 0:
        refuse("the helix sweep profile is edge-on to the helix somewhere (the solid would "
               "fold over itself)", where)


def helix_samples(path: list[dict[str, Any]], per_turn: int = 16) -> list[Vec]:
    """Points along the path's helices (16 a turn and the end), for a
    bounding box; nothing for other entities or an invalid helix."""
    points: list[Vec] = []
    for entity in path:
        if not isinstance(entity, dict) or entity.get("type") != "helix":
            continue
        try:
            spec = helix_spec(entity)
        except (TypeError, ValueError):
            continue
        steps = max(4, math.ceil(per_turn * spec.turns))
        points += [spec.point(spec.turns * i / steps) for i in range(steps + 1)]
    return points
