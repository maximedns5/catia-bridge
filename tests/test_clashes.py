"""Clash analysis (catia_mcp/tree/clashes.py): offline, from the STEP files'
shapes or the tree's build123d reference, and CATIA V5's DMU clash on a fake
product (tests/clash_fakes.py)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp.tree import clashes
from tests import clash_fakes as fakes

FIXTURES = Path(__file__).parent / "fixtures" / "assembly"


def _plan(name: str, step: bool = True):
    from catia_mcp.tree.assembly import plan_assembly
    from catia_mcp.tree.step_structure import read

    tree = json.loads((FIXTURES / f"{name}.tree.json").read_text())
    plan, problems = plan_assembly(tree, read(str(FIXTURES / f"{name}.step")) if step else None)
    assert problems == []
    return plan


def _run(name: str, clearance: float = 0.0, step: bool = True):
    pytest.importorskip("OCP")
    if not step:
        pytest.importorskip("build123d")
    plan = _plan(name, step)
    return clashes.offline(plan, str(FIXTURES / f"{name}.step") if step else None, clearance)


def _kinds(report):
    return [(c["first"], c["second"], c["kind"]) for c in report["clashes"]]


# ── Offline, the existing assembly fixtures: contacts, no interference ──


def test_the_bracket_bolt_stands_on_the_plate_the_turned_one_is_7_mm_away():
    report = _run("bracket")
    assert report["ok"] and not report["interference"]
    assert _kinds(report) == [("/Bracket/Plate", "/Bracket/Bolt", "contact")]
    assert report["clashes"][0]["location"] == [15.0, 0.0, 4.0]  # the bolt's foot
    # The boxes keep the far pairs from being measured.
    assert (report["pairs"], report["pairs_measured"]) == (3, 1)
    wider = _run("bracket", clearance=10)
    assert _kinds(wider)[1] == ("/Bracket/Plate", "/Bracket/Bolt#2", "clearance")
    assert wider["clashes"][1]["value"] == pytest.approx(7.0)
    assert wider["counts"] == {"interference": 0, "contact": 1, "clearance": 1}


@pytest.mark.parametrize("name, expected", [
    ("pin_plate", [("/PinPlate/Plate", "/PinPlate/Pin")]),
    ("box_base", [("/BoxBase/Base", "/BoxBase/Box")]),
    ("bolted_plates", [("/BoltedPlates/Plate", "/BoltedPlates/Plate#2"),
                       ("/BoltedPlates/Plate", "/BoltedPlates/Bolt"),
                       ("/BoltedPlates/Plate", "/BoltedPlates/Bolt#2"),
                       ("/BoltedPlates/Plate#2", "/BoltedPlates/Bolt"),
                       ("/BoltedPlates/Plate#2", "/BoltedPlates/Bolt#2")]),
])
def test_mated_fixtures_touch_and_do_not_interfere(name, expected):
    report = _run(name)
    assert report["counts"]["interference"] == 0 and "errors" not in report
    assert _kinds(report) == [(a, b, "contact") for a, b in expected]
    assert all(c["value"] == 0.0 and c["unit"] == "mm" for c in report["clashes"])


def test_a_contact_is_located_where_the_faces_touch():
    # The box, a quarter turn about Z, stands at (15, 10) on the base.
    assert _run("box_base")["clashes"][0]["location"] == [15.0, 10.0, 0.0]


# ── Offline, the clash fixtures ──


def test_a_pin_larger_than_its_hole_interferes():
    report = _run("pin_interference")
    assert report["interference"] and report["ok"]
    (clash,) = report["clashes"]
    assert (clash["first"], clash["second"], clash["kind"], clash["unit"]) == (
        "/PinInterference/Plate", "/PinInterference/Pin", "interference", "mm3")
    assert clash["value"] == pytest.approx(math.pi * (4.5 ** 2 - 4 ** 2) * 10, rel=1e-6)
    assert clash["location"] == pytest.approx([10.0, 5.0, 5.0])  # the hole's middle
    assert (clash["first_name"], clash["second_name"]) == ("Plate", "Pin")


def test_a_clearance_is_reported_only_when_asked_for_and_under_it():
    assert _kinds(_run("shaft_bushing")) == [
        ("/ShaftBushing/Bushing", "/ShaftBushing/Collar", "contact"),
        ("/ShaftBushing/Shaft", "/ShaftBushing/Collar", "contact")]
    report = _run("shaft_bushing", clearance=0.5)
    clearance = report["clashes"][0]
    assert (clearance["first"], clearance["second"], clearance["kind"]) == (
        "/ShaftBushing/Bushing", "/ShaftBushing/Shaft", "clearance")
    assert clearance["value"] == pytest.approx(0.2)
    first, second = clearance["points"]
    assert math.hypot(first[0], first[1]) == pytest.approx(5.0)  # on the bore
    assert math.hypot(second[0], second[1]) == pytest.approx(4.8)  # on the shaft
    assert report["counts"] == {"interference": 0, "contact": 2, "clearance": 1}
    assert _run("shaft_bushing", clearance=0.1)["counts"]["clearance"] == 0


@pytest.mark.parametrize("name", ["pin_interference", "shaft_bushing", "bolted_plates"])
def test_the_tree_reference_gives_what_the_step_file_gives(name):
    with_step, without = _run(name, 0.5), _run(name, 0.5, step=False)
    assert without["source"] == "tree" and with_step["source"] == "step"
    assert _kinds(without) == _kinds(with_step)
    for a, b in zip(without["clashes"], with_step["clashes"], strict=True):
        assert a["value"] == pytest.approx(b["value"], abs=1e-4)


def test_the_analysis_is_deterministic():
    one, two = _run("bolted_plates", 1.0), _run("bolted_plates", 1.0)
    one.pop("elapsed_s"), two.pop("elapsed_s")
    assert one == two


def test_a_hundred_instances_are_checked_quickly():
    pytest.importorskip("OCP")
    from build123d import Align, Box

    cube = Box(10, 10, 10, align=(Align.MIN, Align.MIN, Align.MIN)).wrapped
    items = [clashes.Item(f"/Grid/B{i}_{j}", "B",
                          clashes.moved(cube, [[1, 0, 0, 10 * i], [0, 1, 0, 10 * j], [0, 0, 1, 0]]))
             for i in range(10) for j in range(10)]
    # One more, sunk 2 mm into the first.
    items.append(clashes.Item("/Grid/Sunk", "B",
                              clashes.moved(cube, [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 8]])))
    report = clashes.analyze(items)
    # Neighbours by a face or an edge: 2 * 90 + 2 * 81; the sunk cube also
    # touches the first cube's three neighbours.
    assert report["counts"] == {"interference": 1, "contact": 342 + 3, "clearance": 0}
    assert report["pairs"] == 101 * 100 // 2 and report["pairs_measured"] == 346
    (interference,) = [c for c in report["clashes"] if c["kind"] == "interference"]
    assert interference["value"] == pytest.approx(200.0)
    assert interference["location"] == pytest.approx([5.0, 5.0, 9.0])
    assert report["elapsed_s"] < 30


def test_a_negative_clearance_is_refused():
    with pytest.raises(ValueError, match="negative"):
        clashes.analyze([], -1.0)


def test_summary_reads_the_counts():
    report = {"counts": {"interference": 1, "contact": 2, "clearance": 3}, "clearance_mm": 0.5,
              "pairs": 6, "pairs_measured": 4}
    assert clashes.summary(report) == ("1 interference(s), 2 contact(s), 3 clearance(s) under "
                                       "0.5 mm (4 of 6 pairs measured)")


# ── CATIA V5: DMU Space Analysis on a fake product ──

NAMES = {"Plate.1": "/PinInterference/Plate", "Pin.1": "/PinInterference/Pin"}


def test_the_v5_clash_is_computed_between_all_and_read_back():
    product = fakes.Product([(0, -0.5, "Plate.1", "Pin.1")])
    result = clashes.compute_v5(fakes.app(), product, 0.0, NAMES)
    clash = product.clashes.items[0]
    assert product.asked == ["Clashes"]
    assert (clash.ComputationType, clash.InterferenceType, clash.Clearance) == (0, 0, None)
    assert clash.computed and result["status"] == "ok"
    assert result["conflicts"] == [{"first": "/PinInterference/Plate",
                                    "second": "/PinInterference/Pin", "first_name": "Plate.1",
                                    "second_name": "Pin.1", "kind": "interference",
                                    "value": -0.5}]
    assert result["computation_enum"] == "documented"  # a fake has no type library


def test_a_clearance_asks_catia_for_a_clearance_clash():
    product = fakes.Product([(2, 0.2, "Bushing.1", "Shaft.1")])
    result = clashes.compute_v5(fakes.app(), product, 0.5)
    clash = product.clashes.items[0]
    assert (clash.InterferenceType, clash.Clearance) == (1, 0.5)
    assert result["conflicts"][0]["kind"] == "clearance"
    assert result["conflicts"][0]["first"] == "Bushing.1"  # unknown names kept


def test_the_comparison_is_by_pair_and_kind():
    offline = [{"first": "a", "second": "b", "kind": "contact"},
               {"first": "a", "second": "c", "kind": "interference"}]
    catia = [{"first": "b", "second": "a", "kind": "contact"},
             {"first": "c", "second": "d", "kind": "contact"}]
    assert clashes.compare(offline, catia) == {
        "same": False, "agree": 1,
        "differences": [{"pair": ["a", "c"], "offline": "interference", "catia": None},
                        {"pair": ["c", "d"], "offline": None, "catia": "contact"}]}
    assert clashes.compare(offline[:1], catia[:1])["same"]


class _Worker:
    def __init__(self, error=None):
        self.error = error

    def run_guarded(self, fn, *args, timeout=None, label=None):
        if self.error is not None:
            raise self.error
        return fn(*args)


def _conn(is_3dx=False, licences=("SPA", "PDG")):
    return SimpleNamespace(is_3dx=is_3dx, app=fakes.app(licences))


def test_the_catia_route_compares_with_the_offline_result():
    offline = {"clashes": [{"first": "/PinInterference/Plate", "second": "/PinInterference/Pin",
                            "kind": "interference"}]}
    assembly = {"instances": [{"id": v, "catia_name": k} for k, v in NAMES.items()]}
    result = clashes.catia_route(_conn(), _Worker(), fakes.Product([(0, -0.5, "Plate.1", "Pin.1")]),
                                 assembly, offline)
    assert result["status"] == "ok"
    assert result["comparison"] == {"same": True, "agree": 1, "differences": []}


def test_without_the_licence_nothing_is_asked_of_catia():
    product = fakes.Product()
    result = clashes.catia_route(_conn(licences=("PDG",)), _Worker(), product, {}, None)
    assert result["status"] == "unavailable" and "SPA" in result["reason"]
    assert product.asked == []
    assert result.get("comparison") is None


def test_an_unproven_licence_answer_still_tries_and_a_missing_api_is_unavailable():
    # Every product says no: the name form is unproven, the clash is tried.
    product = fakes.Product(missing=True)
    result = clashes.catia_route(_conn(licences=()), _Worker(), product, {}, None)
    assert product.asked == ["Clashes"]
    assert result["status"] == "unavailable" and "GetTechnologicalObject" in result["reason"]


def test_a_licence_dialog_is_reported_by_the_watchdog():
    from catia_mcp.errors import BlockedByDialog

    dialog = BlockedByDialog("CATIA shows a dialog that blocks the DMU clash",
                             data={"watchdog": {"dialogs": [{"title": "Licence"}]}})
    result = clashes.catia_route(_conn(), _Worker(dialog), fakes.Product(), {}, None)
    assert (result["status"], result["code"]) == ("unavailable", "BLOCKED_BY_DIALOG")
    assert result["dialog"]["dialogs"][0]["title"] == "Licence"


def test_on_3dexperience_the_offline_analysis_is_the_result():
    result = clashes.catia_route(_conn(is_3dx=True), _Worker(), None, {}, None)
    assert result["status"] == "unavailable" and "probe_clashes" in result["reason"]


def test_no_v5_product_no_catia_clash():
    result = clashes.catia_route(_conn(), _Worker(), None, {}, None)
    assert result["status"] == "unavailable"


# ── Command line ──


def test_the_clashes_command_writes_the_report(tmp_path, capsys):
    pytest.importorskip("OCP")
    from catia_mcp.tree.__main__ import main

    out = tmp_path / "clashes.json"
    code = main(["clashes", str(FIXTURES / "shaft_bushing.tree.json"), "--step",
                 str(FIXTURES / "shaft_bushing.step"), "--clearance", "0.5", "--expect", "none",
                 "--report", str(out)])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["counts"] == {"interference": 0, "contact": 2, "clearance": 1}
    assert "clearance 0.2 mm: /ShaftBushing/Bushing / /ShaftBushing/Shaft" in capsys.readouterr().out


def _stub_catia(monkeypatch, conflicts, assemble_error=None):
    """A build with every CATIA piece stubbed: the connection, the probe, the
    builder (a success) and the V5 assembler (a fake product)."""
    import catia_mcp.connection as connection_module
    import catia_mcp.tree.assembler as assembler_module
    import catia_mcp.tree.builder as builder_module
    from catia_mcp import capabilities
    from tests.watchdog_fakes import FakeScreen, fake_watchdog

    class Connection:
        is_3dx = False

        def __init__(self, platform=None, allow_launch=True):
            self.watchdog = fake_watchdog(FakeScreen())
            self.app = fakes.app()

        def connect(self):
            return "Connected (stub)."

    class Builder:
        def __init__(self, conn, **options):
            self.targets, self.part_documents, self.properties = {}, {}, None

        def build(self, tree, expected, check_editability):
            self.part_documents = {p["id"]: p["id"] for p in tree["parts"]}
            return {"success": True, "parts_detail": [], "elapsed_s": 0.1}

    class Assembler:
        def __init__(self, app, read_position=None, log=None):
            self.root = fakes.Product(conflicts)

        def assemble(self, plan, documents):
            if assemble_error:
                raise RuntimeError(assemble_error)
            counts: dict[str, int] = {}
            rows = []
            for placed in plan.instances:
                counts[placed.name] = counts.get(placed.name, 0) + 1
                rows.append({"id": placed.id, "catia_name": f"{placed.name}.{counts[placed.name]}",
                             "part": placed.prototype, "position_ok": True})
            return {"product": plan.root.name, "catia_product": plan.root.name,
                    "instances": rows, "ok": True}

    monkeypatch.setattr(connection_module, "CATIAConnection", Connection)
    monkeypatch.setattr(capabilities, "probe", lambda conn, save=False: {})
    monkeypatch.setattr(builder_module, "TreeBuilder", Builder)
    monkeypatch.setattr(assembler_module, "AssemblerV5", Assembler)


def _build(tmp_path, name, *options):
    from catia_mcp.tree.__main__ import main

    out = tmp_path / "report.json"
    code = main(["build", str(FIXTURES / f"{name}.tree.json"), "--assembly", "product",
                 "--step", str(FIXTURES / f"{name}.step"), "--expect", "none",
                 "--properties", "off", "--report", str(out), *options])
    return code, json.loads(out.read_text())


def test_a_built_assembly_reports_its_clashes_offline_by_default(monkeypatch, tmp_path):
    pytest.importorskip("OCP")
    _stub_catia(monkeypatch, [])
    code, report = _build(tmp_path, "pin_interference")
    assert code == 0 and report["success"]  # a clash never fails the build
    result = report["assembly"]["clashes"]
    assert result["method"] == "offline" and result["counts"]["interference"] == 1
    assert "catia" not in result


def test_the_catia_route_runs_after_the_assembly_and_is_compared(monkeypatch, tmp_path):
    pytest.importorskip("OCP")
    _stub_catia(monkeypatch, [(0, -0.5, "Plate.1", "Pin.1")])
    code, report = _build(tmp_path, "pin_interference", "--clashes", "catia")
    assert code == 0
    catia = report["assembly"]["clashes"]["catia"]
    assert catia["status"] == "ok"
    assert catia["conflicts"][0]["first"] == "/PinInterference/Plate"
    assert catia["comparison"]["same"] is True


def test_a_catia_clash_that_disagrees_is_reported_not_fatal(monkeypatch, tmp_path):
    pytest.importorskip("OCP")
    _stub_catia(monkeypatch, [])  # CATIA finds nothing
    code, report = _build(tmp_path, "pin_interference", "--clashes", "catia")
    assert code == 0
    comparison = report["assembly"]["clashes"]["catia"]["comparison"]
    assert comparison["differences"] == [{"pair": ["/PinInterference/Pin",
                                                   "/PinInterference/Plate"],
                                          "offline": "interference", "catia": None}]


def test_clashes_none_leaves_the_assembly_report_as_it_was(monkeypatch, tmp_path):
    pytest.importorskip("OCP")
    _stub_catia(monkeypatch, [])
    code, report = _build(tmp_path, "pin_interference", "--clashes", "none")
    assert code == 0 and "clashes" not in report["assembly"]


def test_a_failed_assembly_still_reports_the_offline_clashes(monkeypatch, tmp_path):
    pytest.importorskip("OCP")
    _stub_catia(monkeypatch, [], assemble_error="AddExternalComponent failed")
    code, report = _build(tmp_path, "pin_interference", "--clashes", "catia")
    assert code == 1
    result = report["assembly"]["clashes"]
    assert result["counts"]["interference"] == 1
    assert result["catia"]["status"] == "unavailable"


# ── The 3DEXPERIENCE probe (scripts/probe_clashes.py), offline parts ──


def test_the_probe_offers_only_collections_it_found_with_add():
    from scripts import probe_clashes

    rows = [{"call": 'GetItem("Clashes")', "found": True, "signatures": {"Add": [], "Count": 0}},
            {"call": 'GetItem("CATClashes")', "error": "not found"},
            {"call": 'GetItem("Clash")', "found": True, "signatures": "error: no type info"},
            {"call": 'GetItem("Interferences")', "found": True, "signatures": {"Item": []}}]
    assert probe_clashes._with_add(rows) == ["Clashes"]
    assert probe_clashes._with_add("no GetItem") == []


def test_the_probe_records_the_offline_reference():
    pytest.importorskip("OCP")
    from scripts import probe_clashes

    report = probe_clashes.offline(str(FIXTURES / "bracket.tree.json"),
                                   str(FIXTURES / "bracket.step"), 0.0)
    assert _kinds(report) == [("/Bracket/Plate", "/Bracket/Bolt", "contact")]
