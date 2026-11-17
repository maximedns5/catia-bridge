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
- a point (a Hole's position): the same as a circle's centre (2).

Other profiles (slots, polygons, arcs, splines) are left free, and the
report says so. Coordinates are in the CATIA sketch's own frame (H, V).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# CatConstraintType (the Automation help's order).
DISTANCE, ON, LENGTH, HORIZONTAL, VERTICAL, RADIUS, SYMMETRY = 1, 2, 5, 10, 13, 14, 15
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
class Plan:
    constraints: list[Constraint] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)
    free: list[str] = field(default_factory=list)


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
    ref = lambda e: ("entity", e["id"])  # noqa: E731
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
