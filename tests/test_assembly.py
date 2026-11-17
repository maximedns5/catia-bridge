"""Assemblies: the STEP structure, part frames, the assembly plan and the V5
assembler against a fake CATIA. The STEP reader needs OCP (build123d)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree.assembler import AssemblerV5, AssemblyFailed
from catia_mcp.tree.assembly import plan_assembly
from catia_mcp.tree.placement import (
    catia_components,
    from_catia_components,
    inverse,
    place_part,
    unplace_part,
)
from catia_mcp.tree.step_structure import IDENTITY, Instance, Node, Structure, apply, compose

FIXTURES = Path(__file__).parent / "fixtures" / "assembly"
TURNED = [[0.0, 0.0, 1.0, -15.0], [0.0, 1.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 14.0]]  # 90 deg about Y


def _flat(rows):
    return [c for row in rows for c in row]


def _tree():
    return json.loads((FIXTURES / "bracket.tree.json").read_text())


def _structure():
    """bracket.step's structure, written out so the tests run without OCP."""
    root = Node("/Bracket", "Bracket", None, IDENTITY)
    bolt = dict(name="Bolt", parent="/Bracket", prototype="p2", color=None,
                volume_mm3=math.pi * 9 * 20, cog_own_mm=(0.0, 0.0, 0.0))
    shifted = [[1.0, 0.0, 0.0, 15.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 14.0]]
    return Structure(root, [], [
        Instance("/Bracket/Plate", "Plate", "Plate", "/Bracket", "p1", IDENTITY, IDENTITY,
                 (0.8, 0.1, 0.1), 19200.0, (0.0, 0.0, 0.0)),
        Instance("/Bracket/Bolt", instance_name="Bolt", local=shifted, world=shifted, **bolt),
        Instance("/Bracket/Bolt#2", instance_name="Bolt", local=TURNED, world=TURNED, **bolt)])


# ── Frames ──


def test_catia_components_are_the_axes_then_the_origin():
    assert catia_components(TURNED) == [0, 0, -1, 0, 1, 0, 1, 0, 0, -15, 0, 14]
    assert from_catia_components(catia_components(TURNED)) == TURNED
    assert _flat(compose(TURNED, inverse(TURNED))) == pytest.approx(_flat(IDENTITY))


def test_a_part_moved_back_into_its_own_frame_is_where_it_was_modelled():
    world_bolt = _tree()["parts"][2]
    own = unplace_part(world_bolt, TURNED)
    plane = own["features"][0]["plane"]
    assert plane["origin"] == pytest.approx([0, 0, -10]) and plane["normal"] == pytest.approx(
        [0, 0, 1])
    again = place_part({**own, "placement": TURNED})
    assert again["features"][0]["plane"]["origin"] == pytest.approx(
        world_bolt["features"][0]["plane"]["origin"])


# ── Plan ──


def test_a_part_used_twice_is_modelled_once_and_placed_twice():
    plan, problems = plan_assembly(_tree(), _structure())
    assert problems == [] and plan.from_step
    assert [p["id"] for p in plan.tree["parts"]] == ["/Bracket/Plate", "/Bracket/Bolt"]
    assert [(i.id, i.prototype) for i in plan.instances] == [
        ("/Bracket/Plate", "/Bracket/Plate"), ("/Bracket/Bolt", "/Bracket/Bolt"),
        ("/Bracket/Bolt#2", "/Bracket/Bolt")]
    bolt = plan.tree["parts"][1]
    assert bolt["name"] == "Bolt"
    assert bolt["features"][0]["plane"]["origin"] == pytest.approx([0, 0, -10])
    assert plan.instances[2].local == TURNED and plan.instances[0].color == (0.8, 0.1, 0.1)
    assert plan.summary() == "Bracket: 3 instance(s) of 2 part(s) (from the STEP file)"


def test_a_step_that_does_not_match_the_tree_is_refused():
    structure = _structure()
    structure.instances[1].volume_mm3 = 500.0
    structure.instances.append(copy.deepcopy(structure.instances[2]))
    structure.instances[-1].id = "/Bracket/Washer"
    expected = {"/Bracket/Bolt": {"volume_mm3": math.pi * 9 * 20, "cog_mm": [15, 0, 14]}}
    _, problems = plan_assembly(_tree(), structure, expected)
    assert problems == [
        "/Bracket/Bolt: the STEP solid measures 500.0000 mm3, the tree's 565.4867",
        "/Bracket/Washer: in the STEP file but not in the tree"]
    structure = _structure()
    expected = {"/Bracket/Bolt": {"volume_mm3": math.pi * 9 * 20, "cog_mm": [15, 0, 4]}}
    _, problems = plan_assembly(_tree(), structure, expected)
    assert problems == [("/Bracket/Bolt: the STEP solid sits 10.0000 mm from the tree's "
                         "(centre of gravity)")]


