"""Write the STEP files of the STEP fallback (development side, needs build123d).

For each feature of a tree the replay has no native rule for (a
`base_feature`, or an unsupported feature that modifies one body, such as a
fillet), the reference (catia_mcp/tree/reference.py) builds the bodies as
they are just before it, and a resolver gives the body just after it. Then:

- a new-body `base_feature` is written whole, as `<feature id>.step`;
- any other feature is written as a "delta" against its body:
  `<feature id>.add.step` = after - before (the material it adds) and
  `<feature id>.remove.step` = before - after (the material it removes).
  Adding then removing them gives `after` exactly, so the native features
  before and after it stay editable in CATIA.

Files go to `<step dir>/<part folder>/`, the layout `--step-dir` reads
(catia_mcp/tree/step_source.py). They are AP214 in mm (what V5's STEP
interface ST1 reads) with a fixed header timestamp, so the same model gives
the same bytes. Each solid is checked first: valid, and edge tolerances
under 1e-3 mm (CATIA works at 1e-3 mm; build123d's own solids sit at 1e-7).

Command line (the model's final solid as one STEP file; a single part, or
one child per part labelled with the part's name):

    python -m catia_mcp.tree.step_fallback TREE.json MODEL.step --step-dir DIR

The final solid gives the body after a feature only when nothing later in
the part touches that body and it is the part's only body; otherwise call
`write_fallback` with `after_solids` for each feature.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from build123d import Unit, export_step, import_step

from catia_mcp.tree.preflight import _live_bodies
from catia_mcp.tree.reference import ReferenceError, _combine, build_part, expected
from catia_mcp.tree.step_source import StepSource, part_folder

FIXED_TIMESTAMP = "2000-01-01T00:00:00"
# Delta solids smaller than this are numerical noise, not material (mm3).
MIN_DELTA_MM3 = 1e-6
# Edge tolerances above this are likely to fail CATIA's import (mm).
MAX_TOLERANCE_MM = 1e-3

Resolver = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any]


def max_tolerance(shape: Any) -> float:
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance

    return float(ShapeAnalysis_ShapeTolerance().Tolerance(shape.wrapped, 1))


def write_step(shape: Any, path: str, label: str) -> str:
    """Export one solid as AP214 in mm with a fixed timestamp, after checking
    it is valid and its tolerances are tight enough for CATIA."""
    if not shape.is_valid:
        raise ReferenceError(f"{label}: the solid is not valid, CATIA would refuse it")
    tolerance = max_tolerance(shape)
    if tolerance > MAX_TOLERANCE_MM:
        raise ReferenceError(f"{label}: an edge tolerance of {tolerance:.2e} mm is above "
                             f"{MAX_TOLERANCE_MM:g} mm, CATIA would likely fail to sew it")
    shape = copy.copy(shape)  # export_step fails on a child of an assembly
    shape.label = label
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        written = export_step(shape, path, unit=Unit.MM, timestamp=FIXED_TIMESTAMP)
    except TypeError:  # build123d without the timestamp argument
        written = export_step(shape, path, unit=Unit.MM)
    if not written:
        raise ReferenceError(f"{label}: build123d could not write {path}")
    return path


def _target(feature: dict[str, Any]) -> str:
    bodies = feature.get("bodies") or []
    if len(bodies) != 1:
        raise ReferenceError(f"'{feature.get('id')}' must modify exactly one body to be "
                             "replaced by STEP deltas")
    return bodies[0]


def _creates_new_body(feature: dict[str, Any]) -> bool:
    return feature.get("type") == "base_feature" and \
        (feature.get("operation") or "new_body") == "new_body"


def write_fallback(tree: dict[str, Any], step_dir: str, resolve: Resolver) -> list[dict[str, Any]]:
    """Write the STEP files for every feature the reference has no rule
    for. `resolve(part, feature, bodies)` gives the solid of the feature's
    body just after it (for a new-body base_feature, the solid it creates).
    Returns one row per file written."""
    written: list[dict[str, Any]] = []

    def external(part: dict[str, Any], feature: dict[str, Any], bodies: dict[str, Any]) -> None:
        fid = feature["id"]
        folder = os.path.join(step_dir, part_folder(part["id"]))
        after = resolve(part, feature, bodies)
        if _creates_new_body(feature):
            path = write_step(after, os.path.join(folder, f"{fid}.step"), fid)
            written.append({"part": part["id"], "feature": fid, "role": "solid", "file": path,
                            "volume_mm3": float(after.volume)})
            bodies[feature["creates"][0]] = after
            return
        target = _target(feature)
        before = bodies[target]
        for role, delta in (("add", after - before), ("remove", before - after)):
            if delta is None or float(delta.volume) <= MIN_DELTA_MM3:
                continue
            path = write_step(delta, os.path.join(folder, f"{fid}.{role}.step"), f"{fid}_{role}")
            written.append({"part": part["id"], "feature": fid, "role": role, "file": path,
                            "volume_mm3": float(delta.volume)})
        bodies[target] = after

    for part in tree["parts"]:
        build_part(part, external)
    return written


def after_solids(solids: dict[tuple[str, str], Any]) -> Resolver:
    """A resolver from given solids, keyed by (part id, feature id)."""

    def resolve(part, feature, bodies):
        key = (part["id"], feature["id"])
        if key not in solids:
            raise ReferenceError(f"no solid given for {key[0]} / {key[1]}")
        return solids[key]

    return resolve


def final_solids(finals: dict[str, Any]) -> Resolver:
    """A resolver from each part's final solid. Exact only for a feature
    whose body nothing later in the part touches and that is the part's only
    final body; anything else is refused."""

    def resolve(part, feature, bodies):
        features = part.get("features") or []
        index = next(i for i, f in enumerate(features) if f is feature)
        body = feature["creates"][0] if _creates_new_body(feature) else _target(feature)
        later = features[index + 1:]
        touched = [f["id"] for f in later
                   if body in (f.get("bodies") or []) + (f.get("targets") or [])
                   + (f.get("tools") or [])]
        final_bodies = part.get("bodies") or _live_bodies(features)
        if touched or final_bodies != [body]:
            raise ReferenceError(
                f"{part['id']} / {feature['id']}: the final solid is not the body after this "
                f"feature (later features {touched or 'none'}, final bodies {final_bodies}); "
                "give its solid with after_solids")
        return finals[part["id"]]

    return resolve


def finals_from_step(tree: dict[str, Any], path: str) -> dict[str, Any]:
    """Each part's final solid from the model's STEP file: the whole file
    for a single part, else the child labelled with the part's name."""
    model = import_step(path)
    parts = tree["parts"]
    if len(parts) == 1:
        return {parts[0]["id"]: model}
    found: dict[str, Any] = {}
    children = list(model.children) or [model]
    for part in parts:
        name = part.get("name") or part["id"].rsplit("/", 1)[-1]
        matches = [c for c in children if c.label == name]
        if len(matches) != 1:
            raise ReferenceError(f"{len(matches)} solids labelled '{name}' in {path}")
        found[part["id"]] = matches[0]
    return found


