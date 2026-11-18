"""Reference geometry made as a designer would: named planes, axes and points.

A designer sketching off the origin planes first makes the plane: an offset
of an origin plane by a length (`Plane Z+20`), or a plane turned about an
axis (`Plane XY +30deg about X`). Each plane is made once and every sketch on
it reuses it. This module decides which plane a tree plane is, names it after
its role, and keeps one CATIA element per distinct plane, axis and point in
the part's construction set. The report lists them (`construction`).

Planes, in the order tried:
- an origin plane (xy, yz or zx) when the tree plane is one;
- an offset of the origin plane it is parallel to (proven live on R428);
- an angle plane (`AddNewPlaneAngle`) when the tree plane contains a
  direction of the X, Y or Z axis: the origin plane holding that axis turned
  about it (about a parallel line through the plane when the plane does not
  pass through the origin). The sense CATIA turns by is not documented: the
  sketch frame CATIA gives the first sketch on it (GetAbsoluteAxisData)
  decides; the angle's sign is changed once, then the plane is replaced by
  an equation plane. A plane wanted by a mirror, a split or an affinity
  (nothing to read it back by) is an equation plane until a sketch has
  confirmed the angle plane;
- an equation plane otherwise (proven live).

Offsets and angles are driven by a user parameter when the sketch's
`params` give the script's number for them (a contract addition, see
docs/surfaces.md): `params.offset` (mm, along the tree plane's normal) and
`params.angle` (degrees).
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field
from typing import Any

from catia_mcp.tree.geometry import Frame, Vec, add, cross, dot, scale, unit

# Origin planes and the sketch frame CATIA gives them (H, V; normal = H x V),
# as in builder.ORIGIN_PLANES.
ORIGIN_PLANES = {
    "PlaneXY": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "PlaneYZ": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "PlaneZX": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
}
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
# Below this a plane passes through the origin, a vector is along an axis.
EPS = 1e-9
# A sketch frame on its plane: normal within this, origin within this (mm).
NORMAL_TOL = 1e-6
ON_PLANE_TOL = 1e-4
# How long the construction line of an axis is drawn (mm).
AXIS_LENGTH = 100.0


def _num(x: float) -> str:
    """A number as a designer would write it in a name: 20, -7.5, 0.7071."""
    x = round(float(x), 4)
    return f"{0.0 if x == 0 else x:g}"


def _signed(x: float) -> str:
    text = _num(x)
    return text if text.startswith("-") else f"+{text}"


def _point_text(p: Vec) -> str:
    return "(" + ", ".join(_num(c) for c in p) + ")"


def _normal_of(attr: str) -> Vec:
    h, v = ORIGIN_PLANES[attr]
    return cross(h, v)


def _axis_letter(direction: Vec) -> str | None:
    for letter, axis in AXES.items():
        along = dot(direction, axis)
        if abs(abs(along) - 1) < EPS:
            return letter if along > 0 else f"-{letter}"
    return None


def canonical(normal: Vec) -> Vec:
    """The normal with its first non-zero component positive: a plane and
    its reverse share one CATIA plane (a sketch on the reverse is padded the
    other way, as on the origin planes)."""
    for c in normal:
        if abs(c) > EPS:
            return normal if c > 0 else scale(normal, -1.0)
    return normal


def plane_key(frame: Frame) -> tuple[float, ...]:
    n = canonical(frame.normal)
    return (*(round(c, 9) for c in n), round(dot(n, frame.origin), 7))


@dataclass
class PlaneSpec:
    """How a tree plane is made in CATIA."""

    kind: str  # "origin", "offset", "angle" or "equation"
    name: str
    base: str | None = None  # origin plane attribute (offset, angle)
    offset: float = 0.0  # along the base plane's normal (offset)
    axis: str | None = None  # "X", "Y" or "Z" (angle)
    through: Vec = (0.0, 0.0, 0.0)  # a point of the turning axis (angle)
    angle: float = 0.0  # degrees, right-handed about the axis (angle)
    normal: Vec = (0.0, 0.0, 1.0)  # the tree plane's normal
    distance: float = 0.0  # dot(normal, any point of the plane)

    def definition(self) -> str:
        if self.kind == "origin":
            return f"origin plane {self.base}"
        if self.kind == "offset":
            return f"{self.base} offset by {_num(self.offset)} mm"
        if self.kind == "angle":
            where = "" if self.through == (0.0, 0.0, 0.0) else f" through {_point_text(self.through)}"
            return f"{self.base} turned {_num(self.angle)} deg about the {self.axis} axis{where}"
        a, b, c = self.normal
        return f"equation {_num(a)} x + {_num(b)} y + {_num(c)} z = {_num(self.distance)}"


def _equation_name(normal: Vec, d: float) -> str:
    terms = [f"{_num(c)}{axis}" for c, axis in zip(normal, "xyz") if abs(round(c, 4)) > 0]
    text = terms[0] + "".join(t if t.startswith("-") else f"+{t}" for t in terms[1:])
    return f"Plane {text}={_num(d)}"


def classify(frame: Frame) -> PlaneSpec:
    """The plane a designer would make for this tree plane."""
    n = frame.normal
    d = dot(n, frame.origin)
    for attr in ORIGIN_PLANES:
        m = _normal_of(attr)
        along = dot(n, m)
        if abs(abs(along) - 1) < EPS:
            offset = dot(frame.origin, m)
            letter = _axis_letter(m)
            if abs(offset) < EPS:
                return PlaneSpec("origin", attr, base=attr, normal=n, distance=d)
            return PlaneSpec("offset", f"Plane {letter}{_signed(offset)}", base=attr,
                             offset=offset, normal=n, distance=d)
    best: PlaneSpec | None = None
    for letter, a in AXES.items():
        if abs(dot(n, a)) > EPS:
            continue
        for attr in ORIGIN_PLANES:
            m = _normal_of(attr)
            if abs(dot(m, a)) > EPS:
                continue  # this origin plane does not hold the axis
            # Turning m about a by theta gives m cos + (a x m) sin: = +-n.
            theta = math.degrees(math.atan2(dot(cross(a, m), n), dot(m, n)))
            if theta > 90:
                theta -= 180
            elif theta <= -90:
                theta += 180
            if best is not None and abs(theta) >= abs(best.angle) - 1e-12:
                continue
            through = scale(n, d) if abs(d) > EPS else (0.0, 0.0, 0.0)
            where = "" if abs(d) <= EPS else f" through {_point_text(through)}"
            best = PlaneSpec("angle", f"Plane {attr[5:]} {_signed(theta)}deg about {letter}{where}",
                             base=attr, axis=letter, through=through, angle=theta, normal=n,
                             distance=d)
    if best is not None:
        return best
    return PlaneSpec("equation", _equation_name(n, d), normal=n, distance=d)


def frame_fits(frame: Frame, sketch_frame: tuple[Vec, Vec, Vec]) -> bool:
    """True when a CATIA sketch frame (origin, H, V) lies on the tree plane."""
    origin, h, v = sketch_frame
    normal = cross(h, v)
    if abs(abs(dot(unit(normal), frame.normal)) - 1) > NORMAL_TOL:
        return False
    return abs(dot(add(origin, scale(frame.origin, -1)), frame.normal)) <= ON_PLANE_TOL


@dataclass
class PlaneEntry:
    spec: PlaneSpec
    obj: Any  # the CATIA plane (None for an origin plane)
    row: dict[str, Any]
    verified: bool = False
    tries: int = 0
    pending: list[dict[str, Any]] = field(default_factory=list)  # sketches to drive from
    driven: bool = False


class Datums:
    """The construction elements of one part, made once each."""

    def __init__(self, builder: Any) -> None:
        self.builder = builder
        self.part = builder.part
        self.planes: dict[tuple[float, ...], PlaneEntry] = {}
        # Rounded coordinates -> (CATIA element, report row).
        self.points: dict[tuple[float, ...], tuple[Any, dict[str, Any]]] = {}
        self.lines: dict[tuple[float, ...], tuple[Any, dict[str, Any]]] = {}
        self.rows: list[dict[str, Any]] = []  # the report's `construction`
        self.names: set[str] = set()
        self.last: PlaneEntry | None = None

    # ── Report ──

    def _row(self, kind: str, name: str, definition: str) -> dict[str, Any]:
        row = {"type": kind, "name": name, "definition": definition, "used_by": []}
        self.rows.append(row)
        return row

    def _use(self, row: dict[str, Any]) -> None:
        feature = getattr(self.builder, "feature_id", None)
        if feature is not None and feature not in row["used_by"]:
            row["used_by"].append(feature)

    def _name(self, obj: Any, wanted: str) -> str:
        """Give a CATIA element a unique readable name; keep CATIA's own if
        it refuses."""
        name, n = wanted, 2
        while name in self.names:
            name, n = f"{wanted} ({n})", n + 1
        self.names.add(name)
        with contextlib.suppress(Exception):
            obj.Name = name
        return name

    def finish(self, record: Any) -> None:
        """Keep a plane's parameter only if the part kept its parameters."""
        kept = {p["name"] for p in getattr(record, "parameters", None) or []}
        for row in self.rows:
            if row.get("parameter") and row["parameter"] not in kept:
                row.pop("parameter")

    # ── Elements ──

    def _append(self, shape: Any) -> None:
        self.builder._construction_set().AppendHybridShape(shape)
        self.part.UpdateObject(shape)

    def point(self, at: Vec) -> Any:
        """A reference to the 3D point at `at`, made once."""
        key = tuple(round(float(c), 7) for c in at)
        if key not in self.points:
            at = tuple(float(c) for c in at)
            shape = self.part.HybridShapeFactory.AddNewPointCoord(*at)
            name = self._name(shape, f"Point {_point_text(at)}")
            self._append(shape)
            self.points[key] = (shape, self._row("point", name, f"coordinates {_point_text(at)}"))
        shape, row = self.points[key]
        self._use(row)
        return self.part.CreateReferenceFromObject(shape)

    def line(self, origin: Vec, direction: Vec, role: str = "Axis") -> Any:
        """A reference to the line through `origin` along `direction`, made
        once (two points and a line between them, proven live)."""
        d = unit(direction)
        key = tuple(round(c, 7) for c in (*origin, *d))
        if key not in self.lines:
            ends = [self.point(origin), self.point(add(origin, scale(d, AXIS_LENGTH)))]
            shape = self.part.HybridShapeFactory.AddNewLinePtPt(*ends)
            letter = _axis_letter(d) or _point_text(d)
            at_origin = all(abs(c) < EPS for c in origin)
            wanted = f"{role} {letter}" + ("" if at_origin else f" through {_point_text(origin)}")
            name = self._name(shape, wanted)
            self._append(shape)
            self.lines[key] = (shape, self._row(
                "axis", name, f"through {_point_text(origin)} along {_point_text(d)}"))
        shape, row = self.lines[key]
        self._use(row)
        return self.part.CreateReferenceFromObject(shape)

    # ── Planes ──

    def plane(self, frame: Frame, sketch: dict[str, Any] | None = None
              ) -> tuple[Any, tuple[Vec, Vec, Vec] | None]:
        """A plane reference for the tree plane, with the sketch frame CATIA
        is known to give it (None when it must be read back). `sketch` is the
        tree sketch drawn on it: its params can drive the plane, and only a
        sketch can confirm an angle plane."""
        part = self.part
        spec = classify(frame)
        if spec.kind == "origin":
            h, v = ORIGIN_PLANES[spec.base]
            self.last = None
            return part.CreateReferenceFromObject(getattr(part.OriginElements, spec.base)), \
                ((0.0, 0.0, 0.0), h, v)
        key = plane_key(frame)
        entry = self.planes.get(key)
        if entry is None:
            if spec.kind == "angle" and sketch is None:
                spec = PlaneSpec("equation", _equation_name(spec.normal, spec.distance),
                                 normal=spec.normal, distance=spec.distance)
            entry = self._make(spec, frame)
            self.planes[key] = entry
        self._use(entry.row)
        if sketch is not None and not entry.driven:
            entry.pending.append(sketch)
            if entry.spec.kind != "angle" or entry.verified:
                self._drive(entry)
        self.last = entry
        return part.CreateReferenceFromObject(entry.obj), None

    def _make(self, spec: PlaneSpec, frame: Frame) -> PlaneEntry:
        factory = self.part.HybridShapeFactory
        notes: list[str] = []
        if spec.kind == "angle":
            try:
                axis_ref = self.line(spec.through, AXES[spec.axis])
                base = self.part.CreateReferenceFromObject(
                    getattr(self.part.OriginElements, spec.base))
                shape = factory.AddNewPlaneAngle(base, axis_ref, spec.angle, False)
                name = self._name(shape, spec.name)
                self._append(shape)
                return PlaneEntry(spec, shape, self._row("plane", name, spec.definition()))
            except Exception as error:  # this CATIA has no angle plane: an equation one
                notes.append(f"angle plane refused ({error}): equation plane instead")
                spec = PlaneSpec("equation", _equation_name(spec.normal, spec.distance),
                                 normal=spec.normal, distance=spec.distance)
        if spec.kind == "offset":
            base = self.part.CreateReferenceFromObject(getattr(self.part.OriginElements, spec.base))
            shape = factory.AddNewPlaneOffset(base, spec.offset, False)
        else:
            # As before this module: the first requester's normal and distance.
            a, b, c = frame.normal
            shape = factory.AddNewPlaneEquation(a, b, c, dot(frame.normal, frame.origin))
        name = self._name(shape, spec.name)
        self._append(shape)
        row = self._row("plane", name, spec.definition())
        if notes:
            row["notes"] = notes
        return PlaneEntry(spec, shape, row, verified=True)

    def check(self, frame: Frame, sketch_frame: tuple[Vec, Vec, Vec]) -> bool:
        """The frame CATIA gave a sketch on the last plane: True when the
        sketch can stay. False when an unconfirmed angle plane is not where
        the tree's plane is: the caller deletes the sketch, calls retry() and
        draws it again."""
        entry = self.last
        if entry is None or entry.verified:
            return True
        if frame_fits(frame, sketch_frame):
            entry.verified = True
            entry.row["verified"] = True
            self._drive(entry)
            return True
        return False

    def retry(self, frame: Frame) -> None:
        """Change the last plane after check() refused it: the angle on the
        other side once, then an equation plane."""
        entry = self.last
        entry.tries += 1
        notes = entry.row.setdefault("notes", [])
        if entry.tries == 1:
            entry.spec.angle = -entry.spec.angle
            self._set_angle(entry.obj, entry.spec.angle)
            notes.append("CATIA turned the other way: angle sign changed")
            return
        old = entry.obj
        with contextlib.suppress(Exception):
            self.builder._delete(old)
        self.names.discard(entry.row["name"])
        entry.spec = PlaneSpec("equation", _equation_name(entry.spec.normal, entry.spec.distance),
                               normal=entry.spec.normal, distance=entry.spec.distance)
        a, b, c = frame.normal
        entry.obj = self.part.HybridShapeFactory.AddNewPlaneEquation(
            a, b, c, dot(frame.normal, frame.origin))
        entry.row["name"] = self._name(entry.obj, entry.spec.name)
        entry.row["definition"] = entry.spec.definition()
        self._append(entry.obj)
        notes.append("neither angle sense gave the tree's plane: equation plane instead")
        entry.verified = True
        entry.row["verified"] = True

    def _set_angle(self, plane: Any, value: float) -> None:
        try:
            plane.Angle.Value = value
        except Exception:  # an Angle given as a plain number
            plane.Angle = value
        self.part.UpdateObject(plane)

    def _drive(self, entry: PlaneEntry) -> None:
        """A user parameter on the plane's offset or angle when a sketch on it
        gives the script's number for it (and that number is the plane's)."""
        from catia_mcp.tree.parameters import spec_for

        plan = getattr(self.builder, "plan", None)
        while entry.pending and not entry.driven and plan is not None:
            sketch = entry.pending.pop(0)
            spec = entry.spec
            if spec.kind == "offset":
                key, have = "offset", spec.offset
                # The tree's offset runs along its own normal.
                along = dot(Frame.from_tree(sketch["plane"]).normal, _normal_of(spec.base))
                target = lambda obj=entry.obj: obj.Offset
            elif spec.kind == "angle":
                key, have, along = "angle", spec.angle, 1.0
                target = lambda obj=entry.obj: obj.Angle
            else:
                return
            script = spec_for(sketch, key)
            if script is None:
                continue
            value = float(script.get("factor", 1.0)) * (
                float(script.get("slope", 1.0)) * float(script["value"])
                + float(script.get("intercept", 0.0)))
            if not value:
                continue
            ratio = have / value
            if key == "offset":
                ratio = along if abs(value * along - have) < 1e-6 else 0.0
            elif abs(abs(ratio) - 1) > 1e-6:
                ratio = 0.0
            if not ratio:
                continue  # the number is not this plane's (a moved sketch)
            plan.drive(sketch, key, target, round(ratio))
            parameter = plan.by_key.get((str(sketch.get("id")), key))
            if parameter is not None:
                entry.row["parameter"] = parameter.name
            entry.driven = True
