"""The one-line installer the app serves: install.ps1 with its placeholders
filled in.

    __SERVER__   the address the bridge keeps (https, or http to this PC)
    __CODE__     the pairing code
    __PACKAGE__  what pip installs: the bridge's release archive by default

Every value is checked here before it goes into the script, and again by
the script itself: the code is 8 characters of the pairing alphabet, the
server an https address, the package an https URL or a plain requirement.
None may hold a quote, so none can end the script's single-quoted strings.
"""

from __future__ import annotations

import re
from importlib import resources

from catia_mcp.link.client import check_server

DEFAULT_PACKAGE = "https://github.com/maximedns5/catia-bridge/archive/refs/heads/main.zip"
CODE_RE = re.compile(r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$")
# An https URL, or a requirement such as catia-v5-mcp-server==0.3.0.
PACKAGE_RE = re.compile(r"^(https://[^\s'\"`$]+|[A-Za-z0-9][A-Za-z0-9._-]*([<>=!~]=?[A-Za-z0-9.*+!-]+)?)$")


def template() -> str:
    return resources.files("catia_mcp.link").joinpath("install.ps1").read_text(encoding="utf-8")


def render(server: str, code: str, package: str = DEFAULT_PACKAGE) -> str:
    code = code.strip().upper()
    if not CODE_RE.match(code):
        raise ValueError("a pairing code is 8 characters from 23456789ABCDEFGHJKMNPQRSTUVWXYZ")
    server = check_server(server)
    if any(c in server for c in "'\"`$ "):
        raise ValueError(f"unsafe server address: {server!r}")
    if not PACKAGE_RE.match(package):
        raise ValueError(f"the package is an https URL or a requirement: {package!r}")
    script = template()
    for placeholder, value in (("__SERVER__", server), ("__CODE__", code),
                               ("__PACKAGE__", package)):
        if script.count(f"'{placeholder}'") != 1:
            raise RuntimeError(f"install.ps1 must hold '{placeholder}' once")
        script = script.replace(f"'{placeholder}'", f"'{value}'")
    return script
