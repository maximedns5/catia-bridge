"""Offline checks of the differential harness (scripts/differential).

The harness itself runs against a live CATIA; these tests make sure every
case turns into valid MCP calls, that the build123d reference computes the
intended geometry, and that the comparison and runner report failures.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from jsonschema import validate
from mcp.types import CallToolResult, TextContent

from catia_mcp.server import CATIAMCPServer
from scripts.differential.cases import CASES, by_name
from scripts.differential.catia import CatiaRunner, ToolFailed, case_calls
from scripts.differential.compare import compare

# The intent of each case in closed form (mm3). A new case needs one here,
# so the reference interpreter is checked against geometry worked out by hand.
CLOSED_FORM_MM3 = {
    "block": 100 * 60 * 40,
    "xy_offset_pad": 40 * 20 * 15,
    "yz_offset_pad": 40 * 20 * 30,
    "zx_offset_pad": 40 * 20 * 30,
    "pad_symmetric": 50 * 50 * 40,
    "circular_pockets": 240000 - 4 * math.pi * 4**2 * 20,
    "rect_pattern_pockets": 100 * 60 * 10 - 6 * math.pi * 4**2 * 5,
    "rect_pattern_dir1": 100 * 60 * 10 - 2 * math.pi * 4**2 * 5,
    "rect_pattern_dir2": 100 * 60 * 10 - 2 * math.pi * 4**2 * 5,
    "shaft_ring": 2 * math.pi * 20 * (10 * 30),
    "groove_on_cylinder": math.pi * 20**2 * 60 - math.pi * (20**2 - 17**2) * 10,
    "hole_from_sketch": 240000 - math.pi * 4**2 * 25,
    "mirror_body": 2 * 40 * 20 * 15,
    "boolean_remove": 240000 - 30 * 20 * 40,
    "boolean_add": 240000 + math.pi * 5**2 * 20,
    "boolean_intersect": math.pi * 25**2 * 40,
}


def test_every_case_translates_to_valid_mcp_calls():
    server = CATIAMCPServer()
    try:
        for case in CASES:
            for name, args in case_calls(case):
                assert name in server._schemas, (case.name, name)
                validate(args, server._schemas[name])
    finally:
        asyncio.run(server.close())


def test_reference_matches_the_closed_form_of_every_case():
    pytest.importorskip("build123d")
    from scripts.differential.reference import expected

    assert set(CLOSED_FORM_MM3) == {case.name for case in CASES}
    for case in CASES:
        assert expected(case)["volume"] == pytest.approx(CLOSED_FORM_MM3[case.name], rel=1e-9), case.name


def test_reference_refuses_a_pocket_with_material_on_both_sides():
    pytest.importorskip("build123d")
    from scripts.differential.ops import Case, Pad, Pocket, Rect, Sketch
    from scripts.differential.reference import CaseError, expected

    straddling = Case("straddling", "", (
        Sketch("xy", (Rect(10, 10),)), Pad(5, symmetric=True),
        Sketch("xy", (Rect(2, 2),)), Pocket(3),
    ))
    with pytest.raises(CaseError, match="both sides"):
        expected(straddling)


def _props(volume=1000.0, cog=(0.0, 0.0, 0.0)):
    return {"volume": volume, "area": 100.0, "cog": list(cog),
            "bbox": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 10.0]}}


def test_compare_accepts_rounding_and_flags_real_differences():
    rows = {row["property"]: row for row in compare(
        _props(), {"volume": 1000.005, "area": 100.0, "cog": [0.0, 0.0, 0.5]},
        ("volume", "area", "cog"), freeform=False,
    )}
    assert rows["volume"]["ok"] and rows["area"]["ok"]
    assert not rows["cog"]["ok"]


def test_an_unmeasured_property_fails_with_its_reason():
    (row,) = compare(_props(), {"volume": "unavailable: no SPA workbench"}, ("volume",), False)
    assert not row["ok"] and row["note"] == "unavailable: no SPA workbench"


class _FakeServer:
    """call_tool double: canned measurements, optional failing tool."""

    def __init__(self, inertia, fail=None):
        self.inertia, self.fail = inertia, fail

    async def call_tool(self, name, args):
        if name == self.fail:
            return CallToolResult(
                content=[TextContent(type="text", text="boom")],
                structuredContent={"ok": False, "code": "COM_ERROR", "message": "boom"},
                isError=True,
            )
        text = json.dumps(self.inertia) if name == "catia_get_inertia" else "ok"
        return CallToolResult(content=[TextContent(type="text", text=text)])


def test_runner_builds_measures_and_stops_at_the_first_failing_tool():
    (block,) = by_name(["block"])
    inertia = {"volume_mm3": 240000.0, "area_mm2": 24800.0,
               "center_of_gravity_mm": {"x": 0.0, "y": 0.0, "z": 20.0}}
    runner = CatiaRunner(_FakeServer(inertia))
    runner.build(block, "MCP_Diff_block", "3DShape")
    assert runner.log[0]["args"] == {"name": "MCP_Diff_block", "plm_type": "3DShape"}
    assert [entry["tool"] for entry in runner.log[1:]] == [name for name, _ in case_calls(block)]
    assert runner.measure(("volume", "area", "cog")) == {
        "volume": 240000.0, "area": 24800.0, "cog": [0.0, 0.0, 20.0]}

    failing = CatiaRunner(_FakeServer(inertia, fail="catia_pad"))
    with pytest.raises(ToolFailed) as error:
        failing.build(block, "MCP_Diff_block", None)
    assert error.value.tool == "catia_pad" and error.value.code == "COM_ERROR"
    assert "plm_type" not in failing.log[0]["args"]
    assert failing.log[-1]["tool"] == "catia_pad"  # nothing ran after the failure