def test_without_a_step_every_part_is_placed_where_the_tree_puts_it():
    plan, problems = plan_assembly(_tree())
    assert problems == [] and not plan.from_step and plan.root.name == "Bracket"
    assert len(plan.tree["parts"]) == 3
    assert all(i.local == IDENTITY and i.prototype == i.id for i in plan.instances)


# ── STEP reader (needs OCP) ──


def test_the_step_reader_finds_shared_parts_their_places_and_colours():
    pytest.importorskip("OCP")
    from catia_mcp.tree.step_structure import read

    structure = read(str(FIXTURES / "bracket.step"))
    assert structure.root.name == "Bracket"
    ids = [(i.id, i.prototype) for i in structure.instances]
    assert ids == [("/Bracket/Plate", "p1"), ("/Bracket/Bolt", "p2"), ("/Bracket/Bolt#2", "p2")]
    plate, bolt, turned = structure.instances
    assert plate.color == pytest.approx((0.8, 0.1, 0.1), abs=1e-6)
    assert [r[3] for r in bolt.local] == pytest.approx([15, 0, 14])
    assert _flat(turned.world) == pytest.approx(_flat(TURNED))
    assert turned.volume_mm3 == pytest.approx(math.pi * 9 * 20, rel=1e-9)
    assert apply(turned.world, turned.cog_own_mm) == pytest.approx((-15, 0, 14))


def test_the_bracket_tree_matches_its_step_file():
    pytest.importorskip("build123d")
    from catia_mcp.tree.reference import expected
    from catia_mcp.tree.step_structure import read

    tree = _tree()
    world = expected(tree, features=False)
    plan, problems = plan_assembly(tree, read(str(FIXTURES / "bracket.step")), world)
    assert problems == []
    (_, bolt) = expected(plan.tree, features=False).values()
    assert bolt["cog_mm"] == pytest.approx([0, 0, 0], abs=1e-9)  # modelled in its own frame


# ── V5 assembler against a fake CATIA ──


class _Products:
    def __init__(self, app, parent):
        self.app, self.parent, self.items = app, parent, []

    def _add(self, reference, part_number):
        product = _Product(self.app, part_number, reference)
        self.items.append(product)
        self.app.calls.append((self.parent.PartNumber, part_number))
        return product

    def AddNewProduct(self, part_number):
        return self._add(None, part_number)

    def AddExternalComponent(self, document):
        return self._add(document.Product, document.Product.PartNumber)

    def AddComponent(self, reference):
        return self._add(reference, reference.PartNumber)


class _Product:
    def __init__(self, app, part_number, reference=None):
        self.PartNumber = self.Name = part_number
        self.ReferenceProduct = reference or self
        self.Products = _Products(app, self)
        self.Position = SimpleNamespace(values=[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0])
        self.Position.SetComponents = lambda v, p=self.Position: setattr(p, "values", list(v))
        self.updated = False

    def Update(self):
        self.updated = True


class _App:
    def __init__(self):
        self.calls, self.colours = [], []
        self.product_doc = None
        selection = SimpleNamespace(
            Clear=lambda: None, Add=lambda item: self.colours.append(item.Name),
            VisProperties=SimpleNamespace(SetRealColor=lambda r, g, b, i: self.colours.append(
                (r, g, b))))
        self.ActiveDocument = SimpleNamespace(Selection=selection)
        self.Documents = SimpleNamespace(Add=self._add)

    def _add(self, kind):
        assert kind == "Product"
        self.product_doc = SimpleNamespace(Product=_Product(self, "Product1"))
        return self.product_doc


def _documents():
    return {"/Bracket/Plate": SimpleNamespace(Product=_Product(_App(), "Part1")),
            "/Bracket/Bolt": SimpleNamespace(Product=_Product(_App(), "Part2"))}


def test_the_assembler_places_each_instance_and_reuses_a_part():
    app = _App()
    plan, _ = plan_assembly(_tree(), _structure())
    documents = _documents()
    report = AssemblerV5(app, read_position=lambda p: p.values).assemble(plan, documents)
    root = app.product_doc.Product
    assert root.PartNumber == "Bracket" and root.updated
    plate, bolt, turned = root.Products.items
    assert [p.Name for p in (plate, bolt, turned)] == ["Plate.1", "Bolt.1", "Bolt.2"]
    assert turned.ReferenceProduct is bolt.ReferenceProduct is documents["/Bracket/Bolt"].Product
    assert documents["/Bracket/Bolt"].Product.PartNumber == "Bolt"
    assert turned.Position.values == [0, 0, -1, 0, 1, 0, 1, 0, 0, -15, 0, 14]
    assert report["ok"] and [i["position_ok"] for i in report["instances"]] == [True] * 3
    assert app.colours == ["Plate.1", (204, 26, 26)]


