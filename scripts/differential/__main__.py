"""Differential test: build123d as the reference, CATIA through the MCP tools.

Usage (from the repository root):
    python -m scripts.differential --reference-only        expected values only, no CATIA
    python -m scripts.differential --plm-type 3DShape      full run on 3DEXPERIENCE
    python -m scripts.differential --case block --case hole_from_sketch

Needs build123d (pip install -e ".[differential]"). A full run needs Windows
with CATIA started (and, on 3DEXPERIENCE, logged in). Nothing is saved.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from scripts.differential.cases import by_name
from scripts.differential.compare import compare
from scripts.differential.reference import CaseError, expected


def _reference_only(cases) -> int:
    failures = 0
    for case in cases:
        try:
            props = expected(case)
        except CaseError as error:
            failures += 1
            print(f"{case.name:24} CASE ERROR: {error}")
            continue
        cog = ", ".join(f"{v:.4f}" for v in props["cog"])
        print(f"{case.name:24} volume {props['volume']:.4f} mm3, area {props['area']:.4f} mm2, cog ({cog})")
    return 1 if failures else 0


def _run(cases, args) -> int:
    from catia_mcp.server import CATIAMCPServer
    from scripts.differential.catia import CatiaRunner, ToolFailed

    server = CATIAMCPServer(platform=args.platform)
    runner = CatiaRunner(server)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report: dict[str, Any] = {"cases": []}
    try:
        runner.call("catia_connect")
        report["diagnose"] = json.loads(runner.call("catia_diagnose"))
    except ToolFailed as error:
        print(f"Cannot start: {error}")
        return 2

    for case in cases:
        entry: dict[str, Any] = {"name": case.name, "description": case.description,
                                 "hypothesis": case.hypothesis}
        first_call = len(runner.log)
        try:
            want = expected(case)
            runner.build(case, f"MCP_Diff_{case.name}_{stamp}", args.plm_type)
            got = runner.measure(case.checks)
            rows = compare(want, got, case.checks, case.freeform)
            entry["checks"] = rows
            entry["status"] = "PASS" if all(row["ok"] for row in rows) else "FAIL"
        except CaseError as error:
            entry.update(status="CASE_ERROR", error=str(error))
        except ToolFailed as error:
            entry.update(status="ERROR", error=str(error), code=error.code)
        finally:
            entry["calls"] = runner.log[first_call:]
            if args.close:
                try:
                    runner.call("catia_close_document", {"save": False})
                except ToolFailed:
                    pass
        report["cases"].append(entry)
        failed = [row["property"] for row in entry.get("checks", []) if not row["ok"]]
        detail = f" ({', '.join(failed)})" if failed else (f": {entry['error']}" if "error" in entry else "")
        print(f"{case.name:24} {entry['status']}{detail}")

    counts: dict[str, int] = {}
    for entry in report["cases"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    report["summary"] = counts
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(", ".join(f"{n} {status}" for status, n in sorted(counts.items())) + f". Report: {args.report}")
    return 0 if counts.get("PASS", 0) == len(report["cases"]) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reference-only", action="store_true",
                        help="print build123d's expected values without CATIA")
    parser.add_argument("--case", action="append", help="run only this case (repeatable)")
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", choices=["3DPart", "3DShape"],
                        help="3DEXPERIENCE content type (3DShape without a 3D Part licence)")
    parser.add_argument("--close", action="store_true",
                        help="close each case's part without saving after measuring")
    parser.add_argument("--report", default="differential_report.json", help="JSON report path")
    args = parser.parse_args(argv)
    cases = by_name(args.case)
    if args.reference_only:
        return _reference_only(cases)
    return _run(cases, args)


if __name__ == "__main__":
    sys.exit(main())
