"""Live probe of a clash (interference) API on the active product.

On CATIA V5 a script computes clashes with DMU Space Analysis
(`Product.GetTechnologicalObject("Clashes")`, `Clashes.Add`, `Clash.Compute`,
`Clash.Conflicts`), which the build's `--clashes catia` uses. Whether
3DEXPERIENCE exposes anything similar to automation is not documented. This
probe attaches to a running CATIA (it never starts one), works in the active
window's product (the model's STEP imported by hand, as for
`--assembly adopt`), and records in a JSON report, each step on its own (a
failure is recorded and the next step runs; a dialog is caught by the
watchdog and recorded):

1. the window: with --window, only that window is activated (activating
   every window in turn hung CATIA live); whether it shows a product;
2. the enums of this installation's type libraries whose names contain
   clash, interference, conflict, clearance or penetration, with their values;
3. the members of the root occurrence, its reference, the first instance,
   the editor and the application whose names contain clash, interf,
   conflict, technological, service or getitem, with their signatures;
4. `GetItem(name)` on the root reference, the root occurrence and the first
   instance for each candidate name ("Clashes", "CATClashes"...);
5. `GetTechnologicalObject(name)` where it exists (the root occurrence, its
   reference, the editor's active object, a V5 product);
6. `GetService(name)` on the editor and `GetSessionService(name)` on the
   application for each candidate service name;
7. the offline analysis of --tree / --step (catia_mcp/tree/clashes.py), the
   reference to compare with;
8. with --create, on the first object found that has `Add`: a clash
   (between all, contact, or clearance with --clearance), computed, its
   conflicts read and compared with the offline result. Removed again when
   the collection has `Remove`; otherwise close the product without saving.

Usage (Windows PowerShell, CATIA started, the model's STEP imported):
    python scripts/probe_clashes.py --window "Bracket A:4" --report clashes_probe.json
    python scripts/probe_clashes.py --window "Bracket A:4" --create
    python scripts/probe_clashes.py --tree tests\\fixtures\\assembly\\pin_interference.tree.json
        --step tests\\fixtures\\assembly\\pin_interference.step --create
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from probe_constraints import _safe, _signatures, product_window

FIXTURES = os.path.join(HERE, "..", "tests", "fixtures", "assembly")
ENUM_WORDS = ("clash", "interf", "conflict", "clearance", "penetration")
MEMBER_WORDS = ("clash", "interf", "conflict", "technological", "service", "getitem")
ITEM_NAMES = ("Clashes", "CATClashes", "Clash", "CATClash", "ClashResults", "Interferences",
              "CATInterferences", "Interference", "SPAClashes", "DMUClashes", "CATSpaClashes")
TECHNOLOGICAL_NAMES = ("Clashes", "Clash", "Interferences", "CATClashes", "Measurable",
                       "SPAWorkbench")
SERVICE_NAMES = ("ClashService", "CATClashService", "PLMClashService", "InterferenceService",
                 "CATInterferenceService", "SPAService", "CATSPAService", "DMUService",
                 "CATDMUService", "ClashSimulationService", "SpaceAnalysisService")


def _objects(conn: Any) -> dict[str, Any]:
    """The objects to look on, by label; a missing one is left out."""
    from catia_mcp.automation import safe_get
    from catia_mcp.plm import PLMSession

    out: dict[str, Any] = {"application": conn.app}
    editor = safe_get(conn.app, "ActiveEditor")
    if editor is not None:
        out["editor"] = editor
        active = safe_get(editor, "ActiveObject")
        if active is not None:
            out["editor_active_object"] = active
    with contextlib.suppress(Exception):
        root = conn.root_occurrence()
        out["root_occurrence"] = root
        reference = safe_get(root, "ReferenceRootOccurrenceOf")
        if reference is not None:
            out["root_reference"] = reference
        children = PLMSession(conn).child_occurrences(root)
        if children:
            instance = safe_get(children[0], "InstanceOccurrenceOf")
            if instance is not None:
                out["first_instance"] = instance
    document = safe_get(conn.app, "ActiveDocument")
    product = safe_get(document, "Product") if document is not None else None
    if product is not None:  # CATIA V5
        out["v5_product"] = product
    return out


def enums(conn: Any) -> dict[str, Any]:
    from catia_mcp.introspect import find_enum, list_enums

    objects = list(_objects(conn).values())
    names = [e for e in list_enums(objects) if any(w in e.lower() for w in ENUM_WORDS)]
    return {"names": names,
            "values": {name: _safe(lambda n=name: find_enum(objects, n)) for name in names}}


def members(conn: Any) -> dict[str, Any]:
    from catia_mcp.introspect import describe_members, type_name

    out: dict[str, Any] = {}
    for label, obj in _objects(conn).items():
        row: dict[str, Any] = {"type": _safe(lambda o=obj: type_name(o))}
        try:
            row["matching"] = {m["name"]: m.get("params", m["kind"]) for m in describe_members(obj)
                               if any(w in m["name"].lower() for w in MEMBER_WORDS)}
        except Exception as error:
            row["matching"] = f"error: {error}"
        out[label] = row
    return out


def _try(label: str, fn) -> dict[str, Any]:
    from catia_mcp.introspect import type_name

    row: dict[str, Any] = {"call": label}
    try:
        value = fn()
    except Exception as error:
        row["error"] = str(error)
        return row
    if value is None:
        row["result"] = None
        return row
    row["type"] = _safe(lambda: type_name(value))
    row["signatures"] = _signatures(value).get("members")
    row["count"] = _safe(lambda: int(value.Count))
    row["found"] = True
    return row


def get_items(conn: Any) -> dict[str, Any]:
    from catia_mcp.automation import name_exists

    out: dict[str, Any] = {}
    for label in ("root_reference", "root_occurrence", "first_instance"):
        obj = _objects(conn).get(label)
        if obj is None or not name_exists(obj, "GetItem"):
            out[label] = "no GetItem"
            continue
        out[label] = [_try(f'GetItem("{n}")', lambda o=obj, n=n: o.GetItem(n))
                      for n in ITEM_NAMES]
    return out


def technological(conn: Any) -> dict[str, Any]:
    from catia_mcp.automation import name_exists

    out: dict[str, Any] = {}
    for label, obj in _objects(conn).items():
        if label == "application" or not name_exists(obj, "GetTechnologicalObject"):
            continue
        out[label] = [_try(f'GetTechnologicalObject("{n}")',
                           lambda o=obj, n=n: o.GetTechnologicalObject(n))
                      for n in TECHNOLOGICAL_NAMES]
    return out or {"note": "no object here has GetTechnologicalObject"}


def services(conn: Any) -> dict[str, Any]:
    from catia_mcp.automation import name_exists

    objects = _objects(conn)
    out: dict[str, Any] = {}
    editor = objects.get("editor")
    if editor is not None and name_exists(editor, "GetService"):
        out["editor"] = [_try(f'GetService("{n}")', lambda n=n: editor.GetService(n))
                         for n in SERVICE_NAMES]
    if name_exists(conn.app, "GetSessionService"):
        out["application"] = [_try(f'GetSessionService("{n}")',
                                   lambda n=n: conn.app.GetSessionService(n))
                              for n in SERVICE_NAMES]
    return out


def offline(tree_path: str, step_path: str, clearance: float) -> dict[str, Any]:
    from catia_mcp.tree import clashes
    from catia_mcp.tree.assembly import plan_assembly
    from catia_mcp.tree.step_structure import read

    with open(tree_path, encoding="utf-8") as handle:
        tree = json.load(handle)
    plan, problems = plan_assembly(tree, read(step_path))
    if problems:
        return {"error": "; ".join(problems)}
    return clashes.offline(plan, step_path, clearance)


def _with_add(rows: Any) -> list[str]:
    """The names a step's tries found an object with an Add method for."""
    return [row["call"].split('"')[1] for row in rows if isinstance(rows, list)
            and row.get("found") and isinstance(row.get("signatures"), dict)
            and "Add" in row["signatures"]] if isinstance(rows, list) else []