def test_a_position_catia_does_not_keep_fails_the_assembly():
    plan, _ = plan_assembly(_tree(), _structure())
    with pytest.raises(AssemblyFailed, match="did not keep the position of /Bracket/Bolt#2"):
        AssemblerV5(_App(), read_position=lambda p: [0.0] * 12 if p.values[9] == -15
                    else p.values).assemble(plan, _documents())


def test_sub_assemblies_are_product_components_placed_in_their_parent():
    structure = _structure()
    sub = Node("/Bracket/Fixing", "Fixing", "/Bracket",
               [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 5.0]])
    structure.nodes.append(sub)
    for instance in structure.instances[1:]:
        instance.parent = sub.id
    app = _App()
    plan, _ = plan_assembly(_tree(), structure)
    report = AssemblerV5(app, read_position=lambda p: p.values).assemble(plan, _documents())
    root = app.product_doc.Product
    fixing, _ = root.Products.items  # the nodes first, then the parts
    assert fixing.Name == "Fixing" and fixing.Position.values[11] == 5
    assert [p.Name for p in fixing.Products.items] == ["Bolt.1", "Bolt.2"]
    assert report["nodes"] == [{"id": "/Bracket/Fixing", "catia_name": "Fixing",
                                "position_ok": True}]


# ── 3DEXPERIENCE: adopting the product imported from the STEP file ──


def _leaves(plan):
    """The imported product's leaf occurrences as R428 gave them."""
    references = {"/Bracket/Plate": "prd-1", "/Bracket/Bolt": "prd-2"}
    return [{"path": f"/{i + 1}", "title": placed.name, "reference_id":
             references[placed.prototype], "reference": references[placed.prototype],
             "position": catia_components(placed.local)} for i, placed in
            enumerate(plan.instances)]


def test_the_imported_product_is_matched_instance_by_instance():
    from catia_mcp.tree import adopt

    plan, _ = plan_assembly(_tree(), _structure())
    references, rows, problems = adopt.match(plan, _leaves(plan))
    assert problems == [] and references == {"/Bracket/Plate": "prd-1", "/Bracket/Bolt": "prd-2"}
    assert [(r["id"], r["catia"], r["position_ok"]) for r in rows] == [
        ("/Bracket/Plate", "/1", True), ("/Bracket/Bolt", "/2", True),
        ("/Bracket/Bolt#2", "/3", True)]


def test_an_imported_product_that_differs_is_refused():
    from catia_mcp.tree import adopt

    plan, _ = plan_assembly(_tree(), _structure())
    leaves = _leaves(plan)
    leaves[0]["title"] = "Washer"
    leaves[1]["position"][9] += 1.0
    leaves[2]["reference_id"] = "prd-3"
    _, _, problems = adopt.match(plan, leaves)
    assert problems == [
        "/Bracket/Plate: the product's occurrence /1 is 'Washer', not 'Plate'",
        "/Bracket/Bolt: /2 is not where the STEP file puts it",
        "/Bracket/Bolt#2: /3 is a different part from the other instances of /Bracket/Bolt"]
    _, _, problems = adopt.match(plan, leaves[:2])
    assert problems[0].startswith("the active product has 2 part occurrence(s), the model 3")


def test_a_part_is_rebuilt_inside_the_imported_one_and_replaces_its_solid(monkeypatch):
    from tests.test_tree import _builder, _FakePart, _mass

    part = _FakePart()
    imported = part.MainBody
    imported.solids = [(60 * 40 * 8, (0.0, 0.0, 0.0))]
    built = _builder(part, monkeypatch)
    deleted = []
    built.conn.hso = SimpleNamespace(Clear=lambda: None, Add=deleted.append,
                                     Delete=lambda: None)
    plan, _ = plan_assembly(_tree(), _structure())
    plate_tree = {"version": 1, "parts": [plan.tree["parts"][0]]}
    built.targets = {"/Bracket/Plate": part}
    report = built.build(plate_tree, {"/Bracket/Plate": {"volume_mm3": 19200.0}},
                         check_editability=False)
    assert report["success"], report.get("error")
    detail = report["parts_detail"][0]
    assert detail["imported_volume_mm3"] == 19200.0
    assert part.MainBody is not imported and deleted == [imported]
    assert _mass(part.MainBody.solids)[0] == pytest.approx(19200.0)
    assert part.MainBody.Name == imported.Name  # the native body takes the main body's name
    assert detail["import_note"].startswith("the imported solid (19200 mm3) was replaced")
