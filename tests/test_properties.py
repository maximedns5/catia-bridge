"""Appearance, materials and mass, product properties and Knowledgeware
after a part's build (catia_mcp/tree/properties.py and its steps), against
fake CATIA objects (tests/properties_fakes.py)."""

from __future__ import annotations

import copy
import json
import math
from types import SimpleNamespace

import pytest

from catia_mcp.tree.knowledge import KnowledgeStep
from catia_mcp.tree.preflight import analyze
from catia_mcp.tree.properties import (
    PartProperties,
    carries_fields,
    colours_from_step,
    problems,
    summary,
)
from tests import properties_fakes as fakes
from tests.test_tree import TREES, _builder, _FakePart, _mass, _script_expected, load

FIXTURE = "p1_properties_plate"
PLATE_MM3 = 60 * 40 * 8 - math.pi * 9 * 8


def _expected():
    return {"/Props/Plate": _script_expected()["/Demo/Plate"]}


def _build(monkeypatch, tmp_path, tree=None, v5=False, density_of=None, advisor=False,
           mode="auto", **equipment):
    """Build the fixture (or `tree`) with the properties step on a fake part.
    `density_of()` is the density CATIA's inertia uses (kg/m3)."""
    part = _FakePart(through_depth=8.0)
    built = _builder(part, monkeypatch)
    documents = fakes.equip(part, built.conn, v5=v5, **equipment)
    density_of = density_of or (lambda: 1000.0)
    built.measure = SimpleNamespace(inertia=lambda p, body: {
        "mass_kg": _mass(body.solids)[0] * 1e-9 * density_of()})
    built.properties = PartProperties(mode, catalog="C:/catalogs/Catalog.CATMaterial",
                                      sheet_dir=str(tmp_path), knowledge_advisor=advisor)
    report = built.build(tree or load(FIXTURE), _expected(), check_editability=False)
    assert report["success"], report.get("error")
    return part, report["parts_detail"][0], documents


# ── Contract ──

def test_the_fixture_carries_every_field_and_passes_preflight():
    tree = load(FIXTURE)
    assert not analyze(tree)
    assert carries_fields(tree["parts"][0])
    assert not carries_fields(load("m1_holes")["parts"][0])


@pytest.mark.parametrize("change, reason", [
    (lambda p: p.update(color=[1.2, 0, 0]), "three sRGB numbers"),
    (lambda p: p["features"][2].update(color="red"), "feature hole1: a color"),
    (lambda p: p["material"].pop("density_kg_m3"), "positive density_kg_m3"),
    (lambda p: p["material"].update(name=""), "a material has a name"),
    (lambda p: p["properties"].update(colour="red"), "unknown property 'colour'"),
    (lambda p: p["properties"].update(source="stolen"), "source is one of"),
    (lambda p: p["properties"]["user"].update(Bad=[1]), "user properties map"),
    (lambda p: p["parameters"].append({"name": "X", "type": "volume", "value": 1}),
     "type is one of"),
    (lambda p: p["parameters"].append({"name": "Y", "type": "integer", "value": 1.5}),
     "whole number"),
    (lambda p: p["parameters"].append({"name": "Extrude1_height", "type": "length",
                                       "value": 1}), "declared twice"),
    (lambda p: p["parameters"].append({"name": "2bad", "type": "real", "value": 1}),
     "starts with a letter"),
    (lambda p: p["configurations"][0]["params"].update(Nope=1), "unknown parameter 'Nope'"),
    (lambda p: p["configurations"][1].update(name="Thin"), "declared twice"),
    (lambda p: p["configurations"][0]["params"].update(Wall="thick"), "Wall: a length"),
    (lambda p: p["checks"][0].update(parameter="Supplier"), "numeric parameter"),
    (lambda p: [p["checks"][0].pop(k) for k in ("min", "max")], "numeric min, max or both"),
])
def test_malformed_property_fields_refuse_the_tree(change, reason):
    tree = load(FIXTURE)
    change(tree["parts"][0])
    reasons = analyze(tree)
    assert any(reason in r and r.startswith("part properties:") for r in reasons), reasons


def test_a_configuration_may_name_the_script_parameters_the_builder_makes():
    part = load(FIXTURE)["parts"][0]
    assert not problems(part)
    part["configurations"] = [{"name": "Long", "params": {"Hole1_radius": 3.5}}]
    assert not problems(part)


# ── The step through the builder (3DEXPERIENCE) ──

