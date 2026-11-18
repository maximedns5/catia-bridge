"""Write the synthetic assemblies used by the clash tests.

Made like scripts/make_mate_fixtures.py (a STEP file, every part in its own
frame, and the feature tree of the same model in world coordinates):

- pin_interference: a plate with a 8 mm hole and a headed pin whose shank is
  9 mm: the shank interferes with the plate (pi * (4.5^2 - 4^2) * 10 mm3),
  the head rests on it;
- shaft_bushing: a bushing with a 10 mm bore, a 9.6 mm shaft through it
  (0.2 mm radial clearance, turned a quarter turn about its axis) and a
  collar on the shaft, resting on the bushing.

    python scripts/make_clash_fixtures.py [OUT_DIR]   (default tests/fixtures/assembly)
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from make_mate_fixtures import (
    _circle,
    _extrude,
    _headed,
    _part,
    _rectangle,
    _sketch,
    _write,
)


def build(out_dir: str) -> list[str]:
    from build123d import Align, Box, Compound, Cylinder, Location, export_step

    low = (Align.CENTER, Align.CENTER, Align.MIN)
    written = []

    # pin_interference: the plate in the world frame, the pin at (10, 5, 10),
    # its shank 0.5 mm larger in radius than the hole.
    plate = Box(40, 40, 10, align=low) - Cylinder(4, 10, align=low).moved(Location((10, 5, 0)))
    plate.label = "Plate"
    pin = (Cylinder(4.5, 24, align=low).moved(Location((0, 0, -20)))
           + Cylinder(7, 4, align=low)).moved(Location((10, 5, 10)))
    pin.label = "Pin"
    tree = {"version": 1, "parts": [
        _part("/PinInterference/Plate", "Plate", [
            _sketch("sketch1", (0, 0, 0), _rectangle(-20, -20, 20, 20) + [_circle("c1", 10, 5, 4)],
                    ["e1", "e2", "e3", "e4"], [["c1"]]),
            _extrude("extrude1", "sketch1", 10)]),
        _headed("/PinInterference/Pin", "Pin", 10, 5, -10, 4.5, 24, 7, 10, 4)]}
    written += _write(out_dir, "pin_interference",
                      Compound(label="PinInterference", children=[plate, pin]), tree, export_step)

    # shaft_bushing: the bushing (bore 10 mm, z 0..20), the shaft (9.6 mm,
    # z -10..30, modelled centred on its own origin), the collar on top.
    bushing = Box(30, 30, 20, align=low) - Cylinder(5, 20, align=low)
    bushing.label = "Bushing"
    shaft = Cylinder(4.8, 40).moved(Location((0, 0, 10), (0, 0, 1), 90))
    shaft.label = "Shaft"
    collar = (Cylinder(8, 5, align=low) - Cylinder(4.8, 5, align=low)).moved(Location((0, 0, 20)))
    collar.label = "Collar"
    tree = {"version": 1, "parts": [
        _part("/ShaftBushing/Bushing", "Bushing", [
            _sketch("sketch1", (0, 0, 0), _rectangle(-15, -15, 15, 15) + [_circle("c1", 0, 0, 5)],
                    ["e1", "e2", "e3", "e4"], [["c1"]]),
            _extrude("extrude1", "sketch1", 20)]),
        _part("/ShaftBushing/Shaft", "Shaft", [
            _sketch("sketch1", (0, 0, -10), [_circle("c1", 0, 0, 4.8)], ["c1"]),
            _extrude("extrude1", "sketch1", 40)]),
        _part("/ShaftBushing/Collar", "Collar", [
            _sketch("sketch1", (0, 0, 20), [_circle("c1", 0, 0, 8), _circle("c2", 0, 0, 4.8)],
                    ["c1"], [["c2"]]),
            _extrude("extrude1", "sketch1", 5)])]}
    written += _write(out_dir, "shaft_bushing",
                      Compound(label="ShaftBushing", children=[bushing, shaft, collar]), tree,
                      export_step)
    return written


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "tests", "fixtures",
                                                                "assembly")
    for written in build(target):
        print(written)
