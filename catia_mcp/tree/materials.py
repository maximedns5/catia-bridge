"""Materials and mass: the material a designer would apply, then the mass
CATIA measures, checked against the tree's volume times its density.

CATIA V5, in order:
1. **Catalog:** the material named `catalog_name` (else `name`; matched
   without case, in the `family` when given) in a material catalog: the one
   given with `--material-catalog`, else each `materials/*.CATMaterial`
   under the startup folders (`CATStartupPath`). The catalog is read without
   a window (`Documents.Read`, `Documents.Open` if refused), applied with
   `GetItem("CATMatManagerVBExt").ApplyMaterialOnPart part, material, 0` (a
   copy, no link) on the part document's product then on the part, else on
   each body (`ApplyMaterialOnBody`), and read back (`GetMaterialOnPart`).
   Its density is read (`AnalysisMaterial.GetValue("SAMDensity")`); if the
   catalog's differs from the tree's, the applied copy is given the tree's
   (`PutValue`), noted.
2. **New material:** a `CATMaterial` document (`Documents.Add`), whose
   first material is renamed, given the tree's density and applied the
   same way; the document is closed without saving.

3DEXPERIENCE: materials are PLM objects (Core Material). When the tree
gives a `catalog_name`, the database is searched for it
(`DatabaseSearch`, types in CORE_MATERIAL_TYPES) and applied with the V5
manager if the part exposes one. Neither route is documented for R428; both
are recorded for scripts/probe_properties.py to settle.

**Fallback** (both platforms): parameters in the part's parameter set:
`Material` (string), `Density` (a DENSITY dimension) and `Mass` (a MASS
dimension driven by `smartVolume(body) * Density`, else valued). CATIA's
own inertia then still uses its default density (1000 kg/m3): the report
says so, and checks the `Mass` parameter instead.

The mass is the sum of each final body's Inertia mass (catia_mcp/measure.py),
compared with volume x density within MASS_REL_TOL.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from catia_mcp.automation import call_out_object
from catia_mcp.tree.properties import route

MASS_REL_TOL = 1e-3
DENSITY_LABEL = "SAMDensity"
MANAGER = "CATMatManagerVBExt"
LINK_COPY = 0
# Core Material reference types tried in a DatabaseSearch (unverified).
CORE_MATERIAL_TYPES = ("CATMatReference", "MATReference")
MM3_TO_M3 = 1e-9


def apply_material(ctx: Any, knowledge: Any, catalog: str | None = None) -> dict[str, Any] | None:
    spec = ctx.tree_part.get("material")
    if not spec:
        return None
    density = float(spec["density_kg_m3"])
    result: dict[str, Any] = {"name": spec["name"], "density_kg_m3": density, "routes": []}
    volume = ctx.volume_mm3()
    with contextlib.suppress(Exception):
        result["mass_before_kg"] = _sig(ctx.mass_kg())
    applied = (_apply_3dx(ctx, spec, result) if ctx.is_3dx
               else _apply_v5(ctx, spec, catalog, result))
    ctx.restore_window()
    if applied:
        result["route"] = applied
    else:
        result["route"] = "parameters"
        _parameters(ctx, knowledge, spec, volume, result)
    mass: dict[str, Any] = {"volume_mm3": round(volume, 4),
                            "expected_kg": _sig(volume * MM3_TO_M3 * density)}
    try:
        mass["measured_kg"] = _sig(ctx.mass_kg())
    except Exception as error:
        mass["error"] = str(error)
    result["mass"] = mass
    expected = mass["expected_kg"]
    if applied:
        ok = "measured_kg" in mass and _close(mass["measured_kg"], expected)
        if not ok and "measured_kg" in mass:
            mass["note"] = "CATIA's mass does not match the volume times the tree's density"
        result["status"] = "ok" if ok else "partial"
        return result
    parameter = result.get("mass_parameter_kg")
    ok = parameter is not None and _close(parameter, expected)
    mass["note"] = ("no CATIA material: the inertia uses CATIA's default density; the mass is "
                    "in the Mass parameter")
    result["status"] = "fallback" if ok else "failed"
    return result


def _sig(x: float) -> float:
    """Ten significant digits: a bolt weighs grams, a frame tonnes."""
    return float(f"{x:.10g}")


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= MASS_REL_TOL * max(abs(b), 1e-12)


# ── CATIA V5 ──

def catalog_paths(ctx: Any, explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    roots: list[str] = []
    with contextlib.suppress(Exception):
        roots += str(ctx.app.SystemService.Environ("CATStartupPath")).split(";")
    roots += (os.environ.get("CATStartupPath") or "").split(";")
    found: list[str] = []
    for root in dict.fromkeys(r.strip() for r in roots if r and r.strip()):
        folder = os.path.join(root, "materials")
        default = os.path.join(folder, "Catalog.CATMaterial")
        if os.path.isfile(default):
            found.append(default)
        with contextlib.suppress(OSError):
            for dirpath, _, files in os.walk(folder):
                found += [os.path.join(dirpath, f) for f in sorted(files)
                          if f.lower().endswith(".catmaterial")
                          and os.path.join(dirpath, f) not in found]
    return found


def find_material(document: Any, wanted: str, family: str | None = None) -> Any | None:
    key = wanted.strip().casefold()
    families = document.Families
    for i in range(1, int(families.Count) + 1):
        group = families.Item(i)
        if family and str(group.Name).strip().casefold() != family.strip().casefold():
            continue
        materials = group.Materials
        for j in range(1, int(materials.Count) + 1):
            material = materials.Item(j)
            if str(material.Name).strip().casefold() == key:
                return material
    return None


def managers(ctx: Any) -> list[tuple[str, Any]]:
    out = []
    for name, owner in (("PartDocument.Product", lambda: ctx.part.Parent.Product),
                        ("Part", lambda: ctx.part)):
        with contextlib.suppress(Exception):
            manager = owner().GetItem(MANAGER)
            if manager is not None:
                out.append((name, manager))
    return out


def density_of(material: Any) -> float | None:
    with contextlib.suppress(Exception):
        return float(material.AnalysisMaterial.GetValue(DENSITY_LABEL))
    return None


def apply_with_manager(ctx: Any, material: Any, result: dict[str, Any]) -> Any | None:
    """Apply `material` (a copy); the material read back on the part."""
    for owner, manager in managers(ctx):
        record, done = route(f"{owner}.GetItem({MANAGER}).ApplyMaterialOnPart",
                             lambda m=manager: m.ApplyMaterialOnPart(ctx.part, material,
                                                                     LINK_COPY) or True)
        result["routes"].append(record)
        if not done:
            record, done = route(f"{owner}.GetItem({MANAGER}).ApplyMaterialOnBody",
                                 lambda m=manager: [m.ApplyMaterialOnBody(b, material, LINK_COPY)
                                                    for b in ctx.bodies])
            result["routes"].append(record)
            if not done:
                continue
            reader = "GetMaterialOnBody"
            target = ctx.bodies[0]
        else:
            reader, target = "GetMaterialOnPart", ctx.part
        ctx.update()
        record, applied = route(reader, lambda m=manager, r=reader, t=target:
                                call_out_object(ctx.app, m, r, t))
        result["routes"].append(record)
        return applied if applied is not None else material
    return None


def _density_from_tree(applied: Any, density: float, result: dict[str, Any]) -> None:
    found = density_of(applied)
    result["catalog_density_kg_m3"] = found
    if found is not None and abs(found - density) <= 1e-6 * density:
        return
    record, _ = route("AnalysisMaterial.PutValue(SAMDensity)",
                      lambda: applied.AnalysisMaterial.PutValue(DENSITY_LABEL, density))
    result["routes"].append(record)
    result["applied_density_kg_m3"] = density_of(applied)
    if record["ok"]:
        result["note"] = (f"the catalog's density ({found}) was replaced by the tree's on the "
                          "applied copy")


def _apply_v5(ctx: Any, spec: dict[str, Any], catalog: str | None,
              result: dict[str, Any]) -> str | None:
    wanted = spec.get("catalog_name") or spec["name"]
    documents = ctx.app.Documents
    for path in catalog_paths(ctx, catalog):
        document = None
        for opener in ("Read", "Open"):
            record, document = route(f"Documents.{opener}({path})",
                                     lambda o=opener, p=path: getattr(documents, o)(p))
            result["routes"].append(record)
            if document is not None:
                break
        if document is None:
            continue
        try:
            record, material = route(f"find '{wanted}'", lambda d=document: find_material(
                d, wanted, spec.get("family")))
            if material is None:
                record["ok"] = False
                record.setdefault("error", "not in this catalog")
            result["routes"].append(record)
            if material is None:
                continue
            ctx.restore_window()
            applied = apply_with_manager(ctx, material, result)
            if applied is None:
                return None  # no manager accepted a material: no other catalog will help
            result["applied_name"] = str(getattr(applied, "Name", wanted))
            result["catalog"] = path
            _density_from_tree(applied, float(spec["density_kg_m3"]), result)
            return "catalog"
        finally:
            with contextlib.suppress(Exception):
                document.Close()
            ctx.restore_window()
    return _new_material_v5(ctx, spec, result)


def _new_material_v5(ctx: Any, spec: dict[str, Any], result: dict[str, Any]) -> str | None:
    record, document = route("Documents.Add(CATMaterial)",
                             lambda: ctx.app.Documents.Add("CATMaterial"))
    result["routes"].append(record)
    if document is None:
        return None
    try:
        material = document.Families.Item(1).Materials.Item(1)
        with contextlib.suppress(Exception):
            material.Name = spec["name"]
        with contextlib.suppress(Exception):
            if not material.ExistAnalysisData():
                material.CreateAnalysisData(spec["name"])
        material.AnalysisMaterial.PutValue(DENSITY_LABEL, float(spec["density_kg_m3"]))
        ctx.restore_window()
        applied = apply_with_manager(ctx, material, result)
        if applied is None:
            return None
        result["applied_name"] = str(getattr(applied, "Name", spec["name"]))
        result["applied_density_kg_m3"] = density_of(applied)
        return "new_material"
    except Exception as error:
        result["routes"].append({"route": "new material", "ok": False, "error": str(error)})
        return None
    finally:
        with contextlib.suppress(Exception):
            document.Close()
        ctx.restore_window()


# ── 3DEXPERIENCE ──

def _apply_3dx(ctx: Any, spec: dict[str, Any], result: dict[str, Any]) -> str | None:
    wanted = spec.get("catalog_name")
    if not wanted:
        result["routes"].append({"route": "Core Material search", "ok": False,
                                 "error": "no catalog_name: nothing to search for"})
        return None
    from catia_mcp.plm import PLMSession

    plm = PLMSession(ctx.conn)
    found = None
    for base_type in CORE_MATERIAL_TYPES:
        record, entities = route(f"DatabaseSearch {base_type} V_Name={wanted}",
                                 lambda b=base_type: plm.search(wanted, base_type=b))
        result["routes"].append(record)
        if entities:
            found = entities[0]
            break
    if found is None:
        return None
    applied = apply_with_manager(ctx, found, result)
    if applied is None:
        return None
    result["applied_name"] = plm.attribute(applied, "V_Name") or str(getattr(applied, "Name", ""))
    return "core_material"


# ── Fallback ──

def _parameters(ctx: Any, knowledge: Any, spec: dict[str, Any], volume: float,
                result: dict[str, Any]) -> None:
    density = float(spec["density_kg_m3"])
    made: dict[str, Any] = {}
    for name, kind, value, magnitude, unit in (
            ("Material", "string", spec["name"], None, ""),
            ("Density", "real", density, "DENSITY", "kg_m3"),
            ("Mass", "real", volume * MM3_TO_M3 * density, "MASS", "kg")):
        record, entry = route(f"parameter {name} ({magnitude or kind})",
                              lambda n=name, k=kind, v=value, m=magnitude, u=unit:
                              knowledge.create(n, k, v, magnitude=m, unit=u))
        if entry is None and magnitude:
            # A real when the magnitude is unknown to this CATIA.
            record2, entry = route(f"parameter {name} (real)",
                                   lambda n=name, v=value: knowledge.create(n, "real", v))
            result["routes"] += [record, record2]
        else:
            result["routes"].append(record)
        if entry is not None:
            made[name] = entry
    result["parameters"] = made
    if "Mass" not in made:
        return
    mass = knowledge.made["Mass"]
    if "Density" in made:
        density_ref = knowledge.relation_name(knowledge.made["Density"], "Density")
        terms = " + ".join(f"smartVolume({knowledge.relation_name(b, str(b.Name))})"
                           for b in ctx.bodies)
        expression = f"({terms})*{density_ref}" if len(ctx.bodies) > 1 else f"{terms}*{density_ref}"
        record, _ = route("Relations.CreateFormula Mass = smartVolume * Density",
                          lambda: ctx.part.Relations.CreateFormula(
                              "Mass_formula", "mass from the volume and the density", mass,
                              expression))
        result["routes"].append(record)
        if record["ok"]:
            with contextlib.suppress(Exception):
                ctx.update()
            result["mass_formula"] = expression
    with contextlib.suppress(Exception):
        result["mass_parameter_kg"] = float(mass.Value)
