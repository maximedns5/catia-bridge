"""Knowledgeware as a designer sets it up: typed parameters in a parameter
set named for the part, with comments; a design table for the tree's
configurations; checks from bounds (Knowledge Advisor only).

- **Parameter set:** `Parameters.RootParameterSet.ParameterSets.CreateSet`,
  then `DirectParameters.Create*` (the V5 Automation recipe). If CATIA
  refuses the set, the parameters go to the part's root, noted.
- **Types:** length and angle are dimensions valued with their unit (`3mm`,
  `30deg`, as the script parameters, live on R428); real, integer, boolean
  and string use `CreateReal`, `CreateInteger`, `CreateBoolean`,
  `CreateString`. Each is read back (`ValueAsString`).
- **Design table:** the bridge writes a tab-separated text sheet (no Excel
  needed): a first row `As built` with the values the part was built with,
  then one row per configuration. `Relations.CreateDesignTable` copies it
  into the part (copy mode), each column is associated to its parameter
  (`AddAssociation`), and every configuration is activated in turn: the
  part is updated and measured, then the `As built` row is restored and the
  volume must come back. Otherwise the table is removed and every value put
  back.
- **Checks:** `Relations.CreateCheck` with an expression the bridge writes
  from the tree's bounds (`Wall >= 1mm and Wall <= 10mm`). Only when the
  Knowledge Advisor licence is proven, since a refused licence can open a
  blocking dialog.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from typing import Any

from catia_mcp.tools.part_design import _set_prop
from catia_mcp.tree.properties import route

DIMENSIONS = {"length": ("LENGTH", "mm"), "angle": ("ANGLE", "deg")}
AS_BUILT = "As built"
VOLUME_REL_TOL = 1e-4


def safe_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(text)).strip("_") or "Part"
    return name if name[:1].isalpha() else f"P_{name}"


def number(value: float) -> str:
    return f"{float(value):.12g}"


def cell(kind: str, value: Any) -> str:
    """A value as the sheet (and ValuateFromString) writes it."""
    if kind in DIMENSIONS:
        return f"{number(value)}{DIMENSIONS[kind][1]}"
    if kind == "integer":
        return str(int(value))
    if kind == "real":
        return number(value)
    if kind == "boolean":
        return "true" if value else "false"
    return str(value)


def read_value(parameter: Any) -> str | None:
    with contextlib.suppress(Exception):
        return str(parameter.ValueAsString())
    with contextlib.suppress(Exception):
        return str(parameter.Value)
    return None


# HRESULT 0x80004001: the method exists but is not implemented here.
E_NOTIMPL = "-2147467263"

class KnowledgeStep:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.made: dict[str, Any] = {}  # parameter name -> CATIA parameter
        self.kinds: dict[str, str] = {}
        self.values: dict[str, Any] = {}
        self._sets: dict[str, tuple[Any, str | None, list[dict[str, Any]]]] = {}

    # ── Parameter sets ──

    def set_name(self) -> str:
        return str(self.ctx.tree_part.get("name") or self.ctx.record.name)

    def collection(self, name: str | None = None) -> tuple[Any, str | None, list[dict[str, Any]]]:
        """The parameters collection of the set `name` (the part's own by
        default), made once: (collection, the set's name or None for the
        part's root, the routes tried)."""
        name = name or self.set_name()
        if name not in self._sets:
            parameters = self.ctx.part.Parameters
            record, made = route("RootParameterSet.ParameterSets.CreateSet", lambda: (
                parameters.RootParameterSet.ParameterSets.CreateSet(name)))
            routes = [record]
            if made is not None:
                record2, direct = route("ParameterSet.DirectParameters",
                                        lambda: made.DirectParameters)
                routes.append(record2)
                if direct is not None:
                    self._sets[name] = (direct, str(getattr(made, "Name", name)), routes)
                    return self._sets[name]
            self._sets[name] = (parameters, None, routes)
        return self._sets[name]

    def create(self, name: str, kind: str, value: Any, comment: str | None = None,
               set_name: str | None = None, magnitude: str | None = None,
               unit: str = "") -> dict[str, Any]:
        """One typed parameter; its record. `magnitude` makes a dimension of
        that magnitude (DENSITY, MASS) valued with `unit`."""
        collection, _, _ = self.collection(set_name)
        entry: dict[str, Any] = {"name": name, "type": kind}
        if magnitude or kind in DIMENSIONS:
            magnitude, unit = (magnitude, unit) if magnitude else DIMENSIONS[kind]
            text = f"{number(value)}{unit}"
            parameter = collection.CreateDimension(name, magnitude, 0)
            parameter.ValuateFromString(text)
            entry["set"] = text
        elif kind == "real":
            parameter = collection.CreateReal(name, float(value))
            entry["set"] = number(value)
        elif kind == "integer":
            parameter = collection.CreateInteger(name, int(value))
            entry["set"] = str(int(value))
        elif kind == "boolean":
            parameter = collection.CreateBoolean(name, bool(value))
            entry["set"] = "true" if value else "false"
        else:
            parameter = collection.CreateString(name, str(value))
            entry["set"] = str(value)
        if comment:
            entry["comment"] = comment if _set_prop(parameter, "Comment", comment) else None
        entry["read"] = read_value(parameter)
        self.made[name] = parameter
        self.kinds[name] = kind
        self.values[name] = value
        return entry

    def lookup(self, name: str) -> Any:
        if name in self.made:
            return self.made[name]
        return self.ctx.part.Parameters.Item(name)

    def relation_name(self, obj: Any, fallback: str) -> str:
        with contextlib.suppress(Exception):
            return str(self.ctx.part.Parameters.GetNameToUseInRelation(obj))
        return fallback if re.match(r"^[A-Za-z]\w*$", fallback) else f"`{fallback}`"

    # ── The tree's parameters ──

    def parameters(self) -> dict[str, Any] | None:
        specs = self.ctx.tree_part.get("parameters")
        if not specs:
            return None
        _, set_name, routes = self.collection()
        result: dict[str, Any] = {"set": set_name, "routes": routes, "created": [],
                                  "failed": []}
        for spec in specs:
            try:
                result["created"].append(self.create(spec["name"], spec["type"], spec["value"],
                                                     spec.get("comment")))
            except Exception as error:
                result["failed"].append({"name": spec["name"], "error": str(error)})
        result["status"] = "ok" if not result["failed"] and set_name else (
            "failed" if not result["created"] else "partial")
        if set_name is None and result["created"]:
            result["note"] = ("CATIA refused the parameter set: the parameters are in the "
                              "part's root")
        if not result["failed"]:
            del result["failed"]
        return result

    # ── Design table ──

    def _columns(self) -> tuple[list[str], dict[str, str], dict[str, Any]]:
        """Each configured parameter, its kind and its as-built value."""
        script = {p["name"]: p for p in getattr(self.ctx.record, "parameters", None) or []}
        names: list[str] = []
        for row in self.ctx.tree_part["configurations"]:
            names += [n for n in row["params"] if n not in names]
        kinds, built = {}, {}
        for name in names:
            if name in self.kinds:
                kinds[name], built[name] = self.kinds[name], self.values[name]
            elif name in script:
                kinds[name] = "angle" if script[name].get("unit") == "deg" else "length"
                built[name] = script[name]["value"]
        return names, kinds, built

    def sheet(self, sheet_dir: str | None, names: list[str], kinds: dict[str, str],
              built: dict[str, Any]) -> str:
        folder = sheet_dir or os.path.join(tempfile.gettempdir(), "catia_bridge_tables")
        os.makedirs(folder, exist_ok=True)
        path = os.path.abspath(os.path.join(folder, f"{safe_name(self.set_name())}"
                                                    "_configurations.txt"))
        rows = [["Configuration", *names], [AS_BUILT, *(cell(kinds[n], built[n]) for n in names)]]
        for row in self.ctx.tree_part["configurations"]:
            rows.append([row["name"], *(cell(kinds[n], row["params"].get(n, built[n]))
                                        for n in names)])
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\r\n".join("\t".join(r) for r in rows) + "\r\n")
        return path

    def design_table(self, sheet_dir: str | None = None) -> dict[str, Any] | None:
        if not self.ctx.tree_part.get("configurations"):
            return None
        names, kinds, built = self._columns()
        result: dict[str, Any] = {"columns": names, "configurations": []}
        missing = [n for n in names if n not in built]
        parameters: dict[str, Any] = {}
        for name in names:
            if name in missing:
                continue
            _, found = route(f"Parameters.Item({name})", lambda n=name: self.lookup(n))
            if found is None:
                missing.append(name)
            else:
                parameters[name] = found
        if missing:
            result.update(status="failed", missing=missing,
                          note="a configured parameter does not exist in the part (script "
                               "parameters exist only in the human style, with build123d)")
            return result
        result["sheet"] = path = self.sheet(sheet_dir, names, kinds, built)
        relations = self.ctx.part.Relations
        table_name = f"{safe_name(self.set_name())}_Configurations"
        before = self.ctx.volume_mm3()
        table = None
        try:
            table = self._create_table(relations, table_name, path, result)
            result["name"] = str(getattr(table, "Name", table_name))
            for name in names:
                table.AddAssociation(parameters[name], name)
            table.Configuration = 1
            self.ctx.update()
            as_built = self.ctx.volume_mm3()
            result["as_built_volume_mm3"] = round(as_built, 4)
            if not _same_volume(as_built, before):
                raise RuntimeError(f"the 'As built' row moved the part from {before:.4f} to "
                                   f"{as_built:.4f} mm3")
            for index, row in enumerate(self.ctx.tree_part["configurations"], start=2):
                entry: dict[str, Any] = {"name": row["name"], "row": index}
                try:
                    table.Configuration = index
                    self.ctx.update()
                    entry["volume_mm3"] = round(self.ctx.volume_mm3(), 4)
                    entry["values"] = {n: read_value(parameters[n]) for n in names}
                except Exception as error:  # this row does not build: noted
                    entry["error"] = str(error)
                result["configurations"].append(entry)
            table.Configuration = 1
            self.ctx.update()
            restored = self.ctx.volume_mm3()
            result["restored_volume_mm3"] = round(restored, 4)
            if not _same_volume(restored, before):
                raise RuntimeError(f"back on 'As built' the part measures {restored:.4f} mm3, "
                                   f"not {before:.4f}")
        except Exception as error:
            if table is not None:
                with contextlib.suppress(Exception):
                    relations.Remove(table_name)
            for name, parameter in parameters.items():
                with contextlib.suppress(Exception):
                    parameter.ValuateFromString(cell(kinds[name], built[name]))
            with contextlib.suppress(Exception):
                self.ctx.update()
            status = "failed"
            if table is None and result.get("attempts") and all(
                    a.get("not_implemented") for a in result["attempts"]):
                # 3DEXPERIENCE R428 answers E_NOTIMPL: design tables cannot
                # be made through COM there. The sheet stays for a manual import.
                status = "unavailable"
                result["note"] = ("this CATIA does not implement design tables through COM; "
                                  "the sheet can be imported by hand (Formula > Design Table)")
            result.update(status=status, error=str(error), removed=table is not None)
            return result
        bad = [c["name"] for c in result["configurations"] if "error" in c]
        result["status"] = "partial" if bad else "ok"
        if bad:
            result["note"] = f"these configurations did not build: {', '.join(bad)}"
        return result

    # Each way to make the table, in turn: the copied sheet, the sheet kept
    # linked, then the horizontal layout. Every attempt is recorded.
    TABLE_ROUTES = (("CreateDesignTable", True), ("CreateDesignTable", False),
                    ("CreateHorizontalDesignTable", True))

    def _create_table(self, relations: Any, table_name: str, path: str,
                      result: dict[str, Any]) -> Any:
        attempts = result.setdefault("attempts", [])
        last: Exception | None = None
        for method, copy in self.TABLE_ROUTES:
            entry: dict[str, Any] = {"route": f"{method}(copy={copy})"}
            try:
                table = getattr(relations, method)(table_name, "configurations of the tree",
                                                   copy, path)
            except Exception as error:
                entry.update(ok=False, error=str(error),
                             not_implemented=E_NOTIMPL in str(error))
                attempts.append(entry)
                last = error
                continue
            entry["ok"] = True
            attempts.append(entry)
            return table
        raise last if last is not None else RuntimeError("no design table route")

    # ── Checks ──

    def check_expression(self, check: dict[str, Any]) -> str:
        name = check["parameter"]
        reference = self.relation_name(self.lookup(name), name)
        kind = self.kinds.get(name, "length")
        unit = DIMENSIONS[kind][1] if kind in DIMENSIONS else ""
        terms = []
        if "min" in check:
            terms.append(f"{reference} >= {number(check['min'])}{unit}")
        if "max" in check:
            terms.append(f"{reference} <= {number(check['max'])}{unit}")
        return " and ".join(terms)

    def checks(self, knowledge_advisor: bool | None) -> dict[str, Any] | None:
        specs = self.ctx.tree_part.get("checks")
        if not specs:
            return None
        if not knowledge_advisor:
            return {"status": "skipped", "count": len(specs),
                    "note": "checks need Knowledge Advisor, not proven available here (a "
                            "refused licence can open a dialog): run scripts/probe_properties.py "
                            "--kwa, then build with --checks on"}
        result: dict[str, Any] = {"created": [], "failed": []}
        for check in specs:
            try:
                expression = self.check_expression(check)
                made = self.ctx.part.Relations.CreateCheck(check["name"],
                                                           check.get("comment") or "",
                                                           expression)
                self.ctx.update()
                entry = {"name": check["name"], "expression": expression}
                with contextlib.suppress(Exception):
                    entry["satisfied"] = bool(made.Value)
                result["created"].append(entry)
            except Exception as error:
                result["failed"].append({"name": check["name"], "error": str(error)})
        result["status"] = "ok" if not result["failed"] else (
            "failed" if not result["created"] else "partial")
        if not result["failed"]:
            del result["failed"]
        return result


def _same_volume(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-3, VOLUME_REL_TOL * abs(b))
