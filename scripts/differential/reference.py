"""build123d interpreter: turns a case into the expected solid and properties.

build123d runs on OpenCascade, a kernel independent from CATIA's, so a match
is evidence about CATIA and the MCP tools rather than one implementation
agreeing with itself. Lengths are mm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from build123d import (
    Axis,
    BuildSketch,
    CenterOf,
    Locations,
    Plane,
    Pos,
    Rectangle,
    extrude,
    revolve,
)
from build123d import Circle as B3Circle

from scripts.differential import ops

PLANES = {"xy": Plane.XY, "yz": Plane.YZ, "zx": Plane.ZX}
AXES = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
MAIN = "main"
# Below this intersection volume (mm3) a cutting tool is taken to miss the solid.
_MISS_MM3 = 1e-6


class CaseError(Exception):
    """The case cannot be interpreted unambiguously (a bug in the case)."""


@dataclass
class _Feature:
    tool: Any
    mode: str  # "add" or "subtract"
    body: str


class Reference:
    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {MAIN: None}
        self.active = MAIN
        self.sketch: tuple[Any, Any, list[tuple[float, float]]] | None = None
        self.last: _Feature | None = None

    # ── Operations ──

    def run(self, case: ops.Case) -> Any:
        for op in case.ops:
            getattr(self, f"_{type(op).__name__.lower()}")(op)
        solid = self.bodies[MAIN]
        if solid is None:
            raise CaseError("The main body is empty at the end of the case.")
        return solid

    def _sketch(self, op: ops.Sketch) -> None:
        plane = PLANES[op.plane]
        points = [shape.at for shape in op.shapes if isinstance(shape, ops.Point)]
        faces = [shape for shape in op.shapes if not isinstance(shape, ops.Point)]
        built = None
        if faces:
            with BuildSketch(plane) as sketch:
                for shape in faces:
                    with Locations(shape.at):
                        if isinstance(shape, ops.Rect):
                            Rectangle(shape.width, shape.height)
                        else:
                            B3Circle(shape.radius)
            built = sketch.sketch
        self.sketch = (plane, built, points)

    def _profile(self) -> tuple[Any, Any]:
        if self.sketch is None or self.sketch[1] is None:
            raise CaseError("This operation needs a sketch with a profile.")
        return self.sketch[0], self.sketch[1]

    def _apply(self, tool: Any, mode: str) -> None:
        current = self.bodies[self.active]
        if mode == "add":
            self.bodies[self.active] = tool if current is None else current + tool
        else:
            if current is None:
                raise CaseError("Cannot remove material from an empty body.")
            self.bodies[self.active] = current - tool
        self.last = _Feature(tool, mode, self.active)

    def _pad(self, op: ops.Pad) -> None:
        _, profile = self._profile()
        if op.symmetric:
            tool = extrude(profile, amount=op.height, both=True)
        else:
            tool = extrude(profile, amount=op.height)
        self._apply(tool, "add")

    def _material_side(self, make_tool) -> Any:
        """The tool on the one side of the sketch plane that meets material."""
        current = self.bodies[self.active]
        if current is None:
            raise CaseError("Nothing to cut: the active body is empty.")
        hits = []
        for sign in (1, -1):
            tool = make_tool(sign)
            overlap = current & tool
            if overlap is not None and overlap.volume > _MISS_MM3:
                hits.append(tool)
        if len(hits) != 1:
            raise CaseError(
                "The cut meets material on both sides of the sketch plane."
                if hits else "The cut meets no material on either side."
            )
        return hits[0]

    def _pocket(self, op: ops.Pocket) -> None:
        plane, profile = self._profile()
        tool = self._material_side(
            lambda sign: extrude(profile, amount=op.depth, dir=plane.z_dir * sign)
        )
        self._apply(tool, "subtract")

    def _hole(self, op: ops.Hole) -> None:
        if self.sketch is None or len(self.sketch[2]) != 1:
            raise CaseError("A hole needs a sketch with exactly one Point.")
        plane, _, (at,) = self.sketch
        with BuildSketch(plane) as disk, Locations(at):
            B3Circle(op.diameter / 2)
        tool = self._material_side(
            lambda sign: extrude(disk.sketch, amount=op.depth, dir=plane.z_dir * sign)
        )
        self._apply(tool, "subtract")

    def _revolve(self, angle: float) -> Any:
        plane, profile = self._profile()
        return revolve(profile, axis=Axis(plane.origin, plane.y_dir), revolution_arc=angle)

    def _shaft(self, op: ops.Shaft) -> None:
        self._apply(self._revolve(op.angle), "add")

    def _groove(self, op: ops.Groove) -> None:
        self._apply(self._revolve(op.angle), "subtract")

    def _pattern(self, copies: list[Any]) -> None:
        feature = self.last
        if feature is None:
            raise CaseError("A pattern needs a previous feature.")
        if feature.body != self.active:
            raise CaseError("A pattern applies to a feature of the active body.")
        for tool in copies:
            self._apply(tool, feature.mode)
        self.last = feature

    def _rectpattern(self, op: ops.RectPattern) -> None:
        tool = self._last_tool()
        self._pattern([
            Pos(i * op.spacing1, j * op.spacing2, 0) * tool
            for i in range(op.count1)
            for j in range(op.count2)
            if (i, j) != (0, 0)
        ])

    def _circpattern(self, op: ops.CircPattern) -> None:
        tool = self._last_tool()
        axis = Axis(op.center, AXES[op.axis])
        self._pattern([tool.rotate(axis, k * 360.0 / op.count) for k in range(1, op.count)])

    def _last_tool(self) -> Any:
        if self.last is None:
            raise CaseError("A pattern needs a previous feature.")
        return self.last.tool

    def _mirror(self, op: ops.Mirror) -> None:
        current = self.bodies[self.active]
        if current is None:
            raise CaseError("Nothing to mirror: the active body is empty.")
        self.bodies[self.active] = current + current.mirror(PLANES[op.plane])
        self.last = None

    def _newbody(self, op: ops.NewBody) -> None:
        if op.name in self.bodies:
            raise CaseError(f"Body '{op.name}' already exists.")
        self.bodies[op.name] = None
        self.active = op.name
        self.last = None

    def _activebody(self, op: ops.ActiveBody) -> None:
        name = op.name or MAIN
        if name not in self.bodies:
            raise CaseError(f"No body named '{name}'.")
        self.active = name
        self.last = None

    def _boolean(self, op: ops.Boolean) -> None:
        if op.body == self.active or op.body not in self.bodies:
            raise CaseError(f"Cannot combine body '{op.body}' into '{self.active}'.")
        tool, target = self.bodies.pop(op.body), self.bodies[self.active]
        if tool is None or target is None:
            raise CaseError("A boolean needs two non-empty bodies.")
        if op.operation == "add":
            result = target + tool
        elif op.operation == "remove":
            result = target - tool
        elif op.operation == "intersect":
            result = target & tool
        else:
            raise CaseError(f"Unsupported boolean '{op.operation}'.")
        self.bodies[self.active] = result
        self.last = None


def properties(solid: Any) -> dict[str, Any]:
    """The properties the comparison uses, in mm-based units."""
    cog = solid.center(CenterOf.MASS)
    box = solid.bounding_box()
    return {
        "volume": float(solid.volume),
        "area": float(solid.area),
        "cog": [float(cog.X), float(cog.Y), float(cog.Z)],
        "bbox": {
            "min": [float(box.min.X), float(box.min.Y), float(box.min.Z)],
            "max": [float(box.max.X), float(box.max.Y), float(box.max.Z)],
        },
    }


def expected(case: ops.Case) -> dict[str, Any]:
    return properties(Reference().run(case))