def test_properties_on_3dexperience_land_as_a_designer_would(monkeypatch, tmp_path):
    part, detail, _ = _build(monkeypatch, tmp_path)
    section = detail["properties"]
    assert section["ok"], section
    # Typed parameters in a set named for the part, with their comments.
    parameters = section["parameters"]
    assert parameters["set"] == "Plate" and parameters["status"] == "ok"
    made = part.Parameters.sets["Plate"].DirectParameters.items
    assert [p["name"] for p in parameters["created"]] == list(made)[:6]
    assert made["Wall"].text == "2.5mm" and made["Wall"].Comment == "minimum wall left by a hole"
    assert made["DraftAngle"].text == "1.5deg" and made["DraftAngle"].magnitude == "ANGLE"
    assert made["HoleCount"].Value == 1 and made["Painted"].Value is True
    assert made["Supplier"].Value == "in-house" and made["Margin"].Value == 1.2
    # PLM attributes: title and description; the part number is refused by
    # the platform and kept, with the rest, as string parameters.
    attributes = section["attributes"]
    assert attributes["set"] == {"V_Name": "Plate",
                                 "V_description": "60 x 40 x 8 plate with one hole"}
    assert "PLM_ExternalID" in attributes["refused"]
    kept = part.Parameters.sets["Properties"].DirectParameters.items
    assert kept["PartNumber"].Value == "PLATE-001" and kept["Source"].Value == "made"
    assert kept["TreeId"].Value == "/Props/Plate" and kept["AppModelId"].Value == "demo-plate"
    assert "Description" not in kept
    # No Core Material route here: the material is kept as parameters.
    material = section["material"]
    assert material["route"] == "parameters" and material["status"] == "fallback"
    assert made["Density"].text == "7850kg_m3" and made["Material"].Value == "Steel"
    expected_kg = PLATE_MM3 * 1e-9 * 7850
    assert material["mass"]["expected_kg"] == pytest.approx(expected_kg, rel=1e-9)
    assert made["Mass"].Value == pytest.approx(expected_kg, rel=1e-9)
    assert part.Relations.formulas["Mass_formula"][1] == "smartVolume(`Corps principal`)*`Density`"
    assert material["mass"]["measured_kg"] == pytest.approx(PLATE_MM3 * 1e-6, rel=1e-9)
    # The colour on the PartBody, read back; the hole's own colour on its Hole.
    appearance = section["appearance"]
    assert appearance["color_set"] == appearance["color_read"] == [204, 26, 26]
    assert part.MainBody.colour == (204, 26, 26)
    (hole,) = [f for f in part.pads if getattr(f, "kind", None) == "hole"]
    assert hole.colour == (51, 102, 204) and appearance["features"][0]["ok"]
    # The design table: its sheet, the script parameters driven, each row tried.
    table = section["design_table"]
    assert table["status"] == "ok" and table["name"] == "Plate_Configurations"
    rows = (tmp_path / "Plate_configurations.txt").read_text().splitlines()
    assert rows[0].split("\t") == ["Configuration", "Extrude1_height", "Hole1_radius", "Wall"]
    assert rows[1].split("\t") == ["As built", "8mm", "3mm", "2.5mm"]
    assert rows[2].split("\t") == ["Thin", "6mm", "2.5mm", "2mm"]
    assert [c["values"]["Extrude1_height"] for c in table["configurations"]] == ["6mm", "12mm"]
    assert part.Parameters.items["Extrude1_height"].text == "8mm"  # back on As built
    assert part.Relations.tables["Plate_Configurations"].Configuration == 1
    # Checks wait for a proven Knowledge Advisor licence.
    assert section["checks"]["status"] == "skipped" and not part.Relations.checks
    assert "colour (204, 26, 26)" in summary(section)


def test_without_property_fields_auto_does_nothing_and_on_sets_the_bom(monkeypatch, tmp_path):
    tree = load(FIXTURE)
    plain = tree["parts"][0]
    for key in ("color", "material", "properties", "parameters", "configurations", "checks"):
        plain.pop(key)
    plain["features"][2].pop("color")
    _, detail, _ = _build(monkeypatch, tmp_path, tree=copy.deepcopy(tree))
    assert "properties" not in detail
    part, detail, _ = _build(monkeypatch, tmp_path, tree=tree, mode="on")
    assert set(detail["properties"]) == {"attributes", "ok"}
    kept = part.Parameters.sets["Properties"].DirectParameters.items
    assert kept["PartNumber"].Value == "Plate" and kept["TreeId"].Value == "/Props/Plate"


