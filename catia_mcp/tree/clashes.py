"""Clashes: what a designer checks once the assembly is put together.

For every pair of instances the analysis says whether they

- **interfere**: their solids share a volume larger than `VOLUME_TOL_MM3`
  (a pin larger than its hole); the value is that volume, the location its
  centre;
- **touch** (contact): their distance is at most `CONTACT_TOL_MM` and they
  share no volume, as mated faces do (a bolt head on a plate, a pin in a hole
  of its own diameter); the value is the distance (0), the location the
  centre of where they touch (their section edges);
- sit closer than a **clearance** asked for (`clearance_mm` > 0): the value
  is the minimum distance, the location the middle of the closest points.

A pair further apart than the clearance is not reported.

The offline analysis is the reference: it needs no CATIA, only the shapes
of the STEP file (each prototype once, moved to each of its instances, as
catia_mcp/tree/mates.py does) or of the tree's build123d reference. Boxes
first (a pair whose bounding boxes are further apart than the clearance is
skipped), then the exact minimum distance (BRepExtrema_DistShapeShape), then,
for pairs at distance 0, the common volume (BRepAlgoAPI_Common). Pairs are
taken in the plan's instance order, values rounded: the result is
deterministic.

CATIA routes (`--clashes catia`): on V5 the DMU Space Analysis clash of the
built product (`compute_v5`), compared with the offline result; it needs the
DMU Space Analysis licence, so it runs under the dialog watchdog and an
unavailable licence is reported, never fatal. On 3DEXPERIENCE no clash API
is known yet (scripts/probe_clashes.py looks for one): the result is the
offline analysis, exact from the STEP file. See docs/clashes.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CONTACT_TOL_MM = 1e-3
VOLUME_TOL_MM3 = 1e-3
DIGITS = 6

Rows = list[list[float]]
Vec = tuple[float, float, float]

KINDS = ("interference", "contact", "clearance")


def available() -> bool:
    """Whether OCP (installed with build123d) is there for the offline analysis."""
    try:
        import OCP.BRepExtrema  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class Item:
    """One instance: its id, name and solid in world coordinates (OCP)."""
    id: str
    name: str
    shape: Any = field(repr=False)
    prototype: str = ""


@dataclass
class Clash:
    first: str
    second: str
    kind: str  # interference, contact or clearance
    value: float  # mm3 for an interference, mm otherwise
    location: Vec
    points: tuple[Vec, Vec] | None = None  # the closest points (a clearance)

    @property
    def unit(self) -> str:
        return "mm3" if self.kind == "interference" else "mm"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"first": self.first, "second": self.second, "kind": self.kind,
                               "value": self.value, "unit": self.unit,
                               "location": list(self.location)}
        if self.points is not None:
            out["points"] = [list(p) for p in self.points]
        return out


def _round(value: float, digits: int = DIGITS) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded  # no -0.0


def _vec(p) -> Vec:
    return (_round(p.X()), _round(p.Y()), _round(p.Z()))


def _mean(points: list[Vec]) -> Vec:
    n = float(len(points))
    return tuple(_round(sum(p[i] for p in points) / n) for i in range(3))


# ── Shapes (needs OCP) ──


def moved(shape: Any, rows: Rows) -> Any:
    """An OCP shape moved by a rigid placement [R|t] (the geometry is shared)."""
    from OCP.gp import gp_Trsf
    from OCP.TopLoc import TopLoc_Location

    trsf = gp_Trsf()
    trsf.SetValues(*[float(c) for row in rows for c in row])
    return shape.Moved(TopLoc_Location(trsf))


def items_from_structure(structure: Any, plan: Any = None) -> list[Item]:
    """The instances of a STEP structure (step_structure.read), each
    prototype's shape moved to the instance's world placement; in the plan's
    instance order when a plan is given."""
    out = []
    for instance in structure.instances:
        if instance.shape is None:
            raise ValueError(f"{instance.id}: the STEP structure was read without shapes")
        out.append(Item(instance.id, instance.name, moved(instance.shape, instance.world),
                        instance.prototype))
    if plan is not None:
        by_id = {item.id: item for item in out}
        out = [by_id[i.id] for i in plan.instances if i.id in by_id]
    return out


def items_from_plan(plan: Any) -> list[Item]:
    """The instances of an assembly plan without its STEP file: each
    prototype built once by the tree's build123d reference (in the frame
    the plan models it in), moved to each instance's world placement."""
    from catia_mcp.tree.reference import build_part

    parts = {part["id"]: part for part in plan.tree.get("parts") or []}
    shapes: dict[str, Any] = {}
    out = []
    for placed in plan.instances:
        if placed.prototype not in shapes:
            shapes[placed.prototype] = build_part(parts[placed.prototype]).wrapped
        out.append(Item(placed.id, placed.name, moved(shapes[placed.prototype], placed.world),
                        placed.prototype))
    return out


