"""A scripted Win32 screen for the dialog watchdog and guarded-worker tests."""

from __future__ import annotations

from catia_mcp.watchdog import Watchdog, WindowInfo

MAIN = 100
DIALOG = 200


class FakeScreen:
    """CATIA's processes and windows as tests set them; records presses."""

    available = True

    def __init__(self, processes=((4242, "CNEXT.exe"),)) -> None:
        self.process_list = list(processes)
        self.main_enabled = True
        self.dialog: WindowInfo | None = None
        self.clicked: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def open_dialog(self, title: str, *texts: str, buttons=("OK",), pid: int = 4242,
                    progress: bool = False) -> None:
        self.main_enabled = False
        self.dialog = WindowInfo(hwnd=DIALOG, pid=pid, title=title, class_name="#32770",
                                 owner=MAIN, texts=[*texts, *buttons],
                                 child_classes=["msctls_progress32"] if progress else [])

    def close_dialog(self) -> None:
        self.main_enabled, self.dialog = True, None

    def processes(self):
        return list(self.process_list)

    def windows(self, pids):
        found = [WindowInfo(hwnd=MAIN + pid, pid=pid, title=f"CATIA V5 - [Part{pid}]",
                            class_name="CATIA", owner=0, area=1920 * 1080,
                            enabled=self.main_enabled)
                 for pid, _ in self.process_list if pid in pids]
        if self.dialog is not None and self.dialog.pid in pids:
            self.dialog.owner = MAIN + self.dialog.pid
            found.append(self.dialog)
        return found

    def is_hung(self, hwnd):
        return False

    def click(self, hwnd, button):
        self.clicked.append((hwnd, button))
        self.close_dialog()
        return True

    def close(self, hwnd):
        self.closed.append(hwnd)
        self.close_dialog()
        return True


def fake_watchdog(screen: FakeScreen | None = None, rules=None) -> Watchdog:
    return Watchdog(screen or FakeScreen(), names=("CNEXT.exe", "3DEXPERIENCE.exe"),
                    rules=rules or [])
