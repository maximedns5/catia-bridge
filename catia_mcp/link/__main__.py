"""The link between the app and CATIA, on the CATIA PC.

    python -m catia_mcp.link pair --server URL --code CODE   pair this PC (once)
    python -m catia_mcp.link run                             wait for jobs (leave it open)
    python -m catia_mcp.link status                          the link and CATIA, as seen here
    python -m catia_mcp.link unlink                          forget the token

To try the whole chain on one PC without the app, a reference relay plays
the app's server:

    python -m catia_mcp.link relay                           terminal 1: prints a code
    python -m catia_mcp.link pair --server http://127.0.0.1:8770 --code CODE
    python -m catia_mcp.link run                             terminal 2
    python -m catia_mcp.link send TREE.json                  terminal 3: as the app would
    python -m catia_mcp.link send --ping
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

from catia_mcp.link.config import LOG_NAME, LinkConfig, home


def _logging(verbose: bool = False) -> None:
    folder = home()
    folder.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("catia_mcp")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    to_file = logging.handlers.RotatingFileHandler(folder / LOG_NAME, maxBytes=1_000_000,
                                                   backupCount=3, encoding="utf-8")
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    root.addHandler(to_file)
    root.addHandler(console)


def _pair(args) -> int:
    from catia_mcp.link.client import DeviceClient, LinkError, check_server

    try:
        server = check_server(args.server)
    except ValueError as error:
        print(error)
        return 2
    try:
        token = DeviceClient(server).claim(args.code)
    except LinkError as error:
        print(f"Pairing failed: {error}")
        return 1
    path = LinkConfig(server=server, token=token).save()
    print(f"Paired with {server}. Saved in {path}.")
    print("Now leave this running: python -m catia_mcp.link run")
    return 0


def _run(args) -> int:
    from catia_mcp.link.client import DeviceClient, serve
    from catia_mcp.link.jobs import CatiaJobs, JobRunner

    config = LinkConfig.load()
    if not config.linked:
        print("This PC is not paired yet: python -m catia_mcp.link pair --server URL --code CODE")
        return 2
    _logging(args.verbose)
    log = logging.getLogger("catia_mcp.link")
    log.info("Link to %s; log in %s. Ctrl+C stops it.", config.server, home() / LOG_NAME)
    runner = JobRunner(CatiaJobs(platform=args.platform))

    def moved(server: str) -> None:
        config.server = server
        config.save()

    stop = threading.Event()
    try:
        serve(DeviceClient(config.server, config.token), runner.handle, stop, on_move=moved)
    except KeyboardInterrupt:
        stop.set()
        log.info("Stopped.")
    return 0


def _status(args) -> int:
    config = LinkConfig.load()
    print(f"Folder: {home()}")
    if config.linked:
        print(f"Paired with {config.server} (token saved, not shown).")
    else:
        print("Not paired.")
    from catia_mcp.link.jobs import CatiaJobs

    jobs = CatiaJobs(platform=args.platform)
    ping = jobs.ping({}, 30.0)
    if ping.get("ready"):
        print(f"CATIA: {ping['platform']}, {ping['documents']} document(s) open.")
    else:
        print(f"CATIA: not ready. {ping.get('message', '')}")
    if jobs.worker is not None:
        jobs.worker.shutdown(wait=False)
    return 0


def _unlink(args) -> int:
    config = LinkConfig.load()
    if not config.linked:
        print("Not paired.")
        return 0
    LinkConfig(server=config.server, token="").save()
    print("Token forgotten. The app keeps its side until you unlink it there too.")
    return 0


def _relay(args) -> int:
    from catia_mcp.link.client import _loopback
    from catia_mcp.link.relay import make_server

    if not _loopback(args.host):
        print("The reference relay has no app-side authentication: it only listens on this PC.")
        return 2
    _logging(args.verbose)
    server = make_server(args.host, args.port)
    code = server.RequestHandlerClass.relay.pair()["code"]
    url = f"http://{args.host}:{args.port}"
    print(f"Reference relay on {url} (Ctrl+C stops it).\n"
          f"Pair the bridge (valid 10 minutes):\n"
          f"  python -m catia_mcp.link pair --server {url} --code {code}\n"
          f"then run it in another terminal:  python -m catia_mcp.link run\n"
          f"and send a tree from a third one:  python -m catia_mcp.link send TREE.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _app_call(url: str, method: str, params: dict[str, Any], timeout: float) -> tuple[int, Any]:
    body = json.dumps({"method": method, "params": params, "timeout": timeout}).encode("utf-8")
    request = urllib.request.Request(f"{url.rstrip('/')}/api/catia-link/call", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout + 10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def _send(args) -> int:
    if args.ping:
        method, params = "catia/ping", {}
    else:
        if not args.tree:
            print("send needs a feature tree JSON file, or --ping")
            return 2
        with open(args.tree, encoding="utf-8") as handle:
            tree = json.load(handle)
        method = "catia/check_tree" if args.check else "catia/build_tree"
        params = {"tree": tree, "plm_type": args.plm_type}
        if not args.check:
            from catia_mcp.tree.__main__ import _expected

            expected = _expected(tree, args.expect)
            if expected is not None:
                params["expected"] = expected
    try:
        status, answer = _app_call(args.server, method, params, args.timeout)
    except (urllib.error.URLError, OSError) as error:
        print(f"Cannot reach the relay at {args.server}: {error}")
        return 2
    if status != 200:
        print(f"The relay answered {status}: {answer.get('error')}")
        return 1
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(answer, handle, indent=2, ensure_ascii=False)
    if "error" in answer:
        print(f"The bridge answered an error ({answer.get('error_code')}): {answer['error']}")
        return 1
    result = answer["result"]
    if method == "catia/build_tree":
        if result.get("refused"):
            print(result["message"])
        for part in result.get("parts_detail", []):
            print(f"{part['id']}: volume {part['volume_mm3']} "
                  f"(expected {part['expected_volume_mm3']}), editable {part['editable']}")
        print("Built." if result.get("success") else
              f"Not built: {(result.get('error') or {}).get('message', '')}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False)[:4000])
    print(f"Answer: {args.report}")
    return 0 if result.get("success", True) and not result.get("refused") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m catia_mcp.link",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("pair", help="pair this PC with the app (once)")
    pair.add_argument("--server", required=True, help="the address the app shows")
    pair.add_argument("--code", required=True, help="the pairing code the app shows")
    for name, text in (("run", "wait for jobs"), ("status", "show the link and CATIA")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
        command.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("unlink", help="forget the token")
    relay = sub.add_parser("relay", help="reference relay playing the app's server")
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=8770)
    relay.add_argument("-v", "--verbose", action="store_true")
    send = sub.add_parser("send", help="send a job through the reference relay, as the app would")
    send.add_argument("tree", nargs="?", help="feature tree JSON (version 1)")
    send.add_argument("--ping", action="store_true", help="only ask whether CATIA is ready")
    send.add_argument("--check", action="store_true", help="only check the tree, build nothing")
    send.add_argument("--server", default="http://127.0.0.1:8770")
    send.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"])
    send.add_argument("--expect", default="auto",
                      help="'auto' (build123d), 'none', or a JSON file {part id: {volume_mm3}}")
    send.add_argument("--timeout", type=float, default=300.0)
    send.add_argument("--report", default="link_answer.json")
    args = parser.parse_args(argv)
    return {"pair": _pair, "run": _run, "status": _status, "unlink": _unlink,
            "relay": _relay, "send": _send}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
