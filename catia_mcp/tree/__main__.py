"""Feature tree command line.

    python -m catia_mcp.tree check TREE.json         preflight only, no CATIA
    python -m catia_mcp.tree reference TREE.json     expected solids (needs build123d)
    python -m catia_mcp.tree build TREE.json --plm-type 3DShape
    python -m catia_mcp.tree capabilities [--create] [--risky 3dpart]

build attaches to a running CATIA (it never starts one), builds every part in
a new part, compares each part's volume with build123d's when build123d is
installed, proves editability, and writes a JSON report. Nothing is saved.
Before building it probes what this CATIA can do (read-only, plus the cached
results of earlier capability probes) and refuses a tree that needs something
found unavailable. CATIA runs on a guarded worker thread: a dialog that blocks
the build ends it with a report naming the part, the feature and the dialog's
text. --part and --until build a prefix of one part, for development.

capabilities probes the running CATIA and prints what it can do. Read-only
unless --create (a scratch part, closed without saving) or --risky (a 3D Part
on 3DEXPERIENCE, which can open a blocking licence dialog; a STEP export to a
temporary file on V5).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from catia_mcp.tree.preflight import TreeRefused, analyze, refusal, truncate


def _load(args) -> dict[str, Any]:
    with open(args.tree, encoding="utf-8") as handle:
        tree = json.load(handle)
    if args.part or args.until:
        tree = truncate(tree, args.part, args.until)
    return tree


def _expected(tree: dict[str, Any], mode: str) -> dict[str, Any] | None:
    if mode == "none":
        return None
    if mode != "auto":
        with open(mode, encoding="utf-8") as handle:
            return json.load(handle)
    try:
        from catia_mcp.tree.reference import expected
    except ImportError:
        print("build123d is not installed: building without expected volumes "
              '(pip install -e ".[differential]")')
        return None
    return expected(tree)


def _check(tree) -> int:
    reasons = analyze(tree)
    if reasons:
        print(refusal(reasons))
        return 1
    print(f"Accepted: {len(tree['parts'])} part(s), "
          f"{sum(len(p['features']) for p in tree['parts'])} feature(s).")
    return 0


def _reference(tree) -> int:
    from catia_mcp.tree.reference import expected

    for part_id, props in expected(tree).items():
        cog = ", ".join(f"{c:.4f}" for c in props["cog_mm"])
        print(f"{part_id}: volume {props['volume_mm3']:.4f} mm3, area {props['area_mm2']:.4f} mm2, "
              f"cog ({cog})")
    return 0


def _blocked_report(tree, builder, error, started: float) -> dict[str, Any]:
    """The report of a build a dialog (or a hang) stopped mid-call."""
    partial = getattr(builder, "report", None) or {"parts_detail": []}
    details = [record if isinstance(record, dict) else dict(vars(record))
               for record in partial["parts_detail"]]
    return {
        "success": False, "native_history": False, "editable": None,
        "parts": len(tree["parts"]),
        "features": sum(len(record["features"]) for record in details),
        "parts_detail": details,
        "error": {
            "part": getattr(builder, "part_id", None),
            "feature": getattr(builder, "feature_id", None),
            "code": error.code, "message": str(error), "dialog": error.data.get("watchdog"),
            "note": "The CATIA call is still pending: close the dialog in CATIA, check the "
                    "document (it may contain a partial model), then run the build again.",
        },
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def _build(tree, args) -> int:
    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection
    from catia_mcp.errors import CATIAToolError, SessionError
    from catia_mcp.tree.builder import TreeBuilder

    try:
        expected = _expected(tree, args.expect)
    except Exception as error:
        print(f"Cannot compute the expected volumes: {error}")
        return 2
    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    # Every CATIA call (connect included) runs on this worker; the budget
    # check between features stays the builder's, this timeout is the backstop.
    worker = ComWorker(conn.watchdog, timeout_s=args.deadline + 60, name="catia-tree")
    try:
        print(worker.run_guarded(conn.connect, label="connect"))
    except (RuntimeError, CATIAToolError) as error:
        print(error)
        return 2
    from catia_mcp.capabilities import probe

    # The probe uses the connection, so it runs on the worker that made it.
    try:
        capabilities = worker.run_guarded(lambda: probe(conn, save=False),
                                          label="the capability probe")
    except SessionError as error:
        print(error)
        return 2
    reasons = analyze(tree, capabilities, args.plm_type)
    if reasons:
        print(refusal(reasons))
        return 1
    builder = TreeBuilder(conn, plm_type=args.plm_type, deadline_s=args.deadline, log=print,
                          ownership=args.ownership)
    started = time.monotonic()
    try:
        report = worker.run_guarded(builder.build, tree, expected, not args.no_edit,
                                    label="the tree build")
    except TreeRefused as refused:
        print(refused)
        return 1
    except SessionError as error:
        report = _blocked_report(tree, builder, error, started)
    if worker.dismissed:
        report["dismissed_dialogs"] = worker.dismissed
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    for part in report["parts_detail"]:
        want = part["expected_volume_mm3"]
        versus = f" (build123d {want:.4f})" if want is not None else ""
        print(f"{part['id']}: {part['catia_name']}, volume {part['volume_mm3']}{versus}, "
              f"editable {part['editable']}")
    if report["success"]:
        print(f"Built in {report['elapsed_s']} s. Report: {args.report}")
        return 0
    error = report["error"]
    print(f"FAILED at {error['part']} / {error['feature']}: {error['message']}\n{error['note']}")
    print(f"Report: {args.report}")
    return 1


def _capabilities(args) -> int:
    from catia_mcp.capabilities import probe
    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection
    from catia_mcp.errors import CATIAToolError, SessionError

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    # Risky probes can open a blocking dialog: run them under the watchdog.
    worker = ComWorker(conn.watchdog, timeout_s=args.deadline + 60, name="catia-probe")
    try:
        print(worker.run_guarded(conn.connect, label="connect"))
        report = worker.run_guarded(
            lambda: probe(conn, create=args.create, risky=args.risky or (),
                          use_cache=not args.refresh),
            label="the capability probe")
    except (RuntimeError, CATIAToolError, SessionError) as error:
        print(error)
        return 2
    path = args.report or "capabilities_report.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(report["summary"])
    print(f"Report: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m catia_mcp.tree",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["check", "reference", "build", "capabilities"])
    parser.add_argument("tree", nargs="?", help="feature tree JSON (version 1)")
    parser.add_argument("--part", help="only this part id (development)")
    parser.add_argument("--until", help="only the features up to this id (development)")
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"],
                        help="3DEXPERIENCE content type for each part (default 3DShape)")
    parser.add_argument("--expect", default="auto",
                        help="'auto' (build123d), 'none', or a JSON file {part id: {volume_mm3}}")
    parser.add_argument("--ownership", choices=["auto", "strict", "off"],
                        help="fillet/chamfer/shell topology ownership filter (default: "
                             "$CATIA_MCP_TOPOLOGY_OWNERSHIP or auto)")
    parser.add_argument("--no-edit", action="store_true", help="skip the editability check")
    parser.add_argument("--deadline", type=float, default=170.0, help="build budget in seconds")
    parser.add_argument("--report", help="JSON report path (default tree_report.json, "
                        "capabilities_report.json)")
    parser.add_argument("--create", action="store_true",
                        help="capabilities: build a scratch part, closed without saving")
    parser.add_argument("--risky", action="append", choices=["3dpart", "step_export"],
                        help="capabilities: also run this risky probe (repeatable)")
    parser.add_argument("--refresh", action="store_true",
                        help="capabilities: ignore the results cached by earlier probes")
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        return _capabilities(args)
    if not args.tree:
        parser.error(f"{args.command} needs a feature tree JSON file")
    args.report = args.report or "tree_report.json"
    tree = _load(args)
    if args.command == "check":
        return _check(tree)
    if args.command == "reference":
        return _reference(tree)
    return _build(tree, args)


if __name__ == "__main__":
    sys.exit(main())