def _found(report: dict[str, Any], conn: Any) -> list[tuple[str, Any]]:
    """Every collection the read-only steps found that has Add, in order:
    GetTechnologicalObject first, then GetItem, then services (COM: run it on
    the worker)."""
    objects = _objects(conn)
    out = []
    for label, rows in (report.get("technological") or {}).items():
        for name in _with_add(rows):
            out.append((f'{label}.GetTechnologicalObject("{name}")',
                        lambda o=objects[label], n=name: o.GetTechnologicalObject(n)))
    for label, rows in (report.get("get_items") or {}).items():
        for name in _with_add(rows):
            out.append((f'{label}.GetItem("{name}")', lambda o=objects[label], n=name: o.GetItem(n)))
    for label, rows in (report.get("services") or {}).items():
        holder = objects.get("editor") if label == "editor" else conn.app
        method = "GetService" if label == "editor" else "GetSessionService"
        for name in _with_add(rows):
            out.append((f'{label}.{method}("{name}")',
                        lambda h=holder, m=method, n=name: getattr(h, m)(n)))
    return out


def create(conn: Any, report: dict[str, Any], clearance: float) -> dict[str, Any]:
    """A clash in the first collection found, computed and read back."""
    from catia_mcp.automation import name_exists
    from catia_mcp.tree import clashes

    candidates = _found(report, conn)
    if not candidates:
        return {"note": "no clash collection with Add was found: nothing created"}
    label, get = candidates[0]
    out: dict[str, Any] = {"collection": label, "candidates": [c[0] for c in candidates]}
    collection = get()
    clash = collection.Add()
    out["clash"] = _signatures(clash)
    objs = [clash, conn.app]
    computation, out["computation_enum"] = clashes._enum(objs, "CatClashComputationType",
                                                         clashes.COMPUTATION_TYPES)
    interference, out["interference_enum"] = clashes._enum(objs, "CatClashInterferenceType",
                                                           clashes.INTERFERENCE_TYPES)
    steps = [("ComputationType", lambda: setattr(clash, "ComputationType",
                                                 computation["catClashComputationTypeBetweenAll"]))]
    if clearance > 0:
        steps += [("InterferenceType", lambda: setattr(
                      clash, "InterferenceType",
                      interference["catClashInterferenceTypeClearance"])),
                  ("Clearance", lambda: setattr(clash, "Clearance", float(clearance)))]
    else:
        steps.append(("InterferenceType", lambda: setattr(
            clash, "InterferenceType", interference["catClashInterferenceTypeContact"])))
    steps.append(("Compute", lambda: clash.Compute()))
    out["steps"] = {name: _safe(lambda f=fn: (f(), "ok")[1]) for name, fn in steps}
    conflicts = _safe(lambda: clash.Conflicts)
    rows = []
    if not isinstance(conflicts, str):
        out["conflicts_object"] = _signatures(conflicts)
        for i in range(1, int(_safe(lambda: conflicts.Count) or 0) + 1):
            conflict = conflicts.Item(i)
            row: dict[str, Any] = {}
            for name in ("Type", "Value", "Status", "Comment"):
                row[name] = _safe(lambda n=name, c=conflict: str(getattr(c, n)))
            for name in ("FirstProduct", "SecondProduct"):
                row[name] = _safe(lambda n=name, c=conflict: str(getattr(c, n).Name))
            rows.append(row)
    else:
        out["conflicts_object"] = conflicts
    out["conflicts"] = rows
    reference = (report.get("offline") or {}).get("clashes")
    if reference is not None and rows:
        conflict_types, _ = clashes._enum(objs, "CatConflictType", clashes.CONFLICT_TYPES)
        kinds = {v: clashes.CONFLICT_KINDS.get(k.removeprefix("catConflictType").lower(), k)
                 for k, v in conflict_types.items()}
        named = [{"first": r["FirstProduct"], "second": r["SecondProduct"],
                  "kind": kinds.get(int(r["Type"]) if str(r["Type"]).isdigit() else -1,
                                    r["Type"])} for r in rows]
        names = {c["first"]: c.get("first_name") for c in reference}
        names.update({c["second"]: c.get("second_name") for c in reference})
        out["note"] = ("CATIA names instances (e.g. 'Bolt.1'), the offline analysis by id: "
                       f"compare by hand with {sorted(names.items())}")
        out["catia_kinds"] = named
    if name_exists(collection, "Remove"):
        out["removed"] = _safe(lambda: (collection.Remove(clash), True)[1])
    else:
        out["removed"] = "no Remove: close the product without saving"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", default="clashes_probe.json")
    parser.add_argument("--tree", default=os.path.join(FIXTURES, "bracket.tree.json"))
    parser.add_argument("--step", default=os.path.join(FIXTURES, "bracket.step"),
                        help="the STEP file imported into the active window")
    parser.add_argument("--platform", default="3dx", help="3dx (default), v5 or auto")
    parser.add_argument("--window", default=None,
                        help="the name of the imported product's window to activate first, "
                             "as CATIA's Window menu shows it (e.g. 'Bracket A:4')")
    parser.add_argument("--clearance", type=float, default=0.0,
                        help="clearance in mm for the offline analysis and --create")
    parser.add_argument("--create", action="store_true",
                        help="create a clash in the first collection found, compute it and "
                             "read its conflicts (removed again where CATIA allows)")
    args = parser.parse_args()

    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    worker = ComWorker(conn.watchdog, timeout_s=120, name="probe-clashes")
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "tree": args.tree, "step": args.step}
    try:
        report["connect"] = worker.run_guarded(conn.connect, label="connect")
    except Exception as error:
        print(error)
        return 2
    try:  # no CATIA needed: the reference
        report["offline"] = offline(args.tree, args.step, args.clearance)
    except Exception as error:
        report["offline"] = {"error": str(error)}
    steps = [("product_window", lambda: product_window(conn, args.window)),
             ("enums", lambda: enums(conn)), ("members", lambda: members(conn)),
             ("get_items", lambda: get_items(conn)), ("technological", lambda: technological(conn)),
             ("services", lambda: services(conn))]
    if args.create:
        steps.append(("create", lambda: create(conn, report, args.clearance)))
    for name, step in steps:
        print(f"  {name}")
        try:
            report[name] = worker.run_guarded(step, label=name)
        except Exception as error:  # keep what was found so far
            report[name] = {"error": str(error), "traceback": traceback.format_exc()}
        report["dialog"] = _safe(lambda: conn.watchdog.blocking_dialog())
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    try:
        found = worker.run_guarded(lambda: [label for label, _ in _found(report, conn)],
                                   label="collections")
    except Exception as error:
        found = [f"error: {error}"]
    report["collections_with_add"] = found
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Collections with Add: {found or 'none'}")
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
