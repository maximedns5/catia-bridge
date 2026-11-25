"""Run the whole live test suite on a running CATIA, in one go, and pack the
reports into one zip to send back.

Each live check used to be one command and one report sent by hand. This
script runs them all in order, keeps going after a failure, stops only when
a CATIA dialog blocks the session (every later call would fail the same
way), and writes:

- one report per step (the tree command's report, or a probe's);
- `summary.json`: per step, whether it passed, the measured and expected
  volume and the first line of any error;
- `summary.txt`: the same, short, to read at a glance;
- everything zipped into `--out`.

Steps, in order (each can be skipped):

1. offline, no CATIA: the clash analysis of every assembly fixture;
2. `trees`: every tree fixture in tests/fixtures/trees built with the tree
   command (`--only` keeps the fixtures whose names start with the given
   prefixes, e.g. `--only m10,s`);
3. `probes`: the read-only probes that need a part (properties, drawing);
4. `product` (only with `--product`, after importing the given STEP and
   keeping its window active): the constraints and clash probes and the
   adopt build with drawings and clashes.

It attaches to the running CATIA and never starts one. Nothing is saved.
Every built part stays open in its own window: on 3DEXPERIENCE close the
windows (without saving) between two runs.

Usage (Windows, from the repository folder):
    python scripts/live_suite.py --out live_run.zip
    python scripts/live_suite.py --only m10,s --out live_run.zip
    python scripts/live_suite.py --skip-trees --product tests/fixtures/assembly/bracket.step --out live_product.zip
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

TREES = os.path.join(ROOT, "tests", "fixtures", "trees")
ASSEMBLIES = os.path.join(ROOT, "tests", "fixtures", "assembly")
SCRIPTS = os.path.join(ROOT, "scripts")
# What the tree command prints when a dialog blocks the session: every later
# CATIA call would be refused the same way, so the run stops there.
BLOCKED_MARKS = ("A CATIA dialog is open", "BLOCKED_BY_DIALOG", "still blocked in CATIA",
                 "did not answer")


class _Tee(io.TextIOBase):
    """Writes to the console and keeps a copy for the report."""

    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            with contextlib.suppress(ValueError):  # a stream closed meanwhile
                stream.flush()


def _blocked(text: str) -> bool:
    return any(mark in text for mark in BLOCKED_MARKS)


class Suite:
    def __init__(self, work: str, args: argparse.Namespace) -> None:
        self.work, self.args = work, args
        self.rows: list[dict[str, Any]] = []
        self.stopped: str | None = None

    # ── Steps ──

    def tree_command(self, name: str, argv: list[str], report: str) -> dict[str, Any]:
        """The tree command in this process (as typed by hand), its output kept."""
        from catia_mcp.tree.__main__ import main

        printed = io.StringIO()
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(_Tee(sys.stdout, printed)):
                code = main(argv + ["--report", report])
        except SystemExit as exit_:
            code = int(exit_.code or 0)
        except Exception as error:  # recorded: the next step still runs
            code = -1
            printed.write(f"\n{error!r}\n{traceback.format_exc()}")
        row: dict[str, Any] = {"step": name, "exit_code": code,
                               "elapsed_s": round(time.monotonic() - started, 1)}
        output = printed.getvalue()
        if code != 0:
            row["output"] = output.splitlines()[-25:]
        data = _read(report)
        if data is not None:
            row["report"] = os.path.basename(report)
            row.update(_measures(data))
        if _blocked(output) or (data or {}).get("error", {}).get("code") == "BLOCKED_BY_DIALOG":
            row["blocked"] = True
        return row

    def script(self, name: str, script: str, argv: list[str], report: str) -> dict[str, Any]:
        """A probe script in a child process, so its own argument parsing and
        exit stay its own."""
        started = time.monotonic()
        command = [sys.executable, os.path.join(SCRIPTS, script)] + argv + ["--report", report]
        try:
            done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                  timeout=self.args.step_timeout)
            code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
        except subprocess.TimeoutExpired as error:
            code, output = -2, f"timed out after {error.timeout} s"
        print(output[-2000:])
        row: dict[str, Any] = {"step": name, "exit_code": code,
                               "elapsed_s": round(time.monotonic() - started, 1)}
        if code != 0:
            row["output"] = output.splitlines()[-25:]
        if os.path.exists(report):
            row["report"] = os.path.basename(report)
        if _blocked(output) or _blocked(json.dumps(_read(report) or {})):
            row["blocked"] = True
        return row

    def record(self, row: dict[str, Any]) -> bool:
        """Keep a step's row; False when the run must stop."""
        row["ok"] = row.get("exit_code") == 0 and row.get("success", True) is not False
        self.rows.append(row)
        self.write_summary()
        if row.get("blocked") and not self.args.keep_going:
            self.stopped = (f"stopped after '{row['step']}': a CATIA dialog blocks the session. "
                            "Close it in CATIA (read it first), then run the remaining steps.")
            return False
        return True

    # ── The run ──

    def run(self) -> None:
        if not self.args.skip_offline:
            self.offline()
        if not self.args.skip_trees and self.stopped is None:
            self.trees()
        if not self.args.skip_probes and self.stopped is None:
            self.probes()
        if self.args.product and self.stopped is None:
            self.product()
        self.write_summary()

    def offline(self) -> None:
        for tree in sorted(f for f in os.listdir(ASSEMBLIES) if f.endswith(".tree.json")):
            stem = tree[: -len(".tree.json")]
            step = os.path.join(ASSEMBLIES, f"{stem}.step")
            report = os.path.join(self.work, f"clashes_{stem}.json")
            row = self.tree_command(f"offline clashes {stem}",
                                    ["clashes", os.path.join(ASSEMBLIES, tree), "--step", step,
                                     "--clearance", "0.5"], report)
            data = _read(report) or {}
            row["counts"] = data.get("counts")
            self.record(row)

    def trees(self) -> None:
        prefixes = [p for p in (self.args.only or "").split(",") if p]
        names = sorted(f[:-5] for f in os.listdir(TREES) if f.endswith(".json"))
        if prefixes:
            names = [n for n in names if any(n.startswith(p) for p in prefixes)]
        for name in names:
            report = os.path.join(self.work, f"{name}.json")
            argv = ["build", os.path.join(TREES, f"{name}.json"), "--platform",
                    self.args.platform, "--plm-type", self.args.plm_type]
            if not self.record(self.tree_command(name, argv, report)):
                return

    def probes(self) -> None:
        for name, script, argv in (
                ("probe properties", "probe_properties.py", ["--create"]),
                ("probe drawing", "probe_drawing.py", [])):
            report = os.path.join(self.work, f"{script[:-3]}.json")
            if not self.record(self.script(name, script, argv, report)):
                return

    def product(self) -> None:
        step = os.path.abspath(self.args.product)
        stem = os.path.basename(step).rsplit(".", 1)[0]
        tree = os.path.join(os.path.dirname(step), f"{stem}.tree.json")
        window = ["--window", self.args.window] if self.args.window else []
        for name, script, argv in (
                ("probe constraints", "probe_constraints.py", ["--tree", tree, "--step", step] + window),
                ("probe clashes", "probe_clashes.py", ["--tree", tree, "--step", step] + window)):
            report = os.path.join(self.work, f"{script[:-3]}.json")
            if not self.record(self.script(name, script, argv, report)):
                return
        if os.path.exists(tree):
            report = os.path.join(self.work, f"{stem}_adopt.json")
            self.record(self.tree_command(
                f"{stem} adopt", ["build", tree, "--assembly", "adopt", "--step", step,
                                  "--drawing", "auto", "--clashes", "offline"], report))

    # ── Output ──

    def write_summary(self) -> None:
        passed = sum(1 for r in self.rows if r.get("ok"))
        summary = {"started": self.args.started, "platform": self.args.platform,
                   "steps": len(self.rows), "passed": passed, "stopped": self.stopped,
                   "rows": self.rows}
        with open(os.path.join(self.work, "summary.json"), "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False, default=str)
        lines = [f"{passed}/{len(self.rows)} étapes réussies"]
        if self.stopped:
            lines.append(f"ARRÊT : {self.stopped}")
        for row in self.rows:
            mark = "OK " if row.get("ok") else "ÉCHEC"
            detail = ""
            if row.get("volume_mm3") is not None:
                detail = f"  {row['volume_mm3']:.4f} mm3"
                if row.get("expected_mm3") is not None:
                    detail += f" (attendu {row['expected_mm3']:.4f})"
            if row.get("counts"):
                detail = "  " + ", ".join(f"{k} {v}" for k, v in row["counts"].items())
            if row.get("error"):
                detail += f"  — {row['error']}"
            lines.append(f"{mark} {row['step']}{detail}")
        with open(os.path.join(self.work, "summary.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def pack(self, out: str) -> str:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(os.listdir(self.work)):
                archive.write(os.path.join(self.work, name), name)
        return out


def _read(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _measures(report: dict[str, Any]) -> dict[str, Any]:
    """Success, the measured and expected volumes (summed over parts) and the
    first line of the error, from a tree command report."""
    out: dict[str, Any] = {}
    if "success" in report:
        out["success"] = bool(report["success"])
    parts = report.get("parts_detail") or []
    volumes = [p.get("volume_mm3") for p in parts]
    expected = [p.get("expected_volume_mm3") for p in parts]
    if parts and all(v is not None for v in volumes):
        out["volume_mm3"] = round(sum(volumes), 4)
    if parts and all(v is not None for v in expected):
        out["expected_mm3"] = round(sum(expected), 4)
    error = report.get("error")
    if isinstance(error, dict) and error.get("message"):
        out["error"] = str(error["message"]).splitlines()[0][:300]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="live_run.zip", help="the zip to write")
    parser.add_argument("--only", help="tree fixtures whose names start with these "
                                       "comma-separated prefixes (e.g. m10,s)")
    parser.add_argument("--platform", default="auto", help="auto (default), 3dx or v5")
    parser.add_argument("--plm-type", default="3DShape")
    parser.add_argument("--product", help="the STEP file imported in CATIA, for the product steps")
    parser.add_argument("--window", help="the imported product's window name, if not active")
    parser.add_argument("--skip-offline", action="store_true")
    parser.add_argument("--skip-trees", action="store_true")
    parser.add_argument("--skip-probes", action="store_true")
    parser.add_argument("--keep-going", action="store_true",
                        help="go on after a blocking dialog (later steps will likely fail)")
    parser.add_argument("--step-timeout", type=int, default=900,
                        help="seconds a probe script may run")
    args = parser.parse_args(argv)
    args.started = time.strftime("%Y-%m-%d %H:%M:%S")
    work = tempfile.mkdtemp(prefix="catia_live_")
    suite = Suite(work, args)
    try:
        suite.run()
    except KeyboardInterrupt:
        suite.stopped = "interrupted"
        suite.write_summary()
    out = suite.pack(os.path.abspath(args.out))
    with open(os.path.join(work, "summary.txt"), encoding="utf-8") as handle:
        print("\n" + handle.read())
    print(f"Rapports : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
