"""A reference server for the app's side of the link.

It does what the app's server must do, in one process and for one account,
so the bridge can be tried end to end without the app, and so the app's
developers have a working model to copy:

App side (what the app's own pages call; loopback only here):
    POST /api/catia-link/pair         -> {"code", "expires_in"}
    GET  /api/catia-link              -> {"linked", "connected", "searching", "pairing"}
    POST /api/catia-link/call         {"method", "params", "timeout"} -> {"result"} | {"error"}
    POST /api/catia-link/disconnect   -> {"linked": false}

Device side (what the bridge calls; see client.py):
    POST /api/catia-link/device/claim, GET .../next, POST .../result

Tokens are stored as SHA-256 digests only. Pairing codes are single use and
last 10 minutes. Without a valid token every device route answers 404, like
a route that does not exist.

    python -m catia_mcp.link relay [--port 8770]
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from catia_mcp.link import DEVICE_PATH

logger = logging.getLogger("catia_mcp.link.relay")

APP_PATH = "/api/catia-link"
POLL_WAIT_S = 25.0
CONNECTED_GRACE_S = 35.0
PAIRING_TTL_S = 600.0
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L: read by eye
TOKEN_PREFIX = "cl1."
MAX_BODY_BYTES = 64 * 1024 * 1024
DEFAULT_CALL_TIMEOUT_S = 30.0


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Relay:
    """The state: one pairing code, one link (a token digest), one job queue."""

    def __init__(self, poll_wait_s: float = POLL_WAIT_S) -> None:
        self.poll_wait_s = poll_wait_s
        self._lock = threading.Lock()
        self.code: tuple[str, float] | None = None
        self.link_digest: str | None = None
        self.linked_at: float | None = None
        self.last_poll: float | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.waiting: dict[str, dict[str, Any]] = {}

    # ── App side ──

    def pair(self) -> dict[str, Any]:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with self._lock:
            self.code = (code, time.monotonic() + PAIRING_TTL_S)
        return {"code": code, "expires_in": int(PAIRING_TTL_S)}

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            linked = self.link_digest is not None
            polled = self.last_poll is not None and now - self.last_poll <= CONNECTED_GRACE_S
            pairing = self.code is not None and self.code[1] > now
        return {"linked": linked, "connected": linked and polled,
                "searching": linked and self.last_poll is None, "pairing": pairing}

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            self.link_digest = self.linked_at = self.last_poll = None
            self.code = None
        return {"linked": False}

    def call(self, method: str, params: Any, timeout: float) -> dict[str, Any]:
        """Queue a job and wait for its answer: {"result"} or {"error"}."""
        if not self.status()["connected"]:
            raise LookupError("the bridge is not connected: is it running on the CATIA PC?")
        job_id = uuid.uuid4().hex
        entry: dict[str, Any] = {"event": threading.Event(), "answer": None}
        with self._lock:
            self.waiting[job_id] = entry
        # The bridge's timeout is shorter, so its own message comes back first.
        self.jobs.put({"id": job_id, "method": method, "params": params,
                       "timeout": max(1.0, timeout - 2.0), "limit": timeout})
        try:
            if not entry["event"].wait(timeout):
                raise TimeoutError(f"the bridge did not answer within {timeout:g} s: CATIA may "
                                   "be busy or showing a dialog")
            return entry["answer"]
        finally:
            with self._lock:
                self.waiting.pop(job_id, None)

    # ── Device side ──

    def claim(self, code: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            if self.code is None or self.code[1] < now or \
                    not secrets.compare_digest(self.code[0], str(code).strip().upper()):
                return None
            self.code = None  # single use
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            self.link_digest, self.linked_at, self.last_poll = digest(token), time.time(), None
        return token

    def known(self, token: str | None) -> bool:
        with self._lock:
            return bool(token) and self.link_digest is not None and \
                secrets.compare_digest(self.link_digest, digest(token))

    def next(self) -> dict[str, Any] | None:
        with self._lock:
            self.last_poll = time.monotonic()
        try:
            job = self.jobs.get(timeout=self.poll_wait_s)
        except queue.Empty:
            job = None
        with self._lock:
            self.last_poll = time.monotonic()
        return job

    def result(self, answer: dict[str, Any]) -> bool:
        with self._lock:
            entry = self.waiting.get(str(answer.get("id")))
        if entry is None:
            return False  # a late answer: nobody waits any more
        entry["answer"] = {k: v for k, v in answer.items() if k != "id"}
        entry["event"].set()
        return True


class _Handler(BaseHTTPRequestHandler):
    relay: Relay
    server_version = "catia-link-relay"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.startswith("Bearer ") else None

    def do_GET(self) -> None:
        if self.path == APP_PATH:
            self._send(200, self.relay.status())
        elif self.path == f"{DEVICE_PATH}/next" and self.relay.known(self._token()):
            job = self.relay.next()
            self._send(200, job) if job is not None else self._send(204)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            body = self._body()
        except ValueError as error:
            self._send(400, {"error": str(error)})
            return
        path, relay = self.path, self.relay
        if path == f"{APP_PATH}/pair":
            self._send(200, relay.pair())
        elif path == f"{APP_PATH}/disconnect":
            self._send(200, relay.disconnect())
        elif path == f"{APP_PATH}/call":
            self._call(body)
        elif path == f"{DEVICE_PATH}/claim":
            token = relay.claim(body.get("code", "")) if isinstance(body, dict) else None
            self._send(200, {"token": token}) if token else self._send(404, {"error": "not found"})
        elif path == f"{DEVICE_PATH}/result" and relay.known(self._token()):
            accepted = isinstance(body, dict) and relay.result(body)
            self._send(200, {"accepted": accepted})
        else:
            self._send(404, {"error": "not found"})

    def _call(self, body: Any) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("method"), str):
            self._send(400, {"error": "a call is {method, params, timeout}"})
            return
        try:
            timeout = float(body.get("timeout") or DEFAULT_CALL_TIMEOUT_S)
            answer = self.relay.call(body["method"], body.get("params") or {}, timeout)
        except LookupError as error:
            self._send(409, {"error": str(error)})
        except TimeoutError as error:
            self._send(504, {"error": str(error)})
        except ValueError as error:
            self._send(400, {"error": str(error)})
        else:
            self._send(200, answer)


def make_server(host: str = "127.0.0.1", port: int = 8770,
                relay: Relay | None = None) -> ThreadingHTTPServer:
    """The relay's HTTP server (not started). The app-side routes have no
    authentication here: keep it on the loopback address."""
    handler = type("Handler", (_Handler,), {"relay": relay or Relay()})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
