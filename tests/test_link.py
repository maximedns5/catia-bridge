"""The app link: the bridge's client and job runner against the reference
relay, over real HTTP on the loopback address, with a fake CATIA."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

import catia_mcp.tree.session as session_module
from catia_mcp.link import SERVER_HEADER
from catia_mcp.link.client import DeviceClient, LinkGone, NotYet, check_server, serve
from catia_mcp.link.config import LinkConfig
from catia_mcp.link.jobs import CatiaJobs, JobError, JobRunner, validate
from catia_mcp.link.relay import Relay, digest, make_server


class _Worker:
    def __init__(self):
        self.timeout_s = 180.0

    def run_guarded(self, fn, *args, timeout=None, label=None):
        return fn(*args)

    def shutdown(self, wait=True):
        pass


def _conn(documents=2):
    return SimpleNamespace(connect=lambda: "Connected to running CATIA 3DEXPERIENCE (stub)",
                           app=SimpleNamespace(Documents=SimpleNamespace(Count=documents)),
                           platform_label="CATIA 3DEXPERIENCE", watchdog=None, is_3dx=True)


def _jobs():
    return CatiaJobs(conn_factory=_conn, worker_factory=lambda conn: _Worker())


@pytest.fixture
def link():
    """A relay on an ephemeral port, a paired bridge polling it, and a call()
    that plays the app."""
    relay = Relay(poll_wait_s=0.2)
    server = make_server("127.0.0.1", 0, relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    client = DeviceClient(url)
    client.claim(relay.pair()["code"])
    runner = JobRunner(_jobs())
    stop = threading.Event()
    poller = threading.Thread(target=serve, args=(client, runner.handle, stop), daemon=True)
    poller.start()
    deadline = time.monotonic() + 5
    while not relay.status()["connected"] and time.monotonic() < deadline:
        time.sleep(0.02)

    def call(method, params=None, timeout=10.0):
        return relay.call(method, params or {}, timeout)

    yield SimpleNamespace(relay=relay, client=client, runner=runner, call=call, url=url)
    stop.set()
    poller.join(2)
    server.shutdown()
    server.server_close()


# ── Pairing ──


def test_a_code_pairs_once_and_the_relay_keeps_only_the_token_digest():
    relay = Relay(poll_wait_s=0.1)
    server = make_server("127.0.0.1", 0, relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        code = relay.pair()["code"]
        assert len(code) == 8 and not set(code) & set("01OIL")
        token = DeviceClient(url).claim(code.lower())  # typed by hand: case does not matter
        assert token.startswith("cl1.") and relay.link_digest == digest(token)
        assert token not in json.dumps(vars(relay), default=str)
        with pytest.raises(LinkGone, match="new one"):
            DeviceClient(url).claim(code)  # single use
        with pytest.raises(LinkGone):  # an unknown token sees a route that does not exist
            DeviceClient(url, "cl1.forged").next()
        assert DeviceClient(url, token).next() == (None, None)  # 204 after the hold
        assert relay.status() == {"linked": True, "connected": True, "searching": False,
                                  "pairing": False}
        relay.disconnect()
        with pytest.raises(LinkGone):
            DeviceClient(url, token).next()
    finally:
        server.shutdown()
        server.server_close()


def test_the_server_address_must_be_https_unless_it_is_this_pc():
    assert check_server("https://app.example.com/") == "https://app.example.com"
    assert check_server("http://127.0.0.1:8770") == "http://127.0.0.1:8770"
    assert check_server("http://localhost:8770/") == "http://localhost:8770"
    for bad in ("http://app.example.com", "ftp://x", "app.example.com", "https://x/?a=1"):
        with pytest.raises(ValueError):
            check_server(bad)


def test_the_config_keeps_the_token_out_of_sight(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_LINK_HOME", str(tmp_path))
    assert not LinkConfig.load().linked
    path = LinkConfig(server="https://app.example.com", token="cl1.secret").save()
    assert LinkConfig.load() == LinkConfig("https://app.example.com", "cl1.secret")
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["link.json"]  # no temporary file left


# ── The polling client ──


class _Response(io.BytesIO):
    def __init__(self, status, body=b"", headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(*answers):
    seen = []

    def open_(request, timeout):
        seen.append((request.full_url, dict(request.header_items())))
        answer = answers[min(len(seen), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return open_, seen


def test_the_client_moves_to_the_address_the_app_gives_and_ignores_plain_http():
    open_, seen = _opener(_Response(204, headers={SERVER_HEADER: "https://new.example.com"}))
    client = DeviceClient("https://old.example.com", "cl1.t", opener=open_)
    assert client.next() == (None, "https://new.example.com")
    assert seen[0][0] == "https://old.example.com/api/catia-link/device/next"
    assert seen[0][1]["Authorization"] == "Bearer cl1.t"
    open_, _ = _opener(_Response(204, headers={SERVER_HEADER: "http://evil.example.com"}))
    assert DeviceClient("https://old.example.com", "t", opener=open_).next() == (None, None)


def test_the_loop_waits_on_503_and_404_and_saves_a_move():
    starting = urllib.error.HTTPError("u", 503, "starting", {"Retry-After": "1"}, None)
    gone = urllib.error.HTTPError("u", 404, "gone", {}, None)
    job = json.dumps({"id": "j1", "method": "catia/ping"}).encode()
    open_, seen = _opener(starting, gone,
                          _Response(200, job, {SERVER_HEADER: "https://moved.example.com"}))
    client = DeviceClient("https://app.example.com", "t", opener=open_)
    stop, moves, jobs = threading.Event(), [], []

    class Stop(threading.Event):
        def wait(self, timeout=None):  # no real sleeping in the test
            if len(seen) >= 3:
                stop.set()
            return stop.is_set()

        def is_set(self):
            return stop.is_set()

    serve(client, lambda job, reply: (jobs.append(job), stop.set()), Stop(),
          on_move=moves.append)
    assert jobs == [{"id": "j1", "method": "catia/ping"}]
    assert moves == ["https://moved.example.com"] and client.server == moves[0]
    with pytest.raises(NotYet):
        DeviceClient("https://a.example.com", "t", opener=_opener(starting)[0]).next()


# ── Jobs ──


def test_ping_through_the_relay_says_which_catia_is_ready(link):
    answer = link.call("catia/ping")
    result = answer["result"]
    assert result["catia"] is True and result["ready"] is True
    assert result["platform"] == "CATIA 3DEXPERIENCE" and result["documents"] == 2
    assert result["protocol"] == 1 and "catia/build_tree" in result["methods"]


def test_a_tree_is_built_with_the_volumes_the_app_sent(link, monkeypatch):
    calls = []

    def build(conn, worker, tree, **options):
        calls.append((tree, options))
        return {"success": True, "native_history": True, "editable": True,
                "parts_detail": [{"id": "/P", "volume_mm3": 10.0, "expected_volume_mm3": 10.0}]}

    monkeypatch.setattr(session_module, "build_tree_on", build)
    tree = {"version": 1, "parts": [{"id": "/P", "features": []}]}
    answer = link.call("catia/build_tree", {"tree": tree, "expected": {"/P": {"volume_mm3": 10}}},
                       timeout=60)
    assert answer["result"]["success"] is True
    ((sent, options),) = calls
    assert sent == tree and options["expected"] == {"/P": {"volume_mm3": 10}}
    # The bridge works within the job's timeout (the relay's limit less 2 s),
    # less a margin for the report.
    assert options["deadline_s"] == pytest.approx(60 - 2 - 10)
    assert options["plm_type"] == "3DShape" and options["check_editability"] is True


def test_a_second_job_while_building_is_answered_at_once(link, monkeypatch):
    release, started = threading.Event(), threading.Event()

    def build(conn, worker, tree, **options):
        started.set()
        release.wait(10)
        return {"success": True}

    monkeypatch.setattr(session_module, "build_tree_on", build)
    tree = {"version": 1, "parts": []}
    first = {}
    thread = threading.Thread(target=lambda: first.update(
        link.call("catia/build_tree", {"tree": tree}, timeout=60)))
    thread.start()
    assert started.wait(5)
    try:
        busy = link.call("catia/build_tree", {"tree": tree})
        assert busy["error_code"] == "BUSY" and "catia/build_tree" in busy["error"]
        ping = link.call("catia/ping")["result"]  # answered without touching CATIA
        assert ping["busy"] is True and ping["running"].startswith("catia/build_tree")
    finally:
        release.set()
        thread.join(5)
    assert first["result"] == {"success": True, "notes": [(
        "no expected volumes were sent: each feature was checked, the parts were not "
        "compared with the source model")]}


def test_bad_jobs_are_refused_with_a_reason(link):
    assert link.call("catia/run_script", {"code": "x"})["error_code"] == "UNKNOWN_METHOD"
    answer = link.call("catia/build_tree", {"tree": "not a tree"})
    assert answer["error_code"] == "BAD_REQUEST" and "feature tree" in answer["error"]
    answer = link.call("catia/build_tree", {"tree": {"parts": []}, "plm_type": "Drawing"})
    assert answer["error_code"] == "BAD_REQUEST"
    with pytest.raises(JobError):
        validate({"method": "catia/ping"})  # no id: nothing to answer to
    assert validate({"id": "a", "method": "m", "timeout": 1e9})["timeout"] == 3600.0


def test_catia_not_running_is_its_own_answer(link):
    def closed():
        raise RuntimeError("CATIA is not running. Open CATIA, then try again.")

    link.runner.jobs.conn = SimpleNamespace(connect=closed, watchdog=None)
    ping = link.call("catia/ping")["result"]
    assert ping["catia"] is False and "not running" in ping["message"]
    answer = link.call("catia/capabilities")
    assert answer["error_code"] == "CATIA_NOT_RUNNING"


def test_check_tree_answers_without_catia(link):
    def closed():
        raise RuntimeError("CATIA is not running.")

    link.runner.jobs.conn = SimpleNamespace(connect=closed, watchdog=None)
    tree = {"version": 1, "parts": [{"id": "/P", "features": [
        {"id": "b1", "type": "base_feature", "source": {"line": 7}}]}]}
    result = link.call("catia/check_tree", {"tree": tree})["result"]
    assert result["accepted"] is False and "line 7" in result["message"]
    assert result["note"].startswith("checked without CATIA")


def test_the_relay_refuses_a_call_when_no_bridge_polls_and_ignores_late_answers():
    relay = Relay(poll_wait_s=0.1)
    with pytest.raises(LookupError, match="not connected"):
        relay.call("catia/ping", {}, 1.0)
    assert relay.result({"id": "nobody", "result": {}}) is False


def test_the_command_line_sends_as_the_app_would_and_pairs(link, tmp_path, monkeypatch):
    from catia_mcp.link.__main__ import main

    monkeypatch.setenv("CATIA_LINK_HOME", str(tmp_path))
    report = tmp_path / "answer.json"
    assert main(["send", "--ping", "--server", link.url, "--report", str(report)]) == 0
    assert json.loads(report.read_text())["result"]["ready"] is True
    # Pairing again replaces the fixture bridge's link: last.
    assert main(["pair", "--server", link.url, "--code", link.relay.pair()["code"]]) == 0
    assert LinkConfig.load().server == link.url
    assert LinkConfig.load().token != link.client.token
    assert main(["unlink"]) == 0 and not LinkConfig.load().linked


# ── Installer and background run ──


def test_the_installer_is_filled_in_only_with_checked_values():
    from catia_mcp.link.installer import render

    script = render("https://app.example.com/", "abcd2345")
    assert "$Server = 'https://app.example.com'" in script
    assert "$Code = 'ABCD2345'" in script
    assert "'__" not in script  # every placeholder filled in
    package = "https://github.com/maximedns5/catia-bridge/archive/refs/heads/feat/x.zip"
    assert f"$Package = '{package}'" in render("https://a.example.com", "ABCD2345", package)
    assert "$Package = 'catia-v5-mcp-server==0.3.0'" in render(
        "https://a.example.com", "ABCD2345", "catia-v5-mcp-server==0.3.0")
    for server, code, package in (
            ("https://a.example.com", "ABCD234", None),            # 7 characters
            ("https://a.example.com", "ABCD2340", None),           # 0 is not in the alphabet
            ("https://a.example.com", "ABCD'; x", None),
            ("http://a.example.com", "ABCD2345", None),            # plain HTTP off this PC
            ("https://a.example.com/'+x+'", "ABCD2345", None),
            ("https://a.example.com", "ABCD2345", "https://x/'; rm"),
            ("https://a.example.com", "ABCD2345", "-e ."),
    ):
        with pytest.raises(ValueError):
            render(server, code, *([package] if package else []))


def test_the_relay_serves_the_installer_with_its_own_address(link):
    import urllib.request

    with urllib.request.urlopen(f"{link.url}/catia-link/install.ps1?code=ABCD2345") as answer:
        script = answer.read().decode("utf-8")
    assert f"$Server = '{link.url}'" in script and "$Code = 'ABCD2345'" in script
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(f"{link.url}/catia-link/install.ps1?code=x'")
    assert refused.value.code == 400


def test_only_one_link_runs_at_a_time(tmp_path, monkeypatch):
    from catia_mcp.link.__main__ import main
    from catia_mcp.link.service import InstanceLock, running_pid

    assert running_pid(tmp_path) is None
    first = InstanceLock(tmp_path)
    assert first.acquire()
    try:
        assert not InstanceLock(tmp_path).acquire()
        assert running_pid(tmp_path) == os.getpid()
        monkeypatch.setenv("CATIA_LINK_HOME", str(tmp_path))
        LinkConfig("https://app.example.com", "cl1.t").save(tmp_path)
        monkeypatch.setattr("catia_mcp.link.client.serve",
                            lambda *a, **k: pytest.fail("a second link polled"))
        assert main(["run"]) == 0
    finally:
        first.release()
    assert running_pid(tmp_path) is None and not (tmp_path / "link.pid").exists()


class _Registry:
    """winreg's calls the autostart uses, on a dict."""

    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = "HKCU", 1, 2, 1

    def __init__(self):
        self.values = {}

    def _key(self, root, path, *rest):
        class Key:
            def __enter__(self):
                return (root, path)

            def __exit__(self, *exc):
                return False

        return Key()

    OpenKey = CreateKeyEx = _key

    def QueryValueEx(self, key, name):
        if (*key, name) not in self.values:
            raise FileNotFoundError(name)
        return self.values[(*key, name)], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(*key, name)] = value

    def DeleteValue(self, key, name):
        if self.values.pop((*key, name), None) is None:
            raise FileNotFoundError(name)


