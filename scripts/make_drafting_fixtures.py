"""Write the parts the drafting tests and the live drafting run use.

Three parts a designer would draw with more than the basic views:

- d1_inclined_block: a block with an inclined face (normal along X+Z, seen
  edge on in the front view): an auxiliary view; a hole drilled down;
- d2_holes_not_in_line: a plate with a through hole, a counterbore, a
  tapped M6 hole and four countersinks, not on one line: a stepped section,
  thread and hole callouts with their ISO symbols, a pattern count;
- d3_flange_three_holes: a disc with a central bore and three bolt holes at
  120 degrees: an aligned section, a pitch circle.

Each part goes to tests/fixtures/trees/<name>.json (one part, in the world
frame). The three together, side by side, go to
tests/fixtures/assembly/drafting_parts.step and .tree.json: every part in
its own frame, placed by a translation, as the app sends a model, for the
live run on 3DEXPERIENCE (import the STEP, then --assembly adopt).

    python scripts/make_drafting_fixtures.py [ROOT]   (default: tests/fixtures)
"""

from __future__ import annotations

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

GAP_X = 120.0  # mm between the parts of the assembly


def _lines(points, prefix="e"):
    return [{"type": "line", "start": list(points[i]), "end": list(points[(i + 1) % len(points)]),
             "id": f"{prefix}{i + 1}"} for i in range(len(points))]


def _sketch(sid, plane, entities, outer, inner=()):
    return {"type": "sketch", "id": sid, "name": sid.capitalize(), "plane": plane,
            "entities": entities,
            "profiles": [{"outer": list(outer), "inner": [list(loop) for loop in inner]}]}


def _plane(origin, normal=(0, 0, 1), x_dir=(1, 0, 0)):
    return {"origin": list(origin), "x_dir": list(x_dir), "normal": list(normal)}


def _hole(hid, kind, diameter, depth, positions, thickness, **extra):
    """A hole drilled down (-Z) from each position on a top face."""
    feature = {"type": "hole", "id": hid, "name": hid.capitalize(), "kind": kind,
               "diameter": diameter, "depth": depth,
               "effective_depth": float(depth) if depth is not None else 2.0 * thickness,
               "positions": [list(p) for p in positions], "direction": [0, 0, -1],
               "frames": [[[1, 0, 0, p[0]], [0, 1, 0, p[1]], [0, 0, 1, p[2]]]
                          for p in positions],
               "bodies": ["Body1"]}
    feature.update(extra)
    return feature


def _extrude(eid, sketch, distance, operation="new_body"):
    feature = {"type": "extrude", "id": eid, "name": eid.capitalize(), "sketch": sketch,
               "distance": distance, "operation": operation}
    feature["creates" if operation == "new_body" else "bodies"] = ["Body1"]
    return feature


def inclined_block(pid: str, dx: float = 0.0) -> dict:
    """60 x 25 x 35 mm, its upper right edge cut at 45 degrees from
    (30, 15) to (10, 35) in XZ; a 8 mm hole drilled down at x = -15."""
    profile = [(-30, 0), (30, 0), (30, 15), (10, 35), (-30, 35)]
    return {"id": pid, "name": "InclinedBlock", "bodies": ["Body1"], "features": [
        _sketch("sketch1", _plane((dx, 0, 0), (0, -1, 0)), _lines(profile),
                [f"e{i}" for i in range(1, 6)]),
        _extrude("extrude1", "sketch1", 25),
        _hole("hole1", "simple", 8, None, [(dx - 15, -12.5, 35)], 35),
    ]}


def holes_plate(pid: str, dx: float = 0.0) -> dict:
    """80 x 50 x 12 mm with holes on no common line."""
    t = 12
    corners = [(dx + x, y, t) for x in (-34, 34) for y in (-19, 19)]
    return {"id": pid, "name": "HolePlate", "bodies": ["Body1"], "features": [
        _sketch("sketch1", _plane((dx, 0, 0)), _lines([(-40, -25), (40, -25), (40, 25),
                                                      (-40, 25)]),
                ["e1", "e2", "e3", "e4"]),
        _extrude("extrude1", "sketch1", t),
        _hole("hole1", "simple", 6.6, None, [(dx - 20, -12, t)], t),
        _hole("hole2", "counterbore", 6.6, None, [(dx, 12, t)], t,
              counterbore_diameter=11, counterbore_depth=6.5),
        _hole("hole3", "simple", 5, 10, [(dx + 20, -12, t)], t,
              thread={"designation": "M6", "depth": 8}),
        _hole("hole4", "countersink", 4.5, None, corners, t,
              countersink_diameter=9, countersink_angle=90),
    ]}


def flange(pid: str, dx: float = 0.0) -> dict:
    """A disc of 80 mm, 12 thick, a 20 mm bore, three 9 mm bolt holes on a
    60 mm circle at 90, 210 and 330 degrees."""
    t = 12
    bolts = [(dx + 30 * math.cos(math.radians(a)), 30 * math.sin(math.radians(a)), t)
             for a in (90, 210, 330)]
    bolts = [tuple(round(c, 9) for c in p) for p in bolts]
    return {"id": pid, "name": "Flange", "bodies": ["Body1"], "features": [
        _sketch("sketch1", _plane((dx, 0, 0)),
                [{"type": "circle", "center": [0, 0], "radius": 40, "id": "c1"}], ["c1"]),
        _extrude("extrude1", "sketch1", t),
        _hole("hole1", "simple", 20, None, [(dx, 0, t)], t),
        _hole("hole2", "simple", 9, None, bolts, t),
    ]}


PARTS = (("d1_inclined_block", inclined_block), ("d2_holes_not_in_line", holes_plate),
         ("d3_flange_three_holes", flange))


def build(root: str) -> list[str]:
    from build123d import Compound, Location, export_step

    from catia_mcp.tree.reference import build_part

    written = []
    for name, make in PARTS:
        path = os.path.join(root, "trees", f"{name}.json")
        tree = {"version": 1, "parts": [make(f"/{name.split('_', 1)[0].upper()}/"
                                             f"{make('x')['name']}")]}
        _dump(path, tree)
        written.append(path)
    parts, shapes = [], []
    for index, (_, make) in enumerate(PARTS):
        dx = index * GAP_X
        own = make("own")
        solid = build_part(own).moved(Location((dx, 0, 0)))
        solid.label = own["name"]
        shapes.append(solid)
        parts.append(make(f"/DraftingParts/{own['name']}", dx))
    step = os.path.join(root, "assembly", "drafting_parts.step")
    export_step(Compound(label="DraftingParts", children=shapes), step)
    path = os.path.join(root, "assembly", "drafting_parts.tree.json")
    _dump(path, {"version": 1, "parts": parts})
    return written + [step, path]


def _dump(path: str, tree: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tree, handle, indent=1)
        handle.write("\n")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "tests", "fixtures")
    for written in build(target):
        print(written)
