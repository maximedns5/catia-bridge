"""The assembly a STEP file describes: nodes, part instances, placements,
names and colours (OpenCascade's XCAF reader, installed with build123d).

The feature tree loses each part's own frame: a part moved in build123d
arrives with the move folded into its features (world coordinates). The STEP
of the same model keeps it: each part is stored once, in its own frame (the
prototype), and each instance carries its location relative to its parent.
This module reads that structure so the builder can model each part in its
own frame and place every instance, a part used twice being one CATIA part.

Ids are name paths, as the tree's part ids (`/Demo2/Pipe`); a name repeated
among siblings gets `#2`, `#3`... in sibling order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Rows = list[list[float]]  # 3x4 [R|t], mm


@dataclass
class Node:
    id: str
    name: str
    parent: str | None
    local: Rows
    color: tuple[float, float, float] | None = None


@dataclass
class Instance:
    id: str
    name: str  # the prototype's name (the part)
    instance_name: str
    parent: str
    prototype: str  # instances of one prototype share it
    local: Rows  # part frame -> parent node frame
    world: Rows  # part frame -> root frame
    color: tuple[float, float, float] | None
    volume_mm3: float
    cog_own_mm: tuple[float, float, float]  # in the part's own frame

    @property
    def cog_world_mm(self) -> tuple[float, float, float]:
        return apply(self.world, self.cog_own_mm)


@dataclass
class Structure:
    root: Node
    nodes: list[Node] = field(default_factory=list)  # below the root, parents first
    instances: list[Instance] = field(default_factory=list)

    def prototypes(self) -> dict[str, list[Instance]]:
        out: dict[str, list[Instance]] = {}
        for instance in self.instances:
            out.setdefault(instance.prototype, []).append(instance)
        return out


IDENTITY: Rows = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]


def apply(rows: Rows, point) -> tuple[float, float, float]:
    return tuple(sum(rows[i][k] * float(point[k]) for k in range(3)) + rows[i][3]
                 for i in range(3))


def compose(a: Rows, b: Rows) -> Rows:
    """a then b's frame inside a's: the matrix of a·b."""
    return [[sum(a[i][k] * b[k][j] for k in range(3)) + (a[i][3] if j == 3 else 0.0)
             for j in range(4)] for i in range(3)]


def invert(rows: Rows) -> Rows:
    """The inverse of a rigid motion [R|t]: [R^T | -R^T t]."""
    rt = [[rows[j][i] for j in range(3)] for i in range(3)]
    t = [rows[i][3] for i in range(3)]
    return [rt[i] + [-sum(rt[i][k] * t[k] for k in range(3))] for i in range(3)]


def read(path: str) -> Structure:
    """The structure of a STEP file. Needs OCP (installed with build123d)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.collections import Sequence_TDF_Label
    from OCP.GProp import GProp_GProps
    from OCP.Quantity import Quantity_ColorRGBA, Quantity_TOC_sRGB
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import (
        XCAFDoc_ColorGen,
        XCAFDoc_ColorSurf,
        XCAFDoc_ColorTool,
        XCAFDoc_DocumentTool,
    )
    from OCP.XCAFDoc import XCAFDoc_ShapeTool as Tool

    def name(label: Any) -> str:
        attribute = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
            return attribute.Get().ToExtString()
        return ""

    def rows(location: Any) -> Rows:
        t = location.Transformation()
        return [[t.Value(r, c) for c in range(1, 5)] for r in range(1, 4)]

    def color(label: Any):
        rgba = Quantity_ColorRGBA()
        for kind in (XCAFDoc_ColorGen, XCAFDoc_ColorSurf):
            if XCAFDoc_ColorTool.GetColor_s(label, kind, rgba):
                # GetRGB is linear; the model's colours are sRGB.
                return tuple(round(c, 6) for c in rgba.GetRGB().Values(Quantity_TOC_sRGB))
        return None

    def mass(shape: Any):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        centre = props.CentreOfMass()
        return float(props.Mass()), (centre.X(), centre.Y(), centre.Z())

    document = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    if reader.ReadFile(path) != 1 or not reader.Transfer(document):
        raise ValueError(f"cannot read the STEP file {path}")
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    free = Sequence_TDF_Label()
    tool.GetFreeShapes(free)
    if free.Length() != 1:
        raise ValueError(f"{path}: expected one root shape, found {free.Length()}")
    top = free.Value(1)
    root = Node(f"/{name(top)}", name(top), None, IDENTITY, color(top))
    structure = Structure(root)
    prototypes: dict[int, str] = {}

    def walk(label: Any, node_id: str, world: Any) -> None:
        components = Sequence_TDF_Label()
        Tool.GetComponents_s(label, components)
        seen: dict[str, int] = {}
        for i in range(1, components.Length() + 1):
            instance = components.Value(i)
            referred = TDF_Label()
            Tool.GetReferredShape_s(instance, referred)
            local = Tool.GetLocation_s(instance)
            placed = world.Multiplied(local)
            label_name = name(referred) or name(instance) or f"item{i}"
            seen[label_name] = seen.get(label_name, 0) + 1
            suffix = "" if seen[label_name] == 1 else f"#{seen[label_name]}"
            child_id = f"{node_id}/{label_name}{suffix}"
            if Tool.IsAssembly_s(referred):
                structure.nodes.append(Node(child_id, label_name, node_id, rows(local),
                                            color(instance) or color(referred)))
                walk(referred, child_id, placed)
                continue
            key = prototypes.setdefault(referred.Tag(), f"p{len(prototypes) + 1}")
            volume, cog = mass(Tool.GetShape_s(referred))
            structure.instances.append(Instance(
                child_id, label_name, name(instance) or label_name, node_id, key, rows(local),
                rows(placed), color(instance) or color(referred), volume, cog))

    walk(top, root.id, TopLoc_Location())
    return structure


def match(tree: dict[str, Any], structure: Structure,
          expected: dict[str, dict[str, Any]] | None = None,
          rel_tol: float = 1e-4, cog_tol_mm: float = 1e-3) -> tuple[dict[str, Instance], list[str]]:
    """Each tree part's STEP instance, by id, and the reasons it cannot be
    trusted: no instance, or (with the reference's world volumes and centres
    of gravity) a different solid."""
    by_id = {instance.id: instance for instance in structure.instances}
    found: dict[str, Instance] = {}
    problems: list[str] = []
    for part in tree.get("parts") or []:
        instance = by_id.get(part["id"])
        if instance is None:
            problems.append(f"{part['id']}: no instance of that name in the STEP file")
            continue
        want = (expected or {}).get(part["id"])
        if want:
            volume = float(want["volume_mm3"])
            if abs(instance.volume_mm3 - volume) > rel_tol * max(1.0, abs(volume)):
                problems.append(f"{part['id']}: the STEP solid measures "
                                f"{instance.volume_mm3:.4f} mm3, the tree's {volume:.4f}")
                continue
            if want.get("cog_mm") is not None:
                gap = sum((a - float(b)) ** 2 for a, b in
                          zip(instance.cog_world_mm, want["cog_mm"], strict=True)) ** 0.5
                if gap > cog_tol_mm:
                    problems.append(f"{part['id']}: the STEP solid sits {gap:.4f} mm from the "
                                    "tree's (centre of gravity)")
                    continue
        found[part["id"]] = instance
    return found, problems
