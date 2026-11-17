"""Assembly constraints a designer would have put: inferred from geometry.

The assembly is rebuilt with every instance where the STEP file puts it. A
designer would rather ground one component and position the others by
constraints on their faces. This module finds those constraints, offline,
from each component's faces in world coordinates:

- **coincidence** of axes: two cylindrical faces on one axis line whose
  lengths along it overlap (a pin in a hole, a bolt in a bore, two holes in
  line);
- **contact**: two planar faces in one plane, facing each other, whose areas
  overlap (a box standing on a base, a bolt head on a plate);
- **offset**: two parallel planar faces at a distance (or flush), the
  distance an editable value;
- **angle**: two planar faces at an angle (only used to stop a rotation
  nothing else stops).

Each candidate removes degrees of freedom: written as linear conditions on
the component's small motion (rotation, translation), their rank says how
many are gone. One component is grounded (the largest, or the one asked
for); the others are taken in order and given, against components already
placed, the best candidates that raise the rank, until it reaches 6, then
the set is pruned to a minimal one. A component left free only to spin
about its own axis of symmetry (a pin, a bolt) counts as placed, as a
designer would leave it. A component that cannot be fully placed is fixed
in space instead ("Fix"), and the report says how many freedoms it had left.

Everything is deterministic: faces are numbered in the order OpenCascade
explores them, candidates are sorted by kind, quality, then face ids.

The faces come from the STEP file's shapes (`components_from_structure`) or
from the tree's build123d reference (`components_from_tree`); both need OCP
(installed with build123d). `infer` itself is plain Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Vec = tuple[float, float, float]
Rows = list[list[float]]

LIN_TOL = 1e-3  # mm: planes and axes closer than this coincide
ANG_TOL = 1e-6  # |sin| of the angle between directions taken as parallel
GRID = 32  # samples per side of the overlap grid of two planar faces
RANK_TOL = 1e-7

KIND_ORDER = {"coincidence": 0, "contact": 1, "offset": 2, "angle": 3}


# ── Vectors ──

def _add(a, b) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a, b) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a) -> Vec:
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n)


def _perpendiculars(d: Vec) -> tuple[Vec, Vec]:
    """Two unit vectors perpendicular to d and to each other (deterministic)."""
    helper = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = _unit(_cross(d, helper))
    return e1, _cross(d, e1)


def _parallel(a: Vec, b: Vec) -> bool:
    return _norm(_cross(a, b)) <= ANG_TOL


def _apply(rows: Rows, p) -> Vec:
    return tuple(sum(rows[i][k] * float(p[k]) for k in range(3)) + rows[i][3] for i in range(3))


def _rotate(rows: Rows, v) -> Vec:
    return tuple(sum(rows[i][k] * float(v[k]) for k in range(3)) for i in range(3))


def _invert(rows: Rows) -> Rows:
    rt = [[rows[j][i] for j in range(3)] for i in range(3)]
    t = [rows[i][3] for i in range(3)]
    return [rt[i] + [-sum(rt[i][k] * t[k] for k in range(3))] for i in range(3)]


def _round(v, digits: int = 6) -> list[float]:
    return [round(float(c), digits) + 0.0 for c in v]


# ── Faces and components ──

@dataclass(frozen=True)
class PlaneFace:
    id: str  # F1, F2... in the prototype's exploration order
    point: Vec  # the face's centre (on the plane)
    normal: Vec  # unit, out of the material
    area: float
    loops: tuple[tuple[Vec, ...], ...] = ()  # boundary polygons (outer and holes)

    kind = "plane"

    def moved(self, rows: Rows) -> PlaneFace:
        return PlaneFace(self.id, _apply(rows, self.point), _rotate(rows, self.normal), self.area,
                         tuple(tuple(_apply(rows, p) for p in loop) for loop in self.loops))


@dataclass(frozen=True)
class CylinderFace:
    id: str
    point: Vec  # on the axis
    direction: Vec  # unit
    radius: float
    hole: bool  # the material is outside (a bore), else inside (a shaft)
    span: tuple[float, float]  # the face's extent along the axis, from `point`
    area: float = 0.0

    kind = "cylinder"

    def moved(self, rows: Rows) -> CylinderFace:
        return CylinderFace(self.id, _apply(rows, self.point), _rotate(rows, self.direction),
                            self.radius, self.hole, self.span, self.area)


@dataclass(frozen=True)
class OtherFace:
    """Any other face. `axis` (point, direction) for a surface of revolution
    (cone, sphere, torus), None for a free-form face."""
    id: str
    surface: str
    axis: tuple[Vec, Vec] | None = None

    kind = "other"

    def moved(self, rows: Rows) -> OtherFace:
        axis = None if self.axis is None else (_apply(rows, self.axis[0]),
                                               _rotate(rows, self.axis[1]))
        return OtherFace(self.id, self.surface, axis)


Face = PlaneFace | CylinderFace | OtherFace


@dataclass
class Component:
    """One instance in the assembly, its faces in world coordinates."""
    id: str
    name: str
    faces: list[Face]
    world: Rows  # the part's own frame -> world
    prototype: str = ""
    volume: float = 0.0

    def __post_init__(self) -> None:
        self.prototype = self.prototype or self.id

    def own(self, point: Vec | None = None, direction: Vec | None = None):
        inverse = _invert(self.world)
        if point is not None:
            return _apply(inverse, point)
        return _rotate(inverse, direction)

    @property
    def centre(self) -> Vec:
        points = [f.point for f in self.faces if isinstance(f, (PlaneFace, CylinderFace))]
        if not points:
            return (0.0, 0.0, 0.0)
        return _scale(tuple(sum(p[i] for p in points) for i in range(3)), 1.0 / len(points))

    def symmetry_axis(self) -> tuple[Vec, Vec] | None:
        """The axis the whole component is a solid of revolution about (every
        curved face on it, every plane square to it and bounded by circles
        about it), else None."""
        axes = [(f.point, f.direction) for f in self.faces if isinstance(f, CylinderFace)]
        axes += [f.axis for f in self.faces if isinstance(f, OtherFace) and f.axis is not None]
        if not axes or any(isinstance(f, OtherFace) and f.axis is None for f in self.faces):
            return None
        point, direction = axes[0]
        for p, d in axes[1:]:
            if not _parallel(d, direction) or _line_distance(p, point, direction) > LIN_TOL:
                return None
        for face in self.faces:
            if not isinstance(face, PlaneFace):
                continue
            if not _parallel(face.normal, direction):
                return None
            for loop in face.loops:
                radii = [_line_distance(p, point, direction) for p in loop]
                if radii and max(radii) - min(radii) > LIN_TOL:
                    return None
        return point, direction


def _line_distance(p: Vec, point: Vec, direction: Vec) -> float:
    return _norm(_cross(_sub(p, point), direction))


# ── Mates ──

@dataclass(frozen=True)
class Element:
    """What a constraint holds on one component: the axis of a cylindrical
    face or the plane of a planar face, in world and in the part's frame."""
    kind: str  # "axis" or "plane"
    face: str
    point: Vec
    direction: Vec  # the axis direction or the plane's outward normal
    own_point: Vec
    own_direction: Vec

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "face": self.face, "point": _round(self.point),
                "direction": _round(self.direction), "own_point": _round(self.own_point),
                "own_direction": _round(self.own_direction)}


