"""What each tree feature needs from the CATIA installation.

The builder reaches CATIA through a few families of automation calls: Part
Design (sketches, pads, shafts, booleans, mirrors, splits), its advanced
solids (multi-sections solids and ribs), and Generative Shape Design
wireframe (the construction points, lines and planes it adds when a sketch,
an axis or a cutting plane is not one of the origin elements). This module
names, per feature, the capabilities of catia_mcp.capabilities those calls
need, so the preflight can refuse a tree the installation is known not to
build. Only a capability probed as "unavailable" refuses; "unknown" never
does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from catia_mcp.tree.geometry import (
    add,
    dot,
    frame_axes,
    hole_profile_sketch,
    hole_segments,
    scale,
    unit,
)
from catia_mcp.tree.sweep import sweep_expectation

BASIC = "partdesign.basic"
ADVANCED = "partdesign.advanced"
WIREFRAME = "gsd.wireframe"
MEASURE = "measure"
# The container every tree part is built in, per platform and content type.
CONTAINERS = {("v5", None): "part.create", ("3dx", "3DShape"): "plm.create.3dshape",
              ("3dx", "3DPart"): "plm.create.3dpart"}
ON_TOL = 1e-6


def _off_origin(origin, normal) -> bool:
    """True when the builder must add a construction plane for this plane:
    it is not an origin plane (XY, YZ or ZX through the origin)."""
    n = unit(tuple(float(c) for c in normal))
    on_axis = any(abs(abs(c) - 1) < 1e-9 for c in n)
    return not on_axis or abs(dot(tuple(float(c) for c in origin), n)) > ON_TOL


def _sketch_needs(sketch: dict[str, Any] | None) -> set[str]:
    plane = (sketch or {}).get("plane") or {}
    return {WIREFRAME} if _off_origin(plane["origin"], plane["normal"]) else set()


def _hole_planes(feature: dict[str, Any]):
    for matrix in feature["frames"]:
        if feature["kind"] == "countersink":
            plane = hole_profile_sketch(feature, matrix)[0]["plane"]
            yield plane["origin"], plane["normal"]
            continue
        origin, _, z_dir = frame_axes(matrix)
        for _, top, _ in hole_segments(feature):
            yield add(origin, scale(z_dir, top)), z_dir


def feature_needs(feature: dict[str, Any], sketches: dict[str, dict[str, Any]]) -> set[str]:
    """Capabilities one feature needs. A sketch needs nothing by itself: the
    builder draws it with the feature that consumes it, which carries its
    needs. Raises on a malformed feature (the preflight reports those)."""
    kind = feature.get("type")
    if kind in ("sketch", "combine"):
        return {BASIC} if kind == "combine" else set()
    needs = {BASIC}
    if kind == "extrude":
        needs |= _sketch_needs(sketches.get(feature.get("sketch")))
    elif kind == "revolve":
        needs |= _sketch_needs(sketches.get(feature.get("sketch")))
        if WIREFRAME not in needs:
            # On an origin plane the builder revolves about the sketch's own H
            # or V axis (a coordinate axis through the origin); any other axis
            # is drawn as a 3D line between construction points.
            origin = tuple(float(c) for c in feature["axis"]["origin"])
            direction = unit(tuple(float(c) for c in feature["axis"]["direction"]))
            off_axis = add(origin, scale(direction, -dot(origin, direction)))
            on_axis = any(abs(abs(c) - 1) < 1e-9 for c in direction)
            if not on_axis or dot(off_axis, off_axis) > ON_TOL ** 2:
                needs.add(WIREFRAME)
    elif kind in ("mirror", "split"):
        plane = feature.get("plane") or {}
        if _off_origin(plane["origin"], plane["normal"]):
            needs.add(WIREFRAME)
    elif kind == "hole":
        if any(_off_origin(origin, normal) for origin, normal in _hole_planes(feature)):
            needs.add(WIREFRAME)
    elif kind == "loft":
        # Closing points are construction points.
        needs |= {ADVANCED, WIREFRAME}
        for name in feature.get("sections") or []:
            needs |= _sketch_needs(sketches.get(name))
    elif kind == "sweep":
        sketch = sketches.get(feature.get("sketch"))
        needs |= {ADVANCED} | _sketch_needs(sketch)
        frame, _ = sweep_expectation(sketch, feature.get("path") or [])
        if _off_origin(frame.origin, frame.normal):
            needs.add(WIREFRAME)
    return needs


def container(platform: str | None, plm_type: str | None) -> str | None:
    if platform == "v5":
        return CONTAINERS[("v5", None)]
    return CONTAINERS.get((platform, plm_type))


def refuse_unavailable(tree: dict[str, Any], report: dict[str, Any], plm_type: str | None,
                       refuse: Callable[[str, str], None],
                       where_of: Callable[[dict[str, Any]], str]) -> None:
    """Call refuse(reason, where) for every need the probe report marks
    unavailable. Features too malformed to analyse are skipped: the
    preflight already refuses them."""
    capabilities = report.get("capabilities") or {}
    platform = (report.get("fingerprint") or {}).get("platform")

    def check(need: str, where: str) -> None:
        entry = capabilities.get(need) or {}
        if entry.get("status") == "unavailable":
            label = entry.get("label") or need
            refuse(f"this CATIA cannot {label} ({need}: {entry.get('reason') or 'unavailable'})",
                   where)

    for part in tree.get("parts") or []:
        pid = part.get("id", "?")
        holder = container(platform, plm_type)
        if holder:
            check(holder, pid)
        check(MEASURE, pid)
        sketches: dict[str, dict[str, Any]] = {}
        for feature in part.get("features") or []:
            if feature.get("type") == "sketch":
                sketches[feature.get("id")] = feature
            try:
                needs = feature_needs(feature, sketches)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            for need in sorted(needs):
                check(need, f"{pid} {where_of(feature)}")
