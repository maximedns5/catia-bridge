"""In-memory doubles of CATIA V5 DMU Space Analysis: a product whose
GetTechnologicalObject("Clashes") gives a Clashes collection, its Clash
(ComputationType, InterferenceType, Clearance, Compute) and the Conflicts
Compute fills in from a list given up front, and SystemConfiguration's
IsProductAuthorized.

`licences`: the products IsProductAuthorized says yes to; `missing`: the
Clashes object is refused (no DMU Space Analysis installed).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class Conflict:
    def __init__(self, kind: int, value: float, first: str, second: str) -> None:
        self.Type = kind
        self.Value = value
        self.FirstProduct = SimpleNamespace(Name=first)
        self.SecondProduct = SimpleNamespace(Name=second)


class Conflicts:
    def __init__(self, items: list[Conflict]) -> None:
        self.items = items

    @property
    def Count(self) -> int:
        return len(self.items)

    def Item(self, index: int) -> Conflict:
        return self.items[index - 1]


class Clash:
    def __init__(self, name: str, results: list[tuple[int, float, str, str]]) -> None:
        self.Name = name
        self.ComputationType = None
        self.InterferenceType = None
        self.Clearance = None
        self.computed = False
        self.results = results
        self.Conflicts = Conflicts([])

    def Compute(self) -> None:
        self.computed = True
        self.Conflicts = Conflicts([Conflict(*r) for r in self.results])


class Clashes:
    def __init__(self, results) -> None:
        self.items: list[Clash] = []
        self.results = results

    def Add(self) -> Clash:
        clash = Clash(f"Interference.{len(self.items) + 1}", self.results)
        self.items.append(clash)
        return clash


class Product:
    def __init__(self, results=(), missing: bool = False) -> None:
        self.clashes = Clashes(list(results))
        self.missing = missing
        self.asked: list[str] = []

    def GetTechnologicalObject(self, name: str) -> Any:
        self.asked.append(name)
        if self.missing or name != "Clashes":
            raise RuntimeError("The method GetTechnologicalObject failed")
        return self.clashes


class Configuration:
    def __init__(self, licences) -> None:
        self.licences = set(licences)

    def IsProductAuthorized(self, name: str) -> bool:
        return name in self.licences


def app(licences=("SPA", "PDG", "ASD")) -> Any:
    return SimpleNamespace(SystemConfiguration=Configuration(licences))
