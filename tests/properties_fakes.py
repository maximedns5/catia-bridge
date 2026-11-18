"""Doubles for the properties step (catia_mcp/tree/properties.py): typed
parameters in parameter sets, relations with design tables and checks,
the selection's colours, V5 products with user properties, material
catalogs and the material manager. They model only what the step touches,
and they work with tests/fakes.py's SystemService for out-parameters."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from tests.fakes import Coll, PLMObject, SystemService

UNITS = {"mm": 1.0, "deg": 1.0, "kg": 1.0, "kg_m3": 1.0}


class Parameter:
    def __init__(self, name: str, kind: str, value: Any = None) -> None:
        self.Name, self.kind, self.Value, self.Comment = name, kind, value, ""
        self.text = None if value is None else str(value)

    def ValuateFromString(self, text: str) -> None:  # noqa: N802
        self.text = text
        if self.kind == "boolean":
            self.Value = text == "true"
        elif self.kind == "string":
            self.Value = text
        else:
            match = re.fullmatch(r"(-?[\d.eE+-]+)\s*([a-z_0-9]*)", text)
            if not match or (match.group(2) and match.group(2) not in UNITS):
                raise RuntimeError(f"invalid value {text!r}")
            self.Value = float(match.group(1))

    def ValueAsString(self) -> str:  # noqa: N802
        return self.text if self.text is not None else str(self.Value)


class Parameters:
    """A parameters collection; sets register their parameters in the
    part's root too, as CATIA's Part.Parameters lists every parameter."""

    def __init__(self, root: Parameters | None = None, refuse_sets: bool = False,
                 refuse_magnitudes: tuple[str, ...] = ()) -> None:
        self.items: dict[str, Parameter] = {}
        self.removed: list[str] = []
        self.root = root
        self.refuse_sets, self.refuse_magnitudes = refuse_sets, refuse_magnitudes
        self.sets: dict[str, Any] = {}

    def _add(self, parameter: Parameter) -> Parameter:
        if parameter.Name in self.items or (self.root and parameter.Name in self.root.items):
            raise RuntimeError(f"a parameter {parameter.Name} already exists")
        self.items[parameter.Name] = parameter
        if self.root is not None:
            self.root.items[parameter.Name] = parameter
        return parameter

    def CreateDimension(self, name: str, magnitude: str, value: float) -> Parameter:  # noqa: N802
        if magnitude in (self.root or self).refuse_magnitudes:
            raise RuntimeError(f"unknown magnitude {magnitude}")
        parameter = self._add(Parameter(name, magnitude.lower(), float(value)))
        parameter.magnitude = magnitude
        return parameter

    def CreateReal(self, name: str, value: float) -> Parameter:  # noqa: N802
        return self._add(Parameter(name, "real", float(value)))

    def CreateInteger(self, name: str, value: int) -> Parameter:  # noqa: N802
        return self._add(Parameter(name, "integer", int(value)))

    def CreateBoolean(self, name: str, value: bool) -> Parameter:  # noqa: N802
        parameter = self._add(Parameter(name, "boolean", bool(value)))
        parameter.text = "true" if value else "false"
        return parameter

    def CreateString(self, name: str, value: str) -> Parameter:  # noqa: N802
        return self._add(Parameter(name, "string", str(value)))

    def Item(self, name: str) -> Parameter:  # noqa: N802
        return self.items[name]

    def Remove(self, name: str) -> None:  # noqa: N802
        self.removed.append(name)
        self.items.pop(name, None)

    @property
    def RootParameterSet(self) -> Any:  # noqa: N802
        def create(name: str) -> Any:
            if self.refuse_sets:
                raise RuntimeError("La méthode CreateSet a échoué")
            made = SimpleNamespace(Name=name, DirectParameters=Parameters(root=self))
            self.sets[name] = made
            return made

        return SimpleNamespace(ParameterSets=SimpleNamespace(CreateSet=create))

    @staticmethod
    def GetNameToUseInRelation(obj: Any) -> str:  # noqa: N802
        return f"`{obj.Name}`"


class DesignTable:
    def __init__(self, name: str, path: str, parameters: Parameters) -> None:
        self.Name, self.path = name, path
        with open(path, encoding="utf-8") as handle:
            self.rows = [line.split("\t") for line in handle.read().splitlines() if line]
        self.associations: list[tuple[Parameter, str]] = []
        self._configuration = 0

    def AddAssociation(self, parameter: Parameter, column: str) -> None:  # noqa: N802
        if column not in self.rows[0]:
            raise RuntimeError(f"no column {column}")
        self.associations.append((parameter, column))

    @property
    def Configuration(self) -> int:  # noqa: N802
        return self._configuration

    @Configuration.setter
    def Configuration(self, row: int) -> None:  # noqa: N802
        self._configuration = row
        values = dict(zip(self.rows[0], self.rows[row], strict=True))
        for parameter, column in self.associations:
            parameter.ValuateFromString(values[column])


class Relations:
    def __init__(self, parameters: Parameters, refuse_checks: bool = False) -> None:
        self.parameters = parameters
        self.formulas: dict[str, tuple[Any, str]] = {}
        self.tables: dict[str, DesignTable] = {}
        self.checks: dict[str, str] = {}
        self.removed: list[str] = []
        self.refuse_checks = refuse_checks

    def CreateFormula(self, name: str, comment: str, target: Any, expression: str) -> Any:  # noqa: N802
        self.formulas[name] = (target, expression)
        return SimpleNamespace(Name=name)

    def CreateDesignTable(self, name: str, comment: str, copy: bool, path: str) -> DesignTable:  # noqa: N802
        assert copy is True
        self.tables[name] = DesignTable(name, path, self.parameters)
        return self.tables[name]

    def CreateCheck(self, name: str, comment: str, expression: str) -> Any:  # noqa: N802
        if self.refuse_checks:
            raise RuntimeError("Knowledge Advisor licence required")
        self.checks[name] = expression
        return SimpleNamespace(Name=name, Value=True)

    def Remove(self, name: str) -> None:  # noqa: N802
        self.removed.append(name)
        for store in (self.formulas, self.tables, self.checks):
            store.pop(name, None)


class VisProperties:
    def __init__(self, selection: Selection) -> None:
        self.selection = selection

    def SetRealColor(self, r: int, g: int, b: int, inheritance: int) -> None:  # noqa: N802
        for obj in self.selection.items:
            if getattr(obj, "refuse_colour", False):
                raise RuntimeError("La méthode SetRealColor a échoué")
            obj.colour = (r, g, b)

    def _outs_GetRealColor(self) -> list[int]:  # noqa: N802
        return list(self.selection.items[0].colour)


class Selection:
    def __init__(self, on_delete=None) -> None:
        self.items: list[Any] = []
        self.on_delete = on_delete

    def Clear(self) -> None:  # noqa: N802
        self.items = []

    def Add(self, obj: Any) -> None:  # noqa: N802
        self.items.append(obj)

    def Delete(self) -> None:  # noqa: N802
        if self.on_delete:
            for obj in self.items:
                self.on_delete(obj)

    @property
    def VisProperties(self) -> VisProperties:  # noqa: N802
        return VisProperties(self)


class Material:
    def __init__(self, name: str, density: float) -> None:
        self.Name, self.density = name, density
        self.AnalysisMaterial = SimpleNamespace(
            GetValue=lambda label: self.density if label == "SAMDensity" else 0.0,
            PutValue=self._put)

    def _put(self, label: str, value: float) -> None:
        assert label == "SAMDensity"
        self.density = float(value)

    def ExistAnalysisData(self) -> int:  # noqa: N802
        return 1

    def copy(self) -> Material:
        return Material(self.Name, self.density)


def catalog(families: dict[str, list[tuple[str, float]]]) -> Any:
    groups = [SimpleNamespace(Name=family, Materials=Coll([Material(n, d) for n, d in items]))
              for family, items in families.items()]
    document = SimpleNamespace(Families=Coll(groups), closed=False)
    document.Close = lambda: setattr(document, "closed", True)
    return document


class MaterialManager:
    """Applies a copy of the material (link mode 0) on the part."""

    def __init__(self) -> None:
        self.on_part: Material | None = None

    def ApplyMaterialOnPart(self, part: Any, material: Material, link: int) -> None:  # noqa: N802
        assert link == 0
        self.on_part = material.copy()

    def _out_GetMaterialOnPart(self, part: Any) -> Material | None:  # noqa: N802
        return self.on_part


class Product:
    """A V5 part document's root product."""

    def __init__(self, manager: MaterialManager | None = None,
                 renames: dict[str, str] | None = None) -> None:
        self.PartNumber, self.Revision, self.Definition = "Part1", "", ""
        self.Nomenclature, self.DescriptionRef, self.Source = "", "", 0
        self.UserRefProperties = Parameters()
        self.manager, self.renames = manager, renames or {}

    def __setattr__(self, name: str, value: Any) -> None:
        renames = self.__dict__.get("renames") or {}
        super().__setattr__(name, renames.get(value, value) if name == "PartNumber" else value)

    def GetItem(self, name: str) -> Any:  # noqa: N802
        if name != "CATMatManagerVBExt" or self.manager is None:
            raise RuntimeError(f"no item {name}")
        return self.manager


