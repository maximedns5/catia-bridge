"""Colours, applied as a designer would: the part's colour on each of its
final bodies (the PartBody in a one-body part), through the selection's
`VisProperties.SetRealColor r, g, b, 1` (sRGB 0..255, inheritance flag 1),
on V5 and 3DEXPERIENCE alike; the part object when a body refuses. A
feature's own colour goes on the CATIA features the builder made for it,
found by their names (`Part.FindObjectByName`). Every colour is read back
(`GetRealColor`, through the out-parameter detour).
"""

from __future__ import annotations

import contextlib
from typing import Any

from catia_mcp.automation import call_out_values
from catia_mcp.tree.properties import route, to_rgb255


def set_colour(ctx: Any, obj: Any, rgb: tuple[int, int, int]) -> list[int] | None:
    """Colour one object; the colour read back (None if unreadable)."""
    selection = ctx.selection()
    selection.Clear()
    try:
        selection.Add(obj)
        selection.VisProperties.SetRealColor(rgb[0], rgb[1], rgb[2], 1)
        with contextlib.suppress(Exception):
            read = call_out_values(ctx.app, selection.VisProperties, "GetRealColor", 3)
            return [round(float(c)) for c in read]
        return None
    finally:
        with contextlib.suppress(Exception):
            selection.Clear()


def _catia_objects(ctx: Any, feature_id: str) -> list[Any]:
    names = []
    for entry in getattr(ctx.record, "features", []):
        if entry.get("id") == feature_id:
            names = [n for n in entry.get("catia") or [] if " " not in n]
    found = []
    for name in names:
        with contextlib.suppress(Exception):
            found.append(ctx.part.FindObjectByName(name))
    return found


def apply_colours(ctx: Any) -> dict[str, Any] | None:
    colour = ctx.tree_part.get("color")
    coloured_features = [f for f in ctx.tree_part.get("features") or []
                         if isinstance(f, dict) and f.get("color") is not None]
    if colour is None and not coloured_features:
        return None
    result: dict[str, Any] = {"routes": []}
    ok = True
    if colour is not None:
        rgb = to_rgb255(colour)
        result["color_set"] = list(rgb)
        if ctx.tree_part.get("color_source"):
            result["source"] = ctx.tree_part["color_source"]
        reads = []
        for body in ctx.bodies:
            record, read = route(f"body {getattr(body, 'Name', '?')}: SetRealColor",
                                 lambda b=body: set_colour(ctx, b, rgb))
            result["routes"].append(record)
            if record["ok"]:
                reads.append(read)
        if not reads:
            record, read = route("part: SetRealColor", lambda: set_colour(ctx, ctx.part, rgb))
            result["routes"].append(record)
            reads = [read] if record["ok"] else []
        if reads and reads[0] is not None:
            result["color_read"] = reads[0]
        ok = bool(reads) and all(r is None or list(r) == list(rgb) for r in reads)
    features = []
    for feature in coloured_features:
        rgb = to_rgb255(feature["color"])
        entry: dict[str, Any] = {"id": feature.get("id"), "color_set": list(rgb), "objects": []}
        for obj in _catia_objects(ctx, feature.get("id")):
            record, read = route(f"{getattr(obj, 'Name', '?')}: SetRealColor",
                                 lambda o=obj, c=rgb: set_colour(ctx, o, c))
            record["read"] = read
            entry["objects"].append(record)
        entry["ok"] = bool(entry["objects"]) and all(
            r["ok"] and (r["read"] is None or list(r["read"]) == list(rgb))
            for r in entry["objects"])
        ok = ok and entry["ok"]
        features.append(entry)
    if features:
        result["features"] = features
    result["status"] = "ok" if ok else "failed"
    return result
