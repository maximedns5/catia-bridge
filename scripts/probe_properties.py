"""Live probe of the COM routes behind part properties: appearance,
materials and mass, product properties, Knowledgeware.

The tree replay's properties step (catia_mcp/tree/properties.py,
docs/materials-properties.md) tries several routes per property and keeps
the first that works. Several are undocumented on 3DEXPERIENCE R428, so this
probe tries each one on a scratch part and writes what happened:

1. session: platform, release, and the licences that matter
   (IsProductAuthorized: Knowledge Advisor KWA, KE1, PKT, Part Design...);
2. colour: SetRealColor on the main body, the part and the first feature,
   each read back with GetRealColor;
3. inertia: the mass CATIA measures before any material (default density);
4. material: V5: catalog files under CATStartupPath, families and first
   materials with their density (SAMDensity), the material manager
   (GetItem "CATMatManagerVBExt" on the product and the part),
   ApplyMaterialOnPart, GetMaterialOnPart, a new CATMaterial document, and
   the mass after; 3DEXPERIENCE: the same manager on the part, candidate
   editor services (their members read from the type library, never
   called), and a DatabaseSearch for a Core Material by title (--material);
5. properties: V5 product fields (PartNumber, Revision, Definition,
   Nomenclature, DescriptionRef, Source) and UserRefProperties; on
   3DEXPERIENCE the PLM attributes, each set, read back and restored;
6. parameters: a parameter set, one parameter of each type with a comment,
   DENSITY and MASS dimensions, GetNameToUseInRelation, a smartVolume
   formula;
7. design table: from a tab-separated .txt and from a .csv the probe writes,
   associations, each configuration activated and read back;
8. relations: the members of Relations (which Create* exist), and with
   --kwa a check and a rule (Knowledge Advisor: a refused licence can open a
   blocking dialog, which the session guard reports);
9. publications: Publications.Add on the V5 product.

Each step is recorded on its own (ok, value or error) and never stops the
probe. Nothing is saved. Run it on a scratch part: --create builds a 60 x 40
x 8 plate in a new part (a 3D Shape on 3DEXPERIENCE) first.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in):
    python scripts/probe_properties.py --create --report properties_probe.json
    python scripts/probe_properties.py --create --material Steel --kwa
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import traceback
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

LICENCES = ("KWA", "KE1", "KWE", "PKT", "PDG", "PD1", "MDG", "MES", "XDG", "ST1")
EDITOR_SERVICES = ("CATMatManagerVBExt", "MaterialService", "CATMaterialService",
                   "PLMMaterialService", "MATMaterialService", "CATIMaterialService")
V5_FIELDS = ("PartNumber", "Revision", "Definition", "Nomenclature", "DescriptionRef")
PLM_ATTRIBUTES = ("V_Name", "V_description", "PLM_ExternalID", "revision", "V_usage")
RELATION_MEMBERS = ("CreateFormula", "CreateDesignTable", "CreateCheck", "CreateProgram",
                    "CreateSetOfEquations", "CreateReaction", "CreateRuleBase",
                    "CreateOptimization", "CreateLaw", "CreateHorizontalDesignTable")


def step(fn, *args) -> dict[str, Any]:
    """One route: {ok, value} or {ok: False, error}. Never raises."""
    try:
        value = fn(*args)
        return {"ok": True, "value": value}
    except Exception as error:  # recorded, never fatal
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def plate_tree() -> dict[str, Any]:
    corners = [(-30, -20), (30, -20), (30, 20), (-30, 20)]
    entities = [{"type": "line", "start": list(corners[i]), "end": list(corners[(i + 1) % 4]),
                 "id": f"e{i}"} for i in range(4)]
    return {"version": 1, "parts": [{"id": "/Probe/Plate", "name": "ProbePlate",
                                     "bodies": ["Body1"], "features": [
        {"type": "sketch", "id": "sketch1", "plane": {"origin": [0, 0, 0], "x_dir": [1, 0, 0],
                                                      "normal": [0, 0, 1]},
         "entities": entities, "profiles": [{"outer": [e["id"] for e in entities],
                                             "inner": []}]},
        {"type": "extrude", "id": "extrude1", "sketch": "sketch1", "distance": 8,
         "operation": "new_body", "creates": ["Body1"]}]}]}


class Probe:
    def __init__(self, conn: Any, args: Any) -> None:
        self.conn, self.args = conn, args
        self.part = None
        self.body = None

    # 0. The part
    def create(self) -> dict[str, Any]:
        from catia_mcp.tree.builder import TreeBuilder

        builder = TreeBuilder(self.conn, plm_type=self.args.plm_type, style="exact")
        report = builder.build(plate_tree(), None, check_editability=False)
        return {"success": report["success"], "error": report.get("error"),
                "volume_mm3": report["parts_detail"][0]["volume_mm3"]}

    def attach(self) -> dict[str, Any]:
        self.part = self.conn.get_active_part()
        self.body = self.part.MainBody
        from catia_mcp.measure import Measurer

        self.measure = Measurer(self.conn)
        return {"part": str(self.part.Name), "body": str(self.body.Name),
                "bodies": int(self.part.Bodies.Count),
                "volume_mm3": round(self.measure.volume_mm3(self.part, self.body), 4)}

    # 1. Session
    def session(self) -> dict[str, Any]:
        out: dict[str, Any] = {"platform": self.conn.platform_label}
        conf = self.conn.app.SystemConfiguration
        for attr in ("Version", "Release", "ServicePack"):
            out[attr] = step(lambda a=attr: str(getattr(conf, a)))
        licences = {}
        for name in LICENCES:
            for form in (name, f"{name}.prd"):
                licences[form] = step(lambda f=form: bool(conf.IsProductAuthorized(f)))
        out["licences"] = licences
        return out

    # 2. Colour
    def _colour(self, obj: Any, rgb=(204, 26, 26)) -> dict[str, Any]:
        from catia_mcp.automation import call_out_values

        selection = self.conn.hso
        selection.Clear()
        selection.Add(obj)
        try:
            selection.VisProperties.SetRealColor(rgb[0], rgb[1], rgb[2], 1)
            read = call_out_values(self.conn.app, selection.VisProperties, "GetRealColor", 3)
            return {"set": list(rgb), "read": [round(float(c)) for c in read]}
        finally:
            selection.Clear()

    def colour(self) -> dict[str, Any]:
        out = {"main_body": step(self._colour, self.body),
               "part": step(self._colour, self.part, (26, 128, 204))}
        first = step(lambda: self.body.Shapes.Item(1))
        if first["ok"]:
            out["first_feature"] = step(self._colour, first["value"], (51, 102, 204))
            out["first_feature_name"] = step(lambda: str(first["value"].Name))
            out["find_by_name"] = step(lambda: str(self.part.FindObjectByName(
                str(first["value"].Name)).Name))
        return out

    # 3. Inertia
    def inertia(self) -> dict[str, Any]:
        result = step(self.measure.inertia, self.part, self.body)
        volume = step(self.measure.volume_mm3, self.part, self.body)
        if result["ok"] and volume["ok"] and volume["value"]:
            mass = float(result["value"]["mass_kg"])
            result["density_kg_m3"] = mass / (volume["value"] * 1e-9)
        return result

    # 4. Material
    def _manager_routes(self) -> dict[str, Any]:
        out = {"part.GetItem": step(lambda: self.part.GetItem("CATMatManagerVBExt"))}
        if not self.conn.is_3dx:
            out["product.GetItem"] = step(lambda: self.part.Parent.Product.GetItem(
                "CATMatManagerVBExt"))
        return out

    def _members(self, obj: Any) -> Any:
        from catia_mcp.introspect import describe_members, type_name

        return {"type": type_name(obj),
                "members": [m["name"] for m in describe_members(obj)][:80]}

    def material(self) -> dict[str, Any]:
        from catia_mcp.automation import call_out_object
        from catia_mcp.tree.materials import catalog_paths, density_of, find_material

        out: dict[str, Any] = {}
        managers = self._manager_routes()
        out["managers"] = {k: {"ok": v["ok"], "error": v.get("error"),
                               "members": step(self._members, v["value"]) if v["ok"] else None}
                           for k, v in managers.items()}
        manager = next((v["value"] for v in managers.values() if v["ok"] and v["value"]), None)
        ctx = type("Ctx", (), {"app": self.conn.app})()
        if self.conn.is_3dx:
            services = {}
            for name in EDITOR_SERVICES:
                found = step(lambda n=name: self.conn.active_editor.GetService(n))
                services[name] = {"ok": found["ok"] and found["value"] is not None,
                                  "error": found.get("error")}
                if services[name]["ok"]:
                    services[name]["members"] = step(self._members, found["value"])
            out["editor_services"] = services
            from catia_mcp.plm import PLMSession
            from catia_mcp.tree.materials import CORE_MATERIAL_TYPES

            plm = PLMSession(self.conn)
            searches = {}
            for base in CORE_MATERIAL_TYPES:
                found = step(lambda b=base: plm.search(self.args.material, base_type=b))
                searches[base] = {"ok": found["ok"], "error": found.get("error"),
                                  "count": len(found["value"]) if found["ok"] else None}
                if found["ok"] and found["value"]:
                    entity = found["value"][0]
                    searches[base]["first"] = step(lambda e=entity: plm.describe(e))
                    searches[base]["members"] = step(self._members, entity)
                    if manager is not None:
                        searches[base]["apply"] = step(lambda e=entity: manager.ApplyMaterialOnPart(
                            self.part, e, 0))
            out["core_material_search"] = searches
        else:
            paths = step(catalog_paths, ctx, self.args.catalog)
            out["catalog_paths"] = paths
            document = None
            for path in (paths.get("value") or [])[:1]:
                for opener in ("Read", "Open"):
                    opened = step(lambda o=opener, p=path: getattr(self.conn.app.Documents, o)(p))
                    out[f"Documents.{opener}"] = {"ok": opened["ok"], "error": opened.get("error")}
                    if opened["ok"]:
                        document = opened["value"]
                        break
            if document is not None:
                families = []
                for i in range(1, min(int(document.Families.Count), 10) + 1):
                    family = document.Families.Item(i)
                    names = [str(family.Materials.Item(j).Name)
                             for j in range(1, min(int(family.Materials.Count), 8) + 1)]
                    families.append({"family": str(family.Name), "materials": names})
                out["families"] = families
                material = find_material(document, self.args.material)
                if material is None and families and families[0]["materials"]:
                    material = document.Families.Item(1).Materials.Item(1)
                if material is not None:
                    out["material"] = str(material.Name)
                    out["catalog_density"] = density_of(material)
                    if manager is not None:
                        out["apply_on_part"] = step(lambda: manager.ApplyMaterialOnPart(
                            self.part, material, 0))
                        self.part.Update()
                        applied = step(lambda: call_out_object(self.conn.app, manager,
                                                               "GetMaterialOnPart", self.part))
                        out["get_material_on_part"] = {
                            "ok": applied["ok"], "error": applied.get("error"),
                            "name": step(lambda: str(applied["value"].Name))
                            if applied["ok"] else None,
                            "density": density_of(applied["value"]) if applied["ok"] else None}
                        if applied["ok"]:
                            out["put_density"] = step(
                                lambda: applied["value"].AnalysisMaterial.PutValue(
                                    "SAMDensity", 7850.0))
                            out["density_after_put"] = density_of(applied["value"])
                with contextlib.suppress(Exception):
                    document.Close()
            new_document = step(lambda: self.conn.app.Documents.Add("CATMaterial"))
            out["new_material_document"] = {"ok": new_document["ok"],
                                            "error": new_document.get("error")}
            if new_document["ok"]:
                doc = new_document["value"]
                out["new_material"] = step(lambda: str(doc.Families.Item(1).Materials.Item(1)
                                                       .Name))
                with contextlib.suppress(Exception):
                    doc.Close()
            with contextlib.suppress(Exception):
                self.part.Parent.Activate()
        out["inertia_after"] = self.inertia()
        return out

    # 5. Properties
    def properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.conn.is_3dx:
            from catia_mcp.plm import PLMSession

            plm = PLMSession(self.conn)
            owner = plm.plm_object_of(self.part)
            out["owner"] = step(self._members, owner) if owner is not None else None
            if owner is None:
                return out
            for name in PLM_ATTRIBUTES:
                before = plm.attribute(owner, name)
                entry = {"before": before}
                entry["set"] = step(lambda n=name: owner.SetAttributeValue(n, f"probe {n}"))
                entry["after"] = plm.attribute(owner, name)
                if entry["set"]["ok"] and before is not None:
                    entry["restored"] = step(lambda n=name, b=before: owner.SetAttributeValue(n, b))
                out[name] = entry
            return out
        product = self.part.Parent.Product
        for name in V5_FIELDS:
            entry = {"before": step(lambda n=name: str(getattr(product, n)))}
            entry["set"] = step(lambda n=name: setattr(product, n, f"probe {n}"))
            entry["after"] = step(lambda n=name: str(getattr(product, n)))
            out[name] = entry
        out["Source"] = {"set": step(lambda: setattr(product, "Source", 1)),
                         "after": step(lambda: int(product.Source))}
        user = step(lambda: product.UserRefProperties)
        if user["ok"]:
            props = user["value"]
            out["UserRefProperties"] = {
                "CreateString": step(lambda: str(props.CreateString("ProbeText", "x").Value)),
                "CreateReal": step(lambda: float(props.CreateReal("ProbeReal", 1.5).Value)),
                "CreateBoolean": step(lambda: bool(props.CreateBoolean("ProbeFlag", True).Value)),
                "Item": step(lambda: str(props.Item("ProbeText").Value))}
        else:
            out["UserRefProperties"] = user
        return out

    # 6. Parameters
    def parameters(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        parameters = self.part.Parameters
        made = step(lambda: parameters.RootParameterSet.ParameterSets.CreateSet("ProbeSet"))
        out["CreateSet"] = {"ok": made["ok"], "error": made.get("error"),
                            "name": step(lambda: str(made["value"].Name)) if made["ok"] else None}
        collection = made["value"].DirectParameters if made["ok"] else parameters
        self.collection = collection

        def read(p):
            return {"name": str(p.Name), "value": str(p.ValueAsString())}

        def dimension(name, magnitude, text):
            p = collection.CreateDimension(name, magnitude, 0)
            p.ValuateFromString(text)
            return p

        creators = {
            "length": lambda: dimension("ProbeLength", "LENGTH", "12.5mm"),
            "angle": lambda: dimension("ProbeAngle", "ANGLE", "30deg"),
            "real": lambda: collection.CreateReal("ProbeReal2", 1.25),
            "integer": lambda: collection.CreateInteger("ProbeInteger", 3),
            "boolean": lambda: collection.CreateBoolean("ProbeBoolean", True),
            "string": lambda: collection.CreateString("ProbeString", "hello"),
            "density": lambda: dimension("ProbeDensity", "DENSITY", "7850kg_m3"),
            "mass": lambda: dimension("ProbeMass", "MASS", "1kg"),
        }
        self.made = {}
        for kind, create in creators.items():
            result = step(create)
            if result["ok"]:
                self.made[kind] = result["value"]
                result["value"] = step(read, result["value"])
            out[kind] = result
        if "length" in self.made:
            p = self.made["length"]
            out["comment"] = step(lambda: (setattr(p, "Comment", "probe comment"),
                                           str(p.Comment))[1])
            out["full_name"] = step(lambda: str(parameters.GetNameToUseInRelation(p)))
            out["item_by_short_name"] = step(lambda: str(parameters.Item("ProbeLength").Name))
        if "mass" in self.made and "density" in self.made:
            body = step(lambda: str(parameters.GetNameToUseInRelation(self.body)))
            out["body_name_in_relation"] = body
            density = str(parameters.GetNameToUseInRelation(self.made["density"]))
            body_ref = body["value"] if body["ok"] else f"`{self.body.Name}`"
            expression = f"smartVolume({body_ref})*{density}"
            out["mass_formula"] = step(lambda: str(self.part.Relations.CreateFormula(
                "ProbeMassFormula", "", self.made["mass"], expression).Name))
            out["mass_formula"]["expression"] = expression
            with contextlib.suppress(Exception):
                self.part.Update()
            out["mass_value_kg"] = step(lambda: float(self.made["mass"].Value))
        return out

    # 7. Design table
    def design_table(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "length" not in getattr(self, "made", {}):
            return {"skipped": "no ProbeLength parameter"}
        folder = os.path.join(tempfile.gettempdir(), "catia_bridge_tables")
        os.makedirs(folder, exist_ok=True)
        sheets = {
            "txt_tab_units": ("probe_sheet.txt", "\t", ["12.5mm", "20mm", "5mm"]),
            "txt_tab_numbers": ("probe_sheet_numbers.txt", "\t", ["12.5", "20", "5"]),
            "csv": ("probe_sheet.csv", ",", ["12.5mm", "20mm", "5mm"]),
        }
        relations = self.part.Relations
        for key, (file, separator, values) in sheets.items():
            path = os.path.join(folder, file)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                rows = [["Configuration", "ProbeLength"]] + [
                    [n, v] for n, v in zip(("As built", "Long", "Short"), values, strict=True)]
                handle.write("\r\n".join(separator.join(r) for r in rows) + "\r\n")
            entry: dict[str, Any] = {"path": path}
            made = step(lambda p=path, k=key: relations.CreateDesignTable(
                f"ProbeTable_{k}", "probe", True, p))
            entry["create"] = {"ok": made["ok"], "error": made.get("error")}
            if made["ok"]:
                table = made["value"]
                entry["associate"] = step(lambda t=table: t.AddAssociation(self.made["length"],
                                                                            "ProbeLength"))
                entry["configurations_nb"] = step(lambda t=table: int(t.ConfigurationsNb))
                reads = []
                for row in (2, 3, 1):
                    def activate(t=table, r=row):
                        t.Configuration = r
                        self.part.Update()
                        return str(self.made["length"].ValueAsString())
                    reads.append({"row": row, **step(activate)})
                entry["rows"] = reads
                entry["remove"] = step(lambda k=key: relations.Remove(f"ProbeTable_{k}"))
            out[key] = entry
        return out

    # 8. Relations and Knowledge Advisor
    def relations(self) -> dict[str, Any]:
        from catia_mcp.introspect import member_kind

        relations = self.part.Relations
        out = {"members": {n: step(member_kind, relations, n) for n in RELATION_MEMBERS}}
        if not self.args.kwa or "length" not in getattr(self, "made", {}):
            out["kwa"] = "not tried (pass --kwa)"
            return out
        name = str(self.part.Parameters.GetNameToUseInRelation(self.made["length"]))
        out["check"] = step(lambda: bool(relations.CreateCheck(
            "ProbeCheck", "probe", f"{name} >= 1mm and {name} <= 100mm").Value))
        if "real" in self.made:
            target = str(self.part.Parameters.GetNameToUseInRelation(self.made["real"]))
            body = f"if {name} > 10mm {{ {target} = 1 }} else {{ {target} = 0 }}"
            out["rule"] = step(lambda: str(relations.CreateProgram("ProbeRule", "probe",
                                                                   body).Name))
            with contextlib.suppress(Exception):
                self.part.Update()
            out["rule_result"] = step(lambda: float(self.made["real"].Value))
        return out

    # 9. Publications
    def publications(self) -> dict[str, Any]:
        if self.conn.is_3dx:
            return {"part.Publications": step(lambda: int(self.part.Publications.Count))}
        product = self.part.Parent.Product
        reference = product.CreateReferenceFromName(
            f"{product.PartNumber}/!{self.part.Name}/!{self.body.Name}")
        out = {"Add": step(lambda: str(product.Publications.Add("ProbeBody").Name))}
        out["SetDirect"] = step(lambda: product.Publications.SetDirect("ProbeBody", reference))
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"])
    parser.add_argument("--create", action="store_true",
                        help="build a scratch 60 x 40 x 8 plate in a new part first")
    parser.add_argument("--material", default="Steel",
                        help="material to look for (catalog name, or Core Material title)")
    parser.add_argument("--catalog", help="V5 material catalog (.CATMaterial)")
    parser.add_argument("--kwa", action="store_true",
                        help="also create a check and a rule (Knowledge Advisor licence)")
    parser.add_argument("--report", default="properties_probe.json")
    args = parser.parse_args()

    from catia_mcp.com_guard import ComWorker
    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    worker = ComWorker(conn.watchdog, timeout_s=120, name="probe-properties")
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "args": vars(args)}
    try:
        report["connect"] = worker.run_guarded(conn.connect, label="connect")
    except Exception as error:
        print(error)
        return 2
    probe = Probe(conn, args)
    steps = [("session", probe.session)]
    if args.create:
        steps.append(("create", probe.create))
    steps += [("part", probe.attach), ("colour", probe.colour), ("inertia", probe.inertia),
              ("material", probe.material), ("properties", probe.properties),
              ("parameters", probe.parameters), ("design_table", probe.design_table),
              ("relations", probe.relations), ("publications", probe.publications)]
    for name, run in steps:
        print(f"  {name}")
        try:
            report[name] = worker.run_guarded(run, label=name)
        except Exception as error:  # keep what was found so far
            report[name] = {"error": f"{type(error).__name__}: {error}",
                            "traceback": traceback.format_exc()}
            if name in ("create", "part"):
                break
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    if worker.dismissed:
        report["dismissed_dialogs"] = worker.dismissed
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
