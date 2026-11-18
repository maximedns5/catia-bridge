"""Feature tree command line.

    python -m catia_mcp.tree check TREE.json         preflight only, no CATIA
    python -m catia_mcp.tree reference TREE.json     expected solids (needs build123d)
    python -m catia_mcp.tree build TREE.json --plm-type 3DShape
    python -m catia_mcp.tree capabilities [--create] [--risky 3dpart]
    python -m catia_mcp.tree mates TREE.json --step MODEL.step   assembly constraints, no CATIA

build attaches to a running CATIA (it never starts one), builds every part in
a new part, compares each part's volume with build123d's when build123d is
installed, proves editability, and writes a JSON report. Nothing is saved.
Before building it probes what this CATIA can do (read-only, plus the cached
results of earlier capability probes) and refuses a tree that needs something
found unavailable. CATIA runs on a guarded worker thread: a dialog that blocks
the build ends it with a report naming the part, the feature and the dialog's
text. --part and --until build a prefix of one part, for development.
--drawing auto then makes the drawing of each part and of the product
(docs/drafting.md).
--step-dir DIR lets STEP files stand in for features with no native rule
(CATIA V5 only; see docs/step.md).

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
    reasons = analyze(tree, step=step)
    if reasons:
        print(refusal(reasons))
        return 1
    print(f"Accepted: {len(tree['parts'])} part(s), "
          f"{sum(len(p['features']) for p in tree['parts'])} feature(s).")
    if step is not None:
        from catia_mcp.tree.preflight import SUPPORTED, native

        dead = sum(f.get("type") not in SUPPORTED or not native(f)
                   for p in tree["parts"] for f in p["features"])
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


def _assembly_plan(tree, args):
    """The assembly plan, from the model's STEP file when given; None, with
    the reasons printed, when the STEP does not match the tree."""
    from catia_mcp.tree.assembly import plan_assembly

    structure = world = None
    if args.step:
        from catia_mcp.tree.step_structure import read

        try:
            structure = read(args.step)
        except ImportError:
            print("Reading the STEP file's assembly needs OCP: pip install -e \".[differential]\"")
            return None
        world = _expected(tree, "auto") if args.expect == "auto" else None
        if world:
            world = {k: {"volume_mm3": v["volume_mm3"], "cog_mm": v["cog_mm"]}
                     for k, v in world.items()}
    plan, problems = plan_assembly(tree, structure, world)
    if problems:
        print("The STEP file does not match the tree:\n  " + "\n  ".join(problems))
        return None
    return plan


def _adopt(conn, worker, plan, builder):
    """Match the product imported from the STEP file to the plan and point
    the builder at each part to rebuild; None, with the reasons printed,
    when the product does not match."""
    from catia_mcp.tree import adopt

    if not conn.is_3dx:
        print("--assembly adopt works inside a 3DEXPERIENCE product; on V5 use --assembly product")
        return None
    try:
        leaves = worker.run_guarded(lambda: adopt.leaf_occurrences(conn),
                                    label="the imported product")
    except Exception as error:
        print(f"Cannot read the active product ({error}): import the model's STEP file and "
              "keep its window active")
        return None
    references, rows, problems = adopt.match(plan, leaves)
    if problems:
        print("The active product does not match the model:\n  " + "\n  ".join(problems))
        return None
    for prototype, reference in references.items():
        builder.targets[prototype] = worker.run_guarded(lambda r=reference: adopt.part_of(r),
                                                         label="the imported part")
    print(f"Adopted the imported product: {len(rows)} instance(s) of {len(references)} part(s).")
    return rows, leaves, references


def _mates(plan, args):
    """The assembly constraints to put (--constraints), offline; None when
    there are none or they cannot be inferred (the build goes on without)."""
    if plan is None or args.constraints == "none":
        return None
    from catia_mcp.tree.assembly_constraints import plan_mates

    try:
        mate_plan, components = plan_mates(plan, args.constraints, args.step, args.ground)
    except Exception as error:  # no OCP, an unreadable shape: placed without constraints
        print(f"Assembly constraints skipped: {error}")
        return None
    print(f"Assembly constraints: {mate_plan.summary()}")
    return mate_plan, components


def _constrain(report, apply, mate_plan=None) -> None:
    """Put the constraints; a failure is reported, never fatal (every
    instance stays where the STEP file puts it). The inferred mates go in
    the report either way, for a designer to add by hand (live on
    3DEXPERIENCE R428 no constraint can be given through COM)."""
    try:
        report["assembly"]["constraints"] = result = apply()
        print(f"Constraints: {result.get('counts')}, positions kept: {result.get('positions_ok')}")
    except Exception as error:
        report["assembly"]["constraints"] = {"ok": False, "error": str(error)}
        if mate_plan is not None:
            report["assembly"]["constraints"]["mates"] = mate_plan.to_dict()
        print(f"Constraints not applied: {error}")


def _assemble(conn, worker, builder, plan, report, mates=None) -> None:
    """Place the built parts in a product (V5); on failure the parts stay
    built and the report says why."""
    from catia_mcp.tree.assembler import AssemblerV5

    if conn.is_3dx:
        report["assembly"] = {
            "ok": False, "product": plan.root.name,
            "note": "3DEXPERIENCE: the parts are built in their own frames; a scripted "
                    "Physical Product needs licences most roles lack (see "
                    "scripts/probe_assembly.py). Import the model's STEP to get the product."}
        return
    assembler = AssemblerV5(conn.app, log=print)
    try:
        report["assembly"] = worker.run_guarded(
            lambda: assembler.assemble(plan, builder.part_documents), label="the assembly")
        print(f"Assembled {report['assembly']['catia_product']}: "
              f"{len(report['assembly']['instances'])} instance(s).")
    except Exception as error:  # the parts stay built
        report["assembly"] = {"ok": False, "product": plan.root.name, "error": str(error)}
        report["success"] = False
        report["error"] = {"part": None, "feature": "assembly", "message": str(error),
                           "note": "The parts are built; the product may be partial."}
        return
    if mates is not None:
        from catia_mcp.tree.assembly_constraints import apply_v5

        _constrain(report, lambda: worker.run_guarded(
            lambda: apply_v5(conn.app, assembler, plan, *mates, builder.part_documents,
                             report["assembly"], log=print), label="the assembly constraints"))


def _drawings(conn, worker, tree, builder, report, plan, adopted: bool, args) -> None:
    """The drawing of every built part and of the product (docs/drafting.md):
    the report's `drawing` section. A failed drawing leaves the build a success."""
    from catia_mcp.tree.drawing_builder import make_drawings
    from catia_mcp.tree.drawing_plan import DrawingOptions

    options = DrawingOptions(projection=args.drawing_projection)
    report["drawing"] = make_drawings(conn, worker, tree, builder, report, plan, options,
                                      pdf_dir=args.drawing_pdf, adopted=adopted, log=print)