@dataclass(frozen=True)
class Mate:
    kind: str  # coincidence, contact, offset, angle
    component: str  # the instance it positions
    element: Element
    other: str  # the instance it is positioned against (already placed)
    other_element: Element
    value: float | None = None  # offset in mm, angle in degrees
    orientation: str | None = None  # same / opposite (the planes' normals)
    quality: tuple = field(default=(), compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "component": self.component,
                               "element": self.element.to_dict(), "other": self.other,
                               "other_element": self.other_element.to_dict()}
        if self.value is not None:
            out["value"] = round(self.value, 6) + 0.0
        if self.orientation is not None:
            out["orientation"] = self.orientation
        return out


@dataclass
class Placement:
    """How one component is held: `ground` (fixed, the base), `mates` (the
    constraints, against components placed before it) or `fix` (fixed in
    space: no set of constraints found)."""
    component: str
    status: str  # ground, mates, fix
    mates: list[Mate] = field(default_factory=list)
    dof_left: int = 0  # freedoms the best constraints found left (fix) or 0
    spin_free: bool = False  # left free to spin about its axis of symmetry
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"component": self.component, "status": self.status,
                               "mates": [m.to_dict() for m in self.mates]}
        if self.status == "fix" and self.dof_left:
            out["dof_left"] = self.dof_left
        if self.spin_free:
            out["spin_free"] = True
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class MatePlan:
    ground: str | None
    placements: list[Placement] = field(default_factory=list)  # in application order

    def summary(self) -> str:
        mates = sum(len(p.mates) for p in self.placements)
        fixed = [p.component for p in self.placements if p.status == "fix"]
        kinds: dict[str, int] = {}
        for p in self.placements:
            for m in p.mates:
                kinds[m.kind] = kinds.get(m.kind, 0) + 1
        detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(),
                                                        key=lambda kv: KIND_ORDER[kv[0]]))
        listed = f" ({', '.join(fixed)})" if fixed else ""
        return (f"{len(self.placements)} component(s): {self.ground} grounded, {mates} "
                f"constraint(s){f' ({detail})' if detail else ''}, "
                f"{len(fixed)} fixed in space{listed}")

    def to_dict(self) -> dict[str, Any]:
        return {"ground": self.ground, "summary": self.summary(),
                "placements": [p.to_dict() for p in self.placements]}

    def placement(self, component: str) -> Placement:
        return next(p for p in self.placements if p.component == component)