def test_a_failing_property_step_never_fails_the_part(monkeypatch, tmp_path):
    part = _FakePart(through_depth=8.0)
    built = _builder(part, monkeypatch)
    fakes.equip(part, built.conn)
    built.properties = PartProperties(sheet_dir=str(tmp_path))
    built.measure = SimpleNamespace(inertia=lambda p, b: (_ for _ in ()).throw(
        RuntimeError("no inertia")))
    built.conn.hso = None  # every selection call fails
    report = built.build(load(FIXTURE), _expected(), check_editability=False)
    assert report["success"]
    section = report["parts_detail"][0]["properties"]
    assert not section["ok"] and section["appearance"]["status"] == "failed"
    assert "error" in section["material"]["mass"]


# ── CATIA V5 ──

def test_v5_applies_the_catalog_material_and_measures_the_mass(monkeypatch, tmp_path):
    manager = fakes.MaterialManager()
    steel = fakes.catalog({"Metal": [("Iron", 7870.0), ("Steel", 7860.0)]})
    density = lambda: manager.on_part.density if manager.on_part else 1000.0  # noqa: E731
    part, detail, documents = _build(monkeypatch, tmp_path, v5=True, density_of=density,
                                     catalog_document=steel, manager=manager)
    material = detail["properties"]["material"]
    assert documents.read == ["C:/catalogs/Catalog.CATMaterial"] and steel.closed
    assert material["route"] == "catalog" and material["status"] == "ok", material
    # The catalog's steel is 7860 kg/m3: the applied copy takes the tree's.
    assert material["catalog_density_kg_m3"] == 7860.0
    assert material["applied_density_kg_m3"] == 7850.0 and manager.on_part.density == 7850.0
    mass = material["mass"]
    assert mass["measured_kg"] == pytest.approx(mass["expected_kg"], rel=1e-9)
    assert material["mass_before_kg"] == pytest.approx(PLATE_MM3 * 1e-6, rel=1e-9)
    # Product properties for the BOM.
    product = part.Parent.Product
    attributes = detail["properties"]["attributes"]
    assert attributes["status"] == "ok", attributes
    assert (product.PartNumber, product.Revision, product.Definition) == \
           ("PLATE-001", "A", "Mounting plate")
    assert product.Nomenclature == "Plate" and product.Source == 1
    assert product.DescriptionRef == "60 x 40 x 8 plate with one hole"
    assert attributes["user"] == {"AppModelId": "demo-plate", "Finish": "painted",
                                  "TreeId": "/Props/Plate"}
    assert product.UserRefProperties.items["Finish"].Value == "painted"


def test_v5_makes_the_material_when_no_catalog_has_it(monkeypatch, tmp_path):
    manager = fakes.MaterialManager()
    density = lambda: manager.on_part.density if manager.on_part else 1000.0  # noqa: E731
    _, detail, documents = _build(monkeypatch, tmp_path, v5=True, density_of=density,
                                  catalog_document=fakes.catalog({"Metal": [("Iron", 7870.0)]}),
                                  manager=manager)
    material = detail["properties"]["material"]
    assert documents.added == ["CATMaterial"]
    assert material["route"] == "new_material" and material["status"] == "ok"
    assert manager.on_part.Name == "Steel" and manager.on_part.density == 7850.0


def test_v5_without_a_material_manager_keeps_the_material_as_parameters(monkeypatch, tmp_path):
    _, detail, _ = _build(monkeypatch, tmp_path, v5=True,
                          catalog_document=fakes.catalog({"Metal": [("Steel", 7850.0)]}),
                          refuse_magnitudes=("DENSITY",))
    material = detail["properties"]["material"]
    assert material["route"] == "parameters" and material["status"] == "fallback"
    routes = [r["route"] for r in material["routes"]]
    assert "parameter Density (DENSITY)" in routes and "parameter Density (real)" in routes


def test_v5_reports_a_part_number_catia_renamed(monkeypatch, tmp_path):
    part = _FakePart(through_depth=8.0)
    built = _builder(part, monkeypatch)
    fakes.equip(part, built.conn, v5=True)
    part.Parent.Product = fakes.Product(renames={"PLATE-001": "PLATE-001.1"})
    built.measure = SimpleNamespace(inertia=lambda p, b: {"mass_kg": 0.0})
    built.properties = PartProperties(sheet_dir=str(tmp_path))
    report = built.build(load(FIXTURE), _expected(), check_editability=False)
    attributes = report["parts_detail"][0]["properties"]["attributes"]
    assert attributes["status"] == "partial"
    assert attributes["kept_otherwise"] == {"PartNumber": "PLATE-001.1"}


# ── Knowledgeware ──