def test_autostart_registers_the_windowless_run_for_this_user(monkeypatch, tmp_path):
    from catia_mcp.link import service

    exe = tmp_path / "Scripts dir" / "python.exe"
    exe.parent.mkdir()
    exe.write_text("")
    (exe.parent / "pythonw.exe").write_text("")
    monkeypatch.setattr(service.sys, "executable", str(exe))
    monkeypatch.setattr(service.sys, "platform", "win32")
    registry = _Registry()
    assert service.autostart_enabled(registry) is None
    command = service.enable_autostart(registry)
    assert command == f'"{exe.parent / "pythonw.exe"}" -m catia_mcp.link run'
    assert registry.values == {("HKCU", service.RUN_KEY, "CATIA Bridge Link"): command}
    assert service.autostart_enabled(registry) == command
    assert service.disable_autostart(registry) is True
    assert service.disable_autostart(registry) is False and not registry.values


def test_start_launches_the_run_detached_and_waits_for_its_lock(monkeypatch, tmp_path):
    from catia_mcp.link import service

    monkeypatch.setenv("CATIA_LINK_HOME", str(tmp_path))
    launched, held = [], service.InstanceLock(tmp_path)

    def popen(command, **options):
        launched.append((command, options))
        held.acquire()  # the started process takes the lock

    try:
        assert service.start_background(wait_s=2, popen=popen) == os.getpid()
        ((command, options),) = launched
        assert command[1:] == ["-m", "catia_mcp.link", "run"]
        assert options["stdin"] == options["stdout"] == service.subprocess.DEVNULL
        # Already running: nothing more is launched.
        assert service.start_background(wait_s=2, popen=popen) == os.getpid()
        assert len(launched) == 1
    finally:
        held.release()
