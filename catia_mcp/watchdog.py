"""See what CATIA shows on screen without going through COM.

A modal dialog in CATIA (licence refusal, update error, file alert) blocks
the COM call that raised it until a person closes it, and Python cannot
interrupt that call. This module looks from the outside instead, with plain
Win32 calls that are safe from any thread:

- the CATIA processes (V5 CNEXT.exe, the 3DEXPERIENCE native app), found by
  executable name (CATIA_MCP_PROCESS_NAMES, comma separated, overrides);
- their visible top-level windows: title, class, enabled state, owner and
  the text of their child controls (static texts, buttons);
- whether the main window is disabled, the generic sign that a modal
  dialog is open (a modal dialog disables its owner).

Nothing here touches COM, so it keeps working while the worker thread is
stuck in a call. On other platforms (and when a Win32 call fails) the screen
reports itself unavailable and every check passes through.

Dialogs are only reported by default. Closing one automatically needs an
explicit allowlist (CATIA_MCP_DIALOG_AUTODISMISS, a JSON file); a rule never
applies to a licence dialog unless it says so, and save / discard prompts are
never pressed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from catia_mcp.errors import AmbiguousTarget

logger = logging.getLogger("catia_mcp")

# V5 runs CNEXT.exe. The 3DEXPERIENCE native app name is unverified on a live
# installation (scripts/probe_dialog_watchdog.py records it from the
# CATIA.Application LocalServer32 registration); override if it differs.
DEFAULT_PROCESS_NAMES = ("CNEXT.exe", "3DEXPERIENCE.exe")

# Dialog texts that mark a licence or a data-loss prompt, English and French.
LICENCE_WORDS = ("licen", "autoris", "authoriz", "role", "rôle")
SAVE_WORDS = ("save", "enregistr", "sauvegard", "discard", "abandon", "close without")
# Child window classes of a progress window: such a window is CATIA working,
# not CATIA waiting for a person [inferred, CATIA's own toolkit may differ].
PROGRESS_CLASSES = ("msctls_progress32",)


def process_names() -> tuple[str, ...]:
    raw = os.environ.get("CATIA_MCP_PROCESS_NAMES", "")
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    return names or DEFAULT_PROCESS_NAMES


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str = ""
    class_name: str = ""
    visible: bool = True
    enabled: bool = True
    owner: int = 0
    area: int = 0
    texts: list[str] = field(default_factory=list)
    child_classes: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {"hwnd": self.hwnd, "pid": self.pid, "title": self.title,
                "class": self.class_name, "enabled": self.enabled,
                "texts": [text for text in self.texts if text]}


class Screen(Protocol):
    available: bool

    def processes(self) -> list[tuple[int, str]]: ...

    def windows(self, pids: set[int]) -> list[WindowInfo]: ...

    def is_hung(self, hwnd: int) -> bool: ...

    def click(self, hwnd: int, button: str) -> bool: ...

    def close(self, hwnd: int) -> bool: ...


class NoScreen:
    """Stand-in where Win32 is not available (Linux, tests)."""

    available = False

    def processes(self) -> list[tuple[int, str]]:
        return []

    def windows(self, pids: set[int]) -> list[WindowInfo]:
        return []

    def is_hung(self, hwnd: int) -> bool:
        return False

    def click(self, hwnd: int, button: str) -> bool:
        return False

    def close(self, hwnd: int) -> bool:
        return False


class Win32Screen:
    """Win32 through ctypes (no pywin32 needed, no COM)."""

    available = True

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32, self.kernel32 = user32, kernel32
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [self.enum_proc, wintypes.LPARAM]
        user32.EnumChildWindows.argtypes = [wintypes.HWND, self.enum_proc, wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        for name in ("IsWindowVisible", "IsWindowEnabled", "IsHungAppWindow"):
            getattr(user32, name).argtypes = [wintypes.HWND]
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                        wintypes.LPARAM]
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        self.ProcessEntry = ProcessEntry
        for name in ("Process32FirstW", "Process32NextW"):
            getattr(kernel32, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]

    def processes(self) -> list[tuple[int, str]]:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == self.wintypes.HANDLE(-1).value:
            return []
        found: list[tuple[int, str]] = []
        try:
            entry = self.ProcessEntry()
            entry.dwSize = self.ctypes.sizeof(self.ProcessEntry)
            more = self.kernel32.Process32FirstW(snapshot, self.ctypes.byref(entry))
            while more:
                found.append((int(entry.th32ProcessID), entry.szExeFile))
                more = self.kernel32.Process32NextW(snapshot, self.ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(snapshot)
        return found

    def _text(self, hwnd: int) -> str:
        buffer = self.ctypes.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buffer, 512)
        if buffer.value:
            return buffer.value
        # Controls of another process may only answer WM_GETTEXT; never wait
        # on a hung window for it.
        result = self.ctypes.c_size_t()
        if self.user32.SendMessageTimeoutW(hwnd, 0x000D, 512, self.ctypes.addressof(buffer),
                                           0x0002, 300, self.ctypes.byref(result)):
            return buffer.value
        return ""

    def _class(self, hwnd: int) -> str:
        buffer = self.ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _children(self, hwnd: int) -> list[int]:
        children: list[int] = []

        def collect(child, _):
            children.append(child)
            return len(children) < 200

        self.user32.EnumChildWindows(hwnd, self.enum_proc(collect), 0)
        return children

    def windows(self, pids: set[int]) -> list[WindowInfo]:
        handles: list[tuple[int, int]] = []

        def collect(hwnd, _):
            pid = self.wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
            if pid.value in pids and self.user32.IsWindowVisible(hwnd):
                handles.append((hwnd, pid.value))
            return True

        self.user32.EnumWindows(self.enum_proc(collect), 0)
        found = []
        for hwnd, pid in handles:
            rect = self.wintypes.RECT()
            self.user32.GetWindowRect(hwnd, self.ctypes.byref(rect))
            children = self._children(hwnd)
            found.append(WindowInfo(
                hwnd=hwnd, pid=pid, title=self._text(hwnd), class_name=self._class(hwnd),
                visible=True, enabled=bool(self.user32.IsWindowEnabled(hwnd)),
                owner=self.user32.GetWindow(hwnd, 4) or 0,  # GW_OWNER
                area=max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top),
                texts=[self._text(child) for child in children],
                child_classes=[self._class(child) for child in children],
            ))
        return found

    def is_hung(self, hwnd: int) -> bool:
        return bool(self.user32.IsHungAppWindow(hwnd))

    def click(self, hwnd: int, button: str) -> bool:
        wanted = _label(button)
        for child in self._children(hwnd):
            if _label(self._text(child)) == wanted:
                result = self.ctypes.c_size_t()
                # BM_CLICK, abandoned after 1 s if the window does not answer.
                return bool(self.user32.SendMessageTimeoutW(
                    child, 0x00F5, 0, 0, 0x0002, 1000, self.ctypes.byref(result)))
        return False

    def close(self, hwnd: int) -> bool:
        return bool(self.user32.PostMessageW(hwnd, 0x0010, 0, 0))  # WM_CLOSE


def _label(text: str) -> str:
    return text.replace("&", "").strip().lower()


def default_screen() -> Screen:
    if sys.platform != "win32":
        return NoScreen()
    try:
        return Win32Screen()
    except Exception as error:  # noqa: BLE001 - ctypes setup must never stop the server
        logger.warning("Dialog watchdog unavailable: %s", error)
        return NoScreen()


@dataclass
class DismissRule:
    """One allowlisted dialog: every given field must be contained (case
    insensitive) in the dialog's title / text. `button` is the button to
    press (default: close the window). `licence` must be true for the rule to
    apply to a dialog that mentions a licence."""

    title: str = ""
    text: str = ""
    button: str = ""
    licence: bool = False

    def matches(self, dialog: WindowInfo) -> bool:
        if not (self.title or self.text):
            return False
        body = " ".join(dialog.texts).lower()
        return self.title.lower() in dialog.title.lower() and self.text.lower() in body


def load_rules(path: str | None = None) -> list[DismissRule]:
    """Rules from the JSON file named by CATIA_MCP_DIALOG_AUTODISMISS, a list of
    {"title", "text", "button", "licence"} objects. No file, no rule."""
    path = path if path is not None else os.environ.get("CATIA_MCP_DIALOG_AUTODISMISS", "")
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        return [DismissRule(**{key: entry[key] for key in entry
                               if key in ("title", "text", "button", "licence")})
                for entry in raw]
    except Exception as error:  # noqa: BLE001 - a bad file disables auto-dismiss, loudly
        logger.warning("Ignoring the dialog allowlist %s: %s", path, error)
        return []


def _mentions(dialog: WindowInfo, words: tuple[str, ...]) -> bool:
    body = " ".join([dialog.title, *dialog.texts]).lower()
    return any(re.search(rf"\b{re.escape(word)}", body) for word in words)


class Watchdog:
    def __init__(self, screen: Screen | None = None, names: tuple[str, ...] | None = None,
                 rules: list[DismissRule] | None = None) -> None:
        self.screen = screen if screen is not None else default_screen()
        self.names = names or process_names()
        self.rules = rules if rules is not None else load_rules()

    @property
    def available(self) -> bool:
        return bool(getattr(self.screen, "available", False))

    def catia_processes(self) -> list[dict[str, Any]]:
        """CATIA processes by executable name. With window enumeration, only
        those showing a visible top-level window count: helper processes
        sharing the name are not a second CATIA."""
        wanted = {name.lower() for name in self.names}
        try:
            found = [(pid, name) for pid, name in self.screen.processes()
                     if name.lower() in wanted]
            if len(found) > 1:
                windows = self.screen.windows({pid for pid, _ in found})
                shown = {window.pid for window in windows if not window.owner}
                found = [entry for entry in found if entry[0] in shown] or found
        except Exception as error:  # noqa: BLE001 - Win32 failures degrade to "unknown"
            logger.warning("Cannot list CATIA processes: %s", error)
            return []
        return [{"pid": pid, "name": name} for pid, name in found]

    def refuse_several_catia(self) -> None:
        """CATIA V5 and 3DEXPERIENCE register the same ProgID and
        GetActiveObject returns whichever instance registered first, so with
        two CATIA running the bridge could drive the wrong one: refuse."""
        running = self.catia_processes()
        if len(running) <= 1:
            return
        listing = ", ".join(f"{entry['name']} (pid {entry['pid']})" for entry in running)
        if os.environ.get("CATIA_MCP_ALLOW_SEVERAL_CATIA", "").strip().lower() in {
                "1", "true", "yes", "on"}:
            logger.warning("Several CATIA processes are running (%s); attaching anyway", listing)
            return
        raise AmbiguousTarget(
            f"{len(running)} CATIA processes are running: {listing}. CATIA V5 and "
            "3DEXPERIENCE both register CATIA.Application and COM attaches to whichever "
            "registered first, so the bridge could drive the wrong one. Close the CATIA "
            "you do not want to drive, then connect again (CATIA_MCP_ALLOW_SEVERAL_CATIA=1 "
            "attaches anyway; CATIA_MCP_PROCESS_NAMES sets the executable names).")

    def snapshot(self) -> dict[str, Any]:
        """What CATIA shows now: processes, main window, open dialogs."""
        result: dict[str, Any] = {"available": self.available, "processes": [],
                                  "main_window": None, "dialogs": [], "blocked": False}
        if not self.available:
            return result
        try:
            processes = self.catia_processes()
            result["processes"] = processes
            pids = {entry["pid"] for entry in processes}
            windows = self.screen.windows(pids) if pids else []
            main = _main_window(windows)
            dialogs = [w for w in windows if main is None or w.hwnd != main.hwnd]
            if main is not None:
                result["main_window"] = {"hwnd": main.hwnd, "pid": main.pid,
                                         "title": main.title, "enabled": main.enabled,
                                         "hung": self.screen.is_hung(main.hwnd)}
                # The modal is normally owned by the main window; keep the
                # other visible windows too but list the owned ones first.
                dialogs.sort(key=lambda w: w.owner != main.hwnd)
            result["dialogs"] = [dict(w.describe(), progress=_is_progress(w)) for w in dialogs]
            # A disabled main window with only progress windows open is CATIA
            # working; with no readable dialog it is still reported blocked.
            progress_only = bool(dialogs) and all(_is_progress(w) for w in dialogs)
            result["blocked"] = bool(main is not None and not main.enabled and not progress_only)
            result["_windows"] = dialogs
        except Exception as error:  # noqa: BLE001 - never let the watchdog raise
            logger.warning("Dialog watchdog snapshot failed: %s", error)
            result["error"] = str(error)
        return result

    def blocking_dialog(self) -> dict[str, Any] | None:
        """The snapshot when a modal dialog blocks CATIA, else None."""
        snap = self.snapshot()
        return snap if snap["blocked"] else None

    def dismiss_allowed(self, snap: dict[str, Any]) -> list[dict[str, Any]]:
        """Close the dialogs the allowlist names; return what was closed."""
        closed = []
        for window in snap.get("_windows", []):
            rule = next((r for r in self.rules if r.matches(window)), None)
            if rule is None or _mentions(window, SAVE_WORDS):
                continue
            if _mentions(window, LICENCE_WORDS) and not rule.licence:
                continue
            done = (self.screen.click(window.hwnd, rule.button) if rule.button
                    else self.screen.close(window.hwnd))
            if done:
                logger.warning("Auto-dismissed CATIA dialog %r (%s) by allowlist rule %s",
                               window.title, " | ".join(t for t in window.texts if t),
                               asdict(rule))
                closed.append(window.describe())
        return closed


def _main_window(windows: list[WindowInfo]) -> WindowInfo | None:
    top = [w for w in windows if not w.owner]
    if not top:
        return None
    named = [w for w in top if re.search(r"catia|3dexperience", w.title, re.IGNORECASE)]
    return max(named or top, key=lambda w: w.area)


def _is_progress(window: WindowInfo) -> bool:
    return any(name.lower() in PROGRESS_CLASSES for name in window.child_classes)


def public(snap: dict[str, Any]) -> dict[str, Any]:
    """A snapshot without its private window objects (JSON safe)."""
    return {key: value for key, value in snap.items() if not key.startswith("_")}


def dialog_summary(snap: dict[str, Any] | None) -> str:
    """One line per dialog: title and text, for error messages."""
    if not snap or not snap.get("dialogs"):
        return "no dialog text could be read"
    lines = []
    for dialog in snap["dialogs"][:3]:
        text = " ".join(t for t in dialog["texts"] if t).strip()
        lines.append(f"'{dialog['title']}'" + (f": {text}" if text else ""))
    return "; ".join(lines)


def windows_ui_language() -> str | None:
    """The Windows user interface language (fr-FR, en-US...). CATIA follows
    it unless Tools > Options > General sets another language [inferred]."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        langid = kernel32.GetUserDefaultUILanguage()
        buffer = ctypes.create_unicode_buffer(85)
        if kernel32.LCIDToLocaleName(langid, buffer, 85, 0):
            return buffer.value
        return hex(langid)
    except Exception:  # noqa: BLE001
        return None