class RefusingPLMObject(PLMObject):
    def __init__(self, refuse: tuple[str, ...] = (), **attributes: Any) -> None:
        super().__init__(**attributes)
        self.refuse = refuse

    def SetAttributeValue(self, name: str, value: Any) -> None:  # noqa: N802
        if name in self.refuse:
            raise RuntimeError(f"attribute {name} is read-only")
        super().SetAttributeValue(name, value)


def equip(part: Any, conn: Any, *, v5: bool = False, catalog_document: Any = None,
          manager: MaterialManager | None = None, refuse_sets: bool = False,
          refuse_checks: bool = False, refuse_magnitudes: tuple[str, ...] = ()) -> Any:
    """Give a fake part (tests/test_tree.py's) and its connection what the
    properties step needs; returns the documents collection."""
    part.Parameters = Parameters(refuse_sets=refuse_sets, refuse_magnitudes=refuse_magnitudes)
    part.Relations = Relations(part.Parameters, refuse_checks)
    part.FindObjectByName = lambda name: next(f for f in part.pads if f.Name == name)
    documents = SimpleNamespace(read=[], added=[])

    def read(path: str) -> Any:
        documents.read.append(path)
        if catalog_document is None:
            raise RuntimeError("no such file")
        return catalog_document

    def add(kind: str) -> Any:
        documents.added.append(kind)
        made = catalog({"New": [("Material", 1000.0)]})
        return made

    documents.Read, documents.Add = read, add
    conn.app = SimpleNamespace(SystemService=SystemService(), Documents=documents)
    conn.is_3dx = not v5
    conn.hso = Selection(on_delete=part.delete)
    if v5:
        part.Parent = SimpleNamespace(Product=Product(manager), Activate=lambda: None)
    else:
        part.Parent = RefusingPLMObject(refuse=("PLM_ExternalID",), V_Name="Part1",
                                        PLM_ExternalID="prd-123", V_description="")
    return documents
