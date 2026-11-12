"""Live probe for the dialog watchdog, the guarded worker and the release fingerprint.

Records, in a JSON report, what only a real CATIA on Windows can tell:
- the executable registered for CATIA.Application (LocalServer32) and the
  running processes that look like CATIA, to confirm CATIA_MCP_PROCESS_NAMES
  (V5: CNEXT.exe; the 3DEXPERIENCE native app name is unverified);
- CATIA's top-level windows as the watchdog sees them (titles, classes,
  enabled state, child classes), before anything is sent to CATIA;
- the connect message and the release fingerprint (Version, Release,
  ServicePack, UI language);
- with --trigger, one harmless blocking dialog: a VBScript MsgBox with an OK
  button only, shown by CATIA through SystemService.Evaluate. The probe checks
  that the watchdog reports it as BLOCKED_BY_DIALOG with its text, presses OK
  on that box only (matched by its unique title), checks that the session
  stays refused until reconnect, then repeats with an allowlist rule to check
  auto-dismiss. Nothing in the model changes.

It never saves, closes or creates documents.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in):
    python scripts/probe_dialog_watchdog.py
    python scripts/probe_dialog_watchdog.py --trigger --report watchdog_probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from catia_mcp.com_guard import ComWorker
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import CATIAToolError
from catia_mcp.watchdog import DismissRule, Watchdog, process_names, public

PROBE_TITLE = "catia-mcp watchdog probe"
MSGBOX = (
    "Public Function catia_mcp_probe(title)\n"
    '    catia_mcp_probe = MsgBox("Harmless test box from the catia-mcp watchdog probe. '
    'The probe closes it; if it stays open, press OK.", 0, title)\n'
    "End Function\n"
)


def registration() -> dict[str, Any]:
    """The server executable registered for CATIA.Application."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CATIA.Application\CLSID") as key:
            clsid = winreg.QueryValue(key, None)
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32") as key:
            server = winreg.QueryValue(key, None)
        return {"clsid": clsid, "local_server32": server}
    except Exception as error:  # noqa: BLE001 - recorded, not fatal
        return {"error": str(error)}


def windows_of(watchdog: Watchdog, pids: set[int]) -> list[dict[str, Any]]:
    return [dict(w.describe(), owner=w.owner, visible=w.visible,
                 child_classes=sorted(set(w.child_classes)))
            for w in watchdog.screen.windows(pids)]


def step(report: dict[str, Any], name: str, fn) -> Any:
    started = time.perf_counter()
    entry: dict[str, Any] = {"step": name}
    report["steps"].append(entry)
    try:
        entry["result"] = fn()
        entry["ok"] = True
    except CATIAToolError as error:
        entry.update(ok=False, code=error.code, message=str(error),
                     data=getattr(error, "data", None))
    except Exception as error:  # noqa: BLE001 - recorded, the probe goes on
        entry.update(ok=False, error=repr(error), traceback=traceback.format_exc())
    entry["seconds"] = round(time.perf_counter() - started, 3)
    print(f"{name}: {'ok' if entry['ok'] else entry.get('code') or entry.get('error')}")
    return entry


def close_probe_box(watchdog: Watchdog) -> bool:
    """Press OK on the probe's own message box, and nothing else."""
    snap = watchdog.snapshot()
    for window in snap.get("_windows", []):
        if window.title == PROBE_TITLE:
            return watchdog.screen.click(window.hwnd, "OK") or watchdog.screen.close(window.hwnd)
    return False


def wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def trigger(report: dict[str, Any], conn: CATIAConnection, worker: ComWorker) -> None:
    def show_box():
        return conn.app.SystemService.Evaluate(MSGBOX, 0, "catia_mcp_probe", [PROBE_TITLE])

    # 1. Report mode: the call blocks on the box, the watchdog must say so.
    worker.watchdog.rules = []
    blocked = step(report, "msgbox_reported", lambda: worker.run_guarded(
        show_box, timeout=30, label="probe MsgBox"))
    blocked["expected"] = "BLOCKED_BY_DIALOG naming the probe box"
    report["msgbox_closed_by_probe"] = close_probe_box(worker.watchdog)
    if not report["msgbox_closed_by_probe"]:
        print(f"Close the message box titled '{PROBE_TITLE}' in CATIA (OK).")
    report["state_after_close"] = (
        worker.state if wait_for(lambda: worker.state != "blocked", 30) else "still blocked")
    refused = step(report, "refused_until_reconnect",
                   lambda: worker.run_guarded(lambda: conn.app.Caption))
    refused["expected"] = "SESSION_BLOCKED (nothing sent to CATIA)"
    step(report, "reconnect", lambda: worker.run_guarded(conn.connect, reconnect=True))

    # 2. Auto-dismiss mode: an allowlist rule for the probe box only.
    worker.watchdog.rules = [DismissRule(title=PROBE_TITLE, button="OK")]
    dismissed = step(report, "msgbox_auto_dismissed", lambda: worker.run_guarded(
        show_box, timeout=30, label="probe MsgBox"))
    dismissed["dismissed"] = list(worker.dismissed)
    dismissed["expected"] = "result 1 (vbOK) and the probe box in dismissed"
    worker.watchdog.rules = []
    if not dismissed["ok"]:
        close_probe_box(worker.watchdog)
        wait_for(lambda: worker.state != "blocked", 30)
        step(report, "reconnect_after_auto_dismiss",
             lambda: worker.run_guarded(conn.connect, reconnect=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--trigger", action="store_true",
                        help="show one harmless MsgBox in CATIA and check the watchdog on it")
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--report", default="watchdog_probe.json", help="JSON report path")
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "python": sys.version.split()[0], "windows": platform.platform(),
        "process_names": list(process_names()), "registration": registration(), "steps": [],
    }
    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    watchdog = conn.watchdog
    report["watchdog_available"] = watchdog.available
    try:
        everything = watchdog.screen.processes()
        report["catia_like_processes"] = [
            {"pid": pid, "name": name} for pid, name in everything
            if any(word in name.lower() for word in ("catia", "cnext", "3dexperience", "3dexp"))]
    except Exception as error:  # noqa: BLE001
        report["catia_like_processes"] = repr(error)
    report["catia_processes"] = watchdog.catia_processes()
    pids = {entry["pid"] for entry in report["catia_processes"]}
    report["windows_before"] = windows_of(watchdog, pids) if pids else []
    report["snapshot_before"] = public(watchdog.snapshot())

    worker = ComWorker(watchdog, timeout_s=60, name="catia-probe")
    connected = step(report, "connect", lambda: worker.run_guarded(conn.connect))
    if connected["ok"]:
        step(report, "fingerprint", lambda: worker.run_guarded(conn.fingerprint))
        if args.trigger:
            trigger(report, conn, worker)
    report["snapshot_after"] = public(watchdog.snapshot())
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {args.report}")
    return 0 if connected["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
