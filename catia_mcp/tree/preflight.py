"""Preflight: refuse a whole tree before touching CATIA.

Mirrors the Fusion path's analyze/refusal: a model is refused if any part is
unread, uses a feature this builder does not support yet, or is malformed.
Each reason is reported once, with its count and the model lines it comes from.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from catia_mcp.tree.geometry import Frame, dot, profile_area_centroid, revolve_expectation, unit

# Milestones 1-2: sketches, extrudes, revolves, combines, holes, mirrors
# joined back into their source, and splits.
SUPPORTED = ("sketch", "extrude", "revolve", "combine", "hole", "mirror", "split")
ENTITY_TYPES = ("line", "circle", "arc")
EXTRUDE_OPERATIONS = ("new_body", "join", "cut", "intersect")
COMBINE_OPERATIONS = ("join", "cut", "intersect")
HOLE_KINDS = ("simple", "counterbore", "countersink")
SPLIT_SIDES = ("top", "bottom")
# Points closer than this to a plane or an axis are on it (mm).
ON_TOL = 1e-6


class TreeRefused(Exception):
    def __init__(self, reasons: OrderedDict[str, list[str]]) -> None:
        self.reasons = reasons
        super().__init__(refusal(reasons))


def _where(feature: dict[str, Any]) -> str:
    source = feature.get("source") or {}
    if source.get("line"):
        return f"{source.get('file', 'model.py')} line {source['line']}"
    return f"feature {feature.get('id', '?')}"


def analyze(tree: dict[str, Any]) -> OrderedDict[str, list[str]]:
    """Every reason to refuse the tree, mapped to where it occurs."""
    reasons: OrderedDict[str, list[str]] = OrderedDict()

    def refuse(reason: str, where: str) -> None:
        reasons.setdefault(reason, []).append(where)

    if tree.get("version") != 1:
        refuse(f"unsupported tree version {tree.get('version')!r} (expected 1)", "tree")
        return reasons
    parts = tree.get("parts") or []
    if not parts:
        refuse("the tree has no part", "tree")
    for part in parts:
        pid = part.get("id", "?")
        if part.get("unread"):
            refuse(f"part could not be read: {part['unread']}", pid)
        if part.get("placement"):
            refuse("parts placed by a parent location are not supported yet", pid)
        ids: set[str] = set()
        sketches: dict[str, dict[str, Any]] = {}
        bodies: set[str] = set()
        features = part.get("features") or []
        for index, feature in enumerate(features):
            kind, fid, where = feature.get("type"), feature.get("id"), _where(feature)
            if fid in ids:
                refuse(f"duplicate feature id '{fid}'", f"{pid} {where}")
            ids.add(fid)
            if kind not in SUPPORTED:
                label = "a solid without a native rule (base_feature)" if kind == "base_feature" \
                    else f"'{kind}' features are not supported yet"
                refuse(label, f"{pid} {where}")
                # Keep its bodies known, so later features are not reported as
                # broken only because this one is unsupported.
                bodies.update(feature.get("creates") or [])
                continue
            if kind == "sketch":
                _check_sketch(feature, refuse, f"{pid} {where}")
                sketches[fid] = feature
            elif kind == "extrude":
                _check_extrude(feature, sketches, bodies, refuse, f"{pid} {where}")
            elif kind == "revolve":
                _check_revolve(feature, sketches, bodies, refuse, f"{pid} {where}")
            elif kind == "mirror":
                following = features[index + 1] if index + 1 < len(features) else None
                _check_mirror(feature, following, bodies, refuse, f"{pid} {where}")
            elif kind == "split":
                _check_split(feature, bodies, refuse, f"{pid} {where}")
            elif kind == "combine":
                _check_combine(feature, bodies, refuse, f"{pid} {where}")
            elif kind == "hole":
                _check_hole(feature, bodies, refuse, f"{pid} {where}")
    return reasons


def _check_sketch(feature, refuse, where) -> None:
    try:
        Frame.from_tree(feature["plane"])
    except (KeyError, TypeError, ValueError) as error:
        refuse(f"sketch plane is invalid ({error})", where)
        return
    for entity in feature.get("entities") or []:
        if entity.get("type") not in ENTITY_TYPES:
            refuse(f"sketch entity type '{entity.get('type')}' is not supported yet", where)
            return
    if not feature.get("profiles"):
        refuse("sketch has no closed profile", where)
        return
    try:
        profile_area_centroid(feature)
    except (KeyError, ValueError) as error:
        refuse(f"sketch profile is not a closed region ({error})", where)


def _check_operation(feature, bodies, refuse, where, kind) -> None:
    operation = feature.get("operation")
    if operation not in EXTRUDE_OPERATIONS:
        refuse(f"{kind} operation '{operation}' is not supported", where)
    elif operation == "new_body":
        created = feature.get("creates") or []
        if len(created) != 1:
            refuse(f"a new-body {kind} must create exactly one body", where)
        bodies.update(created)
    elif len(feature.get("bodies") or []) != 1 or feature["bodies"][0] not in bodies:
        refuse(f"{kind} must modify exactly one known body", where)


def _check_revolve(feature, sketches, bodies, refuse, where) -> None:
    sketch = sketches.get(feature.get("sketch"))
    if sketch is None:
        refuse("revolve refers to an unknown sketch", where)
    angle = feature.get("angle")
    if not isinstance(angle, (int, float)) or not 0 < angle <= 360:
        refuse("revolve angle must be in (0, 360] degrees", where)
        angle = None
    axis = feature.get("axis") or {}
    try:
        origin = tuple(float(c) for c in axis["origin"])
        direction = unit(tuple(float(c) for c in axis["direction"]))
    except (KeyError, TypeError, ValueError):
        refuse("revolve axis is invalid", where)
        origin = direction = None
    if sketch is not None and origin is not None:
        try:
            frame = Frame.from_tree(sketch["plane"])
        except (KeyError, TypeError, ValueError):
            frame = None
        if frame is not None:
            offset = tuple(o - p for o, p in zip(origin, frame.origin))
            if abs(dot(direction, frame.normal)) > ON_TOL or abs(dot(offset, frame.normal)) > ON_TOL:
                refuse("revolve axis does not lie in its sketch plane", where)
            elif angle is not None:
                try:
                    if revolve_expectation(frame, sketch, origin, direction, angle).min_radius < -ON_TOL:
                        refuse("revolve profile crosses its axis", where)
                except (KeyError, ValueError) as error:
                    refuse(f"revolve profile is invalid ({error})", where)
    _check_operation(feature, bodies, refuse, where, "revolve")


def _check_mirror(feature, following, bodies, refuse, where) -> None:
    sources, created = feature.get("bodies") or [], feature.get("creates") or []
    if len(sources) != 1 or sources[0] not in bodies or len(created) != 1:
        refuse("mirror must copy exactly one known body into one new body", where)
        bodies.update(created)
        return
    _check_plane(feature, refuse, where, "mirror")
    # CATIA's Mirror unites a body with its image; a tree mirror creates the
    # image as a new body, so only the usual "join it straight back" maps.
    joined_back = (
        following is not None and following.get("type") == "combine"
        and following.get("operation") == "join"
        and following.get("targets") == sources and following.get("tools") == created
    )
    if not joined_back:
        refuse("mirrored bodies that are not joined straight back into their source are "
               "not supported yet", where)
    bodies.update(created)


def _check_split(feature, bodies, refuse, where) -> None:
    if len(feature.get("bodies") or []) != 1 or feature["bodies"][0] not in bodies:
        refuse("split must cut exactly one known body", where)
    if feature.get("keep") not in SPLIT_SIDES:
        refuse(f"split keep '{feature.get('keep')}' is not supported", where)
    _check_plane(feature, refuse, where, "split")


def _check_plane(feature, refuse, where, kind) -> None:
    plane = feature.get("plane") or {}
    try:
        tuple(float(c) for c in plane["origin"])
        unit(tuple(float(c) for c in plane["normal"]))
    except (KeyError, TypeError, ValueError):
        refuse(f"{kind} plane is invalid", where)


def _check_extrude(feature, sketches, bodies, refuse, where) -> None:
    if feature.get("sketch") not in sketches:
        refuse("extrude refers to an unknown sketch", where)
    if feature.get("taper"):
        refuse("tapered extrudes are not supported yet", where)
    if feature.get("direction"):
        refuse("oblique extrudes (direction) are not supported yet", where)
    _check_operation(feature, bodies, refuse, where, "extrude")
    if not feature.get("distance"):
        refuse("extrude has no distance", where)


def _check_combine(feature, bodies, refuse, where) -> None:
    if feature.get("operation") not in COMBINE_OPERATIONS:
        refuse(f"combine operation '{feature.get('operation')}' is not supported", where)
    targets, tools = feature.get("targets") or [], feature.get("tools") or []
    if len(targets) != 1 or not tools:
        refuse("combine needs exactly one target and at least one tool", where)
    if any(name not in bodies for name in targets + tools):
        refuse("combine refers to an unknown body", where)
    bodies.difference_update(tools)


def _check_hole(feature, bodies, refuse, where) -> None:
    if feature.get("kind") not in HOLE_KINDS:
        refuse(f"'{feature.get('kind')}' holes are not supported yet", where)
    if len(feature.get("bodies") or []) != 1 or feature["bodies"][0] not in bodies:
        refuse("hole must cut exactly one known body", where)
    frames = feature.get("frames") or []
    if not frames or len(frames) != len(feature.get("positions") or []):
        refuse("hole needs one frame per position", where)
    if not feature.get("effective_depth") and feature.get("depth") is None:
        refuse("through hole has no effective depth", where)
    if feature.get("kind") == "countersink":
        angle = feature.get("countersink_angle")
        if not feature.get("countersink_diameter") or not isinstance(angle, (int, float)) \
                or not 0 < angle < 180:
            refuse("countersink hole needs a diameter and an angle in (0, 180) degrees", where)
        elif feature["countersink_diameter"] <= feature.get("diameter", 0):
            refuse("countersink diameter must exceed the hole diameter", where)


def refusal(reasons: OrderedDict[str, list[str]]) -> str:
    lines = []
    for reason, places in reasons.items():
        count = f" ({len(places)}x)" if len(places) > 1 else ""
        shown = ", ".join(dict.fromkeys(places))
        lines.append(f"- {reason}{count}: {shown}")
    return (
        "This model cannot be sent to CATIA yet:\n" + "\n".join(lines)
        + "\nNothing was sent to CATIA; the STEP download is not affected."
    )


def check(tree: dict[str, Any]) -> None:
    reasons = analyze(tree)
    if reasons:
        raise TreeRefused(reasons)


def truncate(tree: dict[str, Any], part_id: str | None, until: str | None) -> dict[str, Any]:
    """Development helper: keep one part and/or its features up to `until`."""
    parts = tree.get("parts") or []
    if part_id is not None:
        parts = [p for p in parts if p.get("id") == part_id]
        if not parts:
            raise ValueError(f"no part '{part_id}' in the tree")
    if until is not None:
        cut = []
        for part in parts:
            features = part.get("features") or []
            ids = [f.get("id") for f in features]
            if until not in ids:
                raise ValueError(f"no feature '{until}' in part '{part.get('id')}'")
            kept = features[: ids.index(until) + 1]
            cut.append({**part, "features": kept, "bodies": _live_bodies(kept)})
        parts = cut
    return {**tree, "parts": parts}


def _live_bodies(features: list[dict[str, Any]]) -> list[str]:
    bodies: list[str] = []
    for feature in features:
        for name in feature.get("creates") or []:
            if name not in bodies:
                bodies.append(name)
        if feature.get("type") == "combine":
            bodies = [b for b in bodies if b not in (feature.get("tools") or [])]
    return bodies
