"""The CATIA STA worker, guarded against dialogs, hangs and a busy CATIA.

Every CATIA call runs on one apartment-threaded worker thread (COM objects
belong to the thread that created them). The caller waits for the worker
with a timeout and, while it waits, asks the dialog watchdog
(catia_mcp/watchdog.py) what CATIA shows:

- a modal dialog that stays open longer than CATIA_MCP_DIALOG_GRACE seconds
  (default 3) ends the wait with BLOCKED_BY_DIALOG and the dialog's text;
- no answer within CATIA_MCP_CALL_TIMEOUT seconds (default 180, 0 = never)
  ends it with SESSION_BLOCKED and whatever the watchdog sees.

A COM call cannot be cancelled in-process, so in both cases the call keeps
running in CATIA. The session is then latched: `blocked` while the worker
is still stuck, `unknown` once the call has returned (its effect on the
model was never reported). Every CATIA call is refused until the user has
dealt with CATIA and called catia_connect, which is accepted only once the
worker is free again. A dialog already open before a call is reported
without sending anything to CATIA and does not latch the session.

The worker thread is a daemon: a call stuck in CATIA never prevents the
server or the tree command from exiting.

Separately, call_with_retry retries a call CATIA rejected because it was busy
(RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER) with a bounded backoff:
pywin32 has no simple way to register a COM message filter, which would do
this transparently. A rejected call was never executed, so retrying that one
call is safe; retrying a whole tool is not, so the server only reports
CATIA_BUSY for those.

run_guarded(fn, *args, timeout=...) is the boundary other modules use to run
CATIA work under the same guard.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import random
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from catia_mcp import __version__
from catia_mcp.errors import BlockedByDialog, CatiaBusy, SessionBlocked
from catia_mcp.watchdog import Watchdog, dialog_summary, process_names, public

logger = logging.getLogger("catia_mcp")

RPC_E_CALL_REJECTED = 0x80010001
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
BUSY_HRESULTS = {RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER}

READY, BLOCKED, UNKNOWN = "ready", "blocked", "unknown"

BUSY_MESSAGE = (
    "CATIA rejected a call because it is busy or waiting for a person (a command or "
    "a dialog is in progress). The tool stopped there; earlier steps of this call may "
    "already have changed the model. Finish or close what is open in CATIA, inspect "
    "the model, then call again."
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number", name, os.environ.get(name))
        return default


def hresult(error: BaseException) -> int | None:
    """The unsigned HRESULT of a pywintypes.com_error (or a lookalike)."""
    value = getattr(error, "hresult", None)
    if value is None and error.args and isinstance(error.args[0], int):
        value = error.args[0]
    return value & 0xFFFFFFFF if isinstance(value, int) else None


def is_busy(error: BaseException) -> bool:
    return hresult(error) in BUSY_HRESULTS


def call_with_retry(fn: Callable[..., Any], *args: Any, budget_s: float | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> Any:
    """Call fn, retrying while CATIA rejects it as busy, with exponential
    backoff (50 ms doubling to 1 s, jittered) for at most budget_s seconds
    (CATIA_MCP_BUSY_RETRY, default 10). Gives up with CatiaBusy."""
    budget = budget_s if budget_s is not None else _env_float("CATIA_MCP_BUSY_RETRY", 10.0)
    delay, waited = 0.05, 0.0
    while True:
        try:
            return fn(*args)
        except Exception as error:
            if not is_busy(error):
                raise
            if waited + delay > budget:
                raise CatiaBusy(
                    f"CATIA kept rejecting the call as busy for {waited:.1f} s "
                    f"(HRESULT {hresult(error):#010x}). Finish or close what is open in "
                    "CATIA, then try again.") from error
            pause = delay * (1 + 0.25 * random.random())  # jitter, not crypto
            sleep(pause)
            waited += pause
            delay = min(delay * 2, 1.0)


# ── pywin32 gen_py cache ──

def gen_py_dir() -> str:
    """A gen_py folder private to this bridge version and Python version."""
    override = os.environ.get("CATIA_MCP_GEN_PY", "").strip()
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "catia-mcp", "gen_py",
                        f"{__version__}-py{sys.version_info[0]}.{sys.version_info[1]}")


def configure_gen_py(win32com: Any) -> str | None:
    """Point pywin32's makepy cache at gen_py_dir() so a stale %TEMP%\\gen_py
    left by another tool cannot change how CATIA objects bind. Call it after
    `import win32com` and before `import win32com.client`. The bridge binds
    late (dynamic Dispatch) and never calls gencache.EnsureDispatch, so this
    folder normally stays empty. CATIA_MCP_GEN_PY=default keeps pywin32's."""
    path = gen_py_dir()
    if path.lower() == "default":
        return getattr(win32com, "__gen_path__", None)
    try:
        os.makedirs(path, exist_ok=True)
        win32com.__gen_path__ = path
        package = sys.modules.get("win32com.gen_py")
        if package is not None:
            package.__path__ = [path]
        gencache = sys.modules.get("win32com.client.gencache")
        if gencache is not None:  # already loaded from the old folder
            gencache.__init__()
    except Exception as error:  # noqa: BLE001 - keep pywin32's default cache
        logger.warning("Cannot use the private gen_py cache %s: %s", path, error)
        return getattr(win32com, "__gen_path__", None)
    return path