# ── Overlap of two planar faces (2D, on the first face's plane) ──

def _frame(normal: Vec) -> tuple[Vec, Vec]:
    return _perpendiculars(normal)


def _project(loops, origin: Vec, u: Vec, v: Vec) -> list[list[tuple[float, float]]]:
    return [[(_dot(_sub(p, origin), u), _dot(_sub(p, origin), v)) for p in loop]
            for loop in loops]


def _inside(x: float, y: float, loops) -> bool:
    """Even-odd rule over every loop (outer boundary and holes)."""
    inside = False
    for loop in loops:
        n = len(loop)
        for i in range(n):
            (x1, y1), (x2, y2) = loop[i], loop[(i + 1) % n]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _box(loops) -> tuple[float, float, float, float] | None:
    points = [p for loop in loops for p in loop]
    if not points:
        return None
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _box_overlap(a, b) -> tuple[float, float, float, float] | None:
    if a is None or b is None:
        return None
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    if box[2] - box[0] <= LIN_TOL or box[3] - box[1] <= LIN_TOL:
        return None
    return box


def overlap_area(a: PlaneFace, b: PlaneFace) -> float:
    """The area common to two planar faces, b projected on a's plane,
    estimated on a GRID x GRID sample of their common bounding box."""
    u, v = _frame(a.normal)
    la, lb = _project(a.loops, a.point, u, v), _project(b.loops, a.point, u, v)
    box = _box_overlap(_box(la), _box(lb))
    if box is None:
        return 0.0
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) / GRID, (y1 - y0) / GRID
    hits = sum(1 for i in range(GRID) for j in range(GRID)
               if _inside(x0 + (i + 0.5) * dx, y0 + (j + 0.5) * dy, la)
               and _inside(x0 + (i + 0.5) * dx, y0 + (j + 0.5) * dy, lb))
    return hits * dx * dy


