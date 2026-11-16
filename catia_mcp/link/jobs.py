"""The methods a job may name, and how each runs against CATIA.

A job is {"id", "method", "params", "timeout", "limit"}: the app waits
`limit` seconds for the answer, and the bridge works within `timeout`
(the app sends a timeout a little shorter than its limit, so the bridge's
own, more precise message is the one the user reads). The answer is
{"id", "result"} or {"id", "error", "error_code"}.

    catia/ping          is CATIA there, which one, is the bridge busy
    catia/capabilities  what this CATIA can do (the read-only probe)
    catia/check_tree    would this tree be built? (no part is created)
    catia/build_tree    build the tree in new parts and report

Jobs carry data only. No method runs code it was sent, and the bridge never
starts CATIA: it attaches to the one the user opened.

One job runs at a time, on its own thread, so the polling goes on while
CATIA builds. A job that arrives meanwhile is answered at once: ping with
busy true, anything else with error_code BUSY.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from catia_mcp import __version__
from catia_mcp.link import PROTOCOL

logger = logging.getLogger("catia_mcp.link")

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 3600.0
# Kept back from a build's timeout for the report, the editability check's
# last update and the answer's trip.
BUILD_MARGIN_S = 10.0
PLM_TYPES = ("3DShape", "3DPart")


class JobError(Exception):
    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def validate(job: Any) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise JobError("a job is a JSON object")
    job_id, method = job.get("id"), job.get("method")
    if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
        raise JobError("a job needs an id (a string of at most 128 characters)")
    if not isinstance(method, str) or not method:
        raise JobError("a job needs a method")
    params = job.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise JobError("a job's params are a JSON object")
    timeout = job.get("timeout", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise JobError("a job's timeout is a positive number of seconds")
    return {"id": job_id, "method": method, "params": params,
            "timeout": float(min(timeout, MAX_TIMEOUT_S))}


class CatiaJobs:
    """The methods, against one CATIA connection and one guarded COM worker.
    `conn` and `worker` are made on first use, so a bridge started before
    CATIA works once CATIA is open."""

    METHODS = ("catia/ping", "catia/capabilities", "catia/check_tree", "catia/build_tree")

    def __init__(self, platform: str = "auto", conn_factory: Callable[[], Any] | None = None,
                 worker_factory: Callable[[Any], Any] | None = None) -> None:
        self.platform = platform
        self._conn_factory = conn_factory
        self._worker_factory = worker_factory
        self.conn: Any = None
        self.worker: Any = None
        self.last_ping: dict[str, Any] = {}

    def _session(self) -> tuple[Any, Any]:
        if self.conn is None:
            if self._conn_factory is not None:
                self.conn = self._conn_factory()
            else:
                from catia_mcp.connection import CATIAConnection

                self.conn = CATIAConnection(platform=self.platform, allow_launch=False)
        if self.worker is None:
            if self._worker_factory is not None:
                self.worker = self._worker_factory(self.conn)
            else:
                from catia_mcp.com_guard import ComWorker

                self.worker = ComWorker(self.conn.watchdog, name="catia-link")
        return self.conn, self.worker

    def run(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        handler = {
            "catia/ping": self.ping,
            "catia/capabilities": self.capabilities,
            "catia/check_tree": self.check_tree,
            "catia/build_tree": self.build_tree,
        }.get(method)
        if handler is None:
            raise JobError(f"unknown method {method!r}; this bridge knows "
                           f"{', '.join(self.METHODS)}", "UNKNOWN_METHOD")
        return handler(params, timeout)

    # ── Methods ──

    def ping(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        out: dict[str, Any] = {"bridge": __version__, "protocol": PROTOCOL,
                               "methods": list(self.METHODS), "busy": False}
        conn, worker = self._session()
        try:
            out["message"] = worker.run_guarded(conn.connect, timeout=min(timeout, 20.0),
                                                label="connect")
            out["documents"] = worker.run_guarded(lambda: int(conn.app.Documents.Count),
                                                  timeout=min(timeout, 10.0),
                                                  label="the document count")
            out.update(catia=True, ready=True, platform=conn.platform_label)
        except RuntimeError as error:  # CATIA not running, pywin32 missing
            out.update(catia=False, ready=False, message=str(error))
        except Exception as error:  # a dialog or a hang: CATIA is there, not ready
            out.update(catia=True, ready=False, message=str(error),
                       error_code=getattr(error, "code", None))
        self.last_ping = {k: v for k, v in out.items() if k != "busy"}
        return out

    def capabilities(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        from catia_mcp.capabilities import probe

        conn, worker = self._session()
        worker.run_guarded(conn.connect, timeout=min(timeout, 20.0), label="connect")
        report = worker.run_guarded(lambda: probe(conn, save=False), timeout=timeout,
                                    label="the capability probe")
        return json.loads(json.dumps(report, default=str))

    def check_tree(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """The preflight, with this CATIA's capabilities when it is open."""
        from catia_mcp.tree.preflight import analyze, refusal

        tree, plm_type = _tree(params), _plm_type(params)
        capabilities, note = None, "checked against this CATIA's capabilities"
        try:
            capabilities = self.capabilities({}, timeout)
        except Exception as error:  # CATIA closed or busy: the static check still answers
            note = f"checked without CATIA ({error})"
        reasons = analyze(tree, capabilities, plm_type)
        return {"accepted": not reasons,
                "reasons": [{"reason": r, "where": list(w)} for r, w in reasons.items()],
                "message": refusal(reasons) if reasons else "",
                "note": note}

    def build_tree(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        from catia_mcp.tree.session import build_tree_on

        tree, plm_type = _tree(params), _plm_type(params)
        expected = params.get("expected")
        if expected is not None and not isinstance(expected, dict):
            raise JobError("expected is {part id: {volume_mm3, cog_mm?}}")
        edit = params.get("check_editability", True)
        if not isinstance(edit, bool):
            raise JobError("check_editability is true or false")
        if timeout <= BUILD_MARGIN_S + 5:
            raise JobError(f"a build needs a timeout above {BUILD_MARGIN_S + 5:g} s")
        conn, worker = self._session()
        deadline = timeout - BUILD_MARGIN_S
        # The worker's per-call backstop: one CATIA call may not outlive the job.
        worker.timeout_s = deadline
        report = build_tree_on(conn, worker, tree, expected=expected, plm_type=plm_type,
                               deadline_s=deadline, check_editability=edit,
                               log=lambda line: logger.info("build: %s", line))
        if expected is None:
            report.setdefault("notes", []).append(
                "no expected volumes were sent: each feature was checked, the parts were not "
                "compared with the source model")
        return json.loads(json.dumps(report, default=str))


def _tree(params: dict[str, Any]) -> dict[str, Any]:
    tree = params.get("tree")
    if not isinstance(tree, dict) or not isinstance(tree.get("parts"), list):
        raise JobError("params.tree is a feature tree (version 1)")
    return tree


def _plm_type(params: dict[str, Any]) -> str:
    plm_type = params.get("plm_type", "3DShape")
    if plm_type not in PLM_TYPES:
        raise JobError(f"plm_type is one of {', '.join(PLM_TYPES)}")
    return plm_type


class JobRunner:
    """Runs one job at a time on its own thread; answers the rest at once."""

    def __init__(self, jobs: CatiaJobs) -> None:
        self.jobs = jobs
        self._lock = threading.Lock()
        self.current: dict[str, Any] | None = None

    def handle(self, raw: Any, reply: Callable[[dict[str, Any]], None]) -> None:
        job_id = raw.get("id") if isinstance(raw, dict) else None
        try:
            job = validate(raw)
        except JobError as error:
            logger.warning("Refused a job: %s", error)
            if isinstance(job_id, str):
                reply({"id": job_id, "error": str(error), "error_code": error.code})
            return
        with self._lock:
            busy = self.current
            if busy is None:
                self.current = {"id": job["id"], "method": job["method"],
                                "started": time.monotonic()}
        if busy is not None:
            reply(self._busy_answer(job, busy))
            return
        logger.info("Job %s: %s (timeout %g s)", job["id"], job["method"], job["timeout"])
        threading.Thread(target=self._run, args=(job, reply), name=f"job-{job['id'][:8]}",
                         daemon=True).start()

    def _busy_answer(self, job: dict[str, Any], busy: dict[str, Any]) -> dict[str, Any]:
        running = f"{busy['method']} ({time.monotonic() - busy['started']:.0f} s so far)"
        if job["method"] == "catia/ping":
            # No COM while a build holds CATIA: what the last ping saw.
            result = {**self.jobs.last_ping, "bridge": __version__, "protocol": PROTOCOL,
                      "methods": list(CatiaJobs.METHODS), "busy": True, "running": running}
            return {"id": job["id"], "result": result}
        return {"id": job["id"], "error_code": "BUSY",
                "error": f"CATIA is busy with another job: {running}. Try again when it ends."}

    def _run(self, job: dict[str, Any], reply: Callable[[dict[str, Any]], None]) -> None:
        started = time.monotonic()
        try:
            answer = {"id": job["id"],
                      "result": self.jobs.run(job["method"], job["params"], job["timeout"])}
        except JobError as error:
            answer = {"id": job["id"], "error": str(error), "error_code": error.code}
        except RuntimeError as error:  # CATIA not running
            answer = {"id": job["id"], "error": str(error), "error_code": "CATIA_NOT_RUNNING"}
        except Exception as error:  # a COM or session error: say it as it is
            logger.exception("Job %s failed", job["id"])
            answer = {"id": job["id"], "error": str(error) or type(error).__name__,
                      "error_code": getattr(error, "code", None) or "FAILED"}
        finally:
            with self._lock:
                self.current = None
        logger.info("Job %s done in %.1f s: %s", job["id"], time.monotonic() - started,
                    "error " + answer["error"] if "error" in answer else "ok")
        reply(answer)