# ── The guarded STA worker ──

_local = threading.local()


class ComWorker:
    """One daemon thread that runs CATIA calls in order, guarded by the
    dialog watchdog and a call timeout."""

    def __init__(self, watchdog: Watchdog | None = None, timeout_s: float | None = None,
                 dialog_grace_s: float | None = None, poll_s: float = 0.25,
                 name: str = "catia-sta") -> None:
        self.watchdog = watchdog
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _env_float("CATIA_MCP_CALL_TIMEOUT", 180.0))
        self.dialog_grace_s = (dialog_grace_s if dialog_grace_s is not None
                               else _env_float("CATIA_MCP_DIALOG_GRACE", 3.0))
        self.poll_s = poll_s
        self.state = READY
        self.blocked_info: dict[str, Any] = {}
        # Dialogs closed by the allowlist during the latest guarded call.
        self.dismissed: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    # ── Thread ──

    def _loop(self) -> None:
        _local.worker = self
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, fn, args = job
            if not future.set_running_or_notify_cancel():
                continue
            future.started_at = time.monotonic()
            try:
                future.set_result(fn(*args))
            except BaseException as error:  # noqa: BLE001 - handed to the waiting caller
                future.set_exception(error)

    def on_worker(self) -> bool:
        return threading.current_thread() is self._thread

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        """Queue fn on the worker without any guard (cleanup, tests)."""
        future: Future = Future()
        self._jobs.put((future, fn, args))
        return future

    def shutdown(self, wait: bool = True) -> None:
        self._jobs.put(None)
        if wait and self.state != BLOCKED and not self.on_worker():
            self._thread.join()

    # ── Session latch ──

    def check_ready(self, reconnect: bool = False) -> None:
        """Refuse a CATIA call while the session is latched; a reconnect call
        clears the `unknown` state (the stuck call has returned)."""
        with self._lock:
            state, info = self.state, dict(self.blocked_info)
            if state == UNKNOWN and reconnect:
                self.state, self.blocked_info = READY, {}
                logger.info("Session latch cleared by %s", "reconnect")
                return
        if state == READY:
            return
        since = time.monotonic() - info.get("since", time.monotonic())
        what = f"The call {info.get('label') or 'to CATIA'} ({since:.0f} s ago)"
        if state == BLOCKED:
            snap = self._snapshot()
            raise SessionBlocked(
                f"{what} is still blocked in CATIA: {dialog_summary(snap)}. Nothing was sent "
                f"to CATIA. {_what_to_do()}",
                data=self._data(snap, info), modified=False)
        raise SessionBlocked(
            f"{what} returned after the bridge stopped waiting, so its effect on the model "
            "is unknown. Nothing was sent to CATIA. Inspect the model in CATIA, then call "
            "catia_connect to resume.",
            data=self._data(info.get("watchdog"), info), modified=False)

    def _latch(self, future: Future, snap: dict[str, Any] | None, reason: str,
               label: str | None) -> None:
        with self._lock:
            self.state = BLOCKED
            self.blocked_info = {"reason": reason, "label": label,
                                 "since": getattr(future, "started_at", time.monotonic()),
                                 "watchdog": public(snap) if snap else None}
        logger.warning("CATIA session latched (%s) during %s: %s", reason, label,
                       dialog_summary(snap))
        future.add_done_callback(self._released)

    def _released(self, future: Future) -> None:
        with self._lock:
            if self.state == BLOCKED:
                self.state = UNKNOWN
        error = future.exception()
        label = self.blocked_info.get("label")
        if error is not None:
            logger.warning("Blocked call %s finished late with an error: %s", label, error)
        else:
            logger.warning("Blocked call %s finished late; its result was discarded", label)

    # ── Guarded calls ──

    def run_guarded(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None,
                    label: str | None = None, reconnect: bool = False) -> Any:
        """Run fn(*args) on the worker and return its result, or raise
        BlockedByDialog / SessionBlocked instead of hanging."""
        self.check_ready(reconnect)
        self.dismissed = []
        if self.on_worker():
            # Nested call from CATIA work already on the worker: no second
            # thread to wait from, only the pre-check.
            self._precheck(label)
            return fn(*args)
        self._precheck(label)
        future = self.submit(fn, *args)
        return self._wait(future, self.timeout_s if timeout is None else timeout, label)

    async def run_async(self, fn: Callable[..., Any], *args: Any, **options: Any) -> Any:
        """run_guarded for the event loop. Cancelling the awaiting task does not
        stop the call: the worker completes it before the next one."""
        return await asyncio.to_thread(self.run_guarded, fn, *args, **options)

    def _snapshot(self) -> dict[str, Any] | None:
        return self.watchdog.snapshot() if self.watchdog is not None else None

    def _blocking(self) -> dict[str, Any] | None:
        if self.watchdog is None or not self.watchdog.available:
            return None
        return self.watchdog.blocking_dialog()

    def _dismiss(self, snap: dict[str, Any]) -> bool:
        closed = self.watchdog.dismiss_allowed(snap) if self.watchdog is not None else []
        self.dismissed.extend(closed)
        return bool(closed)

    def _precheck(self, label: str | None) -> None:
        snap = self._blocking()
        if snap is not None and self._dismiss(snap):
            time.sleep(self.poll_s)
            snap = self._blocking()
        if snap is not None:
            raise BlockedByDialog(
                f"A CATIA dialog is open: {dialog_summary(snap)}. Nothing was sent to CATIA. "
                "Read it, close it in CATIA, then call again.",
                data=self._data(snap, {"label": label}), modified=False)

    def _wait(self, future: Future, timeout: float, label: str | None) -> Any:
        dialog_since: float | None = None
        while True:
            try:
                return future.result(timeout=self.poll_s)
            except FutureTimeout:
                pass
            if not future.running():
                # Still queued behind another call: that call's waiter watches
                # CATIA. If it latched the session, give up the queued call.
                if self.state != READY and future.cancel():
                    self.check_ready()
                continue
            now = time.monotonic()
            snap = self._blocking()
            if snap is not None and self._dismiss(snap):
                dialog_since = None
                continue
            if snap is not None:
                dialog_since = dialog_since or now
                if now - dialog_since >= self.dialog_grace_s:
                    self._latch(future, snap, "dialog", label)
                    raise BlockedByDialog(
                        f"CATIA shows a dialog that blocks {label or 'the call'}: "
                        f"{dialog_summary(snap)}. The call cannot be cancelled and its effect "
                        f"on the model is unknown. {_what_to_do()}",
                        data=self._data(snap, self.blocked_info), modified=None)
            else:
                dialog_since = None
            if timeout and now - future.started_at >= timeout:
                snap = self._snapshot()
                self._latch(future, snap, "timeout", label)
                seen = (f" CATIA shows: {dialog_summary(snap)}." if snap and snap.get("dialogs")
                        else "")
                raise SessionBlocked(
                    f"CATIA did not answer {label or 'the call'} within {timeout:g} s.{seen} "
                    "The call cannot be cancelled and its effect on the model is unknown. "
                    f"{_what_to_do()} If the operation legitimately takes longer, raise "
                    "CATIA_MCP_CALL_TIMEOUT.",
                    data=self._data(snap, self.blocked_info), modified=None)

    def _data(self, snap: dict[str, Any] | None, info: dict[str, Any]) -> dict[str, Any]:
        return {"session_state": self.state, "blocked_call": info.get("label"),
                "watchdog": public(snap) if snap else None,
                "dismissed_dialogs": list(self.dismissed)}