def read_fallback(step_dir: str) -> Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None]:
    """The reference's `external` hook that applies the files of a step dir
    the way the CATIA builder does: a solid with the feature's operation,
    then the add and remove deltas."""
    source = StepSource(step_dir)

    def external(part: dict[str, Any], feature: dict[str, Any], bodies: dict[str, Any]) -> None:
        files = source.files(part["id"], feature)
        if not files:
            raise ReferenceError(f"no STEP file for {part['id']} / {feature['id']}")
        for path, role in files:
            solid = import_step(path)
            if role == "solid" and _creates_new_body(feature):
                bodies[feature["creates"][0]] = solid
                continue
            operation = feature["operation"] if role == "solid" else \
                {"add": "join", "remove": "cut"}[role]
            target = _target(feature)
            bodies[target] = _combine(bodies[target], solid, operation)

    return external


def expected_with_steps(tree: dict[str, Any], step_dir: str) -> dict[str, dict[str, Any]]:
    """The reference's expected properties with the STEP files standing in
    for the features it has no rule for."""
    return expected(tree, read_fallback(step_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m catia_mcp.tree.step_fallback",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("tree", help="feature tree JSON (version 1)")
    parser.add_argument("model", help="the model's final solid(s) as a STEP file")
    parser.add_argument("--step-dir", required=True, help="folder to write the STEP files to")
    args = parser.parse_args(argv)
    with open(args.tree, encoding="utf-8") as handle:
        tree = json.load(handle)
    try:
        rows = write_fallback(tree, args.step_dir, final_solids(finals_from_step(tree, args.model)))
    except ReferenceError as error:
        print(error)
        return 1
    for row in rows:
        print(f"{row['part']} / {row['feature']}: {row['role']} {row['volume_mm3']:.4f} mm3 -> "
              f"{row['file']}")
    if not rows:
        print("Nothing to write: every feature has a native rule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
