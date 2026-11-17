"""Put the built parts together in a CATIA V5 product.

After the builder has modelled each prototype in its own part document,
the assembler makes the product a designer would have made:

- a new Product document named after the root, a product component
  (`AddNewProduct`) per sub-assembly node;
- the first instance of each part from its document
  (`AddExternalComponent`), every further one reusing it (`AddComponent`
  of its reference product): one CATPart placed several times;
- each instance named and placed by `Position.SetComponents` (the axes,
  then the origin, relative to its parent), read back and compared;
- colours, when the STEP gave some.

Every step is checked; nothing is saved. On 3DEXPERIENCE a scripted Physical
Product needs licences the student roles lack: the assembler says so and
leaves the built parts as they are (see scripts/probe_assembly.py).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from catia_mcp.tree.assembly import AssemblyPlan
from catia_mcp.tree.placement import catia_components

POSITION_TOL = 1e-6


class AssemblyFailed(Exception):
    pass


def _default_reader(app: Any) -> Callable[[Any], list[float]]:
    from catia_mcp.automation import call_fill_array

    return lambda position: list(call_fill_array(app, position, "GetComponents", 12))


class AssemblerV5:
    def __init__(self, app: Any, read_position: Callable[[Any], list[float]] | None = None,
                 log: Callable[[str], None] = lambda _: None) -> None:
        self.app = app
        self.read_position = read_position or _default_reader(app)
        self.log = log

    def assemble(self, plan: AssemblyPlan, documents: dict[str, Any]) -> dict[str, Any]:
        """`documents`: the part document built for each prototype (tree part
        id). Returns the report's `assembly` entry."""
        report: dict[str, Any] = {"product": plan.root.name, "nodes": [], "instances": [],
                                  "notes": []}
        missing = sorted({i.prototype for i in plan.instances} - set(documents))
        if missing:
            raise AssemblyFailed(f"no part document was built for {', '.join(missing)}")
        product_doc = self.app.Documents.Add("Product")
        root = product_doc.Product
        self._name(root, "PartNumber", plan.root.name, report)
        report["catia_product"] = str(root.PartNumber)
        parents = {plan.root.id: root}
        for node in plan.nodes:
            parent = parents.get(node.parent)
            if parent is None:
                raise AssemblyFailed(f"sub-assembly {node.id} comes before its parent {node.parent}")
            sub = parent.Products.AddNewProduct(node.name)
            self._name(sub, "Name", node.name, report)
            ok = self._place(sub, node.local)
            parents[node.id] = sub
            report["nodes"].append({"id": node.id, "catia_name": str(sub.Name), "position_ok": ok})
        first: dict[str, Any] = {}
        counts: dict[str, int] = {}
        for placed in plan.instances:
            products = parents[placed.parent].Products
            if placed.prototype in first:
                component = products.AddComponent(first[placed.prototype].ReferenceProduct)
            else:
                document = documents[placed.prototype]
                self._part_number(document, placed.name, report)
                component = products.AddExternalComponent(document)
                first[placed.prototype] = component
            counts[placed.name] = counts.get(placed.name, 0) + 1
            self._name(component, "Name", f"{placed.name}.{counts[placed.name]}", report)
            ok = self._place(component, placed.local)
            if placed.color is not None:
                self._color(component, placed.color, report)
            report["instances"].append({"id": placed.id, "catia_name": str(component.Name),
                                        "part": placed.prototype, "position_ok": ok})
        root.Update()
        bad = [i["id"] for i in report["instances"] + report["nodes"] if not i["position_ok"]]
        if bad:
            raise AssemblyFailed(f"CATIA did not keep the position of {', '.join(bad)}")
        report["ok"] = True
        if not report["notes"]:
            del report["notes"]
        return report

    def _place(self, product: Any, rows: list[list[float]]) -> bool:
        wanted = catia_components(rows)
        product.Position.SetComponents(wanted)
        got = self.read_position(product.Position)
        return len(got) == 12 and all(abs(a - b) <= POSITION_TOL for a, b in
                                      zip(got, wanted, strict=True))

    @staticmethod
    def _name(product: Any, attribute: str, name: str, report: dict[str, Any]) -> None:
        """Set a name and note when CATIA keeps another (duplicates are
        renamed or refused)."""
        try:
            setattr(product, attribute, name)
        except Exception as error:  # noted, never fatal
            report["notes"].append(f"{attribute} '{name}' refused: {error}")
            return
        kept = str(getattr(product, attribute))
        if kept != name:
            report["notes"].append(f"{attribute} '{name}' became '{kept}'")

    def _part_number(self, document: Any, name: str, report: dict[str, Any]) -> None:
        try:
            self._name(document.Product, "PartNumber", name, report)
        except Exception as error:  # a document without a root product
            report["notes"].append(f"part number '{name}' not set: {error}")

    def _color(self, component: Any, rgb, report: dict[str, Any]) -> None:
        """The STEP colour (sRGB 0..1) on the instance, through a selection."""
        try:
            selection = self.app.ActiveDocument.Selection
            selection.Clear()
            selection.Add(component)
            r, g, b = (max(0, min(255, round(c * 255))) for c in rgb)
            selection.VisProperties.SetRealColor(r, g, b, 1)
            selection.Clear()
        except Exception as error:  # a colour is a bonus: noted
            report["notes"].append(f"colour of {component.Name} not set: {error}")
