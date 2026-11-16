"""The bridge's side of the link: three HTTP calls and the polling loop.

    POST {server}/api/catia-link/device/claim   {"code"}  -> {"token"}
    GET  {server}/api/catia-link/device/next    Bearer    -> 200 job | 204 | 503 | 404
    POST {server}/api/catia-link/device/result  Bearer    {"id", "result" | "error"}

Standard library only. The loop never gives up: a network error or a 503
(the app is starting, or has not restored the link yet) waits 3 s, a 404
(the app no longer knows this token) waits 10 s and says to pair again.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from catia_mcp import __version__
from catia_mcp.link import DEVICE_PATH, PROTOCOL, SERVER_HEADER

logger = logging.getLogger("catia_mcp.link")

# The server holds `next` for up to 25 s; the client waits longer than that.
POLL_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 30.0
RETRY_S = 3.0
GONE_RETRY_S = 10.0
# A tree is tens to hundreds of KB; anything far larger is refused unread.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
USER_AGENT = f"catia-bridge/{__version__} (link protocol {PROTOCOL})"


class LinkError(Exception):
    """A link call failed."""


class LinkGone(LinkError):
    """404 (or 401/403): the app does not know this token. Pair again."""


class NotYet(LinkError):
    """503: the app is starting or has not restored the link yet."""

    def __init__(self, message: str, retry_after: float = RETRY_S) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Unreachable(LinkError):
    """No answer: network down, DNS, TLS, or the server's own error."""


def check_server(url: str) -> str:
    """The server address, normalised. HTTPS, or HTTP to this machine only:
    the token travels in every request."""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError(f"not a server address: {url!r}")
    if parsed.scheme == "http" and not _loopback(parsed.hostname):
        raise ValueError(f"refusing plain HTTP to {parsed.hostname}: use https://")
    if parsed.query or parsed.fragment:
        raise ValueError(f"a server address has no query or fragment: {url!r}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DeviceClient:
    def __init__(self, server: str, token: str = "",
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.server = check_server(server)
        self.token = token
        self._open = opener

    def _url(self, route: str) -> str:
        return f"{self.server}{DEVICE_PATH}/{route}"

    def _call(self, method: str, route: str, body: Any = None,
              timeout: float = CALL_TIMEOUT_S) -> tuple[int, dict[str, str], Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self._url(route), data=data, method=method)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._open(request, timeout=timeout) as response:
                status = response.status
                headers = {k.lower(): v for k, v in response.headers.items()}
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            if status in (401, 403, 404):
                raise LinkGone(f"the app answered {status}: it does not know this link") from None
            if status == 503:
                raise NotYet("the app is starting", _retry_after(error.headers)) from None
            raise Unreachable(f"the app answered {status} to {route}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise Unreachable(f"{route}: {getattr(error, 'reason', error)}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise Unreachable(f"{route}: answer larger than {MAX_RESPONSE_BYTES} bytes")
        payload = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise Unreachable(f"{route}: the answer is not JSON") from None
        return status, headers, payload

    def claim(self, code: str) -> str:
        """Trade a pairing code for this device's token (kept by the caller)."""
        try:
            _, _, payload = self._call("POST", "claim", {"code": code.strip().upper()})
        except LinkGone:
            raise LinkGone("the app does not know this code: it may have expired (codes last "
                           "10 minutes) or been used already. Ask for a new one.") from None
        token = (payload or {}).get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise LinkError("the app accepted the code but sent no token")
        self.token = token
        return token

    def next(self) -> tuple[dict[str, Any] | None, str | None]:
        """One long poll: (job or None, the address to move to or None)."""
        status, headers, payload = self._call("GET", "next", timeout=POLL_TIMEOUT_S)
        moved = headers.get(SERVER_HEADER.lower())
        if moved:
            try:
                moved = check_server(moved)
            except ValueError as error:
                logger.warning("ignoring the app's new address: %s", error)
                moved = None
            if moved == self.server:
                moved = None
        if status == 204 or payload is None:
            return None, moved
        if not isinstance(payload, dict):
            raise Unreachable("next: the job is not a JSON object")
        return payload, moved

    def result(self, answer: dict[str, Any]) -> bool:
        """Post a job's answer; False when nobody waits for it any more."""
        _, _, payload = self._call("POST", "result", answer)
        return bool((payload or {}).get("accepted", True)) if isinstance(payload, dict) else True


def _retry_after(headers: Any) -> float:
    try:
        return max(1.0, min(60.0, float(headers.get("Retry-After"))))
    except (TypeError, ValueError, AttributeError):
        return RETRY_S


def serve(client: DeviceClient, handle: Callable[[dict[str, Any], Callable], None],
          stop: threading.Event, on_move: Callable[[str], None] = lambda _: None) -> None:
    """Poll until `stop` is set. Each job goes to handle(job, reply), which
    must return at once (the job runs elsewhere) and call reply(answer)
    when done, so the bridge keeps polling, and so stays "connected", while
    CATIA builds."""
    state = None

    def say(new: str, message: str, *args: Any, level: int = logging.INFO) -> None:
        nonlocal state
        if new != state:  # each state once, not every 3 s
            logger.log(level, message, *args)
            state = new

    while not stop.is_set():
        try:
            job, moved = client.next()
        except NotYet as error:
            say("searching", "Searching for the app (it is starting)...")
            stop.wait(error.retry_after)
            continue
        except LinkGone:
            say("gone", "The app no longer knows this link: pair again "
                "(python -m catia_mcp.link pair).", level=logging.WARNING)
            stop.wait(GONE_RETRY_S)
            continue
        except Unreachable as error:
            say("unreachable", "Cannot reach the app (%s); retrying.", error,
                level=logging.WARNING)
            stop.wait(RETRY_S)
            continue
        say("connected", "Connected to %s, waiting for jobs.", client.server)
        if moved:
            logger.info("The app moved to %s", moved)
            client.server = moved
            on_move(moved)
        if job is not None:
            handle(job, _replier(client))


def _replier(client: DeviceClient) -> Callable[[dict[str, Any]], None]:
    def reply(answer: dict[str, Any]) -> None:
        # A build's answer is worth a few tries: the app may be redeploying.
        for attempt in range(4):
            try:
                if not client.result(answer):
                    logger.warning("The app no longer waited for job %s (it timed out first)",
                                   answer.get("id"))
                return
            except NotYet as error:
                wait = error.retry_after
            except LinkError as error:
                logger.warning("Posting the answer to job %s failed: %s", answer.get("id"), error)
                wait = RETRY_S
            if attempt < 3:
                threading.Event().wait(wait)
        logger.error("Gave up posting the answer to job %s", answer.get("id"))

    return reply