def _facing_box(a: PlaneFace, b: PlaneFace) -> bool:
    """Whether b's outline, projected on a's plane, meets a's (bounding boxes)."""
    u, v = _frame(a.normal)
    return _box_overlap(_box(_project(a.loops, a.point, u, v)),
                        _box(_project(b.loops, a.point, u, v))) is not None


# ── Candidates ──

def _element(component: Component, face: Face) -> Element:
    if isinstance(face, CylinderFace):
        kind, point, direction = "axis", face.point, face.direction
    else:
        kind, point, direction = "plane", face.point, face.normal
    return Element(kind, face.id, point, direction, component.own(point=point),
                   component.own(direction=direction))


def _span_on(face: CylinderFace, point: Vec, direction: Vec) -> tuple[float, float]:
    """The face's extent along another (parallel) axis line."""
    shift = _dot(_sub(face.point, point), direction)
    sign = 1.0 if _dot(face.direction, direction) > 0 else -1.0
    a, b = (shift + sign * face.span[0], shift + sign * face.span[1])
    return (min(a, b), max(a, b))


def candidates(moving: Component, fixed: Component) -> list[Mate]:
    """Every constraint between a face of `moving` and a face of `fixed`."""
    out: list[Mate] = []
    for a in moving.faces:
        for b in fixed.faces:
            if isinstance(a, CylinderFace) and isinstance(b, CylinderFace):
                if not _parallel(a.direction, b.direction) or \
                        _line_distance(b.point, a.point, a.direction) > LIN_TOL:
                    continue
                lo_b, hi_b = _span_on(b, a.point, a.direction)
                lo_a, hi_a = min(a.span), max(a.span)
                common = min(hi_a, hi_b) - max(lo_a, lo_b)
                if common < -LIN_TOL:  # on one line but apart: unrelated features
                    continue
                # A pin in a hole, then two holes in line, then faces end to end.
                fit = (0 if common > LIN_TOL else 1, 0 if a.hole != b.hole else 1)
                out.append(Mate("coincidence", moving.id, _element(moving, a), fixed.id,
                                _element(fixed, b),
                                quality=(*fit, -round(common, 6),
                                         round(abs(a.radius - b.radius), 6))))
            elif isinstance(a, PlaneFace) and isinstance(b, PlaneFace):
                cos = _dot(a.normal, b.normal)
                if _parallel(a.normal, b.normal):
                    gap = _dot(_sub(b.point, a.point), a.normal)
                    opposite = cos < 0
                    if opposite and abs(gap) <= LIN_TOL:
                        area = overlap_area(a, b)
                        if area > 0.0:
                            out.append(Mate("contact", moving.id, _element(moving, a), fixed.id,
                                            _element(fixed, b), quality=(-round(area, 6),)))
                            continue
                    facing = opposite and gap > LIN_TOL and _facing_box(a, b)
                    distance = round(abs(gap), 6) if abs(gap) > LIN_TOL else 0.0
                    out.append(Mate("offset", moving.id, _element(moving, a), fixed.id,
                                    _element(fixed, b), value=distance,
                                    orientation="opposite" if opposite else "same",
                                    quality=(0 if facing else 1, round(distance, 6))))
                else:
                    angle = round(math.degrees(math.acos(max(-1.0, min(1.0, cos)))), 6)
                    out.append(Mate("angle", moving.id, _element(moving, a), fixed.id,
                                    _element(fixed, b), value=angle,
                                    quality=(round(abs(angle - 90.0), 6),)))
    out.sort(key=lambda m: (KIND_ORDER[m.kind], m.quality, m.other, _face_key(m.element.face),
                            _face_key(m.other_element.face)))
    return out


def _face_key(face_id: str) -> tuple[int, str]:
    digits = "".join(c for c in face_id if c.isdigit())
    return (int(digits) if digits else 0, face_id)


# ── Degrees of freedom ──

