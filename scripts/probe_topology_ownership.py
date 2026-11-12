"""Live probe: which topology cells does CATIA's search return, and whom do they belong to?

Settles what the tree replay's fillets, chamfers and shells rely on but
offline tests cannot establish (docs/feature-tree.md, catia_mcp/tree/dressup.py):

- P1, ownership census. Small parts are built through the tree builder: a
  plain block, a block with a U notch cut by a protruding tool body (Remove),
  a block with a blind pocket (Remove, tool edges coinciding with the
  pocket's) and a block with a protruding boss (Add). For each, the edges,
  faces and vertices are searched from several roots (the body, its last
  shape, the tool body, the tool's pad) and scopes (",sel", ",in" with the
  last shape in work, ",scr"), with the plain and the CGM type names, in the
  session's language. Every cell is recorded with its DisplayName, the result
  context the ownership filter reads from it, and its geometry.
- P2, BRep name round trip. A final edge of the U notch is turned into a
  CreateReferenceFromBRepName label (permanent and temporary body variants)
  and resolved in the last shape's context, and Shapes.GetBoundary is tried
  too. The resolved reference is measured, filleted with r = 2 (expected
  volume change -(1 - pi/4) r^2 L), the fillet deleted; the label is resolved
  again after the first pad is lengthened, and after the Remove is deleted
  (it must then fail).
- P3, geometric fallback. Every edge the body search returns is extracted
  (HybridShapeExtract, no propagation), measured and deleted, and the cells
  are compared with build123d's edges of the same part (when build123d is
  installed): matched, foreign (no build123d edge) and missing.

Everything happens in new, unsaved parts; nothing else is touched. Writes a
JSON report.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in; keep the new
part's window active while it runs):
    python scripts/probe_topology_ownership.py --report topology_probe.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
import traceback
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from catia_mcp.references import topology_languages
from catia_mcp.tree.anchors import AnchorError, match_edge
from catia_mcp.tree.dressup import BREP_FLAGS, canonical_brep_name, context_of

FILLET_RADIUS = 2.0
MAX_MEASURED = 120  # cells measured per search, to bound the run time


def _rect(sid: str, z: float, x0: float, x1: float, y0: float, y1: float) -> dict[str, Any]:
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    entities = [{"type": "line", "start": list(corners[i]), "end": list(corners[(i + 1) % 4]),
                 "id": f"{sid}e{i}"} for i in range(4)]
    return {"type": "sketch", "id": sid, "entities": entities,
            "plane": {"origin": [0, 0, z], "x_dir": [1, 0, 0], "normal": [0, 0, 1]},
            "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}


def _extrude(fid: str, sketch: str, distance: float, operation: str = "new_body") -> dict[str, Any]:
    feature = {"type": "extrude", "id": fid, "sketch": sketch, "distance": distance,
               "operation": operation}
    feature["creates" if operation == "new_body" else "bodies"] = ["Body1"]
    return feature


def case_trees() -> dict[str, dict[str, Any]]:
    block = [_rect("sketch1", 0, -30, 30, -20, 20), _extrude("extrude1", "sketch1", 20)]
    cases = {
        "block": block,
        # 40 x 20 x 50 cutter through the end of the block: a U notch, the
        # cutter reaching x = 40 outside the block.
        "boolean_remove": block + [_rect("sketch2", -15, 10, 40, -10, 10),
                                   _extrude("extrude2", "sketch2", 50, "cut")],
        # Blind pocket: the tool's floor edges are the pocket's.
        "boolean_remove_pocket": block + [_rect("sketch2", 20, -10, 10, -5, 5),
                                          _extrude("extrude2", "sketch2", -8, "cut")],
        # Boss protruding from the top.
        "boolean_add": block + [_rect("sketch2", 20, -10, 10, -5, 5),
                                _extrude("extrude2", "sketch2", 10, "join")],
    }
    return {name: {"version": 1, "parts": [{"id": f"/Probe/{name}", "name": f"Probe_{name}",
                                            "bodies": ["Body1"], "features": features}]}
            for name, features in cases.items()}


class Probe:
    def __init__(self, conn: Any, plm_type: str) -> None:
        from catia_mcp.tree.builder import TreeBuilder

        self.conn = conn
        self.builder = TreeBuilder(conn, plm_type=plm_type, log=print)
        self.dress = self.builder.dressups

    # ── Helpers ──

    def build(self, tree: dict[str, Any]) -> dict[str, Any]:
        report = self.builder.build(tree, None, check_editability=False)
        return {"success": report["success"], "error": report.get("error"),
                "volume_mm3": report["parts_detail"][0]["volume_mm3"]}

    @staticmethod
    def attempt(record: dict[str, Any], key: str, action) -> Any:
        try:
            value = action()
            record[key] = value if isinstance(value, (str, int, float, bool, list, dict,
                                                      type(None))) else str(value)
            return value
        except Exception as error:  # a COM error: record it and go on
            record[f"{key}_error"] = f"{type(error).__name__}: {error}"
            return None

    def geometry(self, kind: str, reference: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if kind == "Vertex":
            self.attempt(out, "point", lambda: self.builder.measure.point_mm(reference))
        elif kind.endswith("Edge"):
            def edge():
                length, start, mid, end, center = self.dress._edge_geometry(reference)
                return {"length": length, "start": list(start), "mid": list(mid),
                        "end": list(end), "center": list(center) if center else None}
            self.attempt(out, "edge", edge)
        else:
            def face():
                area, cog = self.dress._face_geometry(reference)
                return {"area": area, "centroid": list(cog)}
            self.attempt(out, "face", face)
        return out

    def search(self, root: Any, kind: str, scope: str, measure: bool = True) -> dict[str, Any]:
        """One Selection.Search: every language variant until one works."""
        sel = self.conn.hso
        record: dict[str, Any] = {"kind": kind, "scope": scope, "root": _name(root)}
        type_names = {"CGMEdge": "CGMEdge", "CGMFace": "CGMFace"}
        tried = []
        for language, names in topology_languages():
            query = f"{names['keyword']}.{names.get(kind) or type_names[kind]},{scope}"
            if query in tried:
                continue
            tried.append(query)
            sel.Clear()
            sel.Add(root)
            try:
                sel.Search(query)
            except Exception as error:  # a rejected query: try the next language
                record.setdefault("rejected", []).append(f"{query}: {error}")
                continue
            record["query"], record["count"] = query, int(sel.Count)
            items = []
            for index in range(1, int(sel.Count) + 1):
                item: dict[str, Any] = {"index": index}
                selected = sel.Item(index)
                self.attempt(item, "type", lambda s=selected: str(s.Type))
                self.attempt(item, "value_name", lambda s=selected: str(s.Value.Name))
                name = self.attempt(item, "display_name",
                                    lambda s=selected: str(s.Reference.DisplayName))
                item["context"] = context_of(name or "")
                item["reference"] = selected.Reference
                items.append(item)
            sel.Clear()
            for item in items:
                reference = item.pop("reference")
                if measure and item["index"] <= MAX_MEASURED:
                    item.update(self.geometry(kind, reference))
            record["items"] = items
            record["contexts"] = _census(items)
            break
        return record

    # ── P1 ──

    def census(self, name: str) -> dict[str, Any]:
        part = self.builder.part
        main = self.builder.bodies["Body1"]
        out: dict[str, Any] = {"bodies": [], "searches": []}
        roots = [("body", main)]
        last = self.dress._context_feature(main)
        out["last_feature"] = self.dress.last_feature_name(main)
        if last is not main:
            roots.append(("last_shape", last))
        for body in _items(part.Bodies):
            record = {"name": _name(body)}
            self.attempt(record, "shapes", lambda b=body: [_name(s) for s in _items(b.Shapes)])
            self.attempt(record, "shown", lambda b=body: self.shown(b))
            out["bodies"].append(record)
            if body is not main and _name(body) != _name(main):
                roots.append((f"tool_body {_name(body)}", body))
                with contextlib.suppress(Exception):
                    roots.append((f"tool_pad {_name(body)}", body.Shapes.Item(1)))
        for label, root in roots:
            for kind in ("Vertex", "Edge", "Face", "CGMEdge", "CGMFace"):
                record = self.search(root, kind, "sel", measure=kind in ("Vertex", "Edge")
                                     or label == "body")
                record["root_label"] = label
                out["searches"].append(record)
        # The ",in" scope follows the object in work; ",scr" what is on screen.
        with contextlib.suppress(Exception):
            part.InWorkObject = last
        for scope in ("in", "scr"):
            for kind in ("Vertex", "Edge"):
                record = self.search(main, kind, scope)
                record["root_label"] = f"body, scope {scope}"
                out["searches"].append(record)
        return out

    def shown(self, body: Any) -> Any:
        from catia_mcp.automation import call_out_values

        sel = self.conn.hso
        sel.Clear()
        sel.Add(body)
        try:
            return call_out_values(self.conn.app, sel.VisProperties, "GetShow", 1)[0]
        finally:
            sel.Clear()

    # ── P2 ──

    def round_trip(self) -> dict[str, Any]:
        part, main = self.builder.part, self.builder.bodies["Body1"]
        out: dict[str, Any] = {}
        last = self.dress._context_feature(main)
        last_name = self.dress.last_feature_name(main)
        cells = [c for c in self.dress.cells(main, "Edge") if c.context == last_name]
        out["owned_edges"] = len(cells)
        # The notch's top edge at x = 10: from (10, -10, 20) to (10, 10, 20).
        anchor = {"type": "edge", "geometry": "line", "length": 20.0,
                  "samples": [[10.0, y, 20.0] for y in (-6.0, 1.0, 4.0)]}
        candidates = []
        for cell in cells:
            with contextlib.suppress(Exception):
                candidates.append(self.dress.edge_candidate(cell.reference, cell))
        try:
            cell = match_edge(anchor, candidates).handle
        except AnchorError as error:
            out["error"] = f"notch edge not found among owned edges: {error}"
            return out
        out["display_name"] = cell.display_name
        label = canonical_brep_name(cell.display_name)
        out["label"] = label
        resolved = None
        variants = {}
        if label:
            variants["permanent"] = label
            variants["temporary"] = label.replace(";WithPermanentBody;", ";WithTemporaryBody;")
        for variant, text in variants.items():
            record: dict[str, Any] = {"label": text}
            ref = self.attempt(record, "reference",
                               lambda t=text: part.CreateReferenceFromBRepName(t, last))
            if ref is not None:
                record.update(self.geometry("Edge", ref))
                record["fits"] = self._fits(anchor, ref)
                resolved = resolved or (ref if record["fits"] else None)
            out[f"brep_{variant}"] = record
        for variant, text in (("display_name", cell.display_name),
                              ("without_prefix", cell.display_name.removeprefix("Selection_"))):
            record = {"label": text}
            ref = self.attempt(record, "reference", lambda t=text: main.Shapes.GetBoundary(t))
            if ref is not None:
                record.update(self.geometry("Edge", ref))
            out[f"get_boundary_{variant}"] = record
        reference = resolved or cell.reference
        out["filleted_with"] = "brep_name" if resolved is not None else "selection"
        out["fillet"] = self.fillet(main, reference, 20.0)
        out["after_pad_edit"] = self.after_pad_edit(variants.get("permanent"), last, anchor)
        out["after_remove_deleted"] = self.after_delete(variants.get("permanent"), last)
        return out

    def _fits(self, anchor: dict[str, Any], reference: Any) -> bool:
        try:
            match_edge(anchor, [self.dress.edge_candidate(reference)])
            return True
        except Exception:  # a COM error or an edge that does not fit
            return False

    def fillet(self, body: Any, reference: Any, length: float) -> dict[str, Any]:
        part = self.builder.part
        record: dict[str, Any] = {"radius": FILLET_RADIUS,
                                  "expected_change_mm3": -(1 - math.pi / 4) * FILLET_RADIUS ** 2
                                  * length}
        before = self.builder._volume(body)
        feature = None
        try:
            part.InWorkObject = body
            feature = part.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius(
                reference, self.dress._enum("CatFilletEdgePropagation", "tangency"), FILLET_RADIUS)
            part.UpdateObject(feature)
            record["change_mm3"] = self.builder._volume(body) - before
            record["ok"] = abs(record["change_mm3"] - record["expected_change_mm3"]) <= 1e-3 * max(
                1.0, abs(record["expected_change_mm3"]))
        except Exception as error:  # record the refusal
            record["error"] = f"{type(error).__name__}: {error}"
        if feature is not None:
            self.attempt(record, "deleted", lambda: self.builder._delete(feature) or True)
        return record

    def after_pad_edit(self, label: str | None, context: Any, anchor: dict[str, Any]) -> Any:
        if not label or self.builder.first_pad is None:
            return {"skipped": "no label or no pad"}
        pad, _ = self.builder.first_pad
        record: dict[str, Any] = {}
        dimension = pad.FirstLimit.Dimension
        original = float(dimension.Value)
        try:
            dimension.Value = original + 5.0
            self.builder.part.Update()
            ref = self.attempt(record, "reference",
                               lambda: self.builder.part.CreateReferenceFromBRepName(label, context))
            if ref is not None:
                record.update(self.geometry("Edge", ref))
                moved = dict(anchor, samples=[[x, y, z + 5.0] for x, y, z in anchor["samples"]])
                record["fits_moved_edge"] = self._fits(moved, ref)
        finally:
            dimension.Value = original
            self.builder.part.Update()
        return record

    def after_delete(self, label: str | None, context: Any) -> Any:
        if not label:
            return {"skipped": "no label"}
        record: dict[str, Any] = {}
        name = _name(context)
        self.attempt(record, "deleted", lambda: self.builder._delete(context) or name)
        main = self.builder.bodies["Body1"]
        ref = self.attempt(record, "reference", lambda: self.builder.part.CreateReferenceFromBRepName(
            label, self.dress._context_feature(main)))
        record["resolved_after_delete"] = ref is not None
        if ref is not None:
            record.update(self.geometry("Edge", ref))
        return record

    # ── P3 ──

    def extracts(self, tree: dict[str, Any]) -> dict[str, Any]:
        part, main = self.builder.part, self.builder.bodies["Body1"]
        hsf = part.HybridShapeFactory
        out: dict[str, Any] = {"cells": []}
        cells = self.dress.cells(main, "Edge")
        candidates = []
        for cell in cells[:MAX_MEASURED]:
            record: dict[str, Any] = {"index": cell.index, "context": cell.context}
            with contextlib.suppress(Exception):
                candidates.append(self.dress.edge_candidate(cell.reference, cell))

            def extract(cell=cell):
                created = hsf.AddNewExtract(cell.reference)
                with contextlib.suppress(Exception):
                    created.PropagationType = 3  # none
                self.builder._construction_set().AppendHybridShape(created)
                part.UpdateObject(created)
                try:
                    length, *_ = self.dress._edge_geometry(part.CreateReferenceFromObject(created))
                finally:
                    self.builder._delete(created)
                return length

            self.attempt(record, "extract_length", extract)
            out["cells"].append(record)
        out["comparison"] = compare_with_reference(tree, candidates)
        return out


def compare_with_reference(tree: dict[str, Any], candidates: list[Any]) -> dict[str, Any]:
    """Which CATIA edges are build123d's edges of the part (matched), which
    are not (foreign) and which build123d edges CATIA did not return."""
    try:
        from catia_mcp.tree.reference import build_part
    except ImportError:
        return {"skipped": "build123d is not installed"}
    solid = build_part(tree["parts"][0])
    matched, missing = [], []
    for edge in solid.edges():
        anchor = {"length": float(edge.length),
                  "samples": [list(edge.position_at(t)) for t in (0.2, 0.45, 0.7)]}
        try:
            cell = match_edge(anchor, candidates).handle
            matched.append({"index": cell.index, "context": cell.context})
        except AnchorError as error:
            missing.append({"length": anchor["length"], "samples": anchor["samples"],
                            "why": str(error)})
    used = {m["index"] for m in matched}
    foreign = [{"index": c.handle.index, "context": c.handle.context, "length": c.length}
               for c in candidates if c.handle.index not in used]
    return {"reference_edges": len(solid.edges()), "matched": matched, "foreign": foreign,
            "missing": missing}


def _items(collection: Any) -> list[Any]:
    return [collection.Item(i) for i in range(1, int(collection.Count) + 1)]


def _name(obj: Any) -> str:
    try:
        return str(obj.Name)
    except Exception:  # not a named object
        return "?"


def _census(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = item.get("context") or "<no context>"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"],
                        help="3DEXPERIENCE content type for each part (default 3DShape)")
    parser.add_argument("--cases", nargs="*", default=list(case_trees()),
                        help="cases to run (default: all)")
    parser.add_argument("--report", default="topology_probe.json", help="JSON report path")
    args = parser.parse_args()

    from catia_mcp.connection import CATIAConnection

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    try:
        print(conn.connect())
    except RuntimeError as error:
        print(error)
        return 2
    probe = Probe(conn, args.plm_type)
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "platform": conn.platform_label, "brep_flags": BREP_FLAGS,
                              "cases": {}}
    trees = case_trees()
    for name in args.cases:
        print(f"== {name}")
        case: dict[str, Any] = {}
        report["cases"][name] = case
        try:
            case["build"] = probe.build(trees[name])
            if not case["build"]["success"]:
                continue
            case["p1_census"] = probe.census(name)
            case["p3_extracts"] = probe.extracts(trees[name])
            if name == "boolean_remove":
                case["p2_round_trip"] = probe.round_trip()
        except Exception as error:  # keep what was found so far
            case["error"] = f"{type(error).__name__}: {error}"
            case["traceback"] = traceback.format_exc()
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
