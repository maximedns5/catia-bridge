"""Live probe of the STEP fallback against a running CATIA (V5 or 3DEXPERIENCE).

Checks, on throw-away content, what the offline fakes assume (docs/step.md,
"Live checklist"). Nothing is saved. Documents this probe opens from STEP
files are closed again; the new parts it creates stay open for inspection.

CATIA V5 (needs the STEP interface licence, ST1):
  1. import tests/fixtures/step/fillet_block.step (100 x 60 x 40 mm block,
     R5 vertical edges, 239141.5927 mm3) and record the document type, the
     main body's features, the geometrical sets and the measures;
  2. paste it as a result into a new part, Add it to PartBody, close the STEP
     document, update, and check the volume again (no link);
  3. list the dead bodies;
  4. read the STEP settings controller, if its name resolves;
  5. replay tests/fixtures/step/fillet_block.tree.json with its --step-dir
     (a native pad, then the fillet as a "remove" delta body).

CATIA 3DEXPERIENCE (no scripted import): records whether Documents.Open
exists, prints the manual import steps, and with --adopt TITLE --target
TITLE pastes the hand-imported part into an open part's window.

Usage (Windows, CATIA running; on 3DEXPERIENCE logged in):
    python scripts/step_probe.py
    python scripts/step_probe.py --step C:/tmp/single.step --expect 248566.37
    python scripts/step_probe.py --adopt "fillet_block" --target "Shape1"   (3DEXPERIENCE)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from catia_mcp.automation import com_name, iter_items, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.introspect import type_name
from catia_mcp.step import MANUAL_IMPORT_3DX, StepExchange

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "step")
BLOCK_STEP = os.path.join(FIXTURES, "fillet_block.step")
BLOCK_VOLUME = 239141.5926535898
# Guessed by analogy with the IGES controller (CATIdeIgesSettingCtrl); not
# confirmed anywhere. Record a macro that changes a STEP option to learn it.
STEP_SETTING_CONTROLLERS = ("CATSdeStepSettingCtrl", "CATIdeStepSettingCtrl")
STEP_SETTINGS = ("AttAP", "AttUnits", "AttASM", "AttOptimizeC2", "AttReport", "AttGeoSet")


class Probe:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def step(self, name: str, action) -> Any:
        try:
            value = action()
        except Exception as error:  # record and go on: this is a probe
            self.rows.append({"step": name, "ok": False, "error": f"{type(error).__name__}: "
                                                                   f"{error}"})
            print(f"[FAIL] {name}: {error}")
            traceback.print_exc(limit=2)
            return None
        self.rows.append({"step": name, "ok": True, "result": value})
        print(f"[ OK ] {name}: {json.dumps(value, default=str)[:400]}")
        return value


def v5(conn: CATIAConnection, probe: Probe, args) -> None:
    exchange = StepExchange(conn)
    app = conn.app
    probe.step("DisplayFileAlerts before", lambda: safe_get(app, "DisplayFileAlerts"))
    imported = probe.step("import STEP", lambda: exchange.import_file(args.step, args.expect))
    probe.step("DisplayFileAlerts restored", lambda: safe_get(app, "DisplayFileAlerts"))
    if imported is None:
        return
    probe.step("imported document", lambda: {
        "name": imported.label, "type": type_name(imported.owner),
        "body": com_name(imported.body), "body_type": type_name(imported.body),
        "features": [(com_name(s), type_name(s)) for s in iter_items(imported.body.Shapes)],
        **imported.summary()})
    documents = conn.documents
    target_doc = probe.step("new target part", lambda: com_name(documents.Add("Part")))
    if target_doc is None:
        exchange.close(imported)
        return
    doc = documents.Item(target_doc)
    part = doc.Part
    pasted = probe.step("paste as result", lambda: exchange.paste_as_result(
        imported, part, doc, doc.Activate, "STEP_probe"))
    if pasted is not None:
        body = pasted[0]
        probe.step("add into PartBody", lambda: exchange.combine(
            part, part.MainBody, body, "join", pasted[1]))
    probe.step("close the STEP document", lambda: exchange.close(imported))
    doc.Activate()
    probe.step("update after closing the source (no link)", lambda: (
        part.Update(), exchange.measure.volume_mm3(part, part.MainBody))[1])
    probe.step("dead bodies", lambda: StepExchange.dead_bodies(part))
    probe.step("STEP settings controller", lambda: step_settings(app))
    if not args.no_tree:
        probe.step("tree with a delta body", lambda: tree_with_delta(conn))


def step_settings(app: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name in STEP_SETTING_CONTROLLERS:
        try:
            controller = app.SettingControllers.Item(name)
        except Exception as error:  # this guess does not resolve
            found[name] = f"not found ({error})"
            continue
        found[name] = {attr: safe_get(controller, attr) for attr in STEP_SETTINGS}
    return found


def tree_with_delta(conn: CATIAConnection) -> dict[str, Any]:
    from catia_mcp.tree.builder import TreeBuilder

    with open(os.path.join(FIXTURES, "fillet_block.tree.json"), encoding="utf-8") as handle:
        tree = json.load(handle)
    builder = TreeBuilder(conn, step_dir=os.path.join(FIXTURES, "steps"), log=print)
    report = builder.build(tree, {"/Probe/FilletBlock": {"volume_mm3": BLOCK_VOLUME}})
    return {key: report.get(key) for key in ("success", "native_history", "editable",
                                              "dead_bodies", "error", "parts_detail")}


def three_dx(conn: CATIAConnection, probe: Probe, args) -> None:
    app = conn.app
    probe.step("Documents collection", lambda: {"resolves": safe_get(app, "Documents") is not None})
    probe.step("scripted import", lambda: str(_refusal(conn, args.step)))
    print("\n" + MANUAL_IMPORT_3DX.format(path=os.path.abspath(args.step)) + "\n")
    if not (args.adopt and args.target):
        print("Import it by hand, open it and a target part, then run again with "
              "--adopt TITLE --target TITLE.")
        return
    exchange = StepExchange(conn)
    source = probe.step("adopt", lambda: exchange.adopt(args.adopt, args.expect))
    if source is None:
        return
    probe.step("adopted", source.summary)

    def paste():
        window = exchange.find_window(args.target)
        window.Activate()
        part = conn.get_active_part()
        body, volume, notes = exchange.paste_as_result(source, part, conn.active_editor,
                                                       window.Activate, "STEP_probe")
        combined = exchange.combine(part, part.MainBody, body, "join", volume)
        return {"body": com_name(body), "volume_mm3": volume, "notes": notes,
                "combine": combined, "dead": StepExchange.dead_bodies(part)}

    probe.step("paste as result into the target", paste)


def _refusal(conn: CATIAConnection, path: str) -> str:
    try:
        StepExchange(conn).import_file(path)
    except Exception as error:  # expected: UNSUPPORTED_CAPABILITY
        return f"{type(error).__name__}: {str(error).splitlines()[0]}"
    return "imported (unexpected: update docs/step.md)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--step", default=BLOCK_STEP, help="STEP file to import")
    parser.add_argument("--expect", type=float, default=None,
                        help="expected volume in mm3 (default: the fixture block's)")
    parser.add_argument("--no-tree", action="store_true", help="V5: skip the tree replay")
    parser.add_argument("--adopt", help="3DEXPERIENCE: title of the hand-imported part's window")
    parser.add_argument("--target", help="3DEXPERIENCE: title of the receiving part's window")
    parser.add_argument("--report", default="step_probe.json", help="JSON report path")
    args = parser.parse_args()
    if args.expect is None and os.path.abspath(args.step) == os.path.abspath(BLOCK_STEP):
        args.expect = BLOCK_VOLUME
    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    print(conn.connect())
    probe = Probe()
    probe.step("platform", lambda: {"platform": conn.platform, "caption": safe_get(conn.app,
                                                                                    "Caption")})
    if conn.is_3dx:
        three_dx(conn, probe, args)
    else:
        v5(conn, probe, args)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(probe.rows, handle, indent=2, default=str, ensure_ascii=False)
    print(f"Report: {args.report} ({sum(r['ok'] for r in probe.rows)}/{len(probe.rows)} ok)")
    return 0 if all(r["ok"] for r in probe.rows) else 1


if __name__ == "__main__":
    sys.exit(main())
