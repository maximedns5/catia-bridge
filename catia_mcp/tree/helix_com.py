"""Sweeps along a helix in CATIA: a GSD Helix and a Rib or Slot.

The TreeBuilder's companion for catia_mcp/tree/helix.py. A designer models a
spring or a modelled thread as:

1. the helix axis (a construction line) and its start point (a construction
   point), then a GSD Helix (HybridShapeFactory.AddNewHelix: axis, invert
   axis, start point, pitch, height, clockwise, starting angle, taper angle,
   taper outward) in the construction set, its pitch and height editable;
2. the profile sketch, swept along the helix by a Rib (a Slot to cut),
   pulled along the helix axis so the profile keeps its place about the
   axis: the screw motion build123d's Frenet sweep makes.

Nothing here is documented for R428, so each step tries its variants in
order and records which one worked:

- the helix: counter-clockwise (right-handed) first, then clockwise, each
  with the axis as given then inverted. Each is checked by measuring its
  distance to two points of the tree's helix (a quarter turn in, and its
  end): a wrong hand or a wrong way misses them by millimetres. When CATIA
  cannot measure them, the helices are tried in turn against the Rib's
  check instead;
- the Rib: AddNewRibFromRef (the profile and the helix as references), then
  AddNewRib (the objects), each pulled along the axis (PullingDirElement),
  then with CATIA's default profile control (keep angle);
- the check: the body's change against build123d's (human style), else
  against the screw motion's closed form (volume and centre of gravity), or,
  for a tapered helix, which has none, the part's final volume only.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from catia_mcp.tools.part_design import _force_solid_inwork
from catia_mcp.tree.geometry import Frame, Vec, distance
from catia_mcp.tree.helix import HelixSpec, ScrewSweep, path_helix, screw_sweep
from catia_mcp.tree.partdesign_com import PartDesignFailed

FEATURE_REL_TOL = 1e-3
# CATIA draws a helix as a spline approximation, as OpenCascade does; the
# part's volume check allows this much of each swept solid (relative).
HELIX_ALLOWANCE_REL = 5e-4
# A helix misses a point of the tree's helix by more than this: another hand
# or another way.
HELIX_POINT_TOL_ABS, HELIX_POINT_TOL_REL = 0.01, 1e-3
# (clockwise, invert axis), in the order tried: counter-clockwise about the
# axis line as drawn first (right-handed, if CATIA looks down the axis).
HELIX_WAYS = ((False, False), (True, False), (False, True), (True, True))


Check = Callable[[tuple[float, Vec]], "str | None"]


class HelixSweeps:
    def __init__(self, builder: Any) -> None:
        self.builder = builder

    def sweep(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        b = self.builder
        spec = path_helix(feature["path"])
        sketch = b.sketches[feature["sketch"]]
        closed = None if spec.tapered else screw_sweep(sketch, spec)
        operation = feature["operation"]
        entry["helix"] = {"pitch": spec.pitch, "axial_pitch": spec.axial_pitch,
                          "height": spec.height, "turns": round(spec.turns, 9),
                          "radius": spec.radius, "lefthand": spec.lefthand,
                          "cone_angle": spec.cone_angle}
        if operation == "new_body":
            (created,) = feature["creates"]
            body = b._new_body(use_main=True)
            b.bodies[created] = body
            check = self._checker(feature, created, closed)
            self._rib_into(body, sketch, spec, False, check, entry)
            return
        (target,) = feature["bodies"]
        if b._human(feature) and operation in ("join", "cut"):
            # Straight into the body, as a designer would: a Rib or a Slot.
            check = self._checker(feature, target, None)
            self._rib_into(b.bodies[target], sketch, spec, operation == "cut", check, entry)
            return
        tool = b._new_body(use_main=False)
        name = f"__helix_tool_{feature.get('id')}"
        b.bodies[name] = tool
        try:
            self._rib_into(tool, sketch, spec, False,
                           self._checker(feature, name, closed, snapshot=False), entry)
        finally:
            b.bodies.pop(name, None)
        b._combine(b.bodies[target], tool, operation, entry)

    # ── Checks ──

    def _checker(self, feature: dict[str, Any], name: str, closed: ScrewSweep | None,
                 snapshot: bool = True) -> tuple[Check, bool]:
        """The check of the body `name` after the Rib, from its state before
        (build123d's body when there is one, else the closed form), and
        whether it checks anything (a tapered helix has no closed form)."""
        b = self.builder

        def check(before: tuple[float, Vec]) -> str | None:
            if snapshot and b._human(feature):
                return b._snapshot_error(name, before)
            if closed is None:
                return None
            body = b.bodies[name]
            volume, cog = b._volume(body), b._cog(body)
            change = volume - before[0]
            if abs(change - closed.volume) > max(1e-3, FEATURE_REL_TOL * closed.volume):
                return (f"changed the body by {change:+.4f} mm3, the screw motion's closed form "
                        f"by {closed.volume:+.4f} mm3")
            centre = tuple((volume * cog[i] - before[0] * before[1][i]) / change
                           for i in range(3))
            tol = max(1e-3, FEATURE_REL_TOL * closed.volume ** (1 / 3))
            if distance(centre, closed.cog) > tol:
                return (f"added its solid around {_fmt(centre)}, the closed form around "
                        f"{_fmt(closed.cog)}")
            return None

        return check, closed is not None or (snapshot and b._human(feature))

    # ── Building ──

    def _rib_into(self, body: Any, sketch: dict[str, Any], spec: HelixSpec, cut: bool,
                  checker: tuple[Check, bool], entry: dict[str, Any]) -> Any:
        b = self.builder
        check, checked = checker
        part = b.part
        profile, _, _ = b._draw(body, sketch, Frame.from_tree(sketch["plane"]), entry,
                                constrain=b.style == "human")
        axis = b.datums.line(spec.center, spec.direction, role="Helix axis")
        tried: list[str] = []
        notes = entry.setdefault("notes", [])
        for helix, how, measured in self._helices(spec, axis, tried):
            for route, make in self._routes(cut):
                for control, steer in (("pulled along the helix axis",
                                        lambda rib: setattr(rib, "PullingDirElement", axis)),
                                       ("keep angle", lambda rib: None)):
                    made = None
                    before = (b._volume(body), b._cog(body))
                    label = f"{route}, {control}"
                    try:
                        _force_solid_inwork(part, profile)
                        part.InWorkObject = body
                        made = make(profile, helix)
                        steer(made)
                        part.UpdateObject(made)
                        error = check(before)
                    except Exception as failure:  # CATIA refused this way: the next one
                        error = str(failure)
                    if error is None:
                        allowance = HELIX_ALLOWANCE_REL * abs(b._volume(body) - before[0])
                        b.record.spline_allowance_mm3 += allowance
                        entry["catia"] += [str(helix.Name), str(made.Name)]
                        notes.append(f"{helix.Name}: {how}")
                        notes.append(f"{made.Name}: {label}")
                        if not checked:
                            notes.append(f"{made.Name}: a tapered helix has no closed form; "
                                         "only the part's final volume checks it")
                        if not measured and abs(spec.turns - round(spec.turns)) < 1e-9 \
                                and checked:
                            notes.append(f"{helix.Name}: its hand is not verified (CATIA did "
                                         "not measure it, and a whole number of turns puts "
                                         "either hand's centre of gravity on the axis)")
                        if tried:
                            notes.append("; ".join(tried))
                        entry.setdefault("helix", {})["built"] = {
                            "helix": how, "rib": label, "measured": measured}
                        return made
                    tried.append(f"{how}, {label}: {error}")
                    if made is not None:
                        with contextlib.suppress(Exception):
                            b._delete(made)
            if measured:
                break  # the helix is right: another one will not help
            with contextlib.suppress(Exception):
                b._delete(helix)
        kind = "Slot" if cut else "Rib"
        raise PartDesignFailed(f"no {kind} along a CATIA helix gave the tree's sweep ("
                               + "; ".join(tried) + ")")

    def _routes(self, cut: bool) -> list[tuple[str, Callable[[Any, Any], Any]]]:
        part = self.builder.part
        factory = part.ShapeFactory
        ref = part.CreateReferenceFromObject
        name = "Slot" if cut else "Rib"
        return [(f"AddNew{name}FromRef",
                 lambda p, h: getattr(factory, f"AddNew{name}FromRef")(ref(p), ref(h))),
                (f"AddNew{name}", lambda p, h: getattr(factory, f"AddNew{name}")(p, h))]

    def _helices(self, spec: HelixSpec, axis: Any, tried: list[str]
                 ) -> Iterator[tuple[Any, str, bool]]:
        """Each CATIA helix that may be the tree's: (helix, how, measured).
        A measured helix is the tree's; an unmeasured one is only a guess."""
        b = self.builder
        part = b.part
        start = b.datums.point(spec.start)
        quarter = spec.point(min(0.25, spec.turns / 2))
        targets = [(quarter, "a quarter turn in"), (spec.end, "its end")]
        tol = max(HELIX_POINT_TOL_ABS, HELIX_POINT_TOL_REL * spec.radius)
        taper = abs(spec.cone_angle)
        for clockwise, invert in HELIX_WAYS:
            how = (f"AddNewHelix pitch {spec.axial_pitch:g} mm, height {spec.height:g} mm, "
                   f"{'clockwise' if clockwise else 'counter-clockwise'}"
                   + (", axis inverted" if invert else "")
                   + (f", taper {spec.cone_angle:g} deg" if taper else ""))
            helix = None
            try:
                helix = part.HybridShapeFactory.AddNewHelix(
                    axis, invert, start, spec.axial_pitch, spec.height, clockwise, 0.0,
                    taper, spec.cone_angle > 0)
                b._construction_set().AppendHybridShape(helix)
                part.UpdateObject(helix)
            except Exception as error:  # the next way
                tried.append(f"{how}: {error}")
                if helix is not None:
                    with contextlib.suppress(Exception):
                        b._delete(helix)
                continue
            try:
                misses = [(self._distance(helix, b.datums.point(p)), what)
                          for p, what in targets]
            except Exception as error:  # no measure: let the Rib's check decide
                tried.append(f"{how}: not measured ({error})")
                yield helix, how + " (not measured)", False
                continue
            wrong = [f"misses {what} by {gap:.3f} mm" for gap, what in misses if gap > tol]
            if not wrong:
                yield helix, how + " (on the tree's helix)", True
                return
            tried.append(f"{how}: {', '.join(wrong)}")
            with contextlib.suppress(Exception):
                b._delete(helix)

    def _distance(self, helix: Any, point: Any) -> float:
        """The distance (mm) from the helix to a construction point."""
        b = self.builder
        return float(b.measure.min_distance(b.part.CreateReferenceFromObject(helix),
                                            point)["distance_mm"])


def _fmt(p: Vec) -> str:
    return "(" + ", ".join(f"{c:.3f}" for c in p) + ")"
