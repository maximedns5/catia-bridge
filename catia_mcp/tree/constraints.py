"""Sketch constraints a designer would put on a profile.

The builder draws each sketch exactly, then constrains the shapes it
recognises, so that a dimension can be edited in CATIA where a designer would
look for it. Every constraint is made on the geometry as drawn, so it holds
already and moves nothing; dimensional constraints take the drawn value.

Recognised, each fully constrained (its degrees of freedom counted):
- an axis-aligned rectangle (4 connected lines): horizontal x2, vertical x2,
  the width and the height, and its position: symmetric about the sketch's
  axes when centred on them, else its distance to them (8 = 4 points x 2);
- a circle: its radius and its centre, on the origin or an axis when it is
  there, else its distance to the axes (3);
- a point (a Hole's position): the same as a circle's centre (2);
- a slot (two lines and two arcs, tangent where they meet, along H or V):
  4 tangencies, each arc's radius, the lines' length, and its position:
  the arc centres symmetric about the axis across the slot when it is
  centred along it, else the slot along its axis and the first centre's
  distance to the axes (10);
- a regular polygon: its vertices on a construction circle, the circle's
  radius and centre, one side horizontal or vertical when one is, and the
  sides driven by formulas from the radius (side = 2 R sin(180 deg / n)),
  so editing the radius keeps it regular (2n + 3).

Other profiles (irregular polygons, open arcs, splines) are left free, and
the report says so. Coordinates are in the CATIA sketch's own frame (H, V).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# CatConstraintType (the Automation help's order).
DISTANCE, ON, TANGENCY, LENGTH, HORIZONTAL, VERTICAL, RADIUS, SYMMETRY = \
    1, 2, 4, 5, 10, 13, 14, 15
DIMENSIONAL = {DISTANCE, LENGTH, RADIUS}
TOL = 1e-6

# A reference is ("entity", id), ("start", id), ("end", id), ("centre", id),
# ("point", id) or ("axis", "H" | "V" | "O").
Ref = tuple[str, str]


@dataclass
class Constraint:
    type: int
    refs: tuple[Ref, ...]
    name: str = ""  # what the value means: "width", "radius", "x"...
    value: float | None = None  # mm, for dimensional constraints

    @property
    def dimensional(self) -> bool:
        return self.type in DIMENSIONAL


@dataclass
class Construction:
    """A construction circle the plan constrains points onto."""

    id: str
    center: tuple[float, float]
    radius: float


@dataclass
class Formula:
    """Drive constraint `target`'s value from others: `expression` names
    them as {0}, {1}... in the order of `sources` (indexes in the plan)."""

    target: int
    expression: str
    sources: tuple[int, ...]


@dataclass
class Plan:
    constraints: list[Constraint] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)
    free: list[str] = field(default_factory=list)
    construction: list[Construction] = field(default_factory=list)
    formulas: list[Formula] = field(default_factory=list)


def plan(loops: list[list[dict[str, Any]]]) -> Plan:
    """Constraints for each loop of local entities: lines {"type", "id",
    "start", "end"}, circles {"type", "id", "center", "radius"}, points
    {"type": "point", "id", "at"}."""
    out = Plan()
    for loop in loops:
        kinds = [e["type"] for e in loop]
        if kinds == ["circle"]:
            circle = loop[0]
            out.constraints.append(Constraint(RADIUS, (("entity", circle["id"]),), "radius",
                                              float(circle["radius"])))
            out.constraints += _position(("centre", circle["id"]), circle["center"])
            out.shapes.append("circle")
        elif kinds == ["point"]:
            out.constraints += _position(("point", loop[0]["id"]), loop[0]["at"])
            out.shapes.append("point")
        elif kinds == ["line"] * 4 and (rectangle := _rectangle(loop)) is not None:
            out.constraints += rectangle
            out.shapes.append("rectangle")
        elif sorted(kinds) == ["arc", "arc", "line", "line"] and (slot := _slot(loop)) is not None:
            out.constraints += slot
            out.shapes.append("slot")
        elif len(loop) >= 3 and set(kinds) == {"line"} and _polygon(loop, out):
            out.shapes.append(f"regular polygon ({len(loop)} sides)")
        else:
            out.free.append(f"{len(loop)} curve(s): {', '.join(sorted(set(kinds)))}")
    return out


def _position(ref: Ref, at) -> list[Constraint]:
    """A point fixed relative to the sketch axes: on the origin, on an axis,
    or at a distance from each."""
    u, v = float(at[0]), float(at[1])
    if abs(u) < TOL and abs(v) < TOL:
        return [Constraint(ON, (ref, ("axis", "O")), "on the origin")]
    out = []
    out.append(Constraint(ON, (ref, ("axis", "V")), "on the V axis") if abs(u) < TOL
               else Constraint(DISTANCE, (ref, ("axis", "V")), "x", abs(u)))
    out.append(Constraint(ON, (ref, ("axis", "H")), "on the H axis") if abs(v) < TOL
               else Constraint(DISTANCE, (ref, ("axis", "H")), "y", abs(v)))
    return out


def _rectangle(lines: list[dict[str, Any]]) -> list[Constraint] | None:
    horizontal = [e for e in lines if abs(e["start"][1] - e["end"][1]) < TOL]
    vertical = [e for e in lines if abs(e["start"][0] - e["end"][0]) < TOL]
    if len(horizontal) != 2 or len(vertical) != 2:
        return None
    bottom, top = sorted(horizontal, key=lambda e: e["start"][1])
    left, right = sorted(vertical, key=lambda e: e["start"][0])
    width = abs(bottom["end"][0] - bottom["start"][0])
    height = abs(left["end"][1] - left["start"][1])
    if width < TOL or height < TOL:
        return None
    ref = lambda e: ("entity", e["id"])
    out = [Constraint(HORIZONTAL, (ref(bottom),)), Constraint(HORIZONTAL, (ref(top),)),
           Constraint(VERTICAL, (ref(left),)), Constraint(VERTICAL, (ref(right),)),
           Constraint(LENGTH, (ref(bottom),), "width", width),
           Constraint(LENGTH, (ref(left),), "height", height)]
    u_left, v_bottom = left["start"][0], bottom["start"][1]
    for low, high, axis, offset, name in ((left, right, "V", u_left + width / 2, "x"),
                                          (bottom, top, "H", v_bottom + height / 2, "y")):
        edge = u_left if axis == "V" else v_bottom
        if abs(offset) < TOL:  # centred on that axis
            out.append(Constraint(SYMMETRY, (ref(low), ref(high), ("axis", axis)),
                                  f"symmetric about {axis}"))
        elif abs(edge) < TOL:
            out.append(Constraint(ON, (ref(low), ("axis", axis)), f"on the {axis} axis"))
        else:
            out.append(Constraint(DISTANCE, (ref(low), ("axis", axis)), name, abs(edge)))
    return out


def _close(a, b) -> bool:
    return abs(a[0] - b[0]) < TOL and abs(a[1] - b[1]) < TOL


def _slot(loop: list[dict[str, Any]]) -> list[Constraint] | None:
    """Two lines and two arcs, each line tangent to the arcs it meets."""
    lines = [e for e in loop if e["type"] == "line"]
    arcs = [e for e in loop if e["type"] == "arc"]
    tangencies = []
    for line in lines:
        du, dv = line["end"][0] - line["start"][0], line["end"][1] - line["start"][1]
        for end in (line["start"], line["end"]):
            arc = next((a for a in arcs if _close(a["start"], end) or _close(a["end"], end)), None)
            if arc is None:
                return None
            ru, rv = end[0] - arc["center"][0], end[1] - arc["center"][1]
            if abs(ru * du + rv * dv) > TOL * max(1.0, abs(du) + abs(dv)) * arc["radius"]:
                return None  # not tangent
            tangencies.append(Constraint(TANGENCY, (("entity", arc["id"]), ("entity", line["id"]))))
    first = lines[0]
    horizontal = abs(first["start"][1] - first["end"][1]) < TOL
    vertical = abs(first["start"][0] - first["end"][0]) < TOL
    if not (horizontal or vertical):
        return None  # a turned slot: its angle would need an angle constraint
    along = 0 if horizontal else 1  # the coordinate along the slot
    c1, c2 = sorted(arcs, key=lambda a: a["center"][along])
    out = tangencies + [Constraint(RADIUS, (("entity", c1["id"]),), "radius", c1["radius"]),
                        Constraint(RADIUS, (("entity", c2["id"]),), "radius", c2["radius"]),
                        Constraint(LENGTH, (("entity", first["id"]),), "length",
                                   abs(first["end"][along] - first["start"][along]))]
    centre1, centre2 = ("centre", c1["id"]), ("centre", c2["id"])
    middle = (c1["center"][along] + c2["center"][along]) / 2
    across_axis, along_axis = ("V", "H") if horizontal else ("H", "V")
    if abs(middle) < TOL:  # centred along the slot: its centres mirror each other
        out.append(Constraint(SYMMETRY, (centre1, centre2, ("axis", across_axis)),
                              f"symmetric about {across_axis}"))
    else:
        out.append(Constraint(HORIZONTAL if horizontal else VERTICAL, (("entity", first["id"]),)))
        out.append(Constraint(DISTANCE, (centre1, ("axis", across_axis)),
                              "x" if horizontal else "y", abs(c1["center"][along])))
    offset = c1["center"][1 - along]
    out.append(Constraint(ON, (centre1, ("axis", along_axis)), f"on the {along_axis} axis")
               if abs(offset) < TOL else
               Constraint(DISTANCE, (centre1, ("axis", along_axis)),
                          "y" if horizontal else "x", abs(offset)))
    return out


def _polygon(loop: list[dict[str, Any]], out: Plan) -> bool:
    """A regular polygon: equal sides, vertices on one circle."""
    n = len(loop)
    vertices = [e["start"] for e in loop]
    if any(not _close(loop[i]["end"], loop[(i + 1) % n]["start"]) for i in range(n)):
        return False
    side = _length(loop[0])
    centre = (sum(v[0] for v in vertices) / n, sum(v[1] for v in vertices) / n)
    radius = _dist(vertices[0], centre)
    if any(abs(_length(e) - side) > TOL * max(1.0, side) for e in loop) or \
            any(abs(_dist(v, centre) - radius) > TOL * max(1.0, radius) for v in vertices):
        return False
    key = f"circle_{loop[0]['id']}"
    out.construction.append(Construction(key, centre, radius))
    base = len(out.constraints)
    cs = [Constraint(ON, (("start", e["id"]), ("entity", key))) for e in loop]
    cs.append(Constraint(RADIUS, (("entity", key),), "radius", radius))
    radius_index = base + len(cs) - 1
    cs += _position(("centre", key), centre)
    straight = next((e for e in loop if abs(e["start"][1] - e["end"][1]) < TOL), None)
    upright = next((e for e in loop if abs(e["start"][0] - e["end"][0]) < TOL), None)
    if straight is not None:
        cs.append(Constraint(HORIZONTAL, (("entity", straight["id"]),)))
    elif upright is not None:
        cs.append(Constraint(VERTICAL, (("entity", upright["id"]),)))
    else:
        out.free.append(f"regular polygon ({n} sides): its turn about its centre")
    first_side = base + len(cs)
    for i, e in enumerate(loop[:-1]):
        cs.append(Constraint(LENGTH, (("entity", e["id"]),), f"side {i + 1}", side))
        out.formulas.append(
            Formula(first_side + i, f"2*{{0}}*sin(180deg/{n})", (radius_index,)) if i == 0
            else Formula(first_side + i, "{0}", (first_side,)))
    out.constraints += cs
    return True


def _length(line) -> float:
    return _dist(line["start"], line["end"])


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
