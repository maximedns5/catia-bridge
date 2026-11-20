"""build123d references for the second Part Design slice: sweeps along a
helix (catia_mcp/tree/helix.py), cosmetic threads, bodies moved or mirrored
in place (catia_mcp/tree/partdesign_extra.py). Called by
catia_mcp/tree/reference.py; needs build123d."""

from __future__ import annotations

from typing import Any

from build123d import Edge, Plane

from catia_mcp.tree.helix import helix_spec, is_helix_path


def helix_edge(entity: dict[str, Any]) -> Any:
    """build123d's Helix(pitch, height, radius, center, direction,
    cone_angle, lefthand) as an edge."""
    spec = helix_spec(entity)
    return Edge.make_helix(spec.pitch, spec.height, spec.radius, spec.center, spec.direction,
                           spec.cone_angle, spec.lefthand)


def has_helix(part: dict[str, Any]) -> bool:
    return any(f.get("type") == "sweep" and is_helix_path(f.get("path"))
               for f in part.get("features") or [])


def apply(feature: dict[str, Any], bodies: dict[str, Any], location) -> None:
    """A thread (cosmetic: the body is unchanged), a move or a mirror in
    place."""
    kind = feature["type"]
    (target,) = feature["bodies"]
    if kind == "move":
        bodies[target] = bodies[target].moved(location(feature["matrix"]))
    elif kind == "mirror_in_place":
        plane = feature["plane"]
        bodies[target] = bodies[target].mirror(Plane(origin=plane["origin"],
                                                     z_dir=plane["normal"]))
