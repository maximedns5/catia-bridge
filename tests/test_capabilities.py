"""Offline tests of the capability probe, its cache, and the preflight gating.

They use in-memory doubles: they establish which evidence leads to which
status and what is refused before touching CATIA, not how a real CATIA
answers the licence queries (see docs/capabilities.md for what needs a live
check).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from catia_mcp import capabilities as caps
from catia_mcp.connection import PLATFORM_3DX, PLATFORM_V5
from catia_mcp.plm import PLMSession
from catia_mcp.tree import builder as builder_module
from catia_mcp.tree.builder import TreeBuilder
from catia_mcp.tree.preflight import analyze, refusal
from catia_mcp.tree.requirements import feature_needs
from tests.fakes import App3DX, NewService, SystemService
from tests.test_tree import _FakePart, _mass

TREES = Path(__file__).parent / "fixtures" / "trees"


def load(name: str) -> dict:
    return json.loads((TREES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_CACHE_DIR", str(tmp_path / "cache"))
    # CI runs on Windows: the no-COM checks see one healthy CATIA there.
    monkeypatch.setattr(caps, "_registry_server",
                        lambda: {"progid_registered": True, "server": r"C:\DS\CNEXT.exe"})
    monkeypatch.setattr(caps, "_process_list", lambda: [("CNEXT.exe", 42)])
    monkeypatch.setattr(caps, "_main_windows",
                        lambda pids: [{"pid": 42, "title": "CATIA V5", "enabled": True}])
    return tmp_path / "cache"


# ── Doubles ──


class Configuration:
    """SystemConfiguration answering IsProductAuthorized from a set of names."""

    def __init__(self, authorized=(), version=5, release=28, count_member="ProductCount",
                 names=("PDG", "GS1")):
        self.Version, self.Release, self.ServicePack = version, release, 3
        self.authorized, self.names, self.asked = set(authorized), list(names), []
        setattr(self, count_member, len(names))

    def IsProductAuthorized(self, name):
        self.asked.append(name)
        return name in self.authorized


class Evaluator(SystemService):
    """Also runs the GetProductNames detour."""

    def Evaluate(self, code, language, function, params):
        if function != "catia_mcp_products":
            return super().Evaluate(code, language, function, params)
        assert "o.GetProductNames a" in code
        conf, count = params
        return conf.names[:count]


class Conn:
    """The CATIAConnection surface the probe uses."""

    def __init__(self, app, platform, part=None):
        self.app, self.platform, self.part = app, platform, part
        self.active_window = SimpleNamespace(Close=lambda: app.ActiveWindow.Close())
        self.hso = SimpleNamespace(Clear=lambda: None, Add=lambda f: None, Delete=lambda: None)

    @property
    def is_3dx(self):
        return self.platform == PLATFORM_3DX

    def ensure_connected(self):
        pass

    def session_service(self, name):
        return self.app.GetSessionService(name)

    def get_active_part(self):
        if self.part is None:
            raise RuntimeError("no active part")
        return self.part


def v5_app(conf=None, **extra):
    return SimpleNamespace(Caption="CATIA V5", FullName="C:/DS/B28/win_b64/code/bin/CNEXT.exe",
                           SystemConfiguration=conf or Configuration(), SystemService=Evaluator(),
                           **extra)


def status(report, cap):
    return report["capabilities"][cap]["status"]


# ── What each feature needs ──


def test_features_need_wireframe_only_off_the_origin_elements():
    def needs(name, index):
        features = load(name)["parts"][0]["features"]
        sketches = {f["id"]: f for f in features if f["type"] == "sketch"}
        return feature_needs(features[index], sketches)

    assert needs("m1_reversed_normal_plane", 1) == {"partdesign.basic"}
    assert needs("m1_offset_block", 1) == {"partdesign.basic", "gsd.wireframe"}
    assert needs("m2_shaft_axis_off_origin", 1) == {"partdesign.basic", "gsd.wireframe"}
    assert needs("m4_loft_rect_prismatoid", 2) == {"partdesign.basic", "partdesign.advanced",
                                                   "gsd.wireframe"}
    assert "partdesign.advanced" in needs("m4_sweep_pipe", 1)
    assert needs("m1_offset_block", 0) == set()  # drawn by its consumer


def _report(platform="3dx", **statuses):
    return {"fingerprint": {"platform": platform},
            "capabilities": {cap: {"status": s, "label": caps.CAPABILITIES[cap][1],
                                   "reason": "probed"} for cap, s in statuses.items()}}


def test_preflight_refuses_on_unavailable_never_on_unknown():
    tree = load("m4_loft_rect_prismatoid")
    assert analyze(tree, _report(**{"partdesign.advanced": "unknown"})) == {}
    reasons = analyze(tree, _report(**{"partdesign.advanced": "unavailable"}))
    (reason, places), = reasons.items()
    assert "cannot build multi-sections solids" in reason and "partdesign.advanced" in reason
    assert places == ["/M4/Prismatoid feature loft1"]
    assert "Nothing was sent to CATIA" in refusal(reasons)
    assert analyze(tree) == {}  # no probe report: no gating


def test_preflight_gates_the_container_by_platform_and_type():
    tree = load("m1_offset_block")
    report = _report(**{"plm.create.3dpart": "unavailable"})
    assert analyze(tree, report, "3DShape") == {}
    assert "create a 3D Part" in next(iter(analyze(tree, report, "3DPart")))
    v5 = _report("v5", **{"part.create": "unavailable", "measure": "unavailable"})
    assert len(analyze(tree, v5)) == 2


# ── Licences ──


def test_v5_licences_authorize_and_refuse_only_when_the_query_is_proven():
    conf = Configuration(authorized={"PDG", "GS1"})
    report = caps.probe(Conn(v5_app(conf), PLATFORM_V5))
    assert report["licences"]["product_names"] == ["PDG", "GS1"]
    assert report["licences"]["count_member"] == "ProductCount"
    assert status(report, "partdesign.basic") == "authorized"
    assert status(report, "gsd.wireframe") == "authorized"
    # ST1 is not in MD2-like configurations: refused, with the place to look.
    step = report["capabilities"]["exchange.step.export"]
    assert step["status"] == "unavailable" and "Licensing" in step["reason"]
    assert status(report, "partdesign.advanced") == "authorized"

    # Every "no": the name form may be wrong, so nothing is refused.
    report = caps.probe(Conn(v5_app(Configuration(count_member="ProductNamesCount")),
                             PLATFORM_V5))
    assert report["licences"]["count_member"] == "ProductNamesCount"
    assert report["licences"]["name_form"] is None
    assert all(e["status"] == "unknown" for e in report["capabilities"].values())


def test_v5_licences_fall_back_to_the_prd_name_form():
    conf = Configuration(authorized={"PD1.prd"})
    report = caps.probe(Conn(v5_app(conf), PLATFORM_V5))
    assert report["licences"]["name_form"] == "trigram.prd"
    assert status(report, "partdesign.basic") == "authorized"
    assert status(report, "knowledge.rules") == "unavailable"


def test_a_failing_stage_is_recorded_and_the_probe_goes_on():
    class Broken(Configuration):
        def IsProductAuthorized(self, name):
            raise RuntimeError("member not found")

    app = v5_app(Broken())
    del app.SystemService  # GetProductNames detour unavailable too
    report = caps.probe(Conn(app, PLATFORM_V5))
    assert report["licences"]["authorized"]["PDG"].startswith("error:")
    assert "product_names_error" in report["licences"]
    assert report["stages"]["names"]["status"] == "skipped"
    assert "Not established yet" in report["summary"]


# ── 3DEXPERIENCE ──


def _app_3dx(authorized=(), services=("PLMNewService", "Search")):
    app = App3DX()
    app.SystemConfiguration = Configuration(authorized, version=6, release=28)
    app.SystemService = Evaluator()
    for name in services:
        app.session_services[name] = SimpleNamespace(
            DatabaseSearch=SimpleNamespace(AddEasyCriteria=lambda *a: None))
    return app


def test_3dx_session_and_static_facts():
    report = caps.probe(Conn(_app_3dx(authorized={"E70"}), PLATFORM_3DX))
    assert status(report, "plm.create.3dpart") == "authorized"
    assert status(report, "plm.create.3dshape") == "unknown"
    assert status(report, "exchange.step.export") == "unavailable"
    assert "part.create" not in report["capabilities"]
    assert report["stages"]["session"]["search_reset_method"] is None

    logged_out = caps.probe(Conn(_app_3dx(services=("Search",)), PLATFORM_3DX))
    entry = logged_out["capabilities"]["plm.create.3dshape"]
    assert entry["status"] == "unavailable" and "log in" in entry["reason"]


def test_names_mark_a_missing_builder_method_unavailable():
    factory = SimpleNamespace(**{m: None for m in caps.NAME_NEEDS["ShapeFactory"]
                                 if m != "AddNewLoft"})
    part = SimpleNamespace(ShapeFactory=factory)  # no HybridShapeFactory at all
    report = caps.probe(Conn(v5_app(), PLATFORM_V5, part))
    loft = report["capabilities"]["partdesign.advanced"]
    assert loft["status"] == "unavailable" and "AddNewLoft" in loft["reason"]
    assert status(report, "partdesign.basic") == "unknown"
    assert status(report, "gsd.wireframe") == "unavailable"


# ── Scratch creation probe ──


def _creation(monkeypatch, part, platform=PLATFORM_V5):
    alerts = []
    app = v5_app(Configuration(authorized={"PDG"}))
    app.DisplayFileAlerts = True
    closed = []
    conn = Conn(app, platform, part)
    conn.active_document = SimpleNamespace(Close=lambda: closed.append(app.DisplayFileAlerts))
    conn.active_window = SimpleNamespace(Close=lambda: closed.append(app.DisplayFileAlerts))
    monkeypatch.setattr("catia_mcp.tools.document.DocumentTools._new_part",
                        lambda self, name, kind: alerts.append(kind))
    monkeypatch.setattr(TreeBuilder, "_volume", lambda self, body: _mass(body.solids)[0])
    monkeypatch.setattr(TreeBuilder, "_cog", lambda self, body: _mass(body.solids)[1])
    monkeypatch.setattr(TreeBuilder, "_sketch_frame",
                        lambda self, sketch, known: known or sketch.ref)
    monkeypatch.setattr(builder_module, "_force_solid_inwork", lambda part, sketch: None)
    return conn, closed


def test_the_creation_probe_verifies_what_the_builder_built_then_discards_it(monkeypatch):
    conn, closed = _creation(monkeypatch, _FakePart())
    report = caps.probe(conn, create=True)
    assert report["stages"]["create"]["build"]["success"]
    for cap in ("part.create", "partdesign.basic", "gsd.wireframe", "partdesign.advanced",
                "measure"):
        assert status(report, cap) == "verified", cap
    # Closed once, with file alerts off, and the setting restored.
    assert closed == [False] and conn.app.DisplayFileAlerts is True
    assert report["stages"]["create"]["scratch"] == "closed without saving"
    assert "Your CATIA can" in report["summary"]


def test_a_licence_refusal_in_the_scratch_part_is_unavailable(monkeypatch):
    part = _FakePart()

    def refuse():
        raise RuntimeError("Operation not authorized")

    part.ShapeFactory.AddNewLoft = refuse
    conn, closed = _creation(monkeypatch, part)
    report = caps.probe(conn, create=True)
    assert status(report, "partdesign.basic") == "verified"
    assert status(report, "gsd.wireframe") == "verified"
    loft = report["capabilities"]["partdesign.advanced"]
    assert loft["status"] == "unavailable" and "Operation not authorized" in loft["reason"]
    assert closed == [False]
    assert "It cannot build multi-sections solids" in report["summary"]


def test_the_scratch_part_is_left_alone_when_another_window_is_active(monkeypatch):
    part = _FakePart()
    conn, closed = _creation(monkeypatch, part)
    builds = []
    real_build = TreeBuilder.build

    def build_then_switch(self, *args, **kwargs):
        builds.append(1)
        result = real_build(self, *args, **kwargs)
        conn.part = _FakePart()  # the user clicked another window
        return result

    monkeypatch.setattr(TreeBuilder, "build", build_then_switch)
    report = caps.probe(conn, create=True)
    assert builds and closed == []
    assert "left open" in report["stages"]["create"]["scratch"]


# ── Cache ──


def test_active_results_are_cached_per_installation_and_reused(monkeypatch, cache_dir):
    conn, _ = _creation(monkeypatch, _FakePart())
    first = caps.probe(conn, create=True)
    assert (cache_dir / f"{first['cache_key']}.json").exists()

    again = caps.probe(conn)
    entry = again["capabilities"]["partdesign.advanced"]
    assert entry["status"] == "verified" and entry["cached_at"] == first["probed_at"]
    assert "from an earlier probe" in again["summary"]
    assert caps.probe(conn, use_cache=False)["capabilities"]["partdesign.advanced"][
        "status"] == "authorized"

    # Fresh evidence that something is gone wins over the cache.
    gone = Configuration(authorized={"GS1"}, names=("GS1",))
    conn.app.SystemConfiguration = gone
    refused = caps.probe(conn)
    assert status(refused, "partdesign.basic") == "unavailable"

    # Another release is another installation.
    conn.app.SystemConfiguration = Configuration(authorized={"PDG"}, release=33)
    assert caps.probe(conn)["capabilities"]["partdesign.advanced"]["status"] == "authorized"


def test_old_or_foreign_cache_files_are_ignored(cache_dir):
    cache_dir.mkdir()
    (cache_dir / "k.json").write_text(json.dumps({"schema": 1, "probed_at": time.time() - 1e8}))
    assert caps.load_cached("k") is None
    (cache_dir / "k.json").write_text("not json")
    assert caps.load_cached("k") is None
    assert caps.load_cached(None) is None


# ── catia_new_part on 3DEXPERIENCE ──


def test_new_part_auto_tries_a_3d_part_only_once_probed(monkeypatch):
    from tests.test_3dexperience import call, run, server_on, text

    app = App3DX()
    service = NewService(app, refuse=("3DPart",))
    app.session_services["PLMNewService"] = service
    conn = Conn(app, PLATFORM_3DX)
    report = caps.new_report()
    report["cache_key"] = caps.cache_key(caps.fingerprint(conn))
    caps._init_capabilities(report, PLATFORM_3DX)
    caps.set_capability(report, "plm.create.3dpart", "verified", "created", "risky")
    caps.save_cached(report)
    assert caps.cached_status(conn, "plm.create.3dpart") == "verified"

    result = run(call(server_on(app), "catia_new_part", {"name": "Bracket"}))
    assert not result.isError, text(result)
    # Tried (and refused here), then the 3D Shape.
    assert service.created == ["3DShape"] and "not authorized" in text(result)


def test_the_3d_part_probe_records_a_refusal(monkeypatch):
    app = _app_3dx()
    app.session_services["PLMNewService"] = NewService(app, refuse=("3DPart",))
    report = caps.probe(Conn(app, PLATFORM_3DX), risky=["3dpart"])
    entry = report["capabilities"]["plm.create.3dpart"]
    assert entry["status"] == "unavailable" and entry["stage"] == "risky"
    with pytest.raises(ValueError):
        caps.probe(Conn(app, PLATFORM_3DX), risky=["format_c"])


def test_a_created_3d_part_is_verified_and_closed_unsaved():
    class Allowing(NewService):
        def _out_PLMCreate(self, plm_type):
            self.created.append(plm_type)
            return self.app.open_editor(object(), "3D Part window")

    app = _app_3dx()
    app.DisplayFileAlerts = True
    app.session_services["PLMNewService"] = Allowing(app)
    report = caps.probe(Conn(app, PLATFORM_3DX), risky=["3dpart"])
    assert status(report, "plm.create.3dpart") == "verified"
    assert app.ActiveWindow.closed and app.DisplayFileAlerts is True
    assert report["stages"]["3dpart"]["scratch"] == "closed without saving"


# ── 3DEXPERIENCE search reset detection ──


def test_search_reset_is_read_from_type_information_without_touching_members(monkeypatch):
    touched = []

    class Search:
        def __getattr__(self, name):
            touched.append(name)
            return lambda: None

    monkeypatch.setattr("catia_mcp.plm.member_kind", lambda obj, name: None)
    assert PLMSession.search_reset_method(Search()) is None and touched == []
    monkeypatch.setattr("catia_mcp.plm.member_kind",
                        lambda obj, name: "method" if name == "RemoveAllCriteria" else None)
    assert PLMSession.search_reset_method(Search()) == "RemoveAllCriteria" and touched == []


def test_search_reset_falls_back_to_name_resolution_without_type_information():
    # Doubles carry no type library: plain attributes are what resolves.
    assert PLMSession.search_reset_method(SimpleNamespace()) is None
    assert PLMSession.search_reset_method(SimpleNamespace(ResetCriteria=None)) == "ResetCriteria"


# ── Entry points ──


def test_mcp_tool_returns_the_summary_first():
    from catia_mcp.server import CATIAMCPServer

    server = CATIAMCPServer()
    server.connection.app = v5_app(Configuration(authorized={"PDG"}))
    server.connection.platform = PLATFORM_V5
    try:
        result = asyncio.run(server.call_tool("catia_capabilities", {}))
    finally:
        asyncio.run(server.close())
    data = json.loads(result.content[0].text)
    assert next(iter(data)) == "summary" and data["fingerprint"]["release"] == 28
    assert data["capabilities"]["partdesign.basic"]["status"] == "authorized"


def _cli(monkeypatch, report):
    from catia_mcp.connection import CATIAConnection

    monkeypatch.setattr(CATIAConnection, "connect", lambda self: "Connected to CATIA")
    monkeypatch.setattr(caps, "probe", lambda conn, **kwargs: report)


def test_cli_capabilities_writes_the_report(monkeypatch, tmp_path, capsys):
    from catia_mcp.tree.__main__ import main

    report = {**_report(**{"partdesign.basic": "verified"}), "summary": "Your CATIA can x."}
    _cli(monkeypatch, report)
    path = tmp_path / "caps.json"
    assert main(["capabilities", "--report", str(path)]) == 0
    assert "Your CATIA can x." in capsys.readouterr().out
    assert json.loads(path.read_text())["capabilities"]["partdesign.basic"]["status"] == "verified"


def test_cli_build_refuses_what_the_probe_found_unavailable(monkeypatch, capsys):
    from catia_mcp.tree.__main__ import main

    _cli(monkeypatch, _report(**{"partdesign.advanced": "unavailable"}))
    monkeypatch.setattr(TreeBuilder, "build", lambda *a, **k: pytest.fail("built"))
    assert main(["build", str(TREES / "m4_loft_rect_prismatoid.json"), "--expect", "none"]) == 1
    assert "cannot build multi-sections solids" in capsys.readouterr().out


def test_the_environment_stage_stops_on_an_open_dialog(monkeypatch):
    monkeypatch.setattr(caps.sys, "platform", "win32")
    monkeypatch.setattr(caps, "_registry_server",
                        lambda: {"progid_registered": True, "server": r'"C:\DS\CNEXT.exe" /Auto'})
    monkeypatch.setattr(caps, "_process_list", lambda: [("CNEXT.exe", 42), ("x.exe", 7)])
    monkeypatch.setattr(caps, "_main_windows",
                        lambda pids: [{"pid": p, "title": "CATIA V5", "enabled": False}
                                      for p in pids])
    conn = Conn(v5_app(), PLATFORM_V5)
    conn.ensure_connected = lambda: pytest.fail("attached behind a dialog")
    report = caps.probe(conn)
    assert report["stages"]["environment"]["processes"] == [{"name": "CNEXT.exe", "pid": 42}]
    assert "dialog is open" in report["summary"]
    assert "fingerprint" not in report["stages"]


def test_environment_findings_do_not_stop_an_attach_that_works(monkeypatch):
    def unregistered():
        raise OSError("no key")

    monkeypatch.setattr(caps.sys, "platform", "win32")
    monkeypatch.setattr(caps, "_registry_server", unregistered)
    monkeypatch.setattr(caps, "_process_list", list)
    report = caps.probe(Conn(v5_app(Configuration(authorized={"PDG"})), PLATFORM_V5))
    assert report["stages"]["environment"]["status"] == "warning"
    assert status(report, "partdesign.basic") == "authorized"