def _rows(mate: Mate, centre: Vec, scale: float) -> list[list[float]]:
    """The conditions a constraint puts on a small motion (W = w * scale, v) of
    the moving component, about `centre`: dx = w ^ (x - centre) + v."""
    element = mate.element
    r = _sub(element.point, centre)
    if mate.kind == "coincidence":
        d = element.direction
        e1, e2 = _perpendiculars(d)
        return [list(_scale(_cross(r, e), 1.0 / scale)) + list(e) for e in (e1, e2)] + \
               [list(e) + [0.0, 0.0, 0.0] for e in (e1, e2)]
    n = element.direction
    if mate.kind in ("contact", "offset"):
        e1, e2 = _perpendiculars(n)
        return [list(_scale(_cross(r, n), 1.0 / scale)) + list(n)] + \
               [list(e) + [0.0, 0.0, 0.0] for e in (e1, e2)]
    axis = _unit(_cross(n, mate.other_element.direction))
    return [list(axis) + [0.0, 0.0, 0.0]]


def rank(rows: list[list[float]]) -> int:
    """Numerical rank (Gaussian elimination, each row normalised)."""
    work = []
    for row in rows:
        size = math.sqrt(sum(c * c for c in row))
        if size > RANK_TOL:
            work.append([c / size for c in row])
    found = 0
    for column in range(6):
        pivot = max(range(found, len(work)), key=lambda i: abs(work[i][column]), default=None)
        if pivot is None or abs(work[pivot][column]) <= RANK_TOL:
            continue
        work[found], work[pivot] = work[pivot], work[found]
        top = work[found]
        for i in range(found + 1, len(work)):
            k = work[i][column] / top[column]
            if k:
                work[i] = [a - k * b for a, b in zip(work[i], top, strict=True)]
        found += 1
    return found


def _spin_only(rows: list[list[float]], axis: tuple[Vec, Vec] | None, centre: Vec,
               scale: float) -> bool:
    """Rank 5 and the free motion is the spin about the axis of symmetry."""
    if axis is None or rank(rows) != 5:
        return False
    point, d = axis
    twist = list(_scale(d, scale)) + list(_cross(d, _sub(centre, point)))
    size = math.sqrt(sum(c * c for c in twist))
    for row in rows:
        row_size = math.sqrt(sum(c * c for c in row))
        if row_size and abs(sum(a * b for a, b in zip(row, twist, strict=True))) > \
                1e-6 * row_size * size:
            return False
    return True


def _held(mates: list[Mate], centre: Vec, scale: float, axis) -> tuple[int, bool]:
    rows = [row for mate in mates for row in _rows(mate, centre, scale)]
    r = rank(rows)
    return r, r == 6 or _spin_only(rows, axis, centre, scale)


def choose(moving: Component, pool: list[Mate], scale: float) -> tuple[list[Mate], int, bool]:
    """A minimal set of the candidates that holds the component: (the set,
    the freedoms left, whether it is held). Greedy in the candidates' order,
    each taken when it removes a freedom, then pruned."""
    centre = moving.centre
    axis = moving.symmetry_axis()
    chosen: list[Mate] = []
    current = 0
    held = False
    for mate in pool:
        r, ok = _held(chosen + [mate], centre, scale, axis)
        if r > current:
            chosen.append(mate)
            current, held = r, ok
            if held:
                break
    if held:
        for mate in reversed(list(chosen)):
            rest = [m for m in chosen if m is not mate]
            if rest and _held(rest, centre, scale, axis)[1]:
                chosen = rest
        current = _held(chosen, centre, scale, axis)[0]
    return chosen, 6 - current, held


def _pool(moving: Component, placed: list[str], by_id: dict[str, Component],
          touching: bool) -> list[Mate]:
    """The candidates against the components placed so far, best first."""
    pool = [m for other in placed for m in candidates(moving, by_id[other])
            if not touching or m.kind in ("coincidence", "contact")]
    pool.sort(key=lambda m: (KIND_ORDER[m.kind], m.quality, placed.index(m.other),
                             _face_key(m.element.face), _face_key(m.other_element.face)))
    return pool


