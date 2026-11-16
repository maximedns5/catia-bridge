"""The link's settings on the PC: the server address and the token.

They live in link.json in the bridge's folder: %APPDATA%\\catia-bridge on
Windows, $XDG_CONFIG_HOME/catia-bridge (or ~/.config/catia-bridge)
elsewhere, or $CATIA_LINK_HOME when set. The log file sits next to it.

The token is a secret: it is written only to this file (readable by the
user alone where the platform allows it) and never printed.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_NAME = "link.json"
LOG_NAME = "link.log"


def home() -> Path:
    override = os.environ.get("CATIA_LINK_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "catia-bridge"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "catia-bridge"


@dataclass
class LinkConfig:
    server: str = ""
    token: str = ""

    @property
    def linked(self) -> bool:
        return bool(self.server and self.token)

    @classmethod
    def load(cls, folder: Path | None = None) -> LinkConfig:
        path = (folder or home()) / CONFIG_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        return cls(server=str(data.get("server") or ""), token=str(data.get("token") or ""))

    def save(self, folder: Path | None = None) -> Path:
        folder = folder or home()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / CONFIG_NAME
        # Write then rename, so a crash never leaves half a token.
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".link-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(self), handle, indent=2)
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return path

