"""In-memory doubles of what assembly constraints touch in CATIA: a root
product with its CATIAConstraints collection, component positions, and
part documents with geometrical sets, reference elements and publications.

`refuse` makes AddBiEltCst raise for some constraint types (as a CATIA that
rejects a contact between two planes would); `movers` lets Update move a
component when a constraint of some type holds it (as a constraint whose
value or orientation CATIA reads otherwise would).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

IDENTITY12 = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]


class Position:
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)

    def SetComponents(self, values) -> None:  # noqa: N802 - COM naming
        self.values = [float(v) for v in values]


class Constraint:
    def __init__(self, name: str, kind: int, references: list[Any]) -> None:
        self.Name = name
        self.Type = kind
        self.Status = 0
        self.Orientation = None
        self.Dimension = SimpleNamespace(Value=None)
        self.references = references


class Constraints:
    def __init__(self, refuse: set[int] | None = None) -> None:
        self.items: list[Constraint] = []
        self.refuse = refuse or set()
        self.made = 0

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def _add(self, kind: int, references: list[Any]) -> Constraint:
        self.made += 1
        constraint = Constraint(f"Constraint.{self.made}", kind, references)
        self.items.append(constraint)
        return constraint

    def AddMonoEltCst(self, kind: int, reference: Any) -> Constraint:  # noqa: N802
        return self._add(kind, [reference])

    def AddBiEltCst(self, kind: int, first: Any, second: Any) -> Constraint:  # noqa: N802
        if kind in self.refuse:
            raise RuntimeError(f"constraint type {kind} refused")
        return self._add(kind, [first, second])

    def Remove(self, name: str) -> None:  # noqa: N802
        self.items = [c for c in self.items if c.Name != name]


class Reference:
    def __init__(self, name: str) -> None:
        self.DisplayName = self.name = name


class Root:
    """The root product: its components by plan id, positions, constraints."""

    def __init__(self, name: str, positions: dict[str, list[float]],
                 refuse: set[int] | None = None,
                 movers: dict[int, tuple[str, int, float]] | None = None,
                 no_connections: bool = False) -> None:
        self.Name = self.PartNumber = name
        self.components = {i: SimpleNamespace(Position=Position(v)) for i, v in positions.items()}
        self.constraints = Constraints(refuse)
        self.movers = movers or {}  # constraint type -> (component, index, shift)
        self.no_connections = no_connections
        self.updates = 0
        self.names: list[str] = []

    def Connections(self, kind: str) -> Constraints:  # noqa: N802
        if self.no_connections or kind != "CATIAConstraints":
            raise RuntimeError("no such connections")
        return self.constraints

    def CreateReferenceFromName(self, name: str) -> Reference:  # noqa: N802
        self.names.append(name)
        return Reference(name)

    def Update(self) -> None:  # noqa: N802
        self.updates += 1
        for constraint in self.constraints.items:
            if constraint.Type in self.movers:
                component, index, shift = self.movers[constraint.Type]
                self.components[component].Position.values[index] += shift
                del self.movers[constraint.Type]


class Shape:
    def __init__(self, kind: str, *args: Any) -> None:
        self.kind, self.args, self.Name = kind, args, kind


class Geoset:
    def __init__(self) -> None:
        self.Name = "Geometrical Set.1"
        self.shapes: list[Shape] = []

    def AppendHybridShape(self, shape: Shape) -> None:  # noqa: N802
        self.shapes.append(shape)


class Geosets:
    def __init__(self) -> None:
        self.items: list[Geoset] = []

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def Item(self, i: int) -> Geoset:  # noqa: N802
        return self.items[i - 1]

    def Add(self) -> Geoset:  # noqa: N802
        self.items.append(Geoset())
        return self.items[-1]


class Factory:
    def AddNewPointCoord(self, x, y, z):  # noqa: N802
        return Shape("point", x, y, z)

    def AddNewDirectionByCoord(self, x, y, z):  # noqa: N802
        return Shape("direction", x, y, z)

    def AddNewLinePtDir(self, point, direction, begin, end, orientation):  # noqa: N802
        return Shape("line", point, direction, begin, end, orientation)

    def AddNewPlaneEquation(self, a, b, c, d):  # noqa: N802
        return Shape("plane", a, b, c, d)


class Part:
    def __init__(self) -> None:
        self.HybridBodies = Geosets()
        self.HybridShapeFactory = Factory()
        self.updated: list[Shape] = []

    def CreateReferenceFromObject(self, shape: Shape) -> Reference:  # noqa: N802
        return Reference(f"object:{shape.Name}")

    def UpdateObject(self, shape: Shape) -> None:  # noqa: N802
        self.updated.append(shape)


class Publications:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.direct: dict[str, Reference] = {}

    def Add(self, name: str) -> None:  # noqa: N802
        self.names.append(name)

    def SetDirect(self, name: str, reference: Reference) -> None:  # noqa: N802
        self.direct[name] = reference


def document(part_number: str) -> SimpleNamespace:
    product = SimpleNamespace(PartNumber=part_number, Publications=Publications(),
                              CreateReferenceFromName=Reference)
    return SimpleNamespace(Part=Part(), Product=product)
