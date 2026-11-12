"""Capability probe: what the connected CATIA can do, and why.

Stages run in increasing order of risk, each behind one function boundary
(_run_stage) that never lets an exception out, so a later dialog watchdog can
wrap exactly one call per stage:

1. environment  no COM (Windows only): CATIA.Application registered and its
                server, CATIA processes, main window enabled. A disabled main
                window means a modal dialog is open: the probe stops there
                (the rest is reported; attaching decides).
2. fingerprint  attach, platform (v5 / 3dx), SystemConfiguration Version,
                Release, ServicePack, install path, session language.
3. licences     read-only: SystemConfiguration ProductCount (or
                ProductNamesCount), GetProductNames (SystemService.Evaluate
                detour), IsProductAuthorized for a list of products; on V5
                also the Tools > Options > Licensing lists. 3DEXPERIENCE
                answers are recorded raw (their meaning is unverified there).
4. session      3DEXPERIENCE session services (logged in or not).
5. names        factory name resolution on the active Part, if any (a method
                that resolves can still be refused at call time).
6. create       opt-in: a scratch V5 Part or 3DEXPERIENCE 3D Shape, built with
                the tree builder itself, then closed without saving.
7. risky        explicit opt-in only: a 3DEXPERIENCE 3D Part (many roles answer
                with a blocking "Operation not authorized" dialog), V5 STEP
                export of the scratch part to a temporary file.

Each capability ends verified (a probe built it), authorized (the licence
query says yes), unavailable (with the reason) or unknown. The tree preflight
refuses only on "unavailable". Results of the active stages (create, risky)
are cached per user and per installation, keyed by the fingerprint, so a risky
probe runs once; the read-only stages are cheap and always rerun, because
floating licences and logins come and go.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import ntpath
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from typing import Any

from catia_mcp.automation import evaluate, safe_get

logger = logging.getLogger("catia_mcp")

SCHEMA = 1
VERIFIED, AUTHORIZED, UNAVAILABLE, UNKNOWN = "verified", "authorized", "unavailable", "unknown"
# Stages whose results are cached and reused by later runs.
DURABLE_STAGES = ("create", "risky")
CACHE_MAX_AGE_S = 30 * 24 * 3600
RISKY_PROBES = ("3dpart", "step_export")

# id -> (platform it applies to, None for both; what it lets you do).
CAPABILITIES: dict[str, tuple[str | None, str]] = {
    "part.create": ("v5", "create a Part document"),
    "plm.create.3dshape": ("3dx", "create a 3D Shape"),
    "plm.create.3dpart": ("3dx", "create a 3D Part (product node with a shape)"),
    "partdesign.basic": (None, "build sketches, pads, shafts, booleans, mirrors and splits"),
    "partdesign.advanced": (None, "build multi-sections solids and ribs (lofts and sweeps)"),
    "gsd.wireframe": (None, "create construction points, lines and planes"),
    "gsd.surface": (None, "create surfaces (Generative Shape Design)"),
    "measure": (None, "measure volumes, areas and centres of gravity"),
    "knowledge.rules": ("v5", "create Knowledge Advisor rules, checks and reactions"),
    "exchange.step.export": (None, "export STEP files from automation"),
}

# V5 products (trigrams of the configuration matrix) and what they authorize.
# Per-method licence gating is not documented: "authorized" is a strong hint,
# and only partdesign.basic, knowledge.rules and STEP are ever marked
# unavailable from the licence query alone.
V5_PRODUCTS = ("PD1", "PDG", "WS1", "GS1", "GSD", "GSO", "DL1", "FSS", "AS1", "ASD", "KE1",
               "KWA", "PKT", "ST1", "IG1", "ID1", "GD1", "GDR")
# Names tried on 3DEXPERIENCE: guesses (role trigrams and the entitlement field
# reports name for scripted 3D Part creation). Recorded raw.
X3D_PRODUCTS = ("E70", "VMX", "MDG", "MES", "PDG", "GSD", "ST1")
V5_GRANTS: dict[str, tuple[tuple[str, ...], bool]] = {
    # capability: (products that authorize it, refuse when none is authorized)
    "partdesign.basic": (("PD1", "PDG"), True),
    "part.create": (("PD1", "PDG"), False),
    "partdesign.advanced": (("PDG",), False),
    "gsd.wireframe": (("GSD", "GS1", "WS1", "PDG"), False),
    "gsd.surface": (("GSD", "GS1", "WS1"), False),
    "knowledge.rules": (("KWA",), True),
    "exchange.step.export": (("ST1",), True),
}

# Factory methods the tree builder calls, by the capability they serve. The
# names stage also probes diagnostics' lists, for the report.
NAME_NEEDS = {
    "ShapeFactory": {
        "AddNewPad": "partdesign.basic", "AddNewShaft": "partdesign.basic",
        "AddNewAdd": "partdesign.basic", "AddNewRemove": "partdesign.basic",
        "AddNewIntersect": "partdesign.basic", "AddNewMirror": "partdesign.basic",
        "AddNewSplit": "partdesign.basic", "AddNewLoft": "partdesign.advanced",
        "AddNewRib": "partdesign.advanced",
    },
    "HybridShapeFactory": {
        "AddNewPointCoord": "gsd.wireframe", "AddNewLinePtPt": "gsd.wireframe",
        "AddNewPlaneOffset": "gsd.wireframe", "AddNewPlaneEquation": "gsd.wireframe",
        "AddNewExtrude": "gsd.surface", "AddNewFill": "gsd.surface",
        "AddNewJoin": "gsd.surface",
    },
}

_SESSION_SERVICES = ("PLMNewService", "PLMOpenService", "PLMPropagateService", "Search")
_REFUSAL_WORDS = ("not authorized", "non autoris", "not allowed", "licen", "not licensed")


def _refused_by_licence(error: BaseException | str) -> bool:
    text = str(error).lower()
    return any(word in text for word in _REFUSAL_WORDS)


# ── Report ──


def new_report() -> dict[str, Any]:
    return {"schema": SCHEMA, "probed_at": time.time(), "fingerprint": {}, "cache_key": None,
            "stages": {}, "licences": {}, "capabilities": {}, "summary": ""}


def set_capability(report: dict[str, Any], cap: str, status: str, reason: str, stage: str,
                   evidence: str | None = None) -> None:
    """Record evidence. A status only moves up (unknown < authorized <
    verified / unavailable); between verified and unavailable the newer
    evidence wins, since stages run from weakest to strongest."""
    entry = report["capabilities"].setdefault(
        cap, {"status": UNKNOWN, "label": CAPABILITIES[cap][1], "reason": "not probed",
              "stage": None, "evidence": []})
    if evidence:
        entry["evidence"].append(evidence)
    rank = {UNKNOWN: 0, AUTHORIZED: 1, VERIFIED: 2, UNAVAILABLE: 2}
    if rank[status] >= rank[entry["status"]]:
        entry.update(status=status, reason=reason, stage=stage)


def _init_capabilities(report: dict[str, Any], platform: str | None) -> None:
    for cap, (only, label) in CAPABILITIES.items():
        if only is None or only == platform:
            report["capabilities"].setdefault(
                cap, {"status": UNKNOWN, "label": label, "reason": "not probed", "stage": None,
                      "evidence": []})


def _run_stage(report: dict[str, Any], name: str, stage: Callable[..., Any], *args: Any) -> bool:
    """Run one stage; record its outcome. The one place a probe may fail."""
    started = time.monotonic()
    try:
        outcome = stage(report, *args)
    except Exception as error:  # noqa: BLE001 -- a probe never raises
        logger.warning("Capability probe stage %s failed: %s", name, error)
        report["stages"][name] = {"status": "error", "error": str(error)}
        return False
    entry = report["stages"].setdefault(name, {})
    entry.setdefault("status", "ok")
    entry["elapsed_s"] = round(time.monotonic() - started, 2)
    return outcome is not False


# ── Stage 1: environment (no COM) ──


def _registry_server() -> dict[str, Any]:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CATIA.Application\CLSID") as key:
        clsid = winreg.QueryValue(key, None)
    info: dict[str, Any] = {"progid_registered": True, "clsid": clsid}
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32") as key:
            info["server"] = winreg.QueryValue(key, None)
    except OSError:
        info["server"] = None
    return info


def _server_exe(command: str) -> str:
    """The executable name of a LocalServer32 command line."""
    command = command.strip()
    path = command[1:].split('"')[0] if command.startswith('"') else command.split(" /")[0]
    return ntpath.basename(path.split(" -")[0].strip()).lower()


def _process_list() -> list[tuple[str, int]]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         timeout=15, check=False).stdout
    rows = []
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) >= 2 and cells[1].isdigit():
            rows.append((cells[0], int(cells[1])))
    return rows


def _main_windows(pids: Iterable[int]) -> list[dict[str, Any]]:
    import win32con
    import win32gui
    import win32process

    wanted, found = set(pids), []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in wanted and win32gui.GetWindowText(hwnd):
            found.append({"pid": pid, "title": win32gui.GetWindowText(hwnd),
                          "enabled": bool(win32gui.IsWindowEnabled(hwnd))})
        return True

    win32gui.EnumWindows(visit, None)
    return found


def _stage_environment(report: dict[str, Any]) -> bool:
    stage = report["stages"].setdefault("environment", {})
    if sys.platform != "win32":
        stage.update(status="skipped", reason="not Windows: CATIA runs only on Windows")
        return True
    try:
        stage.update(_registry_server())
    except OSError:
        # Attaching is the authority: report, and let the next stage try.
        stage.update(status="warning", progid_registered=False,
                     reason="CATIA.Application is not registered: start CATIA once (V5: "
                            "cnext.exe /regserver; 3DEXPERIENCE: from the launcher)")
        return True
    names = {"cnext.exe"} | ({_server_exe(stage["server"])} if stage.get("server") else set())
    processes = [(n, pid) for n, pid in _process_list() if n.lower() in names]
    stage["processes"] = [{"name": n, "pid": pid} for n, pid in processes]
    if not processes:
        # The 3DEXPERIENCE process name is unverified: attaching decides.
        stage.update(status="warning", reason="no CATIA process found by name: start CATIA "
                                              "(on 3DEXPERIENCE log in) if it is not running")
        return True
    with contextlib.suppress(ImportError):
        windows = _main_windows(pid for _, pid in processes)
        stage["windows"] = windows
        if windows and not any(w["enabled"] for w in windows):
            stage.update(status="failed", reason="the CATIA window is disabled: a CATIA dialog "
                                                 "is open. Close it, then probe again")
            return False
    if len(processes) > 1:
        stage["warning"] = ("several CATIA processes run: automation attaches to the one "
                            "registered last; keep one open")
    return True


# ── Stage 2: attach and fingerprint ──


def _windows_ui_language() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import locale

        return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:  # noqa: BLE001 -- a hint only
        return None


def fingerprint(conn: Any) -> dict[str, Any]:
    """Identify the installation. Read-only; members that do not answer are
    left out. The caption is reported but not keyed: on V5 it carries the
    active document's name."""
    from catia_mcp import references

    app = conn.app
    found: dict[str, Any] = {"platform": conn.platform}
    conf = safe_get(app, "SystemConfiguration")
    for key, member in (("version", "Version"), ("release", "Release"),
                        ("service_pack", "ServicePack"), ("os", "OperatingSystem")):
        value = safe_get(conf, member) if conf is not None else None
        if value is not None:
            found[key] = value if isinstance(value, (int, float)) else str(value)
    for key, member in (("install", "FullName"), ("caption", "Caption")):
        value = safe_get(app, member)
        if value is not None:
            found[key] = str(value)
    found["topology_language"] = references._working_language
    found["windows_ui_language"] = _windows_ui_language()
    return found


