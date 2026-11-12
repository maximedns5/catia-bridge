"""Dialog watchdog, several-CATIA refusal, busy retries, release fingerprint
and the private gen_py cache, with a scripted screen instead of Win32."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from catia_mcp import com_guard
from catia_mcp.com_guard import (
    RPC_E_CALL_REJECTED,
    RPC_E_SERVERCALL_RETRYLATER,
    call_with_retry,
    configure_gen_py,
    hresult,
    is_busy,
)
from catia_mcp.errors import AmbiguousTarget, CatiaBusy
from catia_mcp.watchdog import (
    DEFAULT_PROCESS_NAMES,
    DismissRule,
    NoScreen,
    Watchdog,
    dialog_summary,
    load_rules,
    process_names,
    public,
)
from tests.watchdog_fakes import DIALOG, FakeScreen, fake_watchdog


class ComError(Exception):
    """pywintypes.com_error lookalike: (hresult, text, excepinfo, argerror)."""

    def __init__(self, code: int) -> None:
        super().__init__(code, "Call was rejected by callee.", None, None)
        self.hresult = code


def signed(code: int) -> int:
    return code - (1 << 32)


# ── Snapshot ──


def test_enabled_main_window_is_not_blocked():
    snap = fake_watchdog().snapshot()
    assert snap["available"] and not snap["blocked"]
    assert snap["main_window"]["enabled"] and snap["dialogs"] == []
    assert snap["processes"] == [{"pid": 4242, "name": "CNEXT.exe"}]


def test_disabled_main_window_reports_dialog_title_and_text():
    screen = FakeScreen()
    screen.open_dialog("Opération non autorisée", "Vous n'avez pas la licence requise.")
    snap = fake_watchdog(screen).blocking_dialog()
    assert snap is not None and snap["blocked"]
    assert snap["dialogs"][0]["title"] == "Opération non autorisée"
    assert "licence requise" in dialog_summary(snap)
    json.dumps(public(snap))  # JSON safe once the private window objects are dropped


def test_progress_window_is_catia_working_not_a_blocking_dialog():
    screen = FakeScreen()
    screen.open_dialog("Update", "Updating Pad.3", buttons=("Cancel",), progress=True)
    assert fake_watchdog(screen).blocking_dialog() is None


def test_no_screen_passes_everything_through():
    watchdog = Watchdog(NoScreen())
    assert not watchdog.available and watchdog.blocking_dialog() is None
    assert watchdog.catia_processes() == []
    watchdog.refuse_several_catia()


def test_screen_failure_never_raises():
    class Broken(FakeScreen):
        def windows(self, pids):
            raise OSError("access denied")

    screen = Broken(processes=[(1, "CNEXT.exe"), (2, "CNEXT.exe")])
    watchdog = fake_watchdog(screen)
    assert watchdog.catia_processes() == []
    assert watchdog.blocking_dialog() is None


# ── Auto-dismiss policy ──


def test_dialogs_are_never_dismissed_without_an_allowlist():
    screen = FakeScreen()
    screen.open_dialog("Update diagnosis", "Pad.1 cannot be computed")
    watchdog = fake_watchdog(screen)
    assert watchdog.dismiss_allowed(watchdog.snapshot()) == []
    assert screen.clicked == [] and screen.closed == []


def test_allowlisted_dialog_is_pressed():
    screen = FakeScreen()
    screen.open_dialog("Update diagnosis", "Pad.1 cannot be computed")
    watchdog = fake_watchdog(screen, [DismissRule(title="update diagnosis", button="OK")])
    closed = watchdog.dismiss_allowed(watchdog.snapshot())
    assert [c["title"] for c in closed] == ["Update diagnosis"]
    assert screen.clicked == [(DIALOG, "OK")]


def test_licence_dialog_needs_an_explicit_licence_rule():
    screen = FakeScreen()
    screen.open_dialog("Operation not authorized", "No license available for this command")
    rule = DismissRule(title="Operation not authorized", button="OK")
    watchdog = fake_watchdog(screen, [rule])
    assert watchdog.dismiss_allowed(watchdog.snapshot()) == []
    rule.licence = True
    assert len(watchdog.dismiss_allowed(watchdog.snapshot())) == 1


def test_save_prompt_is_never_pressed_even_when_allowlisted():
    screen = FakeScreen()
    screen.open_dialog("Close", "Do you want to save the changes?", buttons=("Yes", "No"))
    watchdog = fake_watchdog(screen, [DismissRule(title="Close", button="No", licence=True)])
    assert watchdog.dismiss_allowed(watchdog.snapshot()) == []
    assert screen.clicked == []


def test_allowlist_file(tmp_path, monkeypatch):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps([{"title": "Update diagnosis", "button": "OK", "extra": 1}]))
    monkeypatch.setenv("CATIA_MCP_DIALOG_AUTODISMISS", str(path))
    assert load_rules() == [DismissRule(title="Update diagnosis", button="OK")]
    path.write_text("not json")
    assert load_rules() == []
    monkeypatch.delenv("CATIA_MCP_DIALOG_AUTODISMISS")
    assert load_rules() == []


# ── Which CATIA ──


def test_process_names_are_configurable(monkeypatch):
    monkeypatch.delenv("CATIA_MCP_PROCESS_NAMES", raising=False)
    assert process_names() == DEFAULT_PROCESS_NAMES
    monkeypatch.setenv("CATIA_MCP_PROCESS_NAMES", "CNEXT.exe, MyCATIA.exe")
    assert process_names() == ("CNEXT.exe", "MyCATIA.exe")


def test_two_catia_processes_refuse_to_attach(monkeypatch):
    monkeypatch.delenv("CATIA_MCP_ALLOW_SEVERAL_CATIA", raising=False)
    screen = FakeScreen(processes=[(1, "CNEXT.exe"), (2, "3dexperience.exe"), (3, "x.exe")])
    with pytest.raises(AmbiguousTarget) as refused:
        fake_watchdog(screen).refuse_several_catia()
    assert "2 CATIA processes" in str(refused.value) and "pid 2" in str(refused.value)
    monkeypatch.setenv("CATIA_MCP_ALLOW_SEVERAL_CATIA", "1")
    fake_watchdog(screen).refuse_several_catia()


def test_helper_process_without_a_window_is_not_a_second_catia():
    class OneWindow(FakeScreen):
        def windows(self, pids):
            return [w for w in super().windows(pids) if w.pid == 1]

    screen = OneWindow(processes=[(1, "3DEXPERIENCE.exe"), (2, "3DEXPERIENCE.exe")])
    assert fake_watchdog(screen).catia_processes() == [{"pid": 1, "name": "3DEXPERIENCE.exe"}]


def _com_connection(monkeypatch, app, screen):
    import catia_mcp.connection as connection_module

    monkeypatch.setattr(connection_module, "HAS_COM", True)
    monkeypatch.setattr(connection_module, "pythoncom",
                        SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None),
                        raising=False)
    calls = []

    def get_active(progid):
        calls.append(progid)
        return app

    monkeypatch.setattr(connection_module, "win32com",
                        SimpleNamespace(client=SimpleNamespace(GetActiveObject=get_active)),
                        raising=False)
    conn = connection_module.CATIAConnection(platform="v5", allow_launch=False)
    conn.watchdog = fake_watchdog(screen)
    return conn, calls


def test_connect_refuses_two_catia_before_attaching(monkeypatch):
    monkeypatch.delenv("CATIA_MCP_ALLOW_SEVERAL_CATIA", raising=False)
    screen = FakeScreen(processes=[(1, "CNEXT.exe"), (2, "3DEXPERIENCE.exe")])
    conn, calls = _com_connection(monkeypatch, SimpleNamespace(Caption="CATIA V5"), screen)
    with pytest.raises(AmbiguousTarget):
        conn.connect()
    assert calls == [] and conn.app is None


def test_connect_reports_release_fingerprint(monkeypatch):
    app = SimpleNamespace(Caption="CATIA V5 - [Part1]", SystemConfiguration=SimpleNamespace(
        Version=5, Release=33, ServicePack=2))
    conn, _ = _com_connection(monkeypatch, app, FakeScreen())
    message = conn.connect()
    assert "Version 5, Release 33, Service Pack 2" in message
    fingerprint = conn.fingerprint()
    assert fingerprint["platform"] == "CATIA V5" and fingerprint["release"] == 33
    assert fingerprint["catia_processes"] == [{"pid": 4242, "name": "CNEXT.exe"}]
    assert {"ui_language", "topology_language", "gen_py_cache"} <= set(fingerprint)


# ── Busy CATIA ──


@pytest.mark.parametrize("code", [RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER])
def test_busy_hresults_signed_or_not(code):
    assert is_busy(ComError(code)) and is_busy(ComError(signed(code)))
    assert hresult(ComError(signed(code))) == code
    assert not is_busy(ComError(0x80020009)) and not is_busy(IndexError(3))


def test_rejected_call_is_retried_with_backoff_until_it_passes():
    attempts, pauses = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 4:
            raise ComError(signed(RPC_E_CALL_REJECTED))
        return "done"

    assert call_with_retry(flaky, budget_s=5, sleep=pauses.append) == "done"
    assert len(attempts) == 4 and len(pauses) == 3
    assert pauses[0] < pauses[1] < pauses[2] <= 1.25


def test_retry_is_bounded_and_ends_in_catia_busy():
    pauses = []

    def busy():
        raise ComError(signed(RPC_E_SERVERCALL_RETRYLATER))

    with pytest.raises(CatiaBusy) as error:
        call_with_retry(busy, budget_s=1.0, sleep=pauses.append)
    assert sum(pauses) <= 1.0 and error.value.code == "CATIA_BUSY"


def test_other_errors_are_not_retried():
    pauses = []
    with pytest.raises(ComError):
        call_with_retry(lambda: (_ for _ in ()).throw(ComError(0x80020009)), sleep=pauses.append)
    assert pauses == []


def test_busy_catia_keeps_the_connection(monkeypatch):
    monkeypatch.setenv("CATIA_MCP_BUSY_RETRY", "0")

    class Busy:
        @property
        def Caption(self):
            raise ComError(signed(RPC_E_CALL_REJECTED))

    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection()
    conn.app = Busy()
    with pytest.raises(CatiaBusy):
        conn.ensure_connected()
    assert conn.app is not None


# ── gen_py ──


def test_private_gen_py_cache_per_bridge_version(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("CATIA_MCP_GEN_PY", raising=False)
    win32com = types.ModuleType("win32com")
    win32com.__gen_path__ = "C:/Temp/gen_py/3.12"
    package = types.ModuleType("win32com.gen_py")
    package.__path__ = [win32com.__gen_path__]
    reloaded = []
    gencache = SimpleNamespace(__init__=lambda: reloaded.append(win32com.__gen_path__))
    monkeypatch.setitem(sys.modules, "win32com.gen_py", package)
    monkeypatch.setitem(sys.modules, "win32com.client.gencache", gencache)
    path = configure_gen_py(win32com)
    assert path.startswith(str(tmp_path)) and com_guard.__version__ in path
    assert win32com.__gen_path__ == path and package.__path__ == [path]
    assert reloaded == [path]
    monkeypatch.setenv("CATIA_MCP_GEN_PY", "default")
    assert configure_gen_py(win32com) == path  # unchanged


def test_main_path_never_early_binds():
    from pathlib import Path

    root = Path(__file__).parents[1] / "catia_mcp"
    offenders = [p.name for p in root.rglob("*.py") if "EnsureDispatch(" in p.read_text("utf-8")]
    assert offenders == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 calls")
def test_real_win32_screen_lists_this_process():
    import os

    from catia_mcp.watchdog import Win32Screen

    screen = Win32Screen()
    processes = screen.processes()
    assert any(pid == os.getpid() for pid, _ in processes)
    screen.windows({os.getpid()})
    Watchdog(screen).snapshot()