def _properties(tree, args, capabilities):
    """The properties step (docs/materials-properties.md), or None when off.
    Parts without a colour take their STEP instance's when --step is given."""
    from catia_mcp.tree.properties import PartProperties

    if args.properties == "off":
        return None
    if args.step:
        try:
            from catia_mcp.tree.properties import colours_from_step
            from catia_mcp.tree.step_structure import read

            coloured = colours_from_step(tree, read(args.step))
            if coloured:
                print(f"Colours from the STEP file: {', '.join(coloured)}")
        except Exception as error:  # a colour is a bonus
            print(f"No colours read from the STEP file: {error}")
    status = ((capabilities or {}).get("capabilities") or {}).get("knowledge.rules") or {}
    advisor = {"on": True, "off": False}.get(
        args.checks, status.get("status") in ("verified", "authorized"))
    return PartProperties(args.properties, catalog=args.material_catalog,
                          sheet_dir=args.sheet_dir, knowledge_advisor=advisor)


def _build(tree, args) -> int:
    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection
    from catia_mcp.errors import CATIAToolError, SessionError
    from catia_mcp.tree.builder import TreeBuilder

    plan = None
    if args.assembly in ("product", "adopt"):
        if args.assembly == "adopt" and not args.step:
            print("--assembly adopt needs --step MODEL.step, the file imported into CATIA")
            return 2
        plan = _assembly_plan(tree, args)
        if plan is None:
            return 1
        print(plan.summary())
        tree = plan.tree  # one part per prototype, in its own frame
    mates = _mates(plan, args)
    try:
        expected = _expected(tree, args.expect, args.step_dir)
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
    step = None
    if args.step_dir:
        from catia_mcp.tree.step_source import StepSource

        step = StepSource(args.step_dir, "3dx" if conn.is_3dx else "v5")
    reasons = analyze(tree, capabilities, args.plm_type, step=step)
    if reasons:
        print(refusal(reasons))
        return 1
    builder = TreeBuilder(conn, plm_type=args.plm_type, deadline_s=args.deadline, log=print,
                          ownership=args.ownership, step_dir=args.step_dir, style=args.style)
    builder.properties = _properties(tree, args, capabilities)
    adopted = None
    if args.assembly == "adopt":
        adopted = _adopt(conn, worker, plan, builder)
        if adopted is None:
            return 1
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
    if adopted is not None:
        rows, leaves, references = adopted
        report["assembly"] = {"ok": bool(report.get("success")), "mode": "adopt",
                              "product": plan.root.name, "instances": rows}
        if mates is not None and report.get("success"):
            from catia_mcp.tree.assembly_constraints import apply_3dx

            _constrain(report, lambda: worker.run_guarded(
                lambda: apply_3dx(conn, plan, *mates, leaves, builder.targets, references,
                                  log=print), label="the assembly constraints"), mates[0])
    elif plan is not None and report.get("success"):
        _assemble(conn, worker, builder, plan, report, mates)
    if args.drawing == "auto" and report.get("success"):
        _drawings(conn, worker, tree, builder, report, plan, adopted is not None, args)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    for part in report["parts_detail"]:
        want = part["expected_volume_mm3"]
        versus = f" (build123d {want:.4f})" if want is not None else ""
        print(f"{part['id']}: {part['catia_name']}, volume {part['volume_mm3']}{versus}, "
              f"editable {part['editable']}")
        if part.get("properties"):
            from catia_mcp.tree.properties import summary

            ok = "ok" if part["properties"].get("ok") else "NOT ok"
            print(f"  properties {ok}: {summary(part['properties'])}")
    _report_dead(report)
    if report["success"]:
        print(f"Built in {report['elapsed_s']} s. Report: {args.report}")
        return 0
    error = report["error"]
    print(f"FAILED at {error['part']} / {error['feature']}: {error['message']}\n{error['note']}")
    print(f"Report: {args.report}")
    return 1


