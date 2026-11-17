"""Work inside a product imported from the model's STEP file (3DEXPERIENCE).

A script may not create a Physical Product on many 3DEXPERIENCE roles (live
on R428: "Opération non autorisée"), but the user can import the model's
STEP file, which gives the product with every instance in place (live: each
occurrence's position equals the STEP's) and one part per shared shape (the
two bolts of a bracket are one reference). The bridge then adopts it:

- the product's leaf occurrences are matched to the plan's instances, in
  order, by name and position, and occurrences of one plan part must share
  one reference;
- each part is reached from its occurrence (reference, its representation,
  then GetItem("Part"): live on R428) and rebuilt there with native
  features, from the product's window (live: a pad built, measured and
  searched there), replacing the imported solid.
"""

from __future__ import annotations

from typing import Any

from catia_mcp.tree.assembly import AssemblyPlan
from catia_mcp.tree.placement import catia_components

POSITION_TOL = 1e-4


def leaf_occurrences(conn: Any) -> list[dict[str, Any]]:
    """The active editor's leaf occurrences, depth first: path, title,
    reference id, position (12 components) and the reference itself."""
    from catia_mcp.automation import call_fill_array, safe_get
    from catia_mcp.plm import PLMSession

    plm = PLMSession(conn)
    rows: list[dict[str, Any]] = []

    def visit(occurrence: Any, path: str) -> None:
        for child in plm.child_occurrences(occurrence):
            instance = safe_get(child, "InstanceOccurrenceOf")
            reference = safe_get(instance, "ReferenceInstanceOf") if instance is not None else None
            name = plm.occurrence_name(child)
            if plm.child_occurrences(child):
                visit(child, f"{path}/{name}")
                continue
            holder = instance if instance is not None else child
            rows.append({
                "path": f"{path}/{name}",
                "title": plm.attribute(reference, "V_Name") if reference is not None else None,
                "reference_id": plm.attribute(reference, "PLM_ExternalID")
                if reference is not None else None,
                "position": [float(v) for v in
                             call_fill_array(conn.app, holder.Position, "GetComponents", 12)],
                "reference": reference,
                "holder": holder,  # its Position (assembly constraints)
            })

    visit(conn.root_occurrence(), "")
    return rows


def part_of(reference: Any) -> Any:
    """The Part behind a 3D Part reference (live on R428)."""
    return reference.RepInstances.Item(1).ReferenceInstanceOf.GetItem("Part")


def match(plan: AssemblyPlan, leaves: list[dict[str, Any]]
          ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """(the Part to build each plan part in, a row per instance for the
    report, the reasons the imported product cannot be trusted)."""
    problems: list[str] = []
    if len(leaves) != len(plan.instances):
        problems.append(f"the active product has {len(leaves)} part occurrence(s), the model "
                        f"{len(plan.instances)}: import the model's STEP file and keep its "
                        "window active")
        return {}, [], problems
    references: dict[str, Any] = {}
    reference_ids: dict[str, str | None] = {}
    rows = []
    for placed, leaf in zip(plan.instances, leaves, strict=True):
        position_ok = all(abs(a - b) <= POSITION_TOL for a, b in
                          zip(leaf["position"], catia_components(placed.local), strict=True))
        rows.append({"id": placed.id, "catia": leaf["path"], "title": leaf["title"],
                     "part": placed.prototype, "position_ok": position_ok})
        if leaf["title"] not in (placed.name, None):
            problems.append(f"{placed.id}: the product's occurrence {leaf['path']} is "
                            f"'{leaf['title']}', not '{placed.name}'")
        if not position_ok:
            problems.append(f"{placed.id}: {leaf['path']} is not where the STEP file puts it")
        known = reference_ids.setdefault(placed.prototype, leaf["reference_id"])
        if known != leaf["reference_id"]:
            problems.append(f"{placed.id}: {leaf['path']} is a different part from the other "
                            f"instances of {placed.prototype}")
        references.setdefault(placed.prototype, leaf["reference"])
    return references, rows, problems