def _what_to_do() -> str:
    names = " / ".join(process_names())
    return (
        "The bridge refuses CATIA calls until catia_connect succeeds. Close the dialog in "
        "CATIA (read it first: it often says why the call failed), check the model, then "
        "call catia_connect. If CATIA is frozen, end it in Task Manager "
        f"({names}), restart it, then call catia_connect; restart the MCP server if "
        "catia_connect still reports the session blocked."
    )


_default_worker: ComWorker | None = None
_default_lock = threading.Lock()


def run_guarded(fn: Callable[..., Any], *args: Any, timeout: float | None = None,
                label: str | None = None, worker: ComWorker | None = None) -> Any:
    """Run CATIA work under the dialog watchdog and a timeout.

    Inside a guarded call (on a worker thread) fn runs inline. Elsewhere it
    runs on `worker`, or on a process-wide worker created on first use. Raises
    BlockedByDialog or SessionBlocked (catia_mcp.errors) instead of hanging.
    """
    current = getattr(_local, "worker", None)
    if worker is None and current is not None:
        worker = current
    if worker is None:
        global _default_worker
        with _default_lock:
            if _default_worker is None:
                _default_worker = ComWorker(Watchdog(), name="catia-sta-default")
            worker = _default_worker
    return worker.run_guarded(fn, *args, timeout=timeout, label=label)
