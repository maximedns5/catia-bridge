"""Release fingerprint of the connected CATIA.

Platform, Version / Release / ServicePack (Application.SystemConfiguration),
caption, UI language and the CATIA processes seen: the key that makes probe
results and bug reports from different installations comparable.
"""

from __future__ import annotations

from typing import Any

from catia_mcp.com_guard import call_with_retry
from catia_mcp.watchdog import windows_ui_language


def release_fingerprint(conn: Any) -> dict[str, Any]:
    from catia_mcp import references

    info: dict[str, Any] = {"platform": conn.platform_label}
    app = conn.app
    for key, read in (
        ("caption", lambda: app.Caption),
        ("version", lambda: app.SystemConfiguration.Version),
        ("release", lambda: app.SystemConfiguration.Release),
        ("service_pack", lambda: app.SystemConfiguration.ServicePack),
    ):
        try:
            info[key] = call_with_retry(read, budget_s=2.0)
        except Exception:  # noqa: BLE001 - a missing property is reported as None
            info[key] = None
    # Windows' UI language; CATIA follows it unless Tools > Options > General
    # sets another one [inferred]. The topology language is the one a
    # Selection.Search last worked with in this process (None before).
    info["ui_language"] = windows_ui_language()
    info["topology_language"] = references._working_language
    info["catia_processes"] = conn.watchdog.catia_processes()
    try:
        import win32com

        info["gen_py_cache"] = getattr(win32com, "__gen_path__", None)
    except ImportError:
        info["gen_py_cache"] = None
    return info


def release_line(info: dict[str, Any]) -> str:
    """'Version 5, Release 33, Service Pack 2, Windows UI language fr-FR.'"""
    parts = [f"{label} {info[key]}" for key, label in (
        ("version", "Version"), ("release", "Release"), ("service_pack", "Service Pack"))
        if info.get(key) is not None]
    language = info.get("ui_language")
    return (", ".join(parts) or "Release unknown") + (
        f", Windows UI language {language}." if language else ".")