def _mates_command(tree, args) -> int:
    """The assembly constraints the build would put, without CATIA."""
    if args.constraints == "none":
        args.constraints = "infer"
    plan = _assembly_plan(tree, args)
    if plan is None:
        return 1
    mates = _mates(plan, args)
    if mates is None:
        return 1
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(mates[0].to_dict(), handle, indent=2, ensure_ascii=False)
    for placement in mates[0].placements:
        print(f"  {placement.component}: {placement.status}"
              f"{f' ({placement.note})' if placement.note else ''}")
        for mate in placement.mates:
            value = f" {mate.value:g}" if mate.value is not None else ""
            print(f"    {mate.kind}{value}: {mate.element.kind} {mate.element.face} with "
                  f"{mate.other} {mate.other_element.face}")
    print(f"Report: {args.report}")
    return 0


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
    parser.add_argument("command", choices=["check", "reference", "build", "capabilities",
                                            "mates"])
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
    parser.add_argument("--style", default="human", choices=["human", "exact"],
                        help="human (default): joins and cuts go straight into their body as "
                             "Pad, Pocket, Shaft, Groove, checked against build123d after each "
                             "feature; exact: each solid in a tool body, checked by a closed "
                             "form, then combined. Without build123d the build is exact.")
    parser.add_argument("--assembly", default="none", choices=["none", "product", "adopt"],
                        help="product: build each part once, in its own frame when --step "
                             "gives the model's STEP file, then place every instance in a "
                             "CATIA product (V5). adopt (3DEXPERIENCE): the model's STEP "
                             "imported by hand is the active product; each of its parts is "
                             "rebuilt there with native features, replacing the imported solid")
    parser.add_argument("--step", help="the model's STEP file (same compile as the tree): "
                        "its assembly structure, part frames, names and colours")
    parser.add_argument("--constraints", default="none", choices=["none", "fix", "infer"],
                        help="with --assembly: none (default), fix every instance in place, or "
                             "infer coincidence, contact and offset constraints from the faces "
                             "(docs/assembly-constraints.md)")
    parser.add_argument("--ground", help="--constraints infer: the instance id to ground "
                        "(default: the largest)")
    parser.add_argument("--drawing", default="none", choices=["none", "auto"],
                        help="auto: after the build, a drawing of each part (and of the "
                             "product) with the views, scale and sheet a designer would choose; "
                             "the report's 'drawing' section (docs/drafting.md)")
    parser.add_argument("--drawing-projection", default="first", choices=["first", "third"],
                        help="first angle (ISO E, default) or third angle (ISO A)")
    parser.add_argument("--drawing-pdf", help="folder to export each drawing to as PDF")
    parser.add_argument("--properties", default="auto", choices=["auto", "on", "off"],
                        help="colour, material and mass, product properties and "
                             "Knowledgeware after each part (docs/materials-properties.md): "
                             "auto (default) for parts whose tree carries those fields, on for "
                             "every part (part number and tree id at least), off")
    parser.add_argument("--material-catalog", help="CATIA V5 material catalog "
                        "(.CATMaterial); default: materials/*.CATMaterial under CATStartupPath")
    parser.add_argument("--sheet-dir", help="folder for the design table sheets the bridge "
                        "writes (default: the temporary folder)")
    parser.add_argument("--checks", default="auto", choices=["auto", "on", "off"],
                        help="create the tree's checks (Knowledge Advisor): auto when the "
                             "capability probe found its licence, on, off")
    parser.add_argument("--no-edit", action="store_true", help="skip the editability check")
    parser.add_argument("--deadline", type=float, default=170.0, help="build budget in seconds")
    parser.add_argument("--report", help="JSON report path (default tree_report.json, "
                        "capabilities_report.json)")
    parser.add_argument("--step-dir", help="folder of STEP files standing in for features with "
                        "no native rule: <part folder>/<feature id>.step or "
                        ".add.step / .remove.step (CATIA V5 only)")
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
    args.report = args.report or ("mates_report.json" if args.command == "mates"
                                  else "tree_report.json")
    tree = _load(args)
    if args.command == "check":
        return _check(tree, _step_source(args))
    if args.command == "reference":
        return _reference(tree, args.step_dir)
    if args.command == "mates":
        return _mates_command(tree, args)
    return _build(tree, args)


if __name__ == "__main__":
    sys.exit(main())
