"""Live probe of the Part Design additions: drafts, extrudes up to a limit,
tapped and V-bottomed holes, lofts to a point (docs/part-design.md).

None of their COM calls has been validated on a live CATIA yet. This probe
attaches to the running CATIA (it never starts one) and:

1. reads, in a scratch part, the type library's enums the builder relies on
   (limit modes, hole threading, bottoms and thread sides, draft modes),
   since their values are only the CAA help's declared order otherwise;
2. builds each m10 fixture with the tree command, exactly as
   `python -m catia_mcp.tree build <fixture> --report <file>` does, each in
   a new part, and keeps every report: the notes say which way each feature
   was built (which limit mode, which draft orientation, which thread
   standard, whether a V bottom's depth is measured to its tip, whether a
   loft to a point or a Shaft made the cone).

Each step is recorded even when it fails, and the JSON report is written
after every step. Nothing is saved: the parts stay open for inspection.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in; leave CATIA
alone while it runs; build123d installed for the expected volumes):
    python scripts/probe_part_design.py --report part_design_probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from typing import Any

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

TREES = os.path.join(ROOT, "tests", "fixtures", "trees")
# In the order a failure teaches the most from: the simplest first.
FIXTURES = (
    "m10_draft_two_sides",
    "m10_draft_four_sides",
    "m10_pad_up_to_next",
    "m10_pocket_up_to_last",
    "m10_pad_up_to_plane",
    "m10_pad_up_to_face",
    "m10_tapped_holes",
    "m10_loft_pyramid_tip",
    "m10_loft_cone_join",
)
ENUMS = ("CatLimitMode", "CatHoleThreadingMode", "CatHoleThreadSide", "CatHoleBottomType",
         "CatHoleType", "CatDraftMode", "CatDraftNeutralPropagationMode",
         "CatDraftMultiselectionMode", "CatDraftPartingMode", "CatHoleThreadStandard")
SIGNATURES = ("AddNewDraft", "AddNewCloseSurface", "AddNewHoleFromSketch", "AddNewLoft")


def facts(platform: str, plm_type: str) -> dict[str, Any]:
    """The enums, read from the type library in a scratch part."""
    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection
    from catia_mcp.introspect import find_enum, method_params
    from catia_mcp.tools.document import DocumentTools

    conn = CATIAConnection(platform=platform, allow_launch=False)
    worker = ComWorker(conn.watchdog, timeout_s=120, name="catia-probe")
    out: dict[str, Any] = {"connect": worker.run_guarded(conn.connect, label="connect")}

    def read() -> dict[str, Any]:
        DocumentTools(conn)._new_part("PartDesignProbe", plm_type if conn.is_3dx else "auto")
        part = conn.get_active_part()
        objs = [part.ShapeFactory, conn.app]
        found: dict[str, Any] = {name: find_enum(objs, name) for name in ENUMS}
        # The exact signatures: live on R428 AddNewDraft failed every way tried.
        found["signatures"] = {m: method_params(part.ShapeFactory, m) for m in SIGNATURES}
        return found

    out["enums"] = worker.run_guarded(read, label="the enums")
    return out


def build(name: str, args) -> dict[str, Any]:
    """One fixture through the tree command; its report, read back."""
    from catia_mcp.tree.__main__ import main

    path = os.path.join(TREES, f"{name}.json")
    handle, out = tempfile.mkstemp(prefix=f"{name}_", suffix=".json")
    os.close(handle)
    argv = ["build", path, "--report", out, "--platform", args.platform,
            "--plm-type", args.plm_type]
    started = time.monotonic()
    code = main(argv)
    record: dict[str, Any] = {"fixture": name, "exit_code": code,
                              "elapsed_s": round(time.monotonic() - started, 2)}
    try:
        with open(out, encoding="utf-8") as report:
            record["report"] = json.load(report)
    except (OSError, ValueError) as error:  # refused before building: no report
        record["report_error"] = str(error)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    detail = (record.get("report") or {}).get("parts_detail") or []
    record["summary"] = [
        {"id": f.get("id"), "type": f.get("type"), "ok": f.get("ok"), "catia": f.get("catia"),
         "notes": f.get("notes"), "thread": f.get("thread")}
        for part in detail for f in part.get("features", []) if f.get("type") != "sketch"]
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"],
                        help="3DEXPERIENCE content type for each part (default 3DShape)")
    parser.add_argument("--report", default="part_design_probe.json", help="JSON report path")
    parser.add_argument("--only", action="append", choices=FIXTURES,
                        help="only this fixture (repeatable)")
    parser.add_argument("--no-facts", action="store_true", help="skip the enum census")
    args = parser.parse_args()

    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "builds": []}

    def save() -> None:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)

    if not args.no_facts:
        try:
            report["facts"] = facts(args.platform, args.plm_type)
        except Exception as error:  # keep going: the builds read the enums too
            report["facts"] = {"error": str(error), "traceback": traceback.format_exc()}
        save()
    for name in args.only or FIXTURES:
        try:
            report["builds"].append(build(name, args))
        except Exception as error:  # keep what was found so far
            report["builds"].append({"fixture": name, "error": str(error),
                                     "traceback": traceback.format_exc()})
        save()
    ok = [b["fixture"] for b in report["builds"] if b.get("exit_code") == 0]
    print("Built:", ok or "none")
    print("Failed:", [b["fixture"] for b in report["builds"] if b["fixture"] not in ok] or "none")
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
