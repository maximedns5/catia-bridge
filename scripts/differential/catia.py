"""CATIA interpreter: turns a case into MCP tool calls and measures the result.

Tools are called in-process exactly as an MCP client would call them, so the
comparison covers the tools' argument handling and conventions, not just
CATIA. Nothing is saved; on 3DEXPERIENCE each case is a throw-away 3D Shape
(or 3D Part) left open in the session unless --close is passed.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from scripts.differential import ops


def calls_for(op: Any) -> list[tuple[str, dict[str, Any]]]:
    """The MCP tool calls one operation stands for."""
    if isinstance(op, ops.Sketch):
        calls = [("catia_create_sketch", {"plane": op.plane})]
        for shape in op.shapes:
            h, v = shape.at
            if isinstance(shape, ops.Rect):
                calls.append(("catia_sketch_centered_rectangle",
                              {"width": shape.width, "height": shape.height, "cx": h, "cy": v}))
            elif isinstance(shape, ops.Circle):
                calls.append(("catia_sketch_circle", {"cx": h, "cy": v, "radius": shape.radius}))
            else:
                calls.append(("catia_sketch_point", {"x": h, "y": v}))
        calls.append(("catia_close_sketch", {}))
        return calls
    if isinstance(op, ops.Pad):
        args: dict[str, Any] = {"height": op.height}
        if op.symmetric:
            args["symmetric"] = True
        return [("catia_pad", args)]
    if isinstance(op, ops.Pocket):
        # No 'direction': the tool then flips to reach material, which is
        # what the reference models.
        return [("catia_pocket", {"depth": op.depth})]
    if isinstance(op, ops.Shaft):
        return [("catia_shaft", {"angle": op.angle})]
    if isinstance(op, ops.Groove):
        return [("catia_groove", {"angle": op.angle})]
    if isinstance(op, ops.Hole):
        return [("catia_hole", {"diameter": op.diameter, "depth": op.depth})]
    if isinstance(op, ops.RectPattern):
        return [("catia_rect_pattern", {
            "dir1_count": op.count1, "dir1_spacing": op.spacing1,
            "dir2_count": op.count2, "dir2_spacing": op.spacing2,
        })]
    if isinstance(op, ops.CircPattern):
        return [("catia_circ_pattern",
                 {"count": op.count, "axis": op.axis, "center": list(op.center)})]
    if isinstance(op, ops.Mirror):
        return [("catia_mirror", {"plane": op.plane})]
    if isinstance(op, ops.NewBody):
        return [("catia_add_body", {"name": op.name})]
    if isinstance(op, ops.ActiveBody):
        return [("catia_set_active_body", {"body_name": op.name} if op.name else {})]
    if isinstance(op, ops.Boolean):
        return [("catia_boolean", {"operation": op.operation, "body_name": op.body})]
    raise TypeError(f"No MCP translation for {type(op).__name__}")


def case_calls(case: ops.Case) -> list[tuple[str, dict[str, Any]]]:
    return [call for op in case.ops for call in calls_for(op)]


class ToolFailed(Exception):
    def __init__(self, tool: str, message: str, code: str | None) -> None:
        super().__init__(f"{tool} failed ({code or 'error'}): {message}")
        self.tool, self.code = tool, code


class CatiaRunner:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.log: list[dict[str, Any]] = []

    def call(self, name: str, args: dict[str, Any] | None = None) -> str:
        started = time.perf_counter()
        result = asyncio.run(self.server.call_tool(name, args or {}))
        text = result.content[0].text if result.content else ""
        structured = result.structuredContent or {}
        self.log.append({
            "tool": name,
            "args": args or {},
            "ok": not result.isError,
            "seconds": round(time.perf_counter() - started, 2),
            "output": text[:2000],
        })
        if result.isError:
            raise ToolFailed(name, structured.get("message", text), structured.get("code"))
        return text

    def build(self, case: ops.Case, part_name: str, plm_type: str | None) -> None:
        args: dict[str, Any] = {"name": part_name}
        if plm_type:
            args["plm_type"] = plm_type
        self.call("catia_new_part", args)
        for name, call_args in case_calls(case):
            self.call(name, call_args)

    def measure(self, checks: tuple[str, ...]) -> dict[str, Any]:
        # A property CATIA could not measure comes back as "unavailable: ..."
        # text; it is kept as is and reported, not compared.
        inertia = json.loads(self.call("catia_get_inertia"))
        cog = inertia.get("center_of_gravity_mm")
        measured: dict[str, Any] = {
            "volume": inertia.get("volume_mm3"),
            "area": inertia.get("area_mm2"),
            "cog": [cog["x"], cog["y"], cog["z"]] if isinstance(cog, dict) else cog,
        }
        if "bbox" in checks:
            box = json.loads(self.call("catia_get_bounding_box"))
            measured["bbox"] = {
                "min": [box["min"][axis] for axis in "xyz"],
                "max": [box["max"][axis] for axis in "xyz"],
            }
        return measured