def test_checks_are_written_from_bounds_when_knowledge_advisor_is_proven(monkeypatch, tmp_path):
    part, detail, _ = _build(monkeypatch, tmp_path, advisor=True)
    checks = detail["properties"]["checks"]
    assert checks["status"] == "ok"
    assert part.Relations.checks == {"WallCheck": "`Wall` >= 1mm and `Wall` <= 10mm"}
    part, detail, _ = _build(monkeypatch, tmp_path, advisor=True, refuse_checks=True)
    assert detail["properties"]["checks"]["status"] == "failed"
    assert not detail["properties"]["ok"]


def test_a_refused_parameter_set_puts_the_parameters_in_the_root(monkeypatch, tmp_path):
    part, detail, _ = _build(monkeypatch, tmp_path, refuse_sets=True)
    parameters = detail["properties"]["parameters"]
    assert parameters["set"] is None and parameters["status"] == "partial"
    assert "root" in parameters["note"] and "Wall" in part.Parameters.items


class _Ctx:
    """A part whose volume follows its `Height` parameter, for the design
    table's checks."""

    def __init__(self, tree_part, fail_at=None, drift=0.0):
        self.part = SimpleNamespace(Parameters=fakes.Parameters())
        self.part.Relations = fakes.Relations(self.part.Parameters)
        self.tree_part, self.record = tree_part, SimpleNamespace(name="Block", parameters=[])
        self.fail_at, self.drift, self.updates = fail_at, drift, 0

    def update(self):
        self.updates += 1
        height = self.part.Parameters.items["Height"].Value
        if self.fail_at is not None and height == self.fail_at:
            raise RuntimeError("the part does not update")

    def volume_mm3(self):
        height = self.part.Parameters.items["Height"].Value
        return 100.0 * height + (self.drift if self.updates > 2 else 0.0)


def _block(**ctx):
    tree_part = {"name": "Block", "parameters": [
        {"name": "Height", "type": "length", "value": 10}],
        "configurations": [{"name": "Low", "params": {"Height": 5}},
                           {"name": "High", "params": {"Height": 20}}]}
    context = _Ctx(tree_part, **ctx)
    step = KnowledgeStep(context)
    step.parameters()
    return context, step


def test_the_design_table_tries_each_configuration_and_restores_as_built(tmp_path):
    context, step = _block()
    result = step.design_table(str(tmp_path))
    assert result["status"] == "ok"
    assert [c["volume_mm3"] for c in result["configurations"]] == [500.0, 2000.0]
    assert result["restored_volume_mm3"] == 1000.0
    assert context.part.Parameters.items["Height"].Value == 10.0


def test_a_configuration_that_does_not_build_is_noted(tmp_path):
    _, step = _block(fail_at=20.0)
    result = step.design_table(str(tmp_path))
    assert result["status"] == "partial" and "High" in result["note"]
    assert "error" in result["configurations"][1]


def test_a_table_that_does_not_restore_the_part_is_removed(tmp_path):
    context, step = _block(drift=7.0)
    result = step.design_table(str(tmp_path))
    assert result["status"] == "failed" and result["removed"]
    assert context.part.Relations.removed == ["Block_Configurations"]
    assert context.part.Parameters.items["Height"].text == "10mm"


def _not_implemented(*_args):
    raise RuntimeError("(-2147352567, 'Une exception s'est produite.', (0, 'CATIARelations', "
                       "'La méthode CreateDesignTable a échoué', None, 0, -2147467263), None)")


def test_a_table_this_catia_does_not_implement_is_unavailable_not_failed(tmp_path):
    # Live on 3DEXPERIENCE R428: CreateDesignTable answers E_NOTIMPL.
    context, step = _block()
    relations = context.part.Relations
    relations.CreateDesignTable = relations.CreateHorizontalDesignTable = _not_implemented
    result = step.design_table(str(tmp_path))
    assert result["status"] == "unavailable" and not result["removed"]
    assert [a["route"] for a in result["attempts"]] == [
        "CreateDesignTable(copy=True)", "CreateDesignTable(copy=False)",
        "CreateHorizontalDesignTable(copy=True)"]
    assert context.part.Parameters.items["Height"].text == "10mm"


def test_the_table_falls_back_to_a_linked_sheet(tmp_path):
    context, step = _block()
    relations = context.part.Relations
    create = relations.CreateDesignTable

    def linked_only(name, comment, copy, path):
        if copy:
            _not_implemented()
        relations.tables[name] = type(create(name, comment, True, path))(
            name, path, relations.parameters)
        return relations.tables[name]

    relations.CreateDesignTable = linked_only
    result = step.design_table(str(tmp_path))
    assert result["status"] == "ok"
    assert [a["ok"] for a in result["attempts"]] == [False, True]