def cache_key(found: dict[str, Any]) -> str:
    keyed = [found.get(k) for k in ("platform", "version", "release", "service_pack", "install")]
    return hashlib.sha256(json.dumps(keyed, default=str).encode()).hexdigest()[:16]


def _stage_fingerprint(report: dict[str, Any], conn: Any) -> bool:
    conn.ensure_connected()
    report["fingerprint"] = fingerprint(conn)
    report["cache_key"] = cache_key(report["fingerprint"])
    _init_capabilities(report, conn.platform)
    return True


# ── Stage 3: licences (read-only) ──

_PRODUCT_NAMES_VBS = (
    "Public Function catia_mcp_products(o, n)\n"
    "    Dim a()\n"
    "    ReDim a(n - 1)\n"
    "    o.GetProductNames a\n"
    "    catia_mcp_products = a\n"
    "End Function\n"
)


def _authorized(conf: Any, names: Iterable[str]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for name in names:
        try:
            answers[name] = bool(conf.IsProductAuthorized(name))
        except Exception as error:  # noqa: BLE001 -- recorded per product
            answers[name] = f"error: {error}"
    return answers


def _stage_licences(report: dict[str, Any], conn: Any) -> bool:
    app = conn.app
    licences = report["licences"]
    conf = app.SystemConfiguration
    for member in ("ProductCount", "ProductNamesCount"):
        count = safe_get(conf, member)
        if isinstance(count, (int, float)):
            licences["count_member"], licences["product_count"] = member, int(count)
            break
    if licences.get("product_count"):
        try:
            names = evaluate(app, _PRODUCT_NAMES_VBS, "catia_mcp_products",
                             [conf, licences["product_count"]])
            licences["product_names"] = [str(n) for n in names]
        except Exception as error:  # noqa: BLE001 -- recorded
            licences["product_names_error"] = str(error)
    candidates = V5_PRODUCTS if not conn.is_3dx else X3D_PRODUCTS
    answers = _authorized(conf, candidates)
    form = "trigram"
    if not any(v is True for v in answers.values()):
        # The expected name form is unverified: try "PDG.prd" too.
        suffixed = _authorized(conf, (f"{n}.prd" for n in candidates))
        if any(v is True for v in suffixed.values()):
            answers = {n: suffixed[f"{n}.prd"] for n in candidates}
            form = "trigram.prd"
    licences["authorized"] = answers
    # Only a "yes" proves the query understands our names: without one, every
    # "no" may be a name-format miss, so nothing is refused from it.
    proven = any(v is True for v in answers.values())
    licences["name_form"] = form if proven else None
    if not conn.is_3dx:
        _licence_settings(app, licences)
        _grant_v5(report, answers, proven)
    else:
        for name in ("E70", "VMX"):
            if answers.get(name) is True:
                set_capability(report, "plm.create.3dpart", AUTHORIZED,
                               f"IsProductAuthorized('{name}') is true (the entitlement "
                               "field reports name for scripted 3D Part creation)", "licences",
                               f"IsProductAuthorized({name})=True")
    return True


def _licence_settings(app: Any, licences: dict[str, Any]) -> None:
    """Tools > Options > General > Licensing, read only (never written)."""
    controllers = safe_get(app, "SettingControllers")
    if controllers is None:
        return
    for key, controller, args in (("requested", "CATSysLicenseSettingCtrl", (0,)),
                                  ("shareable", "CATSysDynLicenseSettingCtrl", ())):
        try:
            values = controllers.Item(controller).GetLicensesList(*args)
            licences[key] = [str(v) for v in (values or ())]
        except Exception as error:  # noqa: BLE001 -- recorded
            licences[f"{key}_error"] = str(error)


def _grant_v5(report: dict[str, Any], answers: dict[str, Any], proven: bool) -> None:
    for cap, (products, refuse) in V5_GRANTS.items():
        yes = [p for p in products if answers.get(p) is True]
        if yes:
            set_capability(report, cap, AUTHORIZED, f"licence {', '.join(yes)} is authorized",
                           "licences", f"IsProductAuthorized({yes[0]})=True")
        elif refuse and proven and all(answers.get(p) is False for p in products):
            set_capability(report, cap, UNAVAILABLE,
                           f"no licence for it is authorized now ({', '.join(products)}); check "
                           "Tools > Options > General > Licensing (floating licences come and "
                           "go)", "licences", f"IsProductAuthorized({products[0]})=False")


# ── Stage 4: session (3DEXPERIENCE) ──


def _stage_session(report: dict[str, Any], conn: Any) -> bool:
    stage = report["stages"].setdefault("session", {})
    if not conn.is_3dx:
        stage["status"] = "skipped"
        return True
    services = {}
    for name in _SESSION_SERVICES:
        try:
            services[name] = "available" if conn.app.GetSessionService(name) is not None \
                else "none"
        except Exception as error:  # noqa: BLE001 -- recorded
            services[name] = f"unavailable: {error}"
    stage["services"] = services
    if services["Search"] == "available":
        from catia_mcp.plm import PLMSession

        with contextlib.suppress(Exception):
            # None is the documented state: criteria cannot be cleared between
            # searches of one session.
            stage["search_reset_method"] = PLMSession.search_reset_method(
                conn.app.GetSessionService("Search").DatabaseSearch)
    if services["PLMNewService"] != "available":
        for cap in ("plm.create.3dshape", "plm.create.3dpart"):
            set_capability(report, cap, UNAVAILABLE,
                           "PLMNewService does not answer: log in to the platform and pick a "
                           "collaborative space, then probe again", "session")
    # Never the 3DEXPERIENCE way: the automation reference documents no file
    # export, and ExportData was refused live on R428.
    set_capability(report, "exchange.step.export", UNAVAILABLE,
                   "3DEXPERIENCE automation documents no STEP export (ExportData was refused "
                   "on R428); use Export > STEP in CATIA (Converter for STEP)", "session")
    return True


# ── Stage 5: factory name resolution ──


def _stage_names(report: dict[str, Any], conn: Any) -> bool:
    from catia_mcp.tools import diagnostics

    stage = report["stages"].setdefault("names", {})
    try:
        part = conn.get_active_part()
    except Exception:  # noqa: BLE001 -- no Part is a normal state
        part = None
    if part is None:
        stage.update(status="skipped", reason="no active Part (open one to probe factory names)")
        return True
    lists = {"ShapeFactory": diagnostics._SHAPE_FACTORY_METHODS,
             "HybridShapeFactory": diagnostics._HYBRID_SHAPE_FACTORY_METHODS}
    for factory, needs in NAME_NEEDS.items():
        methods = list(dict.fromkeys([*lists[factory], *needs]))
        result = _resolve_methods(part, factory, methods)
        stage[factory] = result
        if str(result.get("factory", "")).startswith("unavailable"):
            for cap in set(needs.values()):
                set_capability(report, cap, UNAVAILABLE, f"{factory} is not reachable "
                               f"({result['factory']})", "names")
            continue
        for method in result.get("missing_methods", []):
            if method in needs:
                set_capability(report, needs[method], UNAVAILABLE,
                               f"{factory}.{method} does not resolve (first suspect: a stale "
                               "pywin32 gen_py cache; else the licence or the release)", "names")
    return True


def _resolve_methods(part: Any, factory_attr: str, methods: list[str]) -> dict[str, Any]:
    """Like catia_diagnose's probe, but from the type library first: a
    late-bound getattr may invoke a zero-argument method such as AddNewLoft
    as a property, which would create a feature in the user's part."""
    from catia_mcp.introspect import member_kind

    try:
        factory = getattr(part, factory_attr)
    except Exception as error:  # noqa: BLE001 -- recorded
        return {"factory": f"unavailable: {error}"}
    exposed, missing, errored = [], [], {}
    for method in methods:
        try:
            kind = member_kind(factory, method)
        except Exception:  # noqa: BLE001 -- no type information: resolve the name
            try:
                getattr(factory, method)
                kind = "method"
            except AttributeError:
                kind = None
            except Exception as error:  # noqa: BLE001 -- recorded
                errored[method] = str(error)
                continue
        (exposed if kind else missing).append(method)
    result: dict[str, Any] = {"factory": "available", "exposed_methods": exposed,
                              "missing_methods": missing}
    if errored:
        result["errored_methods"] = errored
    return result


# ── Stage 6: scratch creation (opt-in) ──


def _rectangle(sid: str, z: float, half_u: float, half_v: float) -> dict[str, Any]:
    corners = [(-half_u, -half_v), (half_u, -half_v), (half_u, half_v), (-half_u, half_v)]
    entities = [{"type": "line", "start": list(corners[i]), "end": list(corners[(i + 1) % 4]),
                 "id": f"{sid}e{i}"} for i in range(4)]
    return {"type": "sketch", "id": sid, "entities": entities,
            "plane": {"origin": [0, 0, z], "x_dir": [1, 0, 0], "normal": [0, 0, 1]},
            "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}


# Least to most demanding: a pad on XY (Part Design, measure), a pad joined
# from an offset plane (wireframe), then a loft (advanced solids). The loft
# sections are the tested prismatoid's.
PROBE_TREE: dict[str, Any] = {"version": 1, "parts": [{
    "id": "/probe", "name": "catia_bridge_probe", "bodies": ["Body1"],
    "features": [
        _rectangle("s1", 0.0, 10.0, 5.0),
        {"type": "extrude", "id": "pad", "sketch": "s1", "distance": 10,
         "operation": "new_body", "creates": ["Body1"]},
        _rectangle("s2", 10.0, 10.0, 5.0),
        {"type": "extrude", "id": "offset_pad", "sketch": "s2", "distance": 10,
         "operation": "join", "bodies": ["Body1"]},
        _rectangle("s3", 20.0, 10.0, 5.0),
        _rectangle("s4", 50.0, 5.0, 3.0),
        {"type": "loft", "id": "loft", "sections": ["s3", "s4"], "ruled": True,
         "operation": "join", "bodies": ["Body1"]},
    ]}]}


def _discard_scratch(conn: Any, scratch: Any) -> str:
    """Close the scratch part without saving, only if it is still the active
    one (never another document)."""
    from catia_mcp.tools.document import _same

    try:
        if scratch is None or not _same(conn.get_active_part(), scratch):
            return "left open: it is no longer the active window; close it without saving"
    except Exception as error:  # noqa: BLE001 -- reported
        return f"left open ({error}); close it without saving"
    app = conn.app
    previous = safe_get(app, "DisplayFileAlerts")
    try:
        if previous is not None:
            app.DisplayFileAlerts = False
        if conn.is_3dx:
            conn.active_window.Close()
        else:
            conn.active_document.Close()
        return "closed without saving"
    except Exception as error:  # noqa: BLE001 -- reported
        return f"left open ({error}); close it without saving"
    finally:
        if previous is not None:
            with contextlib.suppress(Exception):
                app.DisplayFileAlerts = previous


def _stage_create(report: dict[str, Any], conn: Any, step_export: bool) -> bool:
    from catia_mcp.tree.builder import TreeBuilder
    from catia_mcp.tree.requirements import container, feature_needs

    stage = report["stages"].setdefault("create", {})
    holder = container(conn.platform, "3DShape")
    builder = TreeBuilder(conn, plm_type="3DShape", deadline_s=120.0)
    try:
        built = builder.build(PROBE_TREE, None, check_editability=False)
    except Exception as error:  # noqa: BLE001 -- the part itself was refused
        stage["error"] = str(error)
        status = UNAVAILABLE if _refused_by_licence(error) else UNKNOWN
        set_capability(report, holder, status, f"creating it failed: {error}", "create")
        stage["scratch"] = _discard_scratch(conn, getattr(builder, "part", None))
        return True
    stage["build"] = {k: built.get(k) for k in ("success", "elapsed_s", "error")}
    detail = (built.get("parts_detail") or [{}])[0]
    if detail.get("catia_name"):
        set_capability(report, holder, VERIFIED, "a scratch part was created", "create")
    verified: set[str] = set()
    sketches = {}
    for entry, feature in zip(detail.get("features") or [], PROBE_TREE["parts"][0]["features"]):
        if feature["type"] == "sketch":
            sketches[feature["id"]] = feature
        needs = feature_needs(feature, sketches)
        if entry.get("ok"):
            for cap in needs - verified:
                set_capability(report, cap, VERIFIED, f"built in a scratch part ({feature['id']})",
                               "create")
            if needs:
                verified |= needs | {"measure"}
                set_capability(report, "measure", VERIFIED, "measured a scratch solid", "create")
        elif built.get("error"):
            message = built["error"]["message"]
            status = UNAVAILABLE if _refused_by_licence(message) else UNKNOWN
            for cap in needs - verified:
                set_capability(report, cap, status, f"{feature['id']} failed in a scratch part: "
                               f"{message}", "create")
            break
    if step_export:
        _run_stage(report, "step_export", _probe_step_export, conn, builder)
    stage["scratch"] = _discard_scratch(conn, getattr(builder, "part", None))
    return True


# ── Stage 7: risky probes (explicit opt-in) ──


def _probe_step_export(report: dict[str, Any], conn: Any, builder: Any) -> bool:
    from catia_mcp.tools.document import _same
    from catia_mcp.tools.export import ExportTools

    stage = report["stages"].setdefault("step_export", {})
    if not _same(conn.get_active_part(), getattr(builder, "part", None)):
        stage.update(status="skipped", reason="the scratch part is not the active window")
        return True
    folder = tempfile.mkdtemp(prefix="catia_bridge_probe_")
    path = os.path.join(folder, "probe.stp")
    try:
        stage["result"] = ExportTools(conn)._export(path, "stp")
        set_capability(report, "exchange.step.export", VERIFIED,
                       "exported the scratch part to a temporary STEP file", "risky")
    except Exception as error:  # noqa: BLE001 -- the outcome is the result
        status = UNAVAILABLE if _refused_by_licence(error) else UNKNOWN
        set_capability(report, "exchange.step.export", status, f"STEP export failed: {error}",
                       "risky")
    finally:
        with contextlib.suppress(OSError):
            if os.path.exists(path):
                os.remove(path)
            os.rmdir(folder)
    return True


def _probe_3dpart(report: dict[str, Any], conn: Any) -> bool:
    """PLMCreate "3DPart". On roles without the entitlement CATIA shows a
    modal "Operation not authorized" dialog and this call returns only once
    someone closes it."""
    from catia_mcp.plm import PLMSession

    stage = report["stages"].setdefault("3dpart", {})
    session = PLMSession(conn)
    try:
        editor = session.create("3DPart")
    except Exception as error:  # noqa: BLE001 -- the outcome is the result
        # Live on R428 (student role): PLMCreate failed without a licence
        # message while the licence query, which answers, said E70 is not
        # authorized. Either one makes the refusal a licence refusal.
        licences = report.get("licences") or {}
        answers = licences.get("authorized") or {}
        unentitled = licences.get("name_form") is not None and not any(
            answers.get(name) is True for name in ("E70", "VMX"))
        status = UNAVAILABLE if _refused_by_licence(error) or unentitled else UNKNOWN
        reason = f"PLMCreate 3DPart: {error}"
        if unentitled and not _refused_by_licence(error):
            reason += " (and the licence query says E70 and VMX are not authorized)"
        set_capability(report, "plm.create.3dpart", status, reason, "risky")
        return True
    set_capability(report, "plm.create.3dpart", VERIFIED, "a scratch 3D Part was created",
                   "risky")
    try:
        if editor is not None and editor == conn.app.ActiveEditor:
            app = conn.app
            previous = safe_get(app, "DisplayFileAlerts")
            try:
                if previous is not None:
                    app.DisplayFileAlerts = False
                conn.active_window.Close()
            finally:
                if previous is not None:
                    with contextlib.suppress(Exception):
                        app.DisplayFileAlerts = previous
            stage["scratch"] = "closed without saving"
        else:
            stage["scratch"] = "left open: close it without saving"
    except Exception as error:  # noqa: BLE001 -- reported
        stage["scratch"] = f"left open ({error}); close it without saving"
    return True


# ── Probe ──


def probe(conn: Any, create: bool = False, risky: Iterable[str] = (),
          use_cache: bool = True, save: bool = True) -> dict[str, Any]:
    """Run the stages and return the report. A failing stage is recorded, never
    raised (only an unknown risky probe name is a ValueError). `create` runs the
    scratch creation probe; `risky` names the risky probes to run
    (RISKY_PROBES). Cached results of earlier active probes fill in what this
    run does not probe."""
    risky = set(risky)
    unknown = risky - set(RISKY_PROBES)
    if unknown:
        raise ValueError(f"unknown risky probe(s): {', '.join(sorted(unknown))}")
    report = new_report()
    if not _run_stage(report, "environment", _stage_environment):
        report["summary"] = summarize(report)
        return report
    if not _run_stage(report, "fingerprint", _stage_fingerprint, conn):
        report["summary"] = summarize(report)
        return report
    _run_stage(report, "licences", _stage_licences, conn)
    _run_stage(report, "session", _stage_session, conn)
    _run_stage(report, "names", _stage_names, conn)
    if use_cache:
        cached = load_cached(report["cache_key"])
        if cached:
            merge_durable(report, cached)
            report["cached_from"] = cached.get("probed_at")
    step = "step_export" in risky
    if create or (step and not conn.is_3dx):
        _run_stage(report, "create", _stage_create, conn, step and not conn.is_3dx)
    if step and conn.is_3dx:
        report["stages"]["step_export"] = {"status": "skipped", "reason": "no automation "
                                           "route on 3DEXPERIENCE"}
    if "3dpart" in risky:
        if conn.is_3dx:
            _run_stage(report, "3dpart", _probe_3dpart, conn)
        else:
            report["stages"]["3dpart"] = {"status": "skipped", "reason": "3DEXPERIENCE only"}
    report["summary"] = summarize(report)
    if save and report["cache_key"]:
        with contextlib.suppress(OSError):
            save_cached(report)
    return report


def merge_durable(report: dict[str, Any], cached: dict[str, Any]) -> None:
    """Fill the report with cached active-probe results. Fresh "unavailable"
    evidence (a licence gone, a logout) keeps precedence."""
    for cap, entry in (cached.get("capabilities") or {}).items():
        if entry.get("stage") not in DURABLE_STAGES or cap not in report["capabilities"]:
            continue
        current = report["capabilities"][cap]
        if current["status"] == UNAVAILABLE:
            continue
        report["capabilities"][cap] = {**entry, "cached_at": cached.get("probed_at")}


# ── Cache ──


def cache_dir() -> str:
    override = os.environ.get("CATIA_MCP_CACHE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "catia-bridge", "capabilities")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "catia-bridge", "capabilities")


def _cache_path(key: str) -> str:
    return os.path.join(cache_dir(), f"{key}.json")


def load_cached(key: str | None, max_age_s: float = CACHE_MAX_AGE_S) -> dict[str, Any] | None:
    if not key:
        return None
    try:
        with open(_cache_path(key), encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if cached.get("schema") != SCHEMA or time.time() - cached.get("probed_at", 0) > max_age_s:
        return None
    return cached


def save_cached(report: dict[str, Any]) -> str:
    """Write the report; durable results of an earlier run that this run did
    not reprobe are kept."""
    previous = load_cached(report["cache_key"]) or {}
    kept = dict(report)
    capabilities = dict(report["capabilities"])
    for cap, entry in (previous.get("capabilities") or {}).items():
        mine = capabilities.get(cap)
        if entry.get("stage") in DURABLE_STAGES and (mine is None
                                                     or mine.get("stage") not in DURABLE_STAGES):
            capabilities[cap] = entry
    kept["capabilities"] = capabilities
    os.makedirs(cache_dir(), exist_ok=True)
    path = _cache_path(report["cache_key"])
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(kept, handle, indent=2, ensure_ascii=False, default=str)
    os.replace(path + ".tmp", path)
    return path


def cached_status(conn: Any, cap: str) -> str:
    """The cached active-probe status of one capability for the connected
    installation, or "unknown". Cheap and read-only; never raises."""
    try:
        cached = load_cached(cache_key(fingerprint(conn)))
    except Exception:  # noqa: BLE001 -- no cache is "unknown"
        return UNKNOWN
    entry = ((cached or {}).get("capabilities") or {}).get(cap) or {}
    return entry.get("status", UNKNOWN) if entry.get("stage") in DURABLE_STAGES else UNKNOWN


# ── Summary ──


def summarize(report: dict[str, Any]) -> str:
    """Plain-language summary: what this CATIA can and cannot do, and the next step."""
    stages = report["stages"]
    for name in ("environment", "fingerprint"):
        stage = stages.get(name) or {}
        if stage.get("status") in ("failed", "error"):
            reason = stage.get("reason") or stage.get("error")
            hint = (stages.get("environment") or {}).get("reason")
            extra = f" ({hint})" if hint and name == "fingerprint" else ""
            return f"Cannot probe CATIA: {reason}{extra}."
    found = report["fingerprint"]
    platform = "CATIA 3DEXPERIENCE" if found.get("platform") == "3dx" else "CATIA V5"
    release = " ".join(str(found[k]) for k in ("version", "release", "service_pack")
                       if k in found)
    lines = [f"{platform}" + (f" (version, release, service pack: {release})" if release else "")
             + "."]
    groups: dict[str, list[dict[str, Any]]] = {VERIFIED: [], AUTHORIZED: [], UNAVAILABLE: [],
                                               UNKNOWN: []}
    for entry in report["capabilities"].values():
        groups[entry["status"]].append(entry)

    def cached(entry: dict[str, Any]) -> str:
        return " (from an earlier probe)" if entry.get("cached_at") else ""

    if groups[VERIFIED] or groups[AUTHORIZED]:
        can = [e["label"] + cached(e) for e in groups[VERIFIED]] + \
              [f"{e['label']} (licensed, not tried yet)" for e in groups[AUTHORIZED]]
        lines.append("Your CATIA can " + "; ".join(can) + ".")
    for entry in groups[UNAVAILABLE]:
        lines.append(f"It cannot {entry['label']}{cached(entry)}: {entry['reason']}.")
    if groups[UNKNOWN]:
        lines.append("Not established yet: " + "; ".join(e["label"] for e in groups[UNKNOWN])
                     + ".")
    if not any(stages.get(s) for s in ("create", "3dpart", "step_export")) \
            and not report.get("cached_from"):
        lines.append("Next step: run `python -m catia_mcp.tree capabilities --create` to build a "
                     "scratch part (closed without saving) and turn these into verified "
                     "results.")
    elif groups[UNAVAILABLE]:
        lines.append("Next step: trees that need what is unavailable are refused before CATIA "
                     "is touched; fix the cause above and probe again with --refresh.")
    return "\n".join(lines)