def infer(components: list[Component], ground: str | None = None) -> MatePlan:
    """The constraints that place every component. `ground`: the component
    to fix first (default: the largest by volume, the first among equals)."""
    if not components:
        return MatePlan(None)
    by_id = {c.id: c for c in components}
    if ground is None:
        biggest = max(c.volume for c in components)
        ground = next(c.id for c in components if c.volume >= biggest * (1 - 1e-9))
    if ground not in by_id:
        raise ValueError(f"no component {ground} to ground")
    points = [f.point for c in components for f in c.faces if hasattr(f, "point")]
    scale = max([_norm(_sub(p, q)) for p in points[:1] for q in points] + [1.0])
    plan = MatePlan(ground, [Placement(ground, "ground")])
    placed = [ground]
    pending = [c.id for c in components if c.id != ground]
    while pending:
        progress = False
        # Components held by touching faces alone (coincidences, contacts)
        # first, as a designer assembles them; then those that need offsets.
        for touching in (True, False):
            for cid in pending:
                pool = _pool(by_id[cid], placed, by_id, touching)
                chosen, left, held = choose(by_id[cid], pool, scale)
                if held:
                    spin = left == 1
                    plan.placements.append(Placement(
                        cid, "mates", chosen, spin_free=spin,
                        note="free to spin about its own axis of symmetry" if spin else ""))
                    placed.append(cid)
                    pending.remove(cid)
                    progress = True
                    break
            if progress:
                break
        if not progress:
            # Fix the first one left in space, then try the others against it.
            cid = pending.pop(0)
            pool = _pool(by_id[cid], placed, by_id, False)
            _, left, _ = choose(by_id[cid], pool, scale)
            note = ("no face of it meets a placed component" if not pool else
                    f"the constraints found leave {left} degree(s) of freedom")
            plan.placements.append(Placement(cid, "fix", dof_left=left, note=note))
            placed.append(cid)
    return plan


def fix_all(components: list[Component]) -> MatePlan:
    """Every component fixed where it is (`--constraints fix`)."""
    if not components:
        return MatePlan(None)
    plan = MatePlan(components[0].id, [Placement(components[0].id, "ground")])
    plan.placements += [Placement(c.id, "fix") for c in components[1:]]
    return plan


# ── Faces from OpenCascade shapes (needs OCP) ──

