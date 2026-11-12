"""Offline tests of the STEP fallback: import (V5), adopt (3DEXPERIENCE),
paste as result, the MCP tools, the preflight and builder hook, and the
build123d helper that writes the STEP files.

CATIA is a fake here. What the fakes assume of CATIA (Documents.Open of a
.stp, the failed-import signature, PasteSpecial "CATPrtResultWithOutLink"
adding one body) is checked live with scripts/step_probe.py.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from types import SimpleNamespace

import pytest

from catia_mcp.connection import PLATFORM_3DX, PLATFORM_V5, CATIAConnection
from catia_mcp.errors import InvalidArgument, UnsupportedCapability, WrongDocumentType
from catia_mcp.server import CATIAMCPServer
from catia_mcp.step import PASTE_AS_RESULT, StepExchange, StepImportFailed
from catia_mcp.tree.builder import TreeBuilder
from catia_mcp.tree.preflight import TreeRefused, analyze
from catia_mcp.tree.step_source import StepSource, part_folder
from tests.fakes import Coll
from tests.test_tree import _Body, _builder, _FakePart, _mass, load

# ── A fake CATIA V5 / 3DEXPERIENCE for STEP documents ──


class FakeBody:
    def __init__(self, name, volume=0.0, shapes=("Solid.1",)):
        self.Name, self.volume = name, volume
        self.Shapes = Coll([SimpleNamespace(Name=s) for s in shapes])


class FakePart:
    def __init__(self, name, body=None, sets=()):
        self.Name = name
        self.MainBody = body or FakeBody("PartBody", 0.0, shapes=())
        self.Bodies = Coll([self.MainBody])
        self.HybridBodies = Coll([SimpleNamespace(Name=s) for s in sets])
        self.InWorkObject = None
        self.combines = []
        self.ShapeFactory = SimpleNamespace(
            AddNewAdd=lambda tool: self._combine("Add", tool, 1),
            AddNewRemove=lambda tool: self._combine("Remove", tool, -1))

    def _combine(self, kind, tool, sign):
        feature = SimpleNamespace(Name=f"{kind}.{len(self.combines) + 1}", target=self.InWorkObject,
                                  tool=tool, sign=sign)
        self.combines.append(feature)
        return feature

    def UpdateObject(self, feature):
        feature.target.volume += feature.sign * feature.tool.volume

    def Update(self):
        pass


class FakeSelection:
    clipboard = None

    def __init__(self):
        self.items = []
        self.pasted = []

    def Clear(self):
        self.items = []

    def Add(self, obj):
        self.items.append(obj)

    def Copy(self):
        FakeSelection.clipboard = list(self.items)

    def PasteSpecial(self, fmt):
        (part,) = self.items
        (body,) = FakeSelection.clipboard
        self.pasted.append(fmt)
        assert fmt == PASTE_AS_RESULT
        part.Bodies.append(FakeBody(f"Body.{part.Bodies.Count + 1}", body.volume))


class FakeDoc:
    def __init__(self, app, name, part=None, product=None):
        self.app, self.Name = app, name
        if part is not None:
            self.Part = part
        if product is not None:
            self.Product = product
        self.Selection = FakeSelection()
        self.closed = False

    def Activate(self):
        self.app.ActiveDocument = self

    def Close(self):
        self.closed = True
        self.app.close_alerts.append(self.app.DisplayFileAlerts)
        self.app.Documents.items.remove(self)


class FakeDocuments(Coll):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def Open(self, path):
        self.app.opened.append(path)
        self.app.open_alerts.append(self.app.DisplayFileAlerts)
        doc = self.app.on_open(self.app, path)
        self.append(doc)
        doc.Activate()
        return doc


class FakeV5:
    Caption = "CATIA V5"

    def __init__(self, on_open=None):
        self.DisplayFileAlerts = True
        self.Documents = FakeDocuments(self)
        self.ActiveDocument = None
        self.opened, self.open_alerts, self.close_alerts = [], [], []
        self.on_open = on_open or good_import(1000.0)


def good_import(volume):
    def on_open(app, path):
        base = os.path.splitext(os.path.basename(path))[0]
        body = FakeBody("PartBody", volume)
        return FakeDoc(app, f"{base}.CATPart", FakePart(base, body, sets=("Geometrical Set.1",)))
    return on_open


class FakeMeasure:
    def volume_mm3(self, part, body):
        if not body.Shapes.Count and body.volume == 0:
            raise RuntimeError("nothing to measure")
        return body.volume

    def area_mm2(self, part, body):
        return 600.0

    def cog_mm(self, part, body):
        return [1.0, 2.0, 3.0]


def v5_connection(app):
    conn = CATIAConnection(platform="v5")
    conn.app, conn.platform = app, PLATFORM_V5
    return conn


def target_doc(app, name="Target.CATPart", volume=500.0):
    doc = FakeDoc(app, name, FakePart("Target", FakeBody("PartBody", volume, ("Pad.1",))))
    app.Documents.append(doc)
    doc.Activate()
    return doc


@pytest.fixture
def step_file(tmp_path):
    path = tmp_path / "bracket.step"
    path.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    return str(path)


# ── Import (V5) ──


def test_v5_import_opens_a_copy_with_alerts_off_and_measures(step_file):
    app = FakeV5()
    imported = StepExchange(v5_connection(app), FakeMeasure()).import_file(step_file, 1000.0)
    (opened,) = app.opened
    assert opened != step_file and os.path.basename(opened) == "bracket.step"
    assert app.open_alerts == [False] and app.DisplayFileAlerts is True
    assert imported.label == "bracket.CATPart" and imported.opened_by_bridge
    summary = imported.summary()
    assert summary["volume_mm3"] == 1000.0 and summary["cog_mm"] == [1.0, 2.0, 3.0]
    assert summary["features"] == ["Solid.1"]


@pytest.mark.parametrize("make, reason", [
    (lambda app, path: FakeDoc(app, "x.CATPart", FakePart(
        "x", FakeBody("PartBody", 0.0, shapes=()), sets=("Geometrical Set.1", "FaceKO#12")))
     , "empty"),
    (lambda app, path: FakeDoc(app, "x.CATPart", FakePart(
        "x", FakeBody("PartBody", 50.0), sets=("FaceKO#3",))), "failed to transfer"),
    (lambda app, path: FakeDoc(app, "x.CATPart", FakePart("x", FakeBody("PartBody", 0.0))),
     "no volume"),
])
def test_v5_failed_import_signature_is_refused_and_closed(step_file, make, reason):
    app = FakeV5(on_open=make)
    with pytest.raises(StepImportFailed, match=reason) as failure:
        StepExchange(v5_connection(app), FakeMeasure()).import_file(step_file)
    assert failure.value.code == "STEP_IMPORT_FAILED" and failure.value.modified is False
    assert app.Documents.Count == 0 and app.close_alerts == [False]
    assert app.DisplayFileAlerts is True


def test_v5_import_checks_the_expected_volume(step_file):
    app = FakeV5(on_open=good_import(990.0))
    with pytest.raises(StepImportFailed, match="expected 1000.0000"):
        StepExchange(v5_connection(app), FakeMeasure()).import_file(step_file, 1000.0)
    assert app.Documents.Count == 0


def test_v5_assembly_step_is_refused_and_closed(step_file):
    app = FakeV5(on_open=lambda app, path: FakeDoc(app, "assy.CATProduct", product=object()))
    with pytest.raises(WrongDocumentType, match="assembly"):
        StepExchange(v5_connection(app), FakeMeasure()).import_file(step_file)
    assert app.Documents.Count == 0


def test_import_refuses_other_files(tmp_path):
    app = FakeV5()
    other = tmp_path / "bracket.igs"
    other.write_text("")
    exchange = StepExchange(v5_connection(app), FakeMeasure())
    with pytest.raises(InvalidArgument, match="not a STEP file"):
        exchange.import_file(str(other))
    with pytest.raises(InvalidArgument, match="No STEP file"):
        exchange.import_file(str(tmp_path / "missing.stp"))
    assert app.opened == []


def test_3dexperience_import_is_unsupported_with_manual_steps(step_file):
    conn = CATIAConnection(platform="3dx")
    conn.app, conn.platform = SimpleNamespace(Caption="3DEXPERIENCE"), PLATFORM_3DX
    with pytest.raises(UnsupportedCapability) as refused:
        StepExchange(conn, FakeMeasure()).import_file(step_file)
    text = str(refused.value)
    assert step_file in text and "Import" in text and "catia_paste_dead_body" in text
    assert refused.value.modified is False


# ── Paste as result ──


def test_paste_as_result_adds_one_named_body_and_combines_it(step_file):
    app = FakeV5()
    target = target_doc(app)
    exchange = StepExchange(v5_connection(app), FakeMeasure())
    imported = exchange.import_file(step_file)
    body, volume, notes = exchange.paste_as_result(
        imported, target.Part, target, target.Activate, "STEP_bracket")
    assert (body.Name, volume, notes) == ("STEP_bracket", 1000.0, [])
    assert target.Selection.pasted == [PASTE_AS_RESULT] and app.ActiveDocument is target
    combined = exchange.combine(target.Part, target.Part.MainBody, body, "join")
    assert combined["volume_after_mm3"] == 1500.0 and combined["feature"] == "Add.1"
    assert exchange.close(imported) and not exchange.close(imported)
    assert app.Documents.Count == 1


def test_paste_refuses_a_wrong_volume_and_a_wrong_body_count(step_file):
    app = FakeV5()
    target = target_doc(app)
    exchange = StepExchange(v5_connection(app), FakeMeasure())
    imported = exchange.import_file(step_file)
    imported.volume_mm3 = 2000.0
    with pytest.raises(Exception, match="measures 1000.0000 mm3") as wrong:
        exchange.paste_as_result(imported, target.Part, target, target.Activate)
    assert wrong.value.code == "WRONG_GEOMETRY_EFFECT" and wrong.value.modified is None
    target.Selection.PasteSpecial = lambda fmt: None
    with pytest.raises(Exception, match="added 0 bodies") as none:
        exchange.paste_as_result(imported, target.Part, target, target.Activate)
    assert none.value.code == "PASTE_FAILED"


def test_a_combine_outside_its_bounds_fails():
    part = FakePart("P", FakeBody("PartBody", 500.0, ("Pad.1",)))
    tool = FakeBody("STEP_x", 100.0)
    part.UpdateObject = lambda feature: setattr(feature.target, "volume", 1000.0)  # bogus
    exchange = StepExchange(v5_connection(FakeV5()), FakeMeasure())
    with pytest.raises(Exception, match="outside what a cut"):
        exchange.combine(part, part.MainBody, tool, "cut")


def test_3dexperience_adopts_a_hand_imported_window_and_never_closes_it():
    from tests.fakes import App3DX

    app = App3DX()
    source_part = FakePart("bracket", FakeBody("PartBody", 1000.0))
    target_part = FakePart("Target", FakeBody("PartBody", 500.0, ("Pad.1",)))
    for editor in (app.open_editor(source_part, "bracket 2026-11-12 (Import)"),
                   app.open_editor(target_part, "Target")):
        editor.Selection = FakeSelection()
    conn = CATIAConnection(platform="3dx")
    conn.app, conn.platform = app, PLATFORM_3DX
    exchange = StepExchange(conn, FakeMeasure())
    source = exchange.adopt("bracket")
    assert source.label == "bracket 2026-11-12 (Import)" and not source.opened_by_bridge
    window = exchange.find_window("Target")
    window.Activate()
    body, volume, _ = exchange.paste_as_result(source, target_part, conn.active_editor,
                                               window.Activate, "STEP_bracket")
    assert body.Name == "STEP_bracket" and volume == 1000.0
    assert not exchange.close(source) and not any(w.closed for w in app.Windows.items)


def test_dead_bodies_are_named_or_only_isolated_solids():
    part = FakePart("P", FakeBody("PartBody", 1.0, ("Pad.1", "Solide.1")))
    part.Bodies.append(FakeBody("STEP_fillet1_remove", 1.0, ("Solid.1",)))
    part.Bodies.append(FakeBody("Body.3", 1.0, ("Solid.2",)))
    part.Bodies.append(FakeBody("Body.4", 1.0, ("Pocket.1",)))
    rows = {r["name"]: r for r in StepExchange.dead_bodies(part)}
    assert [n for n, r in rows.items() if r["dead"]] == ["STEP_fillet1_remove", "Body.3"]
    assert rows["PartBody"]["reason"].startswith("partly dead")
    assert "reason" not in rows["Body.4"]


# ── MCP tools ──


def _call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    return result, result.content[0].text


def test_tools_import_paste_and_list_dead_bodies(step_file):
    app = FakeV5()
    target_doc(app)
    server = CATIAMCPServer("v5")
    server.connection.app, server.connection.platform = app, PLATFORM_V5
    server.step_tools.exchange.measure = FakeMeasure()
    result, text = _call(server, "catia_import_step", {"file_path": step_file})
    assert not result.isError, text
    data = json.loads(text)
    assert data["document"] == "bracket.CATPart"
    assert data["previous_active_document"] == "Target.CATPart"
    result, text = _call(server, "catia_paste_dead_body",
                         {"source": "bracket.CATPart", "close_source": True})
    assert not result.isError, text
    data = json.loads(text)
    assert data["dead_body"] == "STEP_bracket" and data["source_closed"]
    assert data["combine"]["volume_after_mm3"] == 1500.0
    assert [d.Name for d in app.Documents.items] == ["Target.CATPart"]
    result, text = _call(server, "catia_list_dead_bodies", {})
    assert json.loads(text)["dead"] == ["STEP_bracket"]


def test_tool_never_closes_a_source_it_did_not_open():
    app = FakeV5()
    source = FakeDoc(app, "Mine.CATPart", FakePart("Mine", FakeBody("PartBody", 200.0)))
    app.Documents.append(source)
    target = target_doc(app)
    server = CATIAMCPServer("v5")
    server.connection.app, server.connection.platform = app, PLATFORM_V5
    server.step_tools.exchange.measure = FakeMeasure()
    result, text = _call(server, "catia_paste_dead_body", {
        "source": "Mine.CATPart", "target": "Target.CATPart", "operation": "none",
        "close_source": True})
    assert not result.isError, text
    data = json.loads(text)
    assert "combine" not in data and not data.get("source_closed")
    assert "did not open it" in data["notes"][0] and not source.closed
    assert target.Part.Bodies.Count == 2


def test_tool_import_on_3dexperience_reports_unsupported_capability(step_file):
    server = CATIAMCPServer("3dx")
    server.connection.app = SimpleNamespace(Caption="3DEXPERIENCE R2026x")
    server.connection.platform = PLATFORM_3DX
    result, _ = _call(server, "catia_import_step", {"file_path": step_file})
    assert result.isError and result.structuredContent["code"] == "UNSUPPORTED_CAPABILITY"
    assert result.structuredContent["effects"]["modified"] is False


# ── Preflight and builder hook ──

FILLET = {"type": "fillet", "id": "fillet1", "bodies": ["Body1"], "radius": 5, "edges": []}


def _with(tree, *features):
    tree["parts"][0]["features"] += [dict(f) for f in features]
    return tree


def _touch(step_dir, part_id, name):
    folder = os.path.join(step_dir, part_folder(part_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w") as handle:
        handle.write("ISO-10303-21;")
    return path


def test_part_folders_are_file_system_safe():
    assert part_folder("/Demo/Plate") == "Demo_Plate"
    assert part_folder("/M1/Offset Block:2") == "M1_Offset_Block_2"


def test_preflight_accepts_features_with_step_files_only_where_they_can_be_imported(tmp_path):
    tree = _with(load("m1_offset_block"), FILLET,
                 {"type": "base_feature", "id": "base1", "operation": "new_body",
                  "creates": ["Body2"], "reason": "raw OCP"},
                 {"type": "combine", "id": "combine1", "operation": "join",
                  "targets": ["Body1"], "tools": ["Body2"]})
    step_dir = str(tmp_path)
    reasons = analyze(tree, StepSource(step_dir, "v5"))
    assert len(reasons) == 2 and all("no STEP file" in r for r in reasons)
    _touch(step_dir, "/M1/OffsetBlock", "fillet1.remove.step")
    _touch(step_dir, "/M1/OffsetBlock", "base1.step")
    assert not analyze(tree, StepSource(step_dir, "v5"))
    assert not analyze(tree, StepSource(step_dir, None))  # check, before connecting
    refused = analyze(tree, StepSource(step_dir, "3dx"))
    assert all("3DEXPERIENCE" in r for r in refused) and len(refused) == 2
    assert "'fillet' features are not supported yet" in next(iter(analyze(tree)))


def test_preflight_refuses_deltas_for_features_that_create_or_miss_bodies(tmp_path):
    tree = _with(load("m1_offset_block"),
                 {"type": "copy", "id": "copy1", "bodies": ["Body1"], "creates": ["Body3"]},
                 {"type": "shell", "id": "shell1", "bodies": ["Nope"]})
    _touch(str(tmp_path), "/M1/OffsetBlock", "copy1.add.step")
    _touch(str(tmp_path), "/M1/OffsetBlock", "shell1.add.step")
    reasons = " ".join(analyze(tree, StepSource(str(tmp_path), "v5")))
    assert "cannot be replaced by add/remove deltas" in reasons
    assert "exactly one known body" in reasons


class _FakeStepExchange:
    """Imports give the solid `solids[path]` as (volume, cog); a paste is a
    new fake body holding it."""

    def __init__(self, solids):
        self.solids, self.imported, self.closed = solids, [], []

    def import_file(self, path):
        self.imported.append(path)
        return SimpleNamespace(path=path, solid=self.solids(path))

    def paste_as_result(self, imported, part, home, activate, name):
        body = _Body(name)
        body.solids = [imported.solid]
        return body, imported.solid[0], []

    def close(self, imported):
        self.closed.append(imported.path)
        return True


def _step_builder(part, monkeypatch, step_dir, solids):
    built = _builder(part, monkeypatch)
    built.conn.is_3dx = False
    built.conn.active_document = SimpleNamespace(Activate=lambda: None)
    built.step_dir = step_dir
    exchange = _FakeStepExchange(solids)
    monkeypatch.setattr(TreeBuilder, "_exchange", lambda self: exchange)
    return built, exchange


def test_builder_applies_delta_bodies_and_reports_them_apart(tmp_path, monkeypatch):
    removed = _touch(str(tmp_path), "/M1/OffsetBlock", "fillet1.remove.step")
    part = _FakePart()
    built, exchange = _step_builder(part, monkeypatch, str(tmp_path),
                                    lambda path: (85.8407, (0.0, 0.0, 0.0)))
    report = built.build(_with(load("m1_offset_block"), FILLET),
                         {"/M1/OffsetBlock": {"volume_mm3": 80000 - 85.8407}},
                         check_editability=False)
    assert report["success"], report.get("error")
    assert report["native_history"] == "partial"
    (dead,) = report["dead_bodies"]
    assert dead == {"feature": "fillet1", "type": "fillet", "role": "remove", "file": removed,
                    "body": "STEP_fillet1_remove", "volume_mm3": 85.8407,
                    "part": "/M1/OffsetBlock"}
    fillet = report["parts_detail"][0]["features"][2]
    assert fillet["dead"] and fillet["catia"][0] == "STEP_fillet1_remove"
    assert exchange.closed == [removed]
    assert [f.kind for f in part.pads if hasattr(f, "tool")] == ["remove"]


def test_builder_fails_when_a_delta_boolean_has_no_effect(tmp_path, monkeypatch):
    """Coincident faces can make CATIA's Remove leave the body as it was;
    that passes a plain cut's bounds but not the delta's exact check."""
    _touch(str(tmp_path), "/M1/OffsetBlock", "fillet1.remove.step")
    part = _FakePart()
    update = part.UpdateObject
    part.UpdateObject = lambda obj: None if getattr(obj, "kind", None) == "remove" else update(obj)
    built, _ = _step_builder(part, monkeypatch, str(tmp_path),
                                    lambda path: (85.8407, (0.0, 0.0, 0.0)))
    report = built.build(_with(load("m1_offset_block"), FILLET), check_editability=False)
    assert not report["success"] and report["error"]["feature"] == "fillet1"
    assert "coincident faces" in report["error"]["message"]
    assert report["dead_bodies"][0]["body"] == "STEP_fillet1_remove"


def test_builder_takes_a_base_feature_as_a_new_dead_body(tmp_path, monkeypatch):
    _touch(str(tmp_path), "/P", "base1.step")
    tree = {"version": 1, "parts": [{"id": "/P", "name": "P", "bodies": ["Body1"], "features": [
        {"type": "base_feature", "id": "base1", "operation": "new_body", "creates": ["Body1"],
         "reason": "imported STEP"}]}]}
    part = _FakePart()
    built, _ = _step_builder(part, monkeypatch, str(tmp_path), lambda path: (1234.0, (1, 2, 3)))
    report = built.build(tree, {"/P": {"volume_mm3": 1234.0}}, check_editability=False)
    assert report["success"], report.get("error")
    assert report["parts_detail"][0]["volume_mm3"] == 1234.0
    assert report["dead_bodies"][0]["role"] == "solid"


def test_builder_refuses_step_files_on_3dexperience(tmp_path, monkeypatch):
    _touch(str(tmp_path), "/M1/OffsetBlock", "fillet1.remove.step")
    part = _FakePart()
    built, exchange = _step_builder(part, monkeypatch, str(tmp_path), None)
    built.conn.is_3dx = True
    with pytest.raises(TreeRefused, match="no STEP fallback on 3DEXPERIENCE"):
        built.build(_with(load("m1_offset_block"), FILLET))
    assert exchange.imported == [] and part.pads == []


def test_cli_check_takes_a_step_dir(tmp_path, capsys):
    from catia_mcp.tree.__main__ import main

    tree_path = tmp_path / "tree.json"
    tree_path.write_text(json.dumps(_with(load("m1_offset_block"), FILLET)))
    step_dir = tmp_path / "steps"
    assert main(["check", str(tree_path), "--step-dir", str(step_dir)]) == 1
    assert "no STEP file" in capsys.readouterr().out
    _touch(str(step_dir), "/M1/OffsetBlock", "fillet1.remove.step")
    assert main(["check", str(tree_path), "--step-dir", str(step_dir)]) == 0
    assert "1 of them from STEP files" in capsys.readouterr().out
    assert main(["check", str(tree_path), "--step-dir", str(step_dir), "--platform", "3dx"]) == 1


# ── End to end with build123d: write the files, accept, build against fakes ──


def _filleted_block():
    from build123d import Axis, Box, Pos, fillet

    box = Pos(0, 0, 0) * Box(80, 50, 20)
    return fillet(box.edges().filter_by(Axis.Z), radius=5)


def _step_solid(path):
    from build123d import CenterOf, import_step

    shape = import_step(path)
    center = shape.center(CenterOf.MASS)
    return float(shape.volume), (center.X, center.Y, center.Z)


def test_delta_files_round_trip_through_step_and_the_builder(tmp_path, monkeypatch):
    pytest.importorskip("build123d")
    from catia_mcp.tree.step_fallback import (
        expected_with_steps,
        final_solids,
        write_fallback,
    )

    tree = _with(load("m1_offset_block"), FILLET)
    final = _filleted_block()
    rows = write_fallback(tree, str(tmp_path), final_solids({"/M1/OffsetBlock": final}))
    (row,) = rows
    assert row["role"] == "remove"
    assert row["volume_mm3"] == pytest.approx(4 * (25 - math.pi * 25 / 4) * 20, rel=1e-6)
    with open(row["file"], "rb") as handle:
        first = handle.read()
    write_fallback(tree, str(tmp_path), final_solids({"/M1/OffsetBlock": final}))
    with open(row["file"], "rb") as handle:
        assert handle.read() == first  # fixed timestamp: same bytes
    assert b"AUTOMOTIVE_DESIGN" in first  # AP214

    assert not analyze(tree, StepSource(str(tmp_path), "v5"))
    want = expected_with_steps(tree, str(tmp_path))["/M1/OffsetBlock"]["volume_mm3"]
    assert want == pytest.approx(final.volume, rel=1e-6)
    part = _FakePart()
    built, _ = _step_builder(part, monkeypatch, str(tmp_path), _step_solid)
    report = built.build(tree, {"/M1/OffsetBlock": {"volume_mm3": want}},
                         check_editability=False)
    assert report["success"], report.get("error")
    assert _mass(part.MainBody.solids)[0] == pytest.approx(final.volume, rel=1e-6)


def test_a_base_feature_under_native_features_needs_its_own_solid(tmp_path):
    pytest.importorskip("build123d")
    from build123d import Box, Cylinder, Pos

    from catia_mcp.tree.reference import ReferenceError
    from catia_mcp.tree.step_fallback import (
        after_solids,
        expected_with_steps,
        final_solids,
        write_fallback,
    )

    tree = load("m1_offset_block")
    features = tree["parts"][0]["features"]
    features[1]["id"] = "extrude_cut"
    features[1].update(operation="cut", bodies=["Body1"], distance=20)
    features[1].pop("creates")
    features.insert(0, {"type": "base_feature", "id": "base1", "operation": "new_body",
                        "creates": ["Body1"], "reason": "raw OCP"})
    # The base is a cylinder; the block cut through its middle removes it all
    # inside x in [-40, 40].
    base = Pos(0, 0, 0) * Cylinder(10, 200, rotation=(0, 90, 0))
    with pytest.raises(ReferenceError, match="give its solid"):
        write_fallback(tree, str(tmp_path), final_solids({"/M1/OffsetBlock": Box(1, 1, 1)}))
    (row,) = write_fallback(tree, str(tmp_path), after_solids({("/M1/OffsetBlock", "base1"): base}))
    assert row["role"] == "solid" and row["file"].endswith(os.path.join("M1_OffsetBlock",
                                                                         "base1.step"))
    assert not analyze(tree, StepSource(str(tmp_path), "v5"))
    want = expected_with_steps(tree, str(tmp_path))["/M1/OffsetBlock"]["volume_mm3"]
    assert want == pytest.approx(math.pi * 100 * 120, rel=1e-6)


def test_a_dead_body_can_fill_an_empty_main_body():
    part = FakePart("New")  # PartBody without features: not measurable
    tool = FakeBody("STEP_x", 100.0)
    combined = StepExchange(v5_connection(FakeV5()), FakeMeasure()).combine(
        part, part.MainBody, tool, "join")
    assert combined["volume_before_mm3"] == 0.0 and combined["volume_after_mm3"] == 100.0


FIXTURE_STEPS = os.path.join(os.path.dirname(__file__), "fixtures", "step")


def test_the_probe_fixture_is_accepted_and_matches_its_block():
    with open(os.path.join(FIXTURE_STEPS, "fillet_block.tree.json")) as handle:
        tree = json.load(handle)
    steps = os.path.join(FIXTURE_STEPS, "steps")
    assert not analyze(tree, StepSource(steps, "v5"))
    pytest.importorskip("build123d")
    from catia_mcp.tree.step_fallback import expected_with_steps

    (props,) = expected_with_steps(tree, steps).values()
    assert props["volume_mm3"] == pytest.approx(240000 - 4 * (25 - 25 * math.pi / 4) * 40,
                                                rel=1e-6)
    assert _step_solid(os.path.join(FIXTURE_STEPS, "fillet_block.step"))[0] == \
        pytest.approx(props["volume_mm3"], rel=1e-6)
