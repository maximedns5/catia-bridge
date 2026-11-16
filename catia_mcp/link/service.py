"""Keeping the link running: one instance, started in the background at login.

- One instance: `run` holds link.lock in the bridge's folder for as long as it
  runs, and writes its process id to link.pid. A second `run` sees the lock
  and stops, so two bridges never take jobs for the same CATIA.
- At login: an entry in the user's Run key
  (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run) starts
  `pythonw -m catia_mcp.link run`, with no console window. Per user, no
  administrator rights.
- `start` launches that same command now, detached; `stop` ends the process
  holding the lock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from catia_mcp.link.config import home

LOCK_NAME = "link.lock"
PID_NAME = "link.pid"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CATIA Bridge Link"


class InstanceLock:
    """An exclusive, non-blocking lock on link.lock, released when the
    process ends (even if it crashes)."""

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or home()
        self.handle: Any = None

    def acquire(self) -> bool:
        self.folder.mkdir(parents=True, exist_ok=True)
        handle = open(self.folder / LOCK_NAME, "a+b")  # noqa: SIM115 - held while running
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        (self.folder / PID_NAME).write_text(str(os.getpid()), encoding="ascii")
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            (self.folder / PID_NAME).unlink(missing_ok=True)
        finally:
            self.handle.close()
            self.handle = None


def running_pid(folder: Path | None = None) -> int | None:
    """The process id of the running `run`, or None when nothing holds the lock."""
    folder = folder or home()
    probe = InstanceLock(folder)
    if probe.acquire():
        probe.release()
        return None
    try:
        return int((folder / PID_NAME).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return -1  # running, pid not written yet


def background_command() -> list[str]:
    """The command that runs the link without a console window."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.exists():
        exe = windowless
    return [str(exe), "-m", "catia_mcp.link", "run"]


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg or not arg else arg


def _winreg() -> Any:
    import winreg

    return winreg


def autostart_enabled(registry: Any = None) -> str | None:
    """The command registered to start at login, or None."""
    reg = registry or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
            value, _ = reg.QueryValueEx(key, RUN_VALUE)
            return str(value)
    except OSError:
        return None


def enable_autostart(registry: Any = None) -> str:
    reg = registry or _winreg()
    command = " ".join(_quote(arg) for arg in background_command())
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, command)
    return command


def disable_autostart(registry: Any = None) -> bool:
    reg = registry or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, RUN_VALUE)
        return True
    except OSError:
        return False


def start_background(wait_s: float = 10.0, popen: Any = subprocess.Popen) -> int | None:
    """Start `run` detached, and return its process id once it holds the
    lock (None if it did not within wait_s: see link.log)."""
    pid = running_pid()
    if pid is not None:
        return pid
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                               "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        options["creationflags"] = (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)
    else:
        options["start_new_session"] = True
    popen(background_command(), **options)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        pid = running_pid()
        if pid is not None:
            return pid
        time.sleep(0.2)
    return None


def stop_background(wait_s: float = 10.0) -> bool:
    """End the running `run`. False when none was running."""
    pid = running_pid()
    if pid is None:
        return False
    if pid > 0:
        import signal

        # On Windows os.kill with SIGTERM is TerminateProcess.
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and running_pid() is not None:
        time.sleep(0.2)
    return True