def faces_of(shape: Any) -> list[Face]:
    """The faces of an OCP shape, in its own coordinates, numbered F1, F2...
    in the order OpenCascade explores them."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp, BRepGProp_Face
    from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
    from OCP.GeomAbs import (
        GeomAbs_Cone,
        GeomAbs_Cylinder,
        GeomAbs_Line,
        GeomAbs_Plane,
        GeomAbs_Sphere,
        GeomAbs_Torus,
    )
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    del BRep_Tool  # imported to fail early on an incomplete OCP

    def xyz(p) -> Vec:
        return (float(p.X()), float(p.Y()), float(p.Z()))

    def loops_of(face) -> list[tuple[Vec, ...]]:
        loops = []
        wires = TopExp_Explorer(face, TopAbs_WIRE)
        while wires.More():
            wire = TopoDS.Wire(wires.Current())
            points: list[Vec] = []
            edges = BRepTools_WireExplorer(wire, face)
            while edges.More():
                edge = edges.Current()
                curve = BRepAdaptor_Curve(edge)
                first, last = curve.FirstParameter(), curve.LastParameter()
                count = 1 if curve.GetType() == GeomAbs_Line else 32
                params = [first + (last - first) * i / count for i in range(count + 1)]
                if edge.Orientation() == TopAbs_REVERSED:
                    params.reverse()
                points += [xyz(curve.Value(t)) for t in params[:-1]]
                edges.Next()
            if len(points) >= 3:
                loops.append(tuple(points))
            wires.Next()
        return loops

    unique: list[Any] = []  # each face once, in exploration order
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        current = explorer.Current()
        if not any(current.IsSame(seen) for seen in unique):
            unique.append(current)
        explorer.Next()
    faces: list[Face] = []
    for i, found in enumerate(unique, start=1):
        face = TopoDS.Face(found)
        fid = f"F{i}"
        surface = BRepAdaptor_Surface(face, True)
        kind = surface.GetType()
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = float(props.Mass())
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        point, normal = gp_Pnt(), gp_Vec()
        BRepGProp_Face(face).Normal((u0 + u1) / 2, (v0 + v1) / 2, point, normal)
        n = _unit(xyz(normal))
        if kind == GeomAbs_Plane:
            faces.append(PlaneFace(fid, xyz(props.CentreOfMass()), n, area,
                                   tuple(loops_of(face))))
        elif kind == GeomAbs_Cylinder:
            cylinder = surface.Cylinder()
            axis = cylinder.Axis()
            origin, d = xyz(axis.Location()), _unit(xyz(axis.Direction()))
            at = xyz(point)
            radial = _sub(at, _add(origin, _scale(d, _dot(_sub(at, origin), d))))
            hole = _dot(radial, n) < 0
            ts = [_dot(_sub(p, origin), d) for loop in loops_of(face) for p in loop]
            span = (min(ts), max(ts)) if ts else (0.0, 0.0)
            faces.append(CylinderFace(fid, origin, d, float(cylinder.Radius()), hole, span, area))
        elif kind in (GeomAbs_Cone, GeomAbs_Torus):
            gp = surface.Cone() if kind == GeomAbs_Cone else surface.Torus()
            axis = gp.Axis()
            faces.append(OtherFace(fid, "cone" if kind == GeomAbs_Cone else "torus",
                                   (xyz(axis.Location()), _unit(xyz(axis.Direction())))))
        elif kind == GeomAbs_Sphere:
            faces.append(OtherFace(fid, "sphere", (xyz(surface.Sphere().Location()),
                                                   (0.0, 0.0, 1.0))))
        else:
            faces.append(OtherFace(fid, "free-form"))
    return faces


def components_from_structure(structure: Any, plan: Any = None) -> list[Component]:
    """The components of a STEP structure (step_structure.read), each
    prototype's faces read once and moved to every instance. With the
    assembly `plan`, ids and prototypes are the plan's."""
    prototypes: dict[str, list[Face]] = {}
    out = []
    for instance in structure.instances:
        if instance.shape is None:
            raise ValueError(f"{instance.id}: the STEP structure was read without shapes")
        if instance.prototype not in prototypes:
            prototypes[instance.prototype] = faces_of(instance.shape)
        out.append(Component(instance.id, instance.name,
                             [f.moved(instance.world) for f in prototypes[instance.prototype]],
                             instance.world, instance.prototype, instance.volume_mm3))
    if plan is not None:
        by_id = {c.id: c for c in out}
        out = [by_id[i.id] for i in plan.instances if i.id in by_id]
        for component, placed in zip(out, [i for i in plan.instances if i.id in by_id],
                                     strict=True):
            component.prototype = placed.prototype
    return out


def components_from_tree(tree: dict[str, Any]) -> list[Component]:
    """The components of a tree without a STEP file: each part where the
    tree puts it (its own frame is the world), from the build123d reference."""
    from catia_mcp.tree.reference import _volume, build_part
    from catia_mcp.tree.step_structure import IDENTITY

    out = []
    for part in tree.get("parts") or []:
        solid = build_part(part)
        out.append(Component(part["id"], part.get("name") or part["id"],
                             faces_of(solid.wrapped), [list(r) for r in IDENTITY], part["id"],
                             _volume(solid)))
    return out
