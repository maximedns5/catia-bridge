"""The guarded STA worker: a hung call, a modal dialog, the session latch,
and how the MCP server and the tree command report them."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from catia_mcp import com_guard
from catia_mcp.com_guard import BLOCKED, READY, UNKNOWN, ComWorker, run_guarded
from catia_mcp.errors import BlockedByDialog, SessionBlocked
from catia_mcp.server import CATIAMCPServer
from catia_mcp.watchdog import DismissRule
from tests.watchdog_fakes import FakeScreen, fake_watchdog


def worker_with(screen=None, rules=None, timeout=5.0, grace=0.1):
    return ComWorker(fake_watchdog(screen or FakeScreen(), rules), timeout_s=timeout,
                     dialog_grace_s=grace, poll_s=0.02)


def test_calls_run_in_order_on_one_daemon_thread():
    worker = worker_with()
    threads = [worker.run_guarded(threading.get_ident) for _ in range(3)]
    assert len(set(threads)) == 1 and threads[0] != threading.get_ident()
    assert worker._thread.daemon
    with pytest.raises(ZeroDivisionError):
        worker.run_guarded(lambda: 1 / 0)
    assert worker.state == READY
    worker.shutdown()


def test_hung_call_times_out_latches_and_recovers_only_through_reconnect():
    worker = worker_with(timeout=0.2)
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(SessionBlocked) as error:
        worker.run_guarded(release.wait, label="catia_update")
    assert time.monotonic() - started < 2
    assert error.value.modified is None and "within 0 s" not in str(error.value)
    assert "catia_connect" in str(error.value) and "Task Manager" in str(error.value)
    assert worker.state == BLOCKED and error.value.data["blocked_call"] == "catia_update"

    ran = []
    for reconnect in (False, True):  # nothing reaches the stuck worker, connect included
        with pytest.raises(SessionBlocked) as refused:
            worker.run_guarded(ran.append, 1, reconnect=reconnect)
        assert refused.value.modified is False and "still blocked" in str(refused.value)
    release.set()
    deadline = time.monotonic() + 2
    while worker.state != UNKNOWN and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.state == UNKNOWN
    with pytest.raises(SessionBlocked, match="unknown"):
        worker.run_guarded(ran.append, 2)
    worker.run_guarded(ran.append, 3, reconnect=True)
    assert ran == [3] and worker.state == READY
    worker.shutdown()


def test_dialog_during_a_call_is_reported_with_its_text_instead_of_hanging():
    screen = FakeScreen()
    worker = worker_with(screen, timeout=30)
    release = threading.Event()

    def create_3d_part():
        screen.open_dialog("Opération non autorisée", "Licence 3D Part manquante")
        release.wait()

    started = time.monotonic()
    with pytest.raises(BlockedByDialog) as error:
        worker.run_guarded(create_3d_part, label="catia_new_part")
    assert time.monotonic() - started < 3
    assert "Licence 3D Part manquante" in str(error.value)
    assert error.value.data["watchdog"]["dialogs"][0]["title"] == "Opération non autorisée"
    assert worker.state == BLOCKED and screen.clicked == []  # licence: never pressed
    release.set()
    worker.shutdown(wait=False)


def test_dialog_already_open_refuses_without_touching_catia_or_latching():
    screen = FakeScreen()
    screen.open_dialog("Options", "General")
    worker = worker_with(screen)
    ran = []
    with pytest.raises(BlockedByDialog) as error:
        worker.run_guarded(ran.append, 1)
    assert ran == [] and error.value.modified is False and worker.state == READY
    screen.close_dialog()
    worker.run_guarded(ran.append, 2)
    assert ran == [2]
    worker.shutdown()


def test_allowlisted_dialog_is_dismissed_and_the_call_completes():
    screen = FakeScreen()
    worker = worker_with(screen, [DismissRule(title="Update diagnosis", button="OK")])

    def update():
        screen.open_dialog("Update diagnosis", "Pad.1: the sketch is not closed")
        deadline = time.monotonic() + 3
        while screen.dialog is not None and time.monotonic() < deadline:
            time.sleep(0.01)  # CATIA returns once the dialog is closed
        return "update failed"

    assert worker.run_guarded(update) == "update failed"
    assert screen.clicked and worker.dismissed[0]["title"] == "Update diagnosis"
    assert worker.state == READY
    worker.shutdown()


def test_queued_call_is_given_up_when_the_running_one_latches():
    worker = worker_with(timeout=0.2)
    release = threading.Event()
    ran, outcome = [], {}

    def second():
        try:
            worker.run_guarded(ran.append, "second")
        except SessionBlocked as error:
            outcome["error"] = error

    first = threading.Thread(target=lambda: pytest.raises(SessionBlocked, worker.run_guarded,
                                                          release.wait))
    first.start()
    time.sleep(0.05)
    waiter = threading.Thread(target=second)
    waiter.start()
    first.join(3)
    waiter.join(3)
    release.set()
    time.sleep(0.1)
    assert ran == [] and outcome["error"].modified is False
    worker.shutdown(wait=False)


def test_timeout_counts_from_start_of_execution_not_from_queueing():
    worker = worker_with(timeout=0.3)
    gate = threading.Event()
    results = []
    first = threading.Thread(target=lambda: results.append(worker.run_guarded(
        lambda: gate.wait(0.2) or "first")))
    first.start()
    time.sleep(0.02)
    second = threading.Thread(target=lambda: results.append(worker.run_guarded(
        lambda: time.sleep(0.2) or "second")))
    second.start()
    first.join(3)
    second.join(3)
    assert sorted(results) == ["first", "second"] and worker.state == READY
    worker.shutdown()


def test_run_guarded_boundary_runs_inline_on_the_worker():
    worker = worker_with()

    def outer():
        return run_guarded(threading.get_ident), threading.get_ident()

    inner, outer_thread = run_guarded(outer, worker=worker)
    assert inner == outer_thread
    worker.shutdown()


def test_default_worker_is_created_once(monkeypatch):
    monkeypatch.setattr(com_guard, "_default_worker", None)
    monkeypatch.setattr(com_guard, "Watchdog", lambda: fake_watchdog())
    assert run_guarded(lambda: 7) == 7
    first = com_guard._default_worker
    assert run_guarded(lambda: 8) == 8 and com_guard._default_worker is first
    first.shutdown()


# ── MCP server ──


def test_server_reports_a_hung_tool_and_refuses_until_reconnect(monkeypatch):
    async def run():
        server = CATIAMCPServer()
        server.worker.watchdog = fake_watchdog()
        server.worker.timeout_s, server.worker.poll_s = 0.2, 0.02
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        release = threading.Event()

        def execute(name, arguments):
            if name == "catia_new_part":
                release.wait(5)
            return name

        monkeypatch.setattr(server.document_tools, "execute", execute)
        try:
            hung = await server.call_tool("catia_new_part", {})
            body = hung.structuredContent
            assert hung.isError and body["code"] == "SESSION_BLOCKED"
            assert body["effects"]["modified"] is None
            assert body["data"]["failure_stage"] == "session"
            assert body["data"]["session_state"] == BLOCKED
            assert json.loads(hung.content[0].text) == body

            refused = await server.call_tool("catia_list_documents", {})
            assert refused.structuredContent["code"] == "SESSION_BLOCKED"
            assert refused.structuredContent["effects"]["modified"] is False
            release.set()
            await asyncio.sleep(0.1)
            assert server.worker.state == UNKNOWN
            still = await server.call_tool("catia_list_documents", {})
            assert still.structuredContent["code"] == "SESSION_BLOCKED"
            reconnect = await server.call_tool("catia_connect", {})
            assert not reconnect.isError and reconnect.content[0].text == "catia_connect"
            after = await server.call_tool("catia_list_documents", {})
            assert not after.isError
        finally:
            release.set()
            await server.close()

    asyncio.run(run())


def test_server_reports_a_dialog_with_its_text(monkeypatch):
    async def run():
        screen = FakeScreen()
        server = CATIAMCPServer()
        server.worker.watchdog = fake_watchdog(screen)
        server.worker.dialog_grace_s, server.worker.poll_s = 0.05, 0.02
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        release = threading.Event()

        def execute(name, arguments):
            screen.open_dialog("Erreur de mise à jour", "Pad.1 : esquisse non fermée")
            release.wait(5)
            return name

        monkeypatch.setattr(server.document_tools, "execute", execute)
        try:
            result = await server.call_tool("catia_new_part", {})
            body = result.structuredContent
            assert body["code"] == "BLOCKED_BY_DIALOG"
            assert "esquisse non fermée" in body["message"]
            assert body["data"]["watchdog"]["main_window"]["enabled"] is False
        finally:
            release.set()
            await server.close()

    asyncio.run(run())


def test_server_close_does_not_wait_for_a_stuck_call(monkeypatch):
    async def run():
        server = CATIAMCPServer()
        server.worker.watchdog = fake_watchdog()
        server.worker.timeout_s, server.worker.poll_s = 0.1, 0.02
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        never = threading.Event()
        monkeypatch.setattr(server.document_tools, "execute", lambda *_: never.wait(30))
        await server.call_tool("catia_new_part", {})
        started = time.monotonic()
        await server.close()
        assert time.monotonic() - started < 1
        never.set()

    asyncio.run(run())


def test_busy_error_inside_a_tool_maps_to_catia_busy(monkeypatch):
    class ComError(Exception):
        hresult = -2147418111

    async def run():
        server = CATIAMCPServer()
        server.worker.watchdog = fake_watchdog()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)

        def execute(name, arguments):
            raise ComError(-2147418111, "Call was rejected by callee.", None, None)

        monkeypatch.setattr(server.document_tools, "execute", execute)
        try:
            result = await server.call_tool("catia_new_part", {})
            body = result.structuredContent
            assert body["code"] == "CATIA_BUSY" and body["effects"]["modified"] is None
            assert server.worker.state == READY
        finally:
            await server.close()

    asyncio.run(run())


# ── Tree command ──


def test_tree_build_blocked_by_a_dialog_writes_a_report_naming_part_and_feature(
        monkeypatch, tmp_path, capsys):
    import catia_mcp.connection as connection_module
    import catia_mcp.tree.builder as builder_module
    from catia_mcp.tree.__main__ import main

    screen = FakeScreen()
    release = threading.Event()

    class Connection:
        def __init__(self, platform=None, allow_launch=True):
            self.watchdog = fake_watchdog(screen)

        def connect(self):
            return "Connected to running CATIA 3DEXPERIENCE instance (stub)."

    class Builder:
        def __init__(self, conn, **options):
            pass

        def build(self, tree, expected, check_editability):
            record = builder_module.PartRecord("/P", "P", features=[{"id": "pad1"}])
            self.report = {"parts_detail": [record]}
            self.part_id, self.feature_id = "/P", "pad1"
            screen.open_dialog("Opération non autorisée", "Rôle requis : 3D Part")
            release.wait(10)

    monkeypatch.setattr(connection_module, "CATIAConnection", Connection)
    monkeypatch.setattr(builder_module, "TreeBuilder", Builder)
    monkeypatch.setattr(com_guard, "_env_float", lambda name, default:
                        0.1 if name == "CATIA_MCP_DIALOG_GRACE" else default)
    tree = {"version": 1, "parts": [{"id": "/P", "features": []}]}
    (tmp_path / "tree.json").write_text(json.dumps(tree))
    report_path = tmp_path / "report.json"
    try:
        code = main(["build", str(tmp_path / "tree.json"), "--expect", "none",
                     "--report", str(report_path)])
    finally:
        release.set()
    assert code == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] is False
    error = report["error"]
    assert (error["part"], error["feature"], error["code"]) == ("/P", "pad1", "BLOCKED_BY_DIALOG")
    assert "Rôle requis" in error["message"]
    assert error["dialog"]["dialogs"][0]["title"] == "Opération non autorisée"
    assert "FAILED at /P / pad1" in capsys.readouterr().out
