"""Live probe of assembly constraints on the product imported from a STEP file.

On 3DEXPERIENCE a designer positions parts by engineering connections and
their mechanical constraints; on V5 by Assembly Design constraints. Whether
the automation API lets a script create them on 3DEXPERIENCE is not
documented. This probe attaches to a running CATIA (it never starts one),
works in the active window's product (the model's STEP imported by hand, as
for `--assembly adopt`), and records in a JSON report, each step on its own
(a failure is recorded and the next step runs):

1. the constraint enums of this installation's type library
   (CatConstraintType, CatConstraintOrientation, CatConstraintStatus...);
2. the product's leaf occurrences, their positions, and how they match the
   model's tree and STEP file (the adopt match);
3. the members of the root occurrence, its reference, the first instance,
   its reference and its Part (looking for Connections, GetItem,
   Publications, CreateReferenceFromName);
4. every route to a collection that takes constraints
   (catia_mcp/tree/assembly_constraints.py: constraint_collection), its
   members and the engineering connections already there (create one by
   hand first to see its object model);
5. with --create: a published plane in the first part, then a Fix on the
   first instance, by every reference variant; both are removed again;
6. with --apply fix|infer|contact: the constraints the build would put
   (infer), a Fix on each instance (fix), or the first contact found (the
   bracket's bolt on its plate) without the others, through the build's
   code; every position read back. They are left in place to look at: close
   the product without saving.

Usage (Windows, CATIA started, the model's STEP imported, its window active):
    python scripts/probe_constraints.py --report constraints_probe.json
    python scripts/probe_constraints.py --create --apply contact
    python scripts/probe_constraints.py --tree tests\\fixtures\\assembly\\pin_plate.tree.json
        --step tests\\fixtures\\assembly\\pin_plate.step --apply infer
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

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "fixtures",
                        "assembly")
ENUMS = ("CatConstraintType", "CatConstraintOrientation", "CatConstraintStatus",
         "CatConstraintMode", "CatConstraintReferenceType", "CatConstraintSide",
         "CatConstraintAngleSector")
LOOKED_FOR = ("Connections", "GetItem", "Publications", "CreateReferenceFromName",
              "CreateReferenceFromObject", "AddMonoEltCst", "AddBiEltCst", "AddTriEltCst",
              "Update", "Remove")


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as error:  # recorded, never fatal
        return f"error: {error}"


def _members(obj: Any) -> dict[str, Any]:
    from catia_mcp.automation import name_exists
    from catia_mcp.introspect import describe_members, type_name

    if obj is None:
        return {"object": None}
    out: dict[str, Any] = {"type": type_name(obj)}
    try:
        out["members"] = sorted({m["name"] for m in describe_members(obj)})
    except Exception as error:
        out["members"] = f"error: {error}"
    out["has"] = {name: name_exists(obj, name) for name in LOOKED_FOR}
    return out


def enums(conn: Any) -> dict[str, Any]:
    from catia_mcp.introspect import find_enum, list_enums

    objects = [conn.app]
    with contextlib.suppress(Exception):
        objects.append(conn.root_occurrence())
    with contextlib.suppress(Exception):
        objects.append(conn.app.ActiveEditor)
    out: dict[str, Any] = {name: _safe(lambda n=name: find_enum(objects, n)) for name in ENUMS}
    out["related"] = _safe(lambda: [e for e in list_enums(objects)
                                    if any(k in e.lower() for k in ("cst", "constraint",
                                                                    "engconn", "connection"))])
    return out


def _plan(tree_path: str, step_path: str):
    from catia_mcp.tree.assembly import plan_assembly
    from catia_mcp.tree.step_structure import read

    with open(tree_path, encoding="utf-8") as handle:
        tree = json.load(handle)
    plan, problems = plan_assembly(tree, read(step_path))
    if problems:
        raise ValueError("; ".join(problems))
    return plan


def occurrences(conn: Any, plan: Any) -> dict[str, Any]:
    from catia_mcp.tree import adopt

    leaves = adopt.leaf_occurrences(conn)
    out: dict[str, Any] = {"leaves": [{k: v for k, v in leaf.items()
                                       if k not in ("reference", "holder")} for leaf in leaves]}
    if plan is not None:
        references, rows, problems = adopt.match(plan, leaves)
        out.update(match=rows, problems=problems, parts=sorted(references))
    return out


def members(conn: Any) -> dict[str, Any]:
    from catia_mcp.automation import safe_get
    from catia_mcp.plm import PLMSession
    from catia_mcp.tree import adopt

    root = conn.root_occurrence()
    out: dict[str, Any] = {"root_occurrence": _members(root)}
    out["root_reference"] = _members(safe_get(root, "ReferenceRootOccurrenceOf"))
    children = PLMSession(conn).child_occurrences(root)
    if children:
        out["first_occurrence"] = _members(children[0])
        instance = safe_get(children[0], "InstanceOccurrenceOf")
        out["first_instance"] = _members(instance)
        reference = safe_get(instance, "ReferenceInstanceOf") if instance is not None else None
        out["first_reference"] = _members(reference)
        part = _safe(lambda: adopt.part_of(reference))
        out["first_part"] = _members(part) if not isinstance(part, str) else part
    with contextlib.suppress(Exception):
        out["editor_active_object"] = _members(conn.app.ActiveEditor.ActiveObject)
    return out


def routes(conn: Any) -> dict[str, Any]:
    from catia_mcp.automation import safe_get
    from catia_mcp.tree.assembly_constraints import constraint_collection

    found, attempts = constraint_collection(conn)
    out: dict[str, Any] = {"attempts": attempts, "found": found[0] if found else None}
    if found:
        out["collection"] = _members(found[1])
        out["count"] = _safe(lambda: int(found[1].Count))
    root = conn.root_occurrence()
    reference = safe_get(root, "ReferenceRootOccurrenceOf")
    connections = _safe(lambda: reference.GetItem("CATEngConnections"))
    if not isinstance(connections, str) and connections is not None:
        out["eng_connections"] = _members(connections)
        items = []
        for i in range(1, min(int(_safe(lambda: connections.Count) or 0), 3) + 1):
            item = _safe(lambda i=i: connections.Item(i))
            row = _members(item) if not isinstance(item, str) else {"error": item}
            if not isinstance(item, str):
                for name in ("Name", "Type", "Status", "Mode"):
                    row[name] = _safe(lambda n=name: str(getattr(item, n)))
            items.append(row)
        out["eng_connection_items"] = items
    else:
        out["eng_connections"] = connections
    return out


# Words in the names of the enums an engineering connection may use.
ENG_ENUM_WORDS = ("cnx", "connection", "constraint", "asm", "eng", "mechanical")


def _signatures(obj: Any) -> dict[str, Any]:
    from catia_mcp.introspect import describe_members, type_name

    out: dict[str, Any] = {"type": type_name(obj)}
    try:
        out["members"] = {m["name"]: m.get("params", m["kind"]) for m in describe_members(obj)}
    except Exception as error:
        out["members"] = f"error: {error}"
    return out


def engcnx(conn: Any, create: bool) -> dict[str, Any]:
    """Engineering connections, the 3DEXPERIENCE assembly constraints: the
    collection the root reference's GetItem("CATEngConnections") gives (live
    on R428, with Add and AddWithContext), the signatures of its methods,
    the enums that name connection or constraint types, and the
    publications of a part reference. With `create`, Add is tried with each
    candidate argument; whatever it makes is described, then removed."""
    from catia_mcp.automation import safe_get
    from catia_mcp.introspect import list_enums

    root = conn.root_occurrence()
    reference = safe_get(root, "ReferenceRootOccurrenceOf")
    connections = reference.GetItem("CATEngConnections")
    out: dict[str, Any] = {"collection": _signatures(connections)}
    try:
        names = list_enums([connections, reference, conn.app])
        out["enums"] = {name: _safe(lambda n=name: _enum_values(conn, connections, n))
                        for name in names if any(w in name.lower() for w in ENG_ENUM_WORDS)}
    except Exception as error:
        out["enums"] = f"error: {error}"
    first = _safe(lambda: root.Occurrences.Item(1).InstanceOccurrenceOf.ReferenceInstanceOf)
    if not isinstance(first, str):
        publications = _safe(lambda: first.Publications)
        out["part_publications"] = publications if isinstance(publications, str) \
            else _signatures(publications)
    if not create:
        return out
    occurrences = [root.Occurrences.Item(i) for i in range(1, int(root.Occurrences.Count) + 1)]
    instances = [o.InstanceOccurrenceOf for o in occurrences]
    # Live on R428: Add(iType As CatEngConnectionType, iImpacteds As array).
    # What the impacteds are (instances or occurrences) is tried here, with a
    # Fix on one, then a Rigid and a Cylindrical between the plate and a bolt.
    cases = [("fix", ENG_FIX, 1), ("rigid", ENG_RIGID, 2), ("cylindrical", ENG_CYLINDRICAL, 2)]
    tries = []
    for label, kind, count in cases:
        for what, items in (("instances", instances), ("occurrences", occurrences)):
            row: dict[str, Any] = {"type": label, "impacteds": what}
            made = None
            for how, call in (
                    ("Add", lambda k=kind, i=items[:count]: connections.Add(k, i)),
                    ("Add(tuple)", lambda k=kind, i=items[:count]: connections.Add(k, tuple(i))),
                    ("AddWithContext", lambda k=kind, i=items[:count]: connections.AddWithContext(
                        k, i, reference, f"Probe {label}"))):
                try:
                    made = call()
                    row["made_by"] = how
                    break
                except Exception as error:
                    row[how] = str(error)
            if made is not None:
                row["made"] = _explore(made)
                row["removed"] = _safe(lambda m=made: connections.Remove(m) or True)
            tries.append(row)
            if made is not None:
                break  # this kind of impacted works: no need for the other
    out["add"] = tries
    return out


ENG_FIX, ENG_RIGID, ENG_CYLINDRICAL = 2, 10, 0  # CatEngConnectionType, read live
BASIC_PROPERTIES = {"Application", "Parent", "Name", "Count"}


def _explore(obj: Any) -> dict[str, Any]:
    """An object's members with their signatures, and each object-valued
    property's own members (one level down): the engineering connection's
    constraints collection is somewhere in there."""
    out = _signatures(obj)
    members = out.get("members")
    if not isinstance(members, dict):
        return out
    for name, kind in members.items():
        if kind != "property" or name in BASIC_PROPERTIES:
            continue
        value = _safe(lambda n=name: getattr(obj, n))
        if isinstance(value, (str, int, float, bool)) or value is None:
            out.setdefault("values", {})[name] = value
        else:
            out.setdefault("children", {})[name] = _signatures(value)
    return out


def _enum_values(conn: Any, near: Any, name: str) -> Any:
    from catia_mcp.introspect import find_enum

    return find_enum([near, conn.app], name)


def product_window(conn: Any, wanted: str | None = None) -> dict[str, Any]:
    """Whether the active window shows a product, and the open windows'
    names. Live, activating every window in turn hung CATIA for 120 s
    ('PopupFatherWindow', the first window being the home page), so only the
    one window named by --window is activated, if any."""
    from catia_mcp.errors import WrongDocumentType

    out: dict[str, Any] = {}
    if wanted:
        windows = conn.app.Windows
        names = [str(windows.Item(i).Name) for i in range(1, int(windows.Count) + 1)]
        if wanted not in names:
            out["window"] = f"no open window is named {wanted!r}"
        else:
            windows.Item(names.index(wanted) + 1).Activate()
            out["window"] = f"activated {wanted!r}"
    try:
        conn.root_occurrence()
        out["active"] = "a product"
    except WrongDocumentType:
        out["active"] = None
        out["note"] = ("the active window shows no product: click the imported bracket's "
                       "tab in CATIA, then run the probe again")
    windows = _safe(lambda: conn.app.Windows)
    if not isinstance(windows, str):
        out["windows"] = [_safe(lambda i=i: str(windows.Item(i).Name))
                          for i in range(1, int(_safe(lambda: windows.Count) or 0) + 1)]
    return out


# CatAssemblyConstraintType values read live on R428.
CST_FIX_INSTANCE, CST_PARALLEL_PLANES = 30, 41


def engcst(conn: Any) -> dict[str, Any]:
    """What AssemblyConstraints.Add(iType, iGeometries) takes. Live on R428
    EngConnections.Add(type, [instances]) made connections, each holding an
    AssemblyConstraints collection. Here a Fix connection on the plate tries
    a fix-instance constraint on its instance, occurrence or reference, and a
    Rigid connection between the plate and the first bolt tries a
    parallelism of their XY planes (already parallel: nothing should move)
    given as Part references, plane objects or published names. Each
    success is described; positions are read before and after; then every
    way to remove a constraint and a connection is tried."""
    from catia_mcp.automation import call_fill_array, safe_get
    from catia_mcp.tree import adopt

    root = conn.root_occurrence()
    reference = safe_get(root, "ReferenceRootOccurrenceOf")
    connections = reference.GetItem("CATEngConnections")
    occurrences = [root.Occurrences.Item(i) for i in range(1, int(root.Occurrences.Count) + 1)]
    instances = [o.InstanceOccurrenceOf for o in occurrences]
    references = [i.ReferenceInstanceOf for i in instances]

    def positions() -> list[Any]:
        return [_safe(lambda i=i: [round(float(c), 6) for c in call_fill_array(
            conn.app, i.Position, "GetComponents", 12)]) for i in instances[:2]]

    out: dict[str, Any] = {"before": positions(), "count_before": _safe(lambda: int(connections.Count))}
    made: list[Any] = []

    def attempt(cnx: Any, kind: int, label: str, geometries: list[Any]) -> dict[str, Any]:
        row: dict[str, Any] = {"geometries": label}
        try:
            cst = cnx.AssemblyConstraints.Add(kind, geometries)
        except Exception as error:
            row["error"] = str(error)
            return row
        row["made"] = _explore(cst)
        row["count"] = _safe(lambda: int(cnx.AssemblyConstraints.Count))
        row["after"] = positions()
        row["remove_constraint"] = _safe(lambda: cnx.AssemblyConstraints.Remove(cst) or True)
        return row

    fix = connections.Add(2, [instances[0]])  # CatEngConnectionType catFix
    made.append(fix)
    out["fix"] = [attempt(fix, CST_FIX_INSTANCE, label, geoms) for label, geoms in (
        ("[instance]", [instances[0]]), ("[occurrence]", [occurrences[0]]),
        ("[reference]", [references[0]]))]

    parts = [_safe(lambda r=r: adopt.part_of(r)) for r in references[:2]]
    rigid = connections.Add(10, instances[:2])  # catRigid
    made.append(rigid)
    rows = []
    if not any(isinstance(p, str) for p in parts):
        planes = [p.OriginElements.PlaneXY for p in parts]
        variants = [
            ("Part.CreateReferenceFromObject(PlaneXY)",
             lambda: [p.CreateReferenceFromObject(pl) for p, pl in zip(parts, planes)]),
            ("PlaneXY objects", lambda: list(planes)),
            ("Part.CreateReferenceFromName(plane name)",
             lambda: [p.CreateReferenceFromName(str(pl.Name)) for p, pl in zip(parts, planes)]),
            ("[occurrence, reference] pairs",
             lambda: [occurrences[0], parts[0].CreateReferenceFromObject(planes[0]),
                      occurrences[1], parts[1].CreateReferenceFromObject(planes[1])]),
        ]
        for label, build in variants:
            try:
                geometries = build()
            except Exception as error:
                rows.append({"geometries": label, "build_error": str(error)})
                continue
            rows.append(attempt(rigid, CST_PARALLEL_PLANES, label, geometries))
    else:
        rows.append({"parts": parts})
    out["parallel"] = rows
    removal = []
    for cnx in made:
        for label, way in (("object", lambda c=cnx: connections.Remove(c)),
                           ("name", lambda c=cnx: connections.Remove(str(c.Name))),
                           ("index", lambda: connections.Remove(int(connections.Count)))):
            result = _safe(lambda w=way: w() or True)
            removal.append({"connection": _safe(lambda c=cnx: str(c.Name)), "by": label,
                            "result": result})
            if result is True:
                break
    out["remove"] = removal
    out["count_after"] = _safe(lambda: int(connections.Count))
    out["after"] = positions()
    return out


def _impacted(cnx: Any, k: int) -> Any:
    """GetImpacted(k, out impacted, out context): late binding returns the
    out strings, or wants placeholders for them."""
    for args in ((k,), (k, "", "")):
        try:
            got = cnx.GetImpacted(*args)
        except Exception as error:
            last = str(error)
            continue
        return [str(x) for x in got] if isinstance(got, (tuple, list)) else str(got)
    return f"error: {last}"


SEARCHES = ("CATPrtSearch.Plane,all", "Type=Plane,all", "CATPrtSearch.Plane,sel")


def engcst2(conn: Any) -> dict[str, Any]:
    """Assembly constraints need geometry seen from the product. Live on R428
    AssemblyConstraints.Add refused Part references, plane objects and
    instances (E_FAIL). This step reads how an existing connection names
    its impacteds (GetImpacted's out strings), finds the parts' planes from
    the product with Selection.Search and keeps each found item's Reference
    (the usual way to get a reference in an assembly context), then tries a
    parallelism of two such planes in a Rigid connection and a fix on a
    selected occurrence. Connections made here are removed by name (live:
    Remove(name) works, Remove(object) does not)."""
    from catia_mcp.automation import safe_get

    root = conn.root_occurrence()
    reference = safe_get(root, "ReferenceRootOccurrenceOf")
    connections = reference.GetItem("CATEngConnections")
    occurrences = [root.Occurrences.Item(i) for i in range(1, int(root.Occurrences.Count) + 1)]
    instances = [o.InstanceOccurrenceOf for o in occurrences]
    out: dict[str, Any] = {}

    impacted = []
    for i in range(1, min(int(connections.Count), 3) + 1):
        cnx = connections.Item(i)
        count = _safe(lambda c=cnx: int(c.GetNbImpacteds()))
        row: dict[str, Any] = {"name": _safe(lambda c=cnx: str(c.Name)), "count": count}
        if isinstance(count, int):
            row["impacted"] = [_impacted(cnx, k) for k in range(1, count + 1)]
        impacted.append(row)
    out["impacted"] = impacted

    selection = conn.app.ActiveEditor.Selection
    found: dict[str, Any] = {}
    items: list[Any] = []
    for query in SEARCHES:
        rows = []
        try:
            selection.Clear()
            if query.endswith(",sel"):
                selection.Add(occurrences[0])
            selection.Search(query)
            count = int(selection.Count)
            for k in range(1, min(count, 12) + 1):
                item = selection.Item(k)
                row = {"value": _safe(lambda it=item: str(it.Value.Name)),
                       "type": _safe(lambda it=item: str(it.Type)),
                       "leaf": _safe(lambda it=item: str(it.LeafProduct.Name)),
                       "reference": _safe(lambda it=item: str(it.Reference.DisplayName))}
                rows.append(row)
                if query == SEARCHES[0] or not items:
                    items.append((row, _safe(lambda it=item: it.Reference),
                                  _safe(lambda it=item: it.Value)))
            found[query] = {"count": count, "items": rows}
        except Exception as error:
            found[query] = {"error": str(error)}
    out["search"] = found
    selection.Clear()

    tries = []
    refs = [(row, ref, value) for row, ref, value in items if not isinstance(ref, str)]
    leaves = []
    for row, ref, value in refs:  # the first plane of two different leaves
        if all(row["leaf"] != other[0]["leaf"] for other in leaves):
            leaves.append((row, ref, value))
        if len(leaves) == 2:
            break
    if len(leaves) == 2:
        rigid = connections.Add(10, instances[:2])
        name = str(rigid.Name)
        for label, geometries in (("Selection references", [leaves[0][1], leaves[1][1]]),
                                  ("Selection values", [leaves[0][2], leaves[1][2]])):
            row = {"geometries": label, "leaves": [leaves[0][0], leaves[1][0]]}
            try:
                cst = rigid.AssemblyConstraints.Add(41, geometries)  # parallel planes
                row["made"] = _explore(cst)
            except Exception as error:
                row["error"] = str(error)
            tries.append(row)
        out["remove_rigid"] = _safe(lambda: connections.Remove(name) or True)
    else:
        tries.append({"error": "the search found no planes in two different leaves"})
    try:
        selection.Clear()
        selection.Add(occurrences[0])
        occurrence_ref = selection.Item(1).Reference
        selection.Clear()
        fix = connections.Add(2, [instances[0]])
        name = str(fix.Name)
        row = {"geometries": "selected occurrence's Reference"}
        try:
            row["made"] = _explore(fix.AssemblyConstraints.Add(30, [occurrence_ref]))
        except Exception as error:
            row["error"] = str(error)
        tries.append(row)
        out["remove_fix"] = _safe(lambda: connections.Remove(name) or True)
    except Exception as error:
        tries.append({"geometries": "selected occurrence's Reference", "error": str(error)})
    out["constraints"] = tries
    out["count_after"] = _safe(lambda: int(connections.Count))
    return out


def create(conn: Any, plan: Any) -> dict[str, Any]:
    """A published plane in the first part and a Fix on the first instance,
    both removed again."""
    from catia_mcp.automation import safe_get
    from catia_mcp.tree import adopt
    from catia_mcp.tree.assembly_constraints import (
        CST_TYPES,
        GEOSET,
        X3DBackend,
        _reference_element,
    )
    from catia_mcp.tree.mates import Element

    leaves = adopt.leaf_occurrences(conn)
    first = leaves[0]
    record: dict[str, Any] = {"occurrence": first["path"]}
    part = adopt.part_of(first["reference"])
    instance = plan.instances[0].id if plan is not None else "first"
    prototype = plan.instances[0].prototype if plan is not None else "first"
    root_name = str(safe_get(conn.root_occurrence(), "Name", "") or "")
    backend = X3DBackend(conn, {instance: first["holder"]},
                         {instance: f"{root_name}{first['path']}"}, {prototype: part},
                         {prototype: first["reference"]}, lambda p: [])
    element = Element("plane", "probe", (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0),
                      (0.0, 0.0, 1.0))
    shape = None
    try:
        shape = _reference_element(part, element, "Plane probe")
        record["plane"] = str(shape.Name)
        record["published"] = _safe(lambda: backend.publish(prototype, "Plane probe", shape))
    except Exception as error:
        record["plane"] = f"error: {error}"
    try:
        collection = backend.constraints()
        record["route"] = backend.route
        before = _safe(lambda: int(collection.Count))
        fixes = []
        for label, reference in backend.references(instance):
            row: dict[str, Any] = {"reference": label}
            try:
                made = reference()
                row["display_name"] = _safe(lambda m=made: str(m.DisplayName))
                constraint = collection.AddMonoEltCst(CST_TYPES["reference"], made)
                row["created"] = True
                for name in ("Name", "Type", "Status", "Mode"):
                    row[name] = _safe(lambda n=name: str(getattr(constraint, n)))
                backend.update()
                row["status_after_update"] = _safe(lambda c=constraint: str(c.Status))
                row["count"] = _safe(lambda: int(collection.Count))
                row["removed"] = _safe(lambda c=constraint: (backend.remove(c), True)[1])
            except Exception as error:
                row["created"] = False
                row["error"] = str(error)
            fixes.append(row)
        record["fix"] = fixes
        record["count_before"] = before
        record["count_after"] = _safe(lambda: int(collection.Count))
    except Exception as error:
        record["constraints"] = f"error: {error}"
    finally:
        sets = _safe(lambda: part.HybridBodies)
        for i in range(int(_safe(lambda: sets.Count) or 0), 0, -1):
            geoset = sets.Item(i)
            if str(geoset.Name) == GEOSET:
                record["geoset_removed"] = _safe(lambda g=geoset: _delete(conn, g))
        with contextlib.suppress(Exception):
            part.Update()
    return record


def _delete(conn: Any, obj: Any) -> bool:
    selection = conn.hso
    selection.Clear()
    selection.Add(obj)
    selection.Delete()
    selection.Clear()
    return True


def apply(conn: Any, plan: Any, mode: str, step: str) -> dict[str, Any]:
    """The build's constraints on the imported product (left in place)."""
    from catia_mcp.tree import adopt, mates
    from catia_mcp.tree.assembly_constraints import apply_3dx, plan_mates
    mate_plan, components = plan_mates(plan, "fix" if mode == "fix" else "infer", step)
    if mode == "contact":
        by_id = {c.id: c for c in components}
        ground = by_id[mate_plan.ground]
        for placement in mate_plan.placements:
            if placement.status == "ground":
                continue
            contacts = [m for m in mates.candidates(by_id[placement.component], ground)
                        if m.kind == "contact"]
            if contacts:
                mate_plan = mates.MatePlan(mate_plan.ground, [
                    mate_plan.placement(mate_plan.ground),
                    mates.Placement(placement.component, "mates", contacts[:1])])
                break
        else:
            return {"error": "no contact between the grounded part and another"}
    leaves = adopt.leaf_occurrences(conn)
    references, _, problems = adopt.match(plan, leaves)
    if problems:
        return {"error": "the active product does not match: " + "; ".join(problems)}
    parts = {prototype: adopt.part_of(reference) for prototype, reference in references.items()}
    started = time.monotonic()
    report = apply_3dx(conn, plan, mate_plan, components, leaves, parts, references, log=print)
    report["plan"] = mate_plan.to_dict()
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", default="constraints_probe.json")
    parser.add_argument("--tree", default=os.path.join(FIXTURES, "bracket.tree.json"))
    parser.add_argument("--step", default=os.path.join(FIXTURES, "bracket.step"),
                        help="the STEP file imported into the active window")
    parser.add_argument("--platform", default="3dx", help="3dx (default), v5 or auto")
    parser.add_argument("--create", action="store_true",
                        help="create a published plane and a Fix, then remove them")
    parser.add_argument("--window", default=None,
                        help="the name of the imported product's window to activate first, "
                             "as CATIA's Window menu shows it (e.g. 'Bracket A:4')")
    parser.add_argument("--engcst2", action="store_true",
                        help="read how connections name their impacteds and try constraints "
                             "on planes found by Selection.Search (removed again by name)")
    parser.add_argument("--engcst", action="store_true",
                        help="create a Fix and a Rigid connection and try assembly constraints "
                             "in them (removed again where CATIA allows)")
    parser.add_argument("--engcnx-create", action="store_true",
                        help="try EngConnections.Add with each candidate argument (each "
                             "connection made is removed again)")
    parser.add_argument("--apply", choices=["fix", "infer", "contact"],
                        help="put the build's constraints in the product (left in place)")
    args = parser.parse_args()

    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    worker = ComWorker(conn.watchdog, timeout_s=120, name="probe-constraints")
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "tree": args.tree, "step": args.step}
    try:
        report["connect"] = worker.run_guarded(conn.connect, label="connect")
    except Exception as error:
        print(error)
        return 2
    plan = None
    try:
        plan = _plan(args.tree, args.step)
        report["plan"] = plan.summary()
    except Exception as error:
        report["plan"] = f"error: {error}"
    steps = [("product_window", lambda: product_window(conn, args.window)),
             ("enums", lambda: enums(conn)), ("occurrences", lambda: occurrences(conn, plan)),
             ("members", lambda: members(conn)), ("routes", lambda: routes(conn))]
    steps.append(("engcnx", lambda: engcnx(conn, args.engcnx_create)))
    if args.engcst:
        steps.append(("engcst", lambda: engcst(conn)))
    if args.engcst2:
        steps.append(("engcst2", lambda: engcst2(conn)))
    if args.create:
        steps.append(("create", lambda: create(conn, plan)))
    if args.apply and plan is not None:
        steps.append(("apply", lambda: apply(conn, plan, args.apply, args.step)))
    for name, step in steps:
        print(f"  {name}")
        try:
            report[name] = worker.run_guarded(step, label=name)
        except Exception as error:  # keep what was found so far
            report[name] = {"error": str(error), "traceback": traceback.format_exc()}
        report["dialog"] = _safe(lambda: conn.watchdog.blocking_dialog())
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