def _box(shape: Any, margin: float) -> Any:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    box.Enlarge(margin)
    return box


def _volume(shape: Any) -> tuple[float, Vec]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass()), _vec(props.CentreOfMass())


def _touching(a: Item, b: Item, solutions: list[tuple[Vec, Vec]]) -> Vec:
    """The middle of where two solids touch: the centre of their section
    edges (a face on a face gives its outline), else of the closest points
    (a point contact)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    try:
        BRepGProp.LinearProperties_s(BRepAlgoAPI_Section(a.shape, b.shape).Shape(), props)
        length = props.Mass()
    except Exception:  # noqa: BLE001 -- the closest points will do
        length = 0.0
    if length > CONTACT_TOL_MM:
        return _vec(props.CentreOfMass())
    middles = [_mean([p, q]) for p, q in solutions] or [(0.0, 0.0, 0.0)]
    return _mean(middles)


def _pair(a: Item, b: Item, clearance_mm: float, contact_tol: float,
          volume_tol: float) -> Clash | None:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    extrema = BRepExtrema_DistShapeShape(a.shape, b.shape)
    if not extrema.IsDone():
        raise RuntimeError("the minimum distance was not computed")
    distance = float(extrema.Value())
    if distance > max(clearance_mm, contact_tol):
        return None
    solutions = [(_vec(extrema.PointOnShape1(k)), _vec(extrema.PointOnShape2(k)))
                 for k in range(1, extrema.NbSolution() + 1)]
    solutions.sort()
    if distance <= contact_tol:
        common = BRepAlgoAPI_Common(a.shape, b.shape)
        if not common.IsDone():
            raise RuntimeError("the common volume was not computed")
        volume, centre = _volume(common.Shape())
        if volume > volume_tol:
            return Clash(a.id, b.id, "interference", _round(volume, 4), centre)
        return Clash(a.id, b.id, "contact", _round(distance), _touching(a, b, solutions))
    p, q = solutions[0]
    return Clash(a.id, b.id, "clearance", _round(distance), _mean([p, q]), (p, q))


def analyze(items: list[Item], clearance_mm: float = 0.0, contact_tol: float = CONTACT_TOL_MM,
            volume_tol: float = VOLUME_TOL_MM3) -> dict[str, Any]:
    """Every pair of `items` checked: the report's `clashes` entry."""
    if clearance_mm < 0:
        raise ValueError("the clearance cannot be negative")
    started = time.monotonic()
    margin = max(clearance_mm, contact_tol) / 2.0 + 1e-7
    boxes = [_box(item.shape, margin) for item in items]
    clashes: list[Clash] = []
    errors: list[dict[str, Any]] = []
    measured = 0
    for i, a in enumerate(items):
        for j in range(i + 1, len(items)):
            if boxes[i].IsOut(boxes[j]):
                continue
            measured += 1
            b = items[j]
            try:
                found = _pair(a, b, clearance_mm, contact_tol, volume_tol)
            except Exception as error:  # one pair, recorded
                errors.append({"first": a.id, "second": b.id, "error": str(error)})
                continue
            if found is not None:
                clashes.append(found)
    counts = {kind: sum(c.kind == kind for c in clashes) for kind in KINDS}
    report: dict[str, Any] = {
        "ok": not errors, "method": "offline", "instances": len(items),
        "clearance_mm": clearance_mm,
        "tolerances": {"contact_mm": contact_tol, "volume_mm3": volume_tol},
        "pairs": len(items) * (len(items) - 1) // 2, "pairs_measured": measured,
        "counts": counts, "interference": counts["interference"] > 0,
        "clashes": [c.to_dict() for c in clashes],
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    if errors:
        report["errors"] = errors
    return report


def summary(report: dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    text = (f"{counts.get('interference', 0)} interference(s), {counts.get('contact', 0)} "
            f"contact(s)")
    if report.get("clearance_mm"):
        text += f", {counts.get('clearance', 0)} clearance(s) under {report['clearance_mm']:g} mm"
    return text + f" ({report.get('pairs_measured', 0)} of {report.get('pairs', 0)} pairs measured)"


def offline(plan: Any, step: str | None, clearance_mm: float = 0.0) -> dict[str, Any]:
    """The offline analysis of an assembly plan: shapes from the STEP file
    when given, else from the tree's build123d reference. Needs OCP."""
    if step:
        from catia_mcp.tree.step_structure import read

        items, source = items_from_structure(read(step), plan), "step"
    else:
        items, source = items_from_plan(plan), "tree"
    report = analyze(items, clearance_mm)
    report["source"] = source
    names = {i.id: i.name for i in items}
    for clash in report["clashes"]:
        clash["first_name"], clash["second_name"] = names[clash["first"]], names[clash["second"]]
    return report


# ── CATIA V5: DMU Space Analysis ──

# Values used when the type library does not give the enums (V5 automation
# documentation order).
COMPUTATION_TYPES = {"catClashComputationTypeBetweenAll": 0,
                     "catClashComputationTypeInsideOne": 1,
                     "catClashComputationTypeBetweenTwo": 2,
                     "catClashComputationTypeAgainstAll": 3}
INTERFERENCE_TYPES = {"catClashInterferenceTypeContact": 0,
                      "catClashInterferenceTypeClearance": 1}
CONFLICT_TYPES = {"catConflictTypeClash": 0, "catConflictTypeContact": 1,
                  "catConflictTypeClearance": 2}
CONFLICT_KINDS = {"clash": "interference", "contact": "contact", "clearance": "clearance"}
# Products whose licence the route asks about first (DMU Space Analysis,
# then products every Part Design seat has, to prove the name form).
SPA_PRODUCTS = ("SPA", "SP1")
PROOF_PRODUCTS = ("PDG", "PD1", "ASD", "AS1")


def _enum(objs: list[Any], name: str, default: dict[str, int]) -> tuple[dict[str, int], str]:
    try:
        from catia_mcp.introspect import find_enum

        found = find_enum(objs, name)
    except Exception:  # no type information: the documented values
        found = None
    return (found, "type library") if found else (dict(default), "documented")


def licence(app: Any) -> dict[str, Any]:
    """IsProductAuthorized for DMU Space Analysis, and whether a product a
    Part Design seat has answers yes (else a no proves nothing)."""
    out: dict[str, Any] = {}
    try:
        conf = app.SystemConfiguration
    except Exception as error:
        return {"error": str(error)}
    for name in SPA_PRODUCTS + PROOF_PRODUCTS:
        try:
            out[name] = bool(conf.IsProductAuthorized(name))
        except Exception as error:  # recorded per product
            out[name] = f"error: {error}"
    out["refused"] = (any(out.get(n) is True for n in PROOF_PRODUCTS)
                      and not any(out.get(n) is True for n in SPA_PRODUCTS))
    return out


def compute_v5(app: Any, product: Any, clearance_mm: float = 0.0,
               names: dict[str, str] | None = None,
               log: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """The V5 clash of the whole product (between all its components), its
    conflicts read back. `names`: CATIA instance name -> plan instance id.
    The clash stays in the product (Applications > Interference) for the
    designer to look at. Raises when DMU Space Analysis is unavailable."""
    started = time.monotonic()
    out: dict[str, Any] = {"route": "v5 DMU Space Analysis"}
    out["licence"] = licence(app)
    if out["licence"].get("refused"):
        raise PermissionError("the DMU Space Analysis licence (SPA) is not authorized")
    clashes = product.GetTechnologicalObject("Clashes")
    clash = clashes.Add()
    computation, out["computation_enum"] = _enum([clash, app], "CatClashComputationType",
                                                 COMPUTATION_TYPES)
    interference, out["interference_enum"] = _enum([clash, app], "CatClashInterferenceType",
                                                   INTERFERENCE_TYPES)
    conflict_types, out["conflict_enum"] = _enum([clash, app], "CatConflictType",
                                                 CONFLICT_TYPES)
    clash.ComputationType = computation["catClashComputationTypeBetweenAll"]
    if clearance_mm > 0:
        clash.InterferenceType = interference["catClashInterferenceTypeClearance"]
        clash.Clearance = float(clearance_mm)
    else:
        clash.InterferenceType = interference["catClashInterferenceTypeContact"]
    log("DMU clash: computing")
    clash.Compute()
    out["clash"] = str(getattr(clash, "Name", "") or "")
    kinds = {}
    for enum_name, value in conflict_types.items():
        word = enum_name.removeprefix("catConflictType").lower()
        kinds[value] = CONFLICT_KINDS.get(word, word)
    names = names or {}
    conflicts = clash.Conflicts
    rows = []
    for i in range(1, int(conflicts.Count) + 1):
        conflict = conflicts.Item(i)
        first, second = str(conflict.FirstProduct.Name), str(conflict.SecondProduct.Name)
        code = int(conflict.Type)
        rows.append({"first": names.get(first, first), "second": names.get(second, second),
                     "first_name": first, "second_name": second,
                     "kind": kinds.get(code, f"type {code}"),
                     "value": _round(float(conflict.Value), 4)})
    out["conflicts"] = rows
    out["status"] = "ok"
    out["elapsed_s"] = round(time.monotonic() - started, 3)
    return out


def _pairs(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    return {tuple(sorted((r["first"], r["second"]))): r["kind"] for r in rows}


def compare(offline_rows: list[dict[str, Any]], catia_rows: list[dict[str, Any]]
            ) -> dict[str, Any]:
    """The pairs both analyses report with the same kind, and every
    difference. CATIA's clash value is a penetration depth, the offline one a
    volume: kinds are compared, not values."""
    mine, theirs = _pairs(offline_rows), _pairs(catia_rows)
    agree = sorted(p for p in mine if theirs.get(p) == mine[p])
    differ = [{"pair": list(p), "offline": mine.get(p), "catia": theirs.get(p)}
              for p in sorted(set(mine) | set(theirs)) if mine.get(p) != theirs.get(p)]
    return {"same": not differ, "agree": len(agree), "differences": differ}


X3D_NOTE = ("3DEXPERIENCE: no clash API is known through automation yet "
            "(scripts/probe_clashes.py looks for one); the offline analysis, exact from the "
            "STEP file, is the result")


def catia_route(conn: Any, worker: Any, product: Any, assembly_report: dict[str, Any],
                offline_report: dict[str, Any] | None, clearance_mm: float = 0.0,
                timeout_s: float = 120.0, log: Callable[[str], None] = lambda _: None
                ) -> dict[str, Any]:
    """`--clashes catia`: the CATIA clash of the built product under the
    dialog watchdog, compared with the offline analysis. Never raises: an
    unavailable route (no licence, a dialog, 3DEXPERIENCE) is reported."""
    if getattr(conn, "is_3dx", False):
        return {"status": "unavailable", "route": "3dexperience", "reason": X3D_NOTE}
    if product is None:
        return {"status": "unavailable", "route": "v5 DMU Space Analysis",
                "reason": "no V5 product was assembled"}
    names = {str(i["catia_name"]): i["id"] for i in assembly_report.get("instances") or []
             if i.get("catia_name")}
    from catia_mcp.errors import SessionError

    try:
        result = worker.run_guarded(
            lambda: compute_v5(conn.app, product, clearance_mm, names, log),
            timeout=timeout_s, label="the DMU clash")
    except SessionError as error:  # a licence dialog, a hang: the watchdog's report
        return {"status": "unavailable", "route": "v5 DMU Space Analysis",
                "reason": str(error), "code": error.code,
                "dialog": (error.data or {}).get("watchdog"),
                "note": "Close the dialog in CATIA; the parts and the product are built."}
    except Exception as error:  # no licence, no Clashes object: reported
        return {"status": "unavailable", "route": "v5 DMU Space Analysis", "reason": str(error)}
    if offline_report is not None and offline_report.get("clashes") is not None:
        result["comparison"] = compare(offline_report["clashes"], result["conflicts"])
    return result
