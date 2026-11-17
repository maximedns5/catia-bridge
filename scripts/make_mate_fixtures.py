"""Write the synthetic assemblies used by the assembly-constraint tests.

Each assembly gets a STEP file (build123d: every part modelled in its own
frame, placed by a location, a part used twice stored once) and the feature
tree of the same model in world coordinates, as the app sends it:

- pin_plate: a plate with a hole, a headed pin through it;
- box_base: a box standing on a base, turned a quarter turn;
- bolted_plates: one plate placed twice (the second turned half a turn,
  on top), two headed bolts through both.

    python scripts/make_mate_fixtures.py [OUT_DIR]   (default tests/fixtures/assembly)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _rectangle(x0, y0, x1, y1, prefix="e"):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [{"type": "line", "start": list(corners[i]), "end": list(corners[(i + 1) % 4]),
             "id": f"{prefix}{i + 1}"} for i in range(4)]


def _sketch(sid, origin, entities, outer, inner=()):
    return {"type": "sketch", "id": sid, "name": sid.capitalize(),
            "plane": {"origin": list(origin), "x_dir": [1, 0, 0], "normal": [0, 0, 1]},
            "entities": entities,
            "profiles": [{"outer": list(outer), "inner": [list(loop) for loop in inner]}]}


def _extrude(eid, sketch, distance, operation="new_body"):
    feature = {"type": "extrude", "id": eid, "name": eid.capitalize(), "sketch": sketch,
               "distance": distance, "operation": operation}
    if operation == "new_body":
        feature["creates"] = ["Body1"]
    else:
        feature["bodies"] = ["Body1"]
    return feature


def _circle(cid, x, y, r):
    return {"type": "circle", "center": [x, y], "radius": r, "id": cid}


def _part(pid, name, features):
    return {"id": pid, "name": name, "bodies": ["Body1"], "features": features}


def _headed(pid, name, x, y, z_tip, shank_r, shank_len, head_r, z_head, head_h):
    """A headed pin in world coordinates: a shank from z_tip up, a head on top."""
    return _part(pid, name, [
        _sketch("sketch1", (x, y, z_tip), [_circle("c1", 0, 0, shank_r)], ["c1"]),
        _extrude("extrude1", "sketch1", shank_len),
        _sketch("sketch2", (x, y, z_head), [_circle("c1", 0, 0, head_r)], ["c1"]),
        _extrude("extrude2", "sketch2", head_h, "join")])


def build(out_dir: str) -> list[str]:
    from build123d import Align, Box, Compound, Cylinder, Location, export_step

    low = (Align.CENTER, Align.CENTER, Align.MIN)

    def pin_shape(shank_r, shank_len, head_r, head_h):
        """Modelled in its own frame: the head's underside on z = 0."""
        return (Cylinder(shank_r, shank_len, align=low).moved(Location((0, 0, -shank_len + head_h)))
                + Cylinder(head_r, head_h, align=low))

    written = []

    # pin_plate: the plate in the world frame, the pin at (10, 5, 10).
    plate = Box(40, 40, 10, align=low) - Cylinder(4, 10, align=low).moved(Location((10, 5, 0)))
    plate.label = "Plate"
    pin = pin_shape(4, 24, 7, 4).moved(Location((10, 5, 10)))
    pin.label = "Pin"
    tree = {"version": 1, "parts": [
        _part("/PinPlate/Plate", "Plate", [
            _sketch("sketch1", (0, 0, 0), _rectangle(-20, -20, 20, 20) + [_circle("c1", 10, 5, 4)],
                    ["e1", "e2", "e3", "e4"], [["c1"]]),
            _extrude("extrude1", "sketch1", 10)]),
        _headed("/PinPlate/Pin", "Pin", 10, 5, -10, 4, 24, 7, 10, 4)]}
    written += _write(out_dir, "pin_plate", Compound(label="PinPlate", children=[plate, pin]),
                      tree, export_step)

    # box_base: the base under z = 0, the box a quarter turn about Z at (15, 10, 0).
    base = Box(100, 60, 10, align=(Align.CENTER, Align.CENTER, Align.MAX))
    base.label = "Base"
    box = Box(30, 20, 15, align=low).moved(Location((15, 10, 0), (0, 0, 1), 90))
    box.label = "Box"
    tree = {"version": 1, "parts": [
        _part("/BoxBase/Base", "Base", [
            _sketch("sketch1", (0, 0, -10), _rectangle(-50, -30, 50, 30), ["e1", "e2", "e3", "e4"]),
            _extrude("extrude1", "sketch1", 10)]),
        _part("/BoxBase/Box", "Box", [
            _sketch("sketch1", (0, 0, 0), _rectangle(5, -5, 25, 25), ["e1", "e2", "e3", "e4"]),
            _extrude("extrude1", "sketch1", 15)])]}
    written += _write(out_dir, "box_base", Compound(label="BoxBase", children=[base, box]),
                      tree, export_step)

    # bolted_plates: one plate twice (the upper one turned half a turn), two bolts.
    plate = (Box(80, 40, 6, align=low)
             - Cylinder(3, 6, align=low).moved(Location((25, 0, 0)))
             - Cylinder(3, 6, align=low).moved(Location((-25, 0, 0))))
    plate.label = "Plate"
    upper = plate.moved(Location((0, 0, 6), (0, 0, 1), 180))
    upper.label = "Plate"
    bolt = pin_shape(3, 16, 5, 4)
    bolt.label = "Bolt"
    bolts = [bolt.moved(Location((x, 0, 12))) for x in (25, -25)]
    for b in bolts:
        b.label = "Bolt"

    def plate_part(pid, z):
        return _part(pid, "Plate", [
            _sketch("sketch1", (0, 0, z), _rectangle(-40, -20, 40, 20)
                    + [_circle("c1", 25, 0, 3), _circle("c2", -25, 0, 3)],
                    ["e1", "e2", "e3", "e4"], [["c1"], ["c2"]]),
            _extrude("extrude1", "sketch1", 6)])

    tree = {"version": 1, "parts": [
        plate_part("/BoltedPlates/Plate", 0), plate_part("/BoltedPlates/Plate#2", 6),
        _headed("/BoltedPlates/Bolt", "Bolt", 25, 0, 0, 3, 16, 5, 12, 4),
        _headed("/BoltedPlates/Bolt#2", "Bolt", -25, 0, 0, 3, 16, 5, 12, 4)]}
    written += _write(out_dir, "bolted_plates",
                      Compound(label="BoltedPlates", children=[plate, upper, *bolts]),
                      tree, export_step)
    return written


def _write(out_dir, name, assembly, tree, export_step) -> list[str]:
    step = os.path.join(out_dir, f"{name}.step")
    export_step(assembly, step)
    path = os.path.join(out_dir, f"{name}.tree.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tree, handle, indent=1)
        handle.write("\n")
    return [step, path]


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "tests", "fixtures", "assembly")
    for written in build(target):
        print(written)
