"""Feature tree command line.

    python -m catia_mcp.tree check TREE.json         preflight only, no CATIA
    python -m catia_mcp.tree reference TREE.json     expected solids (needs build123d)
    python -m catia_mcp.tree build TREE.json --plm-type 3DShape

build attaches to a running CATIA (it never starts one), builds every part in
a new part, compares each part's volume with build123d's when build123d is
installed, proves editability, and writes a JSON report. Nothing is saved.
--part and --until build a prefix of one part, for development.
--step-dir DIR lets STEP files stand in for features with no native rule
(CATIA V5 only; see docs/step.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from catia_mcp.tree.preflight import TreeRefused, analyze, refusal, truncate


def _load(args) -> dict[str, Any]:
    with open(args.tree, encoding="utf-8") as handle:
        tree = json.load(handle)
    if args.part or args.until:
        tree = truncate(tree, args.part, args.until)
    return tree


def _step_source(args):
    if not args.step_dir:
        return None
    from catia_mcp.connection import normalize_platform
    from catia_mcp.tree.step_source import StepSource

    return StepSource(args.step_dir, normalize_platform(args.platform))


def _expected(tree: dict[str, Any], mode: str, step_dir: str | None = None) -> dict[str, Any] | None:
    if mode == "none":
        return None
    if mode != "auto":
        with open(mode, encoding="utf-8") as handle:
            return json.load(handle)
    try:
        from catia_mcp.tree.reference import expected
        if step_dir:
            from catia_mcp.tree.step_fallback import expected_with_steps
    except ImportError:
        print("build123d is not installed: building without expected volumes "
              '(pip install -e ".[differential]")')
        return None
    return expected_with_steps(tree, step_dir) if step_dir else expected(tree)


def _check(tree, step=None) -> int:
    reasons = analyze(tree, step)
    if reasons:
        print(refusal(reasons))
        return 1
    print(f"Accepted: {len(tree['parts'])} part(s), "
          f"{sum(len(p['features']) for p in tree['parts'])} feature(s).")
    if step is not None:
        from catia_mcp.tree.preflight import SUPPORTED

        dead = sum(f.get("type") not in SUPPORTED for p in tree["parts"] for f in p["features"])
        if dead:
            print(f"{dead} of them from STEP files in {step.directory}, as bodies without "
                  "history (the build needs CATIA V5).")
    return 0


def _report_dead(report: dict[str, Any]) -> None:
    for dead in report.get("dead_bodies") or []:
        print(f"  dead body {dead['body']} ({dead['part']} / {dead['feature']}, {dead['role']}, "
              f"{dead['volume_mm3']} mm3) from {dead['file']}: no history")


def _reference(tree, step_dir=None) -> int:
    from catia_mcp.tree.reference import expected

    if step_dir:
        from catia_mcp.tree.step_fallback import expected_with_steps
    for part_id, props in (expected_with_steps(tree, step_dir) if step_dir
                           else expected(tree)).items():
        cog = ", ".join(f"{c:.4f}" for c in props["cog_mm"])
        print(f"{part_id}: volume {props['volume_mm3']:.4f} mm3, area {props['area_mm2']:.4f} mm2, "
              f"cog ({cog})")
    return 0


def _build(tree, args) -> int:
    from catia_mcp.connection import CATIAConnection
    from catia_mcp.tree.builder import TreeBuilder

    try:
        expected = _expected(tree, args.expect, args.step_dir)
    except Exception as error:
        print(f"Cannot compute the expected volumes: {error}")
        return 2
    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    try:
        print(conn.connect())
    except RuntimeError as error:
        print(error)
        return 2
    builder = TreeBuilder(conn, plm_type=args.plm_type, deadline_s=args.deadline, log=print,
                          step_dir=args.step_dir)
    try:
        report = builder.build(tree, expected, check_editability=not args.no_edit)
    except TreeRefused as refused:
        print(refused)
        return 1
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    for part in report["parts_detail"]:
        want = part["expected_volume_mm3"]
        versus = f" (build123d {want:.4f})" if want is not None else ""
        print(f"{part['id']}: {part['catia_name']}, volume {part['volume_mm3']}{versus}, "
              f"editable {part['editable']}")
    _report_dead(report)
    if report["success"]:
        print(f"Built in {report['elapsed_s']} s. Report: {args.report}")
        return 0
    error = report["error"]
    print(f"FAILED at {error['part']} / {error['feature']}: {error['message']}\n{error['note']}")
    print(f"Report: {args.report}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m catia_mcp.tree",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["check", "reference", "build"])
    parser.add_argument("tree", help="feature tree JSON (version 1)")
    parser.add_argument("--part", help="only this part id (development)")
    parser.add_argument("--until", help="only the features up to this id (development)")
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"],
                        help="3DEXPERIENCE content type for each part (default 3DShape)")
    parser.add_argument("--expect", default="auto",
                        help="'auto' (build123d), 'none', or a JSON file {part id: {volume_mm3}}")
    parser.add_argument("--no-edit", action="store_true", help="skip the editability check")
    parser.add_argument("--deadline", type=float, default=170.0, help="build budget in seconds")
    parser.add_argument("--report", default="tree_report.json", help="JSON report path")
    parser.add_argument("--step-dir", help="folder of STEP files standing in for features with "
                        "no native rule: <part folder>/<feature id>.step or "
                        ".add.step / .remove.step (CATIA V5 only)")
    args = parser.parse_args(argv)
    tree = _load(args)
    if args.command == "check":
        return _check(tree, _step_source(args))
    if args.command == "reference":
        return _reference(tree, args.step_dir)
    return _build(tree, args)


if __name__ == "__main__":
    sys.exit(main())
