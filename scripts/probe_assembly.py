"""Live probe of what 3DEXPERIENCE lets a script do with assemblies.

The tree replay builds each part in its own 3D Shape. To put them in a
Physical Product, either a script creates the product and its instances
(needs licences the student roles may lack) or the user imports the model's
STEP file, which gives the product, and the bridge works inside it. This
probe records what this session allows, without saving anything:

1. the licences behind products (E70, VMX);
2. with --create: whether a script may create a Physical Product
   (PLMCreate "VPMReference"), watched for a blocking dialog;
3. the active editor's product structure: every occurrence under the root,
   with its instance name, its reference's title and type, and its position
   (Position.GetComponents: the axes, then the origin);
4. with --step: the positions compared with the STEP file's instances
   (the user imported that STEP file into the active editor first);
5. with --move: one instance moved 10 mm along X through
   Position.SetComponents, read back, and moved back;
6. for the first occurrence: which members reach its part (for building
   native features inside an imported part later);
7. whether occurrences of one part share one reference (a bolt placed twice
   should be one part);
8. with --build-inside: in the first occurrence's part, from the product's
   window, a new body with a 5 mm pad of a 3 mm circle: is it built, can it
   be measured (volume, centre of gravity) and searched (faces) there, and
   is it removed again.

Usage (Windows, CATIA started and logged in; for 3 to 6, a product open in
the active window, for example the model's STEP imported with Ajouter >
Importer):
    python scripts/probe_assembly.py --report assembly_probe.json
    python scripts/probe_assembly.py --step tests\\fixtures\\assembly\\bracket.step --move
    python scripts/probe_assembly.py --create
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as error:  # recorded, never fatal
        return f"error: {error}"


def licences(conn: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for product in ("E70.prd", "VMX.prd", "MDG.prd", "PDG.prd"):
        out[product] = _safe(lambda p=product: bool(conn.app.SystemConfiguration
                                                  .IsProductAuthorized(p)))
    return out


def create_product(conn: Any) -> dict[str, Any]:
    from catia_mcp.plm import PLMSession

    record: dict[str, Any] = {}
    started = time.monotonic()
    try:
        created = PLMSession(conn).create("VPMReference")
        record["created"] = True
        record["type"] = _safe(lambda: type(created).__name__)
        with contextlib.suppress(Exception):
            record["window"] = str(conn.app.ActiveWindow.Caption)
    except Exception as error:
        record["created"] = False
        record["error"] = str(error)
    record["seconds"] = round(time.monotonic() - started, 2)
    record["dialog"] = _safe(lambda: conn.watchdog.blocking_dialog())
    return record


def occurrences(conn: Any) -> list[dict[str, Any]]:
    from catia_mcp.automation import call_fill_array, safe_get
    from catia_mcp.introspect import type_name
    from catia_mcp.plm import PLMSession

    plm = PLMSession(conn)
    root = conn.root_occurrence()
    rows: list[dict[str, Any]] = []

    def visit(occurrence: Any, path: str) -> None:
        for child in plm.child_occurrences(occurrence):
            instance = safe_get(child, "InstanceOccurrenceOf")
            reference = safe_get(instance, "ReferenceInstanceOf") if instance is not None else None
            name = plm.occurrence_name(child)
            row: dict[str, Any] = {"path": f"{path}/{name}", "occurrence": type_name(child),
                                   "instance": type_name(instance) if instance else None}
            if reference is not None:
                row["reference"] = type_name(reference)
                row["title"] = plm.attribute(reference, "V_Name")
                row["reference_id"] = plm.attribute(reference, "PLM_ExternalID")
                row["reference_physical_id"] = plm.attribute(reference, "physicalid")
            holder = instance if instance is not None else child
            row["position"] = _safe(lambda h=holder: [round(float(v), 6) for v in
                                    call_fill_array(conn.app, h.Position, "GetComponents", 12)])
            rows.append(row)
            visit(child, row["path"])

    visit(root, "")
    return rows


def compare(rows: list[dict[str, Any]], step: str) -> list[dict[str, Any]]:
    """Each STEP instance's position next to the occurrence in the same
    place of the list (CATIA keeps the STEP order when importing: to check)."""
    from catia_mcp.tree.placement import catia_components
    from catia_mcp.tree.step_structure import read

    structure = read(step)
    placed = [r for r in rows if isinstance(r.get("position"), list)]
    out = []
    for i, instance in enumerate(structure.instances):
        want = catia_components(instance.local)
        got = placed[i]["position"] if i < len(placed) else None
        out.append({"step": instance.id, "catia": placed[i]["path"] if got else None,
                    "want": [round(v, 6) for v in want], "got": got,
                    "same": got is not None and all(abs(a - b) < 1e-4 for a, b in
                                                    zip(got, want, strict=True))})
    return out


def move_one(conn: Any) -> dict[str, Any]:
    from catia_mcp.automation import call_fill_array, safe_get
    from catia_mcp.plm import PLMSession

    plm = PLMSession(conn)
    children = plm.child_occurrences(conn.root_occurrence())
    if not children:
        return {"error": "no occurrence under the root"}
    child = children[0]
    instance = safe_get(child, "InstanceOccurrenceOf")
    holder = instance if instance is not None else child
    record: dict[str, Any] = {"occurrence": plm.occurrence_name(child)}
    try:
        before = list(call_fill_array(conn.app, holder.Position, "GetComponents", 12))
        moved = before[:9] + [before[9] + 10.0, before[10], before[11]]
        holder.Position.SetComponents(moved)
        after = list(call_fill_array(conn.app, holder.Position, "GetComponents", 12))
        holder.Position.SetComponents(before)
        back = list(call_fill_array(conn.app, holder.Position, "GetComponents", 12))
        record.update(before=before, after=after, back=back,
                      moved_10mm=abs(after[9] - before[9] - 10.0) < 1e-6,
                      restored=all(abs(a - b) < 1e-9 for a, b in zip(back, before, strict=True)))
    except Exception as error:
        record["error"] = str(error)
        record["traceback"] = traceback.format_exc()
    return record


def reach_part(conn: Any) -> dict[str, Any]:
    """Which members lead from the first occurrence to a Part object."""
    from catia_mcp.automation import safe_get
    from catia_mcp.introspect import type_name
    from catia_mcp.plm import PLMSession

    plm = PLMSession(conn)
    children = plm.child_occurrences(conn.root_occurrence())
    if not children:
        return {"error": "no occurrence under the root"}
    record: dict[str, Any] = {}
    instance = safe_get(children[0], "InstanceOccurrenceOf")
    reference = safe_get(instance, "ReferenceInstanceOf") if instance is not None else None
    record["reference"] = type_name(reference) if reference is not None else None
    rep_instances = safe_get(reference, "RepInstances") if reference is not None else None
    record["rep_instances"] = _safe(lambda: int(rep_instances.Count)) if rep_instances else None
    if rep_instances is not None:
        rep = _safe(lambda: rep_instances.Item(1))
        rep_ref = safe_get(rep, "ReferenceInstanceOf") if not isinstance(rep, str) else None
        record["rep_reference"] = type_name(rep_ref) if rep_ref is not None else str(rep)
        part = _safe(lambda: rep_ref.GetItem("Part")) if rep_ref is not None else None
        record["part"] = type_name(part) if part is not None and not isinstance(part, str) \
            else part
    return record


def first_part(conn: Any) -> Any:
    from catia_mcp.automation import safe_get
    from catia_mcp.plm import PLMSession

    child = PLMSession(conn).child_occurrences(conn.root_occurrence())[0]
    instance = safe_get(child, "InstanceOccurrenceOf")
    reference = safe_get(instance, "ReferenceInstanceOf")
    return reference.RepInstances.Item(1).ReferenceInstanceOf.GetItem("Part")


def build_inside(conn: Any) -> dict[str, Any]:
    """A throwaway body inside an imported part, from the product's window:
    built, measured, searched, then deleted."""
    import math

    from catia_mcp.measure import Measurer
    from catia_mcp.references import _search_topology

    record: dict[str, Any] = {}
    part = first_part(conn)
    record["part"] = str(part.Name)
    record["bodies_before"] = _safe(lambda: int(part.Bodies.Count))
    body = None
    try:
        body = part.Bodies.Add()
        record["body"] = str(body.Name)
        sketch = body.Sketches.Add(part.CreateReferenceFromObject(part.OriginElements.PlaneXY))
        factory = sketch.OpenEdition()
        factory.CreateClosedCircle(0.0, 0.0, 3.0)
        sketch.CloseEdition()
        part.InWorkObject = body
        pad = part.ShapeFactory.AddNewPad(sketch, 5.0)
        part.UpdateObject(pad)
        record["pad"] = str(pad.Name)
        record["expected_mm3"] = round(math.pi * 9 * 5, 4)
        measure = Measurer(conn)
        record["volume_mm3"] = _safe(lambda: round(measure.volume_mm3(part, body), 4))
        record["cog_mm"] = _safe(lambda: [round(c, 4) for c in measure.cog_mm(part, body)])
        sel = conn.hso
        record["faces"] = _safe(lambda: _search_topology(sel, body, "Face"))
        with contextlib.suppress(Exception):
            sel.Clear()
    except Exception as error:
        record["error"] = str(error)
        record["traceback"] = traceback.format_exc()
    finally:
        if body is not None:
            try:
                sel = conn.hso
                sel.Clear()
                sel.Add(body)
                sel.Delete()
                sel.Clear()
                part.Update()
                record["removed"] = True
            except Exception as error:
                record["removed"] = f"error: {error}"
        record["bodies_after"] = _safe(lambda: int(part.Bodies.Count))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", default="assembly_probe.json")
    parser.add_argument("--create", action="store_true",
                        help="try to create a Physical Product by script (may open a dialog)")
    parser.add_argument("--step", help="the STEP file imported into the active editor")
    parser.add_argument("--move", action="store_true",
                        help="move the first instance 10 mm along X, read back, move back")
    parser.add_argument("--build-inside", action="store_true",
                        help="pad, measure and delete a body inside the first instance's part")
    args = parser.parse_args()

    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection(platform="3dx", allow_launch=False)
    worker = ComWorker(conn.watchdog, timeout_s=120, name="probe-assembly")
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        report["connect"] = worker.run_guarded(conn.connect, label="connect")
    except Exception as error:
        print(error)
        return 2
    steps = [("licences", lambda: licences(conn))]
    if args.create:
        steps.append(("create_product", lambda: create_product(conn)))
    steps.append(("occurrences", lambda: occurrences(conn)))
    for name, step in steps:
        print(f"  {name}")
        try:
            report[name] = worker.run_guarded(step, label=name)
        except Exception as error:  # keep what was found so far
            report[name] = {"error": str(error)}
    if args.step and isinstance(report.get("occurrences"), list):
        report["compare"] = _safe(lambda: compare(report["occurrences"], args.step))
    if args.move:
        report["move"] = _safe(lambda: worker.run_guarded(lambda: move_one(conn), label="move"))
    report["reach_part"] = _safe(lambda: worker.run_guarded(lambda: reach_part(conn),
                                                            label="reach the part"))
    rows = report.get("occurrences") if isinstance(report.get("occurrences"), list) else []
    shared: dict[str, list[str]] = {}
    for row in rows:
        key = row.get("reference_physical_id") or row.get("reference_id")
        if key:
            shared.setdefault(str(key), []).append(row["path"])
    report["shared_references"] = {k: v for k, v in shared.items() if len(v) > 1}
    if args.build_inside:
        report["build_inside"] = _safe(lambda: worker.run_guarded(lambda: build_inside(conn),
                                                                  label="build inside"))
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