def test_a_missing_configured_parameter_fails_before_any_table(tmp_path):
    context, step = _block()
    context.tree_part["configurations"][0]["params"]["Extrude1_height"] = 4
    result = step.design_table(str(tmp_path))
    assert result["status"] == "failed" and result["missing"] == ["Extrude1_height"]
    assert not context.part.Relations.tables


# ── STEP colours and the command line ──

def test_parts_without_a_colour_take_the_step_one():
    tree = {"parts": [{"id": "/A/B"}, {"id": "/A/C", "color": [0, 0, 1]}, {"id": "/A/D"}]}
    structure = SimpleNamespace(instances=[
        SimpleNamespace(id="/A/B", color=(0.5, 0.25, 1.0)),
        SimpleNamespace(id="/A/C", color=(1.0, 0.0, 0.0)),
        SimpleNamespace(id="/A/D", color=None)])
    assert colours_from_step(tree, structure) == ["/A/B"]
    assert tree["parts"][0]["color"] == [0.5, 0.25, 1.0]
    assert tree["parts"][1]["color"] == [0, 0, 1] and "color" not in tree["parts"][2]


def test_cli_check_refuses_a_malformed_material(tmp_path, capsys):
    from catia_mcp.tree.__main__ import main

    tree = load(FIXTURE)
    tree["parts"][0]["material"]["density_kg_m3"] = -1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(tree))
    assert main(["check", str(path)]) == 1
    assert "positive density_kg_m3" in capsys.readouterr().out
    assert main(["check", str(TREES / f"{FIXTURE}.json")]) == 0


@pytest.mark.parametrize("checks, status, advisor", [
    ("auto", "authorized", True), ("auto", "unknown", False), ("on", "unknown", True),
    ("off", "verified", False)])
def test_cli_checks_follow_the_knowledge_advisor_capability(checks, status, advisor):
    from catia_mcp.tree.__main__ import _properties

    args = SimpleNamespace(properties="auto", step=None, checks=checks, material_catalog=None,
                           sheet_dir=None)
    report = {"capabilities": {"knowledge.rules": {"status": status}}}
    assert _properties({"parts": []}, args, report).knowledge_advisor is advisor
    args.properties = "off"
    assert _properties({"parts": []}, args, report) is None


# ── The live probe, offline ──

def test_the_probe_records_every_route_without_stopping(tmp_path, monkeypatch):
    import importlib.util

    from tests.fakes import Coll, SystemService

    spec = importlib.util.spec_from_file_location(
        "probe_properties", TREES.parents[2] / "scripts" / "probe_properties.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    body = SimpleNamespace(Name="PartBody", Shapes=Coll([SimpleNamespace(Name="Pad.1")]))
    part = SimpleNamespace(Name="Part1", MainBody=body, Bodies=Coll([body]),
                           Parameters=fakes.Parameters(), Update=lambda: None,
                           FindObjectByName=lambda name: body.Shapes.Item(name),
                           Parent=fakes.RefusingPLMObject(V_Name="Part1"))
    part.Relations = fakes.Relations(part.Parameters)
    conn = SimpleNamespace(is_3dx=True, platform_label="3DEXPERIENCE", hso=fakes.Selection(),
                           app=SimpleNamespace(SystemService=SystemService()),
                           get_active_part=lambda: part)
    probe = module.Probe(conn, SimpleNamespace(material="Steel", catalog=None, kwa=True))
    probe.part, probe.body = part, body
    probe.measure = SimpleNamespace(inertia=lambda p, b: {"mass_kg": 0.0192},
                                    volume_mm3=lambda p, b: 19200.0)
    report = {name: getattr(probe, name)() for name in (
        "colour", "inertia", "material", "properties", "parameters", "design_table",
        "relations", "publications")}
    json.dumps(report, default=str)
    assert report["colour"]["main_body"]["value"]["read"] == [204, 26, 26]
    assert report["inertia"]["density_kg_m3"] == pytest.approx(1000.0)
    assert report["parameters"]["CreateSet"]["ok"]
    assert report["parameters"]["density"]["value"]["value"]["value"] == "7850kg_m3"
    assert [r["value"] for r in report["design_table"]["txt_tab_units"]["rows"]] == \
           ["20mm", "5mm", "12.5mm"]
    assert report["relations"]["check"]["ok"]
    assert not report["material"]["core_material_search"]["CATMatReference"]["ok"]
    assert report["properties"]["V_Name"]["after"] == "probe V_Name"
    assert report["properties"]["V_Name"]["restored"]["ok"]
