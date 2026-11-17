"""The assembly to build: which parts to model, in which frame, and where
each instance goes.

With the model's STEP file (catia_mcp/tree/step_structure.py), each tree part
is matched to its STEP instance and modelled in its own frame (the STEP
prototype's), once per prototype: a part used twice is one CATIA part placed
twice. Without it, every tree part is its own prototype, modelled where the
tree puts it, placed at the identity.

The plan holds `tree`, the feature tree to build (one part per prototype, in
its own frame), and the product: the root, its sub-assembly nodes and every
instance with its location relative to its parent node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from catia_mcp.tree.placement import place_part, unplace_part
from catia_mcp.tree.step_structure import IDENTITY, Node, Structure, match

Rows = list[list[float]]


@dataclass
class Placed:
    id: str  # the tree part's id
    name: str  # the part's name (its part number)
    instance_name: str
    parent: str  # the node it sits in
    prototype: str  # the id of the tree part modelled for it
    local: Rows  # part frame -> parent node frame
    world: Rows  # part frame -> root frame
    color: tuple[float, float, float] | None = None


@dataclass
class AssemblyPlan:
    root: Node
    nodes: list[Node] = field(default_factory=list)
    instances: list[Placed] = field(default_factory=list)
    tree: dict[str, Any] = field(default_factory=dict)
    from_step: bool = False

    def summary(self) -> str:
        parts = len(self.tree.get("parts") or [])
        return (f"{self.root.name}: {len(self.instances)} instance(s) of {parts} part(s)"
                f"{f', {len(self.nodes)} sub-assembly node(s)' if self.nodes else ''}"
                f"{' (from the STEP file)' if self.from_step else ''}")


def _root_name(tree: dict[str, Any]) -> str:
    ids = [str(p.get("id") or "") for p in tree.get("parts") or []]
    heads = {i.strip("/").split("/")[0] for i in ids if "/" in i.strip("/")}
    return heads.pop() if len(heads) == 1 else "Assembly"


def plan_assembly(tree: dict[str, Any], structure: Structure | None = None,
                  expected: dict[str, dict[str, Any]] | None = None
                  ) -> tuple[AssemblyPlan, list[str]]:
    """The plan, and the reasons it cannot be trusted (empty when it can).
    `expected` (the reference's world properties per tree part) lets the STEP
    instances be checked against the tree before anything is built."""
    parts = tree.get("parts") or []
    if structure is None:
        name = _root_name(tree)
        root = Node(f"/{name}", name, None, IDENTITY)
        plan = AssemblyPlan(root, tree={**tree, "parts": [place_part(p) for p in parts]})
        for part in parts:
            plan.instances.append(Placed(part["id"], part.get("name") or part["id"],
                                         part.get("name") or part["id"], root.id, part["id"],
                                         IDENTITY, IDENTITY))
        return plan, []
    found, problems = match(tree, structure, expected)
    extra = sorted({i.id for i in structure.instances} - {p["id"] for p in parts})
    problems += [f"{i}: in the STEP file but not in the tree" for i in extra]
    plan = AssemblyPlan(structure.root, list(structure.nodes), from_step=True)
    if problems:
        return plan, problems
    built: dict[str, str] = {}  # STEP prototype -> the tree part modelled for it
    own_parts = []
    for part in parts:
        instance = found[part["id"]]
        if instance.prototype not in built:
            built[instance.prototype] = part["id"]
            own = unplace_part(part, instance.world)
            own["name"] = instance.name
            own_parts.append(own)
        plan.instances.append(Placed(part["id"], instance.name, instance.instance_name,
                                     instance.parent, built[instance.prototype], instance.local,
                                     instance.world, instance.color))
    plan.tree = {**tree, "parts": own_parts}
    return plan, []
