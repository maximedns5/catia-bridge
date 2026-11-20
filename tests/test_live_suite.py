"""Offline tests of scripts/live_suite.py: the tree command is replaced by a
fake that writes the reports a live run would."""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location("live_suite", ROOT / "scripts" / "live_suite.py")
live_suite = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_suite)


def _fake_main(outcomes):
    """A tree command that writes, per fixture name, the report it is given."""

    def main(argv):
        report = argv[argv.index("--report") + 1]
        name = Path(argv[1]).stem
        outcome = outcomes.get(name, {"success": True, "volume": 100.0})
        if outcome.get("blocked"):
            print("A CATIA dialog is open: 'Gestion des licences'")
            return 2
        data = {"success": outcome["success"], "parts_detail": [
            {"volume_mm3": outcome["volume"], "expected_volume_mm3": outcome.get("expected", 100.0)}]}
        if not outcome["success"]:
            data["error"] = {"message": "no Draft changed the body\nmore"}
        Path(report).write_text(json.dumps(data), encoding="utf-8")
        return 0 if outcome["success"] else 1

    return main


def _run(tmp_path, monkeypatch, outcomes, *extra):
    from catia_mcp.tree import __main__ as tree_main

    monkeypatch.setattr(tree_main, "main", _fake_main(outcomes))
    out = tmp_path / "run.zip"
    live_suite.main(["--skip-offline", "--skip-probes", "--only", "m10_draft,m10_pad",
                     "--out", str(out), *extra])
    with zipfile.ZipFile(out) as archive:
        summary = json.loads(archive.read("summary.json"))
        text = archive.read("summary.txt").decode("utf-8")
        names = archive.namelist()
    return summary, text, names


def test_every_fixture_is_built_and_summed_up(tmp_path, monkeypatch):
    summary, text, names = _run(tmp_path, monkeypatch,
                                {"m10_draft_two_sides": {"success": False, "volume": 90.0}})
    steps = [row["step"] for row in summary["rows"]]
    assert steps == ["m10_draft_four_sides", "m10_draft_two_sides", "m10_pad_up_to_face",
                     "m10_pad_up_to_next", "m10_pad_up_to_plane"]
    failed = next(r for r in summary["rows"] if r["step"] == "m10_draft_two_sides")
    assert not failed["ok"] and failed["error"] == "no Draft changed the body"
    assert summary["passed"] == 4 and summary["stopped"] is None
    assert "4/5 étapes réussies" in text and "ÉCHEC m10_draft_two_sides" in text
    assert "m10_pad_up_to_face.json" in names


def test_a_blocking_dialog_stops_the_run(tmp_path, monkeypatch):
    summary, text, _ = _run(tmp_path, monkeypatch,
                            {"m10_draft_two_sides": {"blocked": True}})
    assert [row["step"] for row in summary["rows"]] == ["m10_draft_four_sides",
                                                        "m10_draft_two_sides"]
    assert summary["rows"][-1]["blocked"] and "dialog" in summary["stopped"]
    assert "ARRÊT" in text


def test_keep_going_runs_past_a_dialog(tmp_path, monkeypatch):
    summary, _, _ = _run(tmp_path, monkeypatch, {"m10_draft_two_sides": {"blocked": True}},
                         "--keep-going")
    assert len(summary["rows"]) == 5


def test_offline_clashes_run_without_catia(tmp_path):
    pytest.importorskip("OCP")
    out = tmp_path / "offline.zip"
    live_suite.main(["--skip-trees", "--skip-probes", "--out", str(out)])
    with zipfile.ZipFile(out) as archive:
        summary = json.loads(archive.read("summary.json"))
    counts = {row["step"]: row["counts"] for row in summary["rows"]}
    assert counts["offline clashes pin_interference"]["interference"] == 1
    assert counts["offline clashes bracket"] == {"interference": 0, "contact": 1, "clearance": 0}
