"""One tree build against the running CATIA, shared by the tree command and
the app link: attach (never start CATIA), probe what this CATIA can do,
refuse what it cannot build, then build on the guarded COM worker.

The result is always a report dict:
- built or failed: the builder's report (success, native_history, editable,
  parts_detail, error on failure);
- refused before anything was created: success False, refused True,
  reasons ({reason, where}) and message (the user-facing refusal text);
- stopped by a dialog or a hang mid-call: success False and an error whose
  code is the session error's (BLOCKED_BY_DIALOG, TIMEOUT...).

Attaching can raise RuntimeError (CATIA not running, pywin32 missing) or a
CATIAToolError: the caller decides how to tell the user.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from catia_mcp.tree.preflight import TreeRefused, analyze, refusal


def refused_report(tree: dict[str, Any], reasons: Any) -> dict[str, Any]:
    """`reasons` as analyze returns them: reason -> where in the model."""
    return {
        "success": False, "refused": True, "native_history": False, "editable": None,
        "parts": len(tree.get("parts") or []), "features": 0, "parts_detail": [],
        "reasons": [{"reason": reason, "where": list(where)} for reason, where in reasons.items()],
        "message": refusal(reasons),
    }


def blocked_report(tree: dict[str, Any], builder: Any, error: Any,
                   started: float) -> dict[str, Any]:
    """The report of a build a dialog (or a hang) stopped mid-call."""
    partial = getattr(builder, "report", None) or {"parts_detail": []}
    details = [record if isinstance(record, dict) else dict(vars(record))
               for record in partial["parts_detail"]]
    return {
        "success": False, "native_history": False, "editable": None,
        "parts": len(tree["parts"]),
        "features": sum(len(record["features"]) for record in details),
        "parts_detail": details,
        "error": {
            "part": getattr(builder, "part_id", None),
            "feature": getattr(builder, "feature_id", None),
            "code": error.code, "message": str(error), "dialog": error.data.get("watchdog"),
            "note": "The CATIA call is still pending: close the dialog in CATIA, check the "
                    "document (it may contain a partial model), then run the build again.",
        },
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def build_tree_on(conn: Any, worker: Any, tree: dict[str, Any], *,
                  expected: dict[str, Any] | None = None, plm_type: str = "3DShape",
                  deadline_s: float = 170.0, check_editability: bool = True,
                  ownership: str | None = None, step_dir: str | None = None,
                  log: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Attach, probe, preflight and build `tree` on `worker` (a ComWorker
    made with conn.watchdog). Every CATIA call runs on the worker."""
    from catia_mcp.capabilities import probe
    from catia_mcp.errors import SessionError
    from catia_mcp.tree import builder as builder_module

    log(worker.run_guarded(conn.connect, label="connect"))
    # The probe uses the connection, so it runs on the worker that made it.
    capabilities = worker.run_guarded(lambda: probe(conn, save=False),
                                      label="the capability probe")
    step = None
    if step_dir:
        from catia_mcp.tree.step_source import StepSource

        step = StepSource(step_dir, "3dx" if conn.is_3dx else "v5")
    reasons = analyze(tree, capabilities, plm_type, step=step)
    if reasons:
        return refused_report(tree, reasons)
    builder = builder_module.TreeBuilder(conn, plm_type=plm_type, deadline_s=deadline_s, log=log,
                                         ownership=ownership, step_dir=step_dir)
    started = time.monotonic()
    try:
        report = worker.run_guarded(builder.build, tree, expected, check_editability,
                                    label="the tree build")
    except TreeRefused as refused:
        return refused_report(tree, refused.reasons)
    except SessionError as error:
        report = blocked_report(tree, builder, error, started)
    if worker.dismissed:
        report["dismissed_dialogs"] = worker.dismissed
    return report
