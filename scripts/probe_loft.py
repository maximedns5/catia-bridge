"""Live probe of CATIA's Multi-sections Solid (loft) by automation.

The tree replay builds lofts between circles on 3DEXPERIENCE R428 but not
between sections with corners: a closing point given as a 3D point gives a
loft CATIA cannot update, and every later try in the same part is refused.
The first run of this probe showed every rectangle loft failing to update,
whatever the coupling, closing points or orientation, while circles worked:
the rectangles were four lines that only touch. Connected contours (one
sketch point shared by the lines at each corner) are now tried first.

This probe tries each way of building a rectangle-to-rectangle loft, each in
a fresh part so that one failure cannot spoil the next, and records which
one gives the expected solid (20 x 10 mm to 10 x 6 mm, 30 mm apart, corners
paired: 3700 mm3 by the prismatoid formula). A circle-to-circle loft, which
works, is the control.

Each try varies one thing:
- SectionCoupling (left unset, or 1 to 4);
- the closing points (none, passed as VBScript's Nothing; 3D points; points
  drawn in the section sketches; vertices found by a topology search);
- the second section's orientation (1, -1, 2).

It also records the loft's type, the parameters of AddSectionToLoft, and
the type library's enums about lofts. Nothing is saved: the new parts stay
open for inspection.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in; leave CATIA
alone while it runs):
    python scripts/probe_loft.py --report loft_probe.json
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

from catia_mcp.tree.geometry import Frame, add, dot, scale

PRISMATOID_MM3 = 30 / 6 * (20 * 10 + 4 * 15 * 8 + 10 * 6)
FRUSTUM_MM3 = math.pi * 30 / 3 * (10**2 + 10 * 4 + 4**2)


def _plane(z: float) -> dict[str, Any]:
    return {"origin": [0, 0, z], "x_dir": [1, 0, 0], "normal": [0, 0, 1]}


def _rect(z: float, w: float, h: float) -> dict[str, Any]:
    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    entities = [{"type": "line", "start": list(corners[i]), "end": list(corners[(i + 1) % 4]),
                 "id": f"e{i}"} for i in range(4)]
    return {"plane": _plane(z), "entities": entities,
            "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}


def _circle(z: float, r: float) -> dict[str, Any]:
    return {"plane": _plane(z), "entities": [{"type": "circle", "center": [0, 0], "radius": r,
                                              "id": "c"}],
            "profiles": [{"outer": ["c"], "inner": []}]}


SECTIONS = {
    "rect": ((_rect(0, 20, 10), (-10, -5, 0)), (_rect(30, 10, 6), (-5, -3, 30)), PRISMATOID_MM3),
    "circle": ((_circle(0, 10), (10, 0, 0)), (_circle(30, 4), (4, 0, 30)), FRUSTUM_MM3),
}


class Probe:
    def __init__(self, conn: Any, plm_type: str) -> None:
        from catia_mcp.tree.builder import TreeBuilder

        self.conn = conn
        self.plm_type = plm_type
        self.builder = TreeBuilder(conn, plm_type=plm_type, log=print)
        self.count = 0

    def fresh(self, sections: str, connect: bool = False):
        """A new part with the two section sketches in its main body, drawn as
        connected contours (shared corner points) or as touching curves."""
        b = self.builder
        self.count += 1
        name = f"LoftProbe_{self.count:02d}"
        b.documents._new_part(name, self.plm_type if self.conn.is_3dx else "auto")
        b.part = self.conn.get_active_part()
        b.main_body_used, b.bodies = True, {}
        body = b.part.MainBody
        drawn = []
        for sketch, corner in SECTIONS[sections][:2]:
            catia_sketch, _, frame = b._draw(body, sketch, Frame.from_tree(sketch["plane"]),
                                             connect=connect)
            drawn.append((catia_sketch, frame, corner))
        return name, body, drawn

    def point2d(self, catia_sketch: Any, frame, corner) -> Any:
        """A point drawn in the sketch at `corner` (world), and its reference."""
        origin, h, v = frame
        offset = add(corner, scale(origin, -1))
        factory = catia_sketch.OpenEdition()
        point = factory.CreatePoint(dot(offset, h), dot(offset, v))
        catia_sketch.CloseEdition()
        self.builder.part.UpdateObject(catia_sketch)
        return self.builder.part.CreateReferenceFromObject(point)

    def searched_vertex(self, root: Any, corner, record: dict[str, Any], key: str) -> Any:
        from catia_mcp.references import _search_topology

        b = self.builder
        target = b._construction_point(corner)
        sel = self.conn.hso
        try:
            count = _search_topology(sel, root, "Vertex")
            refs = [sel.Item(i).Reference for i in range(1, count + 1)]
        finally:
            with contextlib.suppress(Exception):
                sel.Clear()
        names = [str(getattr(r, "DisplayName", "")) for r in refs]
        record[key] = {"count": len(refs), "names": names[:6]}
        for ref in refs:
            if b.measure.min_distance_mm(ref, target) <= 1e-4:
                return ref
        raise RuntimeError(f"no vertex at {corner} among {len(refs)}")

    def attempt(self, label: str, sections: str = "rect", coupling: int | None = None,
                closing: str = "none", orientation2: int = 1,
                connect: bool = False) -> dict[str, Any]:
        from catia_mcp.introspect import method_params, type_name
        from catia_mcp.tools.part_design import _force_solid_inwork, _read_prop, _set_prop
        from catia_mcp.tree.builder import _add_loft_section

        record: dict[str, Any] = {"label": label, "sections": sections, "coupling": coupling,
                                  "closing": closing, "orientation2": orientation2,
                                  "connected": connect,
                                  "expected_mm3": SECTIONS[sections][2]}
        print(f"  {label}")
        b = self.builder
        try:
            name, body, drawn = self.fresh(sections, connect)
            record["part"] = name
            part = b.part
            if closing == "none":
                closings = [None, None]
            elif closing == "3d point":
                closings = [b._construction_point(c) for _, _, c in drawn]
            elif closing == "sketch point":
                closings = [self.point2d(s, f, c) for s, f, c in drawn]
            elif closing == "vertex from sketch":
                closings = [self.searched_vertex(s, c, record, f"search_{i}")
                            for i, (s, _, c) in enumerate(drawn)]
            elif closing == "vertex from body":
                closings = [self.searched_vertex(body, c, record, f"search_{i}")
                            for i, (_, _, c) in enumerate(drawn)]
            else:
                raise ValueError(closing)
            _force_solid_inwork(part, drawn[0][0])
            part.InWorkObject = body
            loft = part.ShapeFactory.AddNewLoft()
            record["loft_type"] = type_name(loft)
            try:
                shape = loft.HybridShape
            except Exception as error:  # record and use the loft itself
                record["hybrid_shape_error"] = str(error)
                shape = loft
            record["shape_type"] = type_name(shape)
            with contextlib.suppress(Exception):
                record["add_section_params"] = method_params(shape, "AddSectionToLoft")
            record["coupling_before"] = _read_prop(shape, "SectionCoupling")
            if coupling is not None:
                record["coupling_set"] = _set_prop(shape, "SectionCoupling", coupling)
                record["coupling_after"] = _read_prop(shape, "SectionCoupling")
            for (catia_sketch, _, _), closing_ref, sense in zip(drawn, closings,
                                                                (1, orientation2), strict=True):
                _add_loft_section(self.conn.app, shape,
                                  part.CreateReferenceFromObject(catia_sketch), sense, closing_ref)
            record["added"] = True
            part.UpdateObject(loft)
            record["updated"] = True
            volume = b._volume(body)
            record["volume_mm3"] = round(volume, 4)
            record["cog_mm"] = [round(c, 4) for c in b._cog(body)]
            record["ok"] = abs(volume - record["expected_mm3"]) <= 1e-3 * record["expected_mm3"]
        except Exception as error:  # the outcome is the result
            record["error"] = f"{type(error).__name__}: {error}"
        return record

    def facts(self) -> dict[str, Any]:
        from catia_mcp.introspect import find_enum, list_enums

        out: dict[str, Any] = {}
        try:
            objs = [self.builder.part.ShapeFactory, self.builder.part.HybridShapeFactory,
                    self.conn.app]
            names = [n for n in list_enums(objs)
                     if any(w in n.lower() for w in ("loft", "coupling", "orient", "relimit"))]
            out["enums"] = {n: find_enum(objs, n) for n in names}
        except Exception as error:  # record and go on
            out["enums_error"] = str(error)
        return out


ATTEMPTS = [
    {"label": "control: circles, nothing set", "sections": "circle"},
    # Second run: the first found every rectangle loft failing to update
    # whatever was set; connected contours (shared corner points) come first.
    {"label": "connected rect, coupling unset, no closing point", "connect": True},
    {"label": "connected rect, coupling 4, 3D points", "coupling": 4, "closing": "3d point",
     "connect": True},
    {"label": "connected rect, coupling 4, vertex from body", "coupling": 4,
     "closing": "vertex from body", "connect": True},
    {"label": "connected rect, coupling 4, sketch points", "coupling": 4,
     "closing": "sketch point", "connect": True},
    {"label": "connected rect, no closing point, orientation -1", "orientation2": -1,
     "connect": True},
    {"label": "connected rect, no closing point, orientation 2", "orientation2": 2,
     "connect": True},
    {"label": "rect, coupling unset, no closing point"},
    {"label": "rect, coupling 1, no closing point", "coupling": 1},
    {"label": "rect, coupling 2, no closing point", "coupling": 2},
    {"label": "rect, coupling 3, no closing point", "coupling": 3},
    {"label": "rect, coupling 4, no closing point", "coupling": 4},
    {"label": "rect, coupling unset, 3D points", "closing": "3d point"},
    {"label": "rect, coupling 1, 3D points", "coupling": 1, "closing": "3d point"},
    {"label": "rect, coupling unset, sketch points", "closing": "sketch point"},
    {"label": "rect, coupling 4, sketch points", "coupling": 4, "closing": "sketch point"},
    {"label": "rect, coupling unset, vertex from sketch", "closing": "vertex from sketch"},
    {"label": "rect, coupling unset, vertex from body", "closing": "vertex from body"},
    {"label": "rect, no closing point, orientation -1", "orientation2": -1},
    {"label": "rect, no closing point, orientation 2", "orientation2": 2},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--plm-type", default="3DShape", choices=["3DShape", "3DPart"],
                        help="3DEXPERIENCE content type for each part (default 3DShape)")
    parser.add_argument("--report", default="loft_probe.json", help="JSON report path")
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
                              "platform": conn.platform_label, "attempts": []}
    for spec in ATTEMPTS:
        try:
            report["attempts"].append(probe.attempt(**spec))
        except Exception as error:  # keep what was found so far
            report["attempts"].append({"label": spec["label"], "error": str(error),
                                       "traceback": traceback.format_exc()})
        if "facts" not in report and getattr(probe.builder, "part", None) is not None:
            report["facts"] = probe.facts()
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    working = [a["label"] for a in report["attempts"] if a.get("ok")]
    print("Working:", working or "none")
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
