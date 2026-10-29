"""Replay a feature tree in CATIA through Automation, one feature at a time.

Design choices, each backed by a live finding on 3DEXPERIENCE R428 or V5:
- One tree part is one new CATIA part (on 3DEXPERIENCE a 3D Shape, since
  creating a 3D Part or a Physical Product needs a licence many roles lack and
  the refusal is a modal dialog that blocks automation).
- Sketch points are projected into the frame CATIA actually gives the sketch
  (read back with GetAbsoluteAxisData), so the build never depends on CATIA
  honouring a requested H/V orientation. The origin planes' frames are the
  build123d convention (confirmed live).
- Every extrude is padded in a body of its own and checked there (volume and
  centre of gravity against the profile's), with one direction flip if CATIA
  extruded the other way; join / cut / intersect then combine that tool body
  into its target with Add / Remove / Intersect. Holes are cuts of cylinders
  (the contract's own hole profiles).
- Nothing is retried: the first failure stops the part and is reported with
  its feature, and the document may then hold a partial model.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from catia_mcp.automation import call_fill_array
from catia_mcp.connection import CATIAConnection
from catia_mcp.measure import Measurer
from catia_mcp.tools.document import DocumentTools, _same
from catia_mcp.tools.part_design import _force_solid_inwork, _read_prop, _set_prop
from catia_mcp.tree.geometry import (
    Arc,
    Frame,
    Vec,
    add,
    cross,
    distance,
    dot,
    prism_expectation,
    profile_area_centroid,
    scale,
    unit,
)
from catia_mcp.tree.preflight import check
from catia_mcp.tree.reference import frame_axes, hole_segments

# Origin planes and the sketch frame CATIA gives them (H, V; normal = H x V).
ORIGIN_PLANES = {
    "PlaneXY": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "PlaneYZ": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "PlaneZX": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
}
COMBINE_METHODS = {"join": "AddNewAdd", "cut": "AddNewRemove", "intersect": "AddNewIntersect"}
CONSTRUCTION_SET = "Tree_Construction"
# Per-feature checks catch a wrong direction or a misread profile; the strict
# comparison is the final per-part volume.
FEATURE_REL_TOL = 1e-3
PART_REL_TOL = 1e-4
EPS = 1e-9


class BuildFailed(Exception):
    def __init__(self, message: str, part: str | None = None, feature: str | None = None) -> None:
        super().__init__(message)
        self.part, self.feature = part, feature


@dataclass
class PartRecord:
    id: str
    name: str
    catia_name: str = ""
    volume_mm3: float | None = None
    expected_volume_mm3: float | None = None
    features: list[dict[str, Any]] = field(default_factory=list)
    editable: bool | None = None
    editability_note: str = ""


def _parallel(a: Vec, b: Vec) -> float:
    """+1 / -1 when the unit vectors are (anti)parallel, 0 otherwise."""
    d = dot(a, b)
    return math.copysign(1.0, d) if abs(abs(d) - 1) < 1e-9 else 0.0


class TreeBuilder:
    def __init__(self, conn: CATIAConnection, plm_type: str = "3DShape",
                 deadline_s: float = 170.0, log: Callable[[str], None] = lambda _: None) -> None:
        self.conn = conn
        self.measure = Measurer(conn)
        self.documents = DocumentTools(conn)
        self.plm_type = plm_type
        self.deadline_s = deadline_s
        self.log = log

    # ── Entry point ──

    def build(self, tree: dict[str, Any], expected: dict[str, dict[str, Any]] | None = None,
              check_editability: bool = True) -> dict[str, Any]:
        check(tree)
        self.started = time.monotonic()
        report: dict[str, Any] = {"success": False, "native_history": False, "editable": None,
                                  "parts": len(tree["parts"]), "features": 0, "parts_detail": []}
        try:
            for part in tree["parts"]:
                record = PartRecord(part["id"], part.get("name", part["id"]))
                report["parts_detail"].append(record)
                self._build_part(part, record)
                if expected and part["id"] in expected:
                    record.expected_volume_mm3 = expected[part["id"]]["volume_mm3"]
                    self._check_part_volume(record)
                if check_editability:
                    self._check_editability(record)
            report["success"] = True
            report["native_history"] = True
            flags = [r.editable for r in report["parts_detail"]]
            report["editable"] = all(flags) if check_editability else None
        except BuildFailed as error:
            report["error"] = {"part": error.part, "feature": error.feature, "message": str(error),
                               "note": "The CATIA document may contain a partial model: check it "
                                       "before trying again."}
        report["features"] = sum(len(r.features) for r in report["parts_detail"])
        report["elapsed_s"] = round(time.monotonic() - self.started, 2)
        report["parts_detail"] = [vars(r) for r in report["parts_detail"]]
        return report

    # ── Parts ──

    def _build_part(self, part: dict[str, Any], record: PartRecord) -> None:
        self.part_id = part["id"]
        self.log(f"part {part['id']}: creating")
        if self.conn.is_3dx:
            self.documents._new_part(record.name, self.plm_type)
        else:
            self.documents._new_part(record.name, "auto")
        self.part = self.conn.get_active_part()
        record.catia_name = str(self.part.Name)
        self.main_body_used = False
        self.bodies: dict[str, Any] = {}
        self.sketches: dict[str, dict[str, Any]] = {}
        self.first_pad: tuple[Any, Any] | None = None
        for feature in part["features"]:
            if time.monotonic() - self.started > self.deadline_s:
                raise BuildFailed(f"the build exceeded its {self.deadline_s:.0f} s budget",
                                  part["id"], feature.get("id"))
            entry = {"id": feature.get("id"), "type": feature["type"], "catia": [], "ok": False}
            record.features.append(entry)
            self.log(f"  {feature['type']} {feature.get('id')}")
            try:
                getattr(self, f"_feature_{feature['type']}")(feature, entry)
            except BuildFailed as error:
                error.part, error.feature = part["id"], feature.get("id")
                raise
            except Exception as error:  # a COM error from CATIA
                raise BuildFailed(f"CATIA rejected the feature: {error}", part["id"],
                                  feature.get("id")) from error
            entry["ok"] = True
        try:
            self.part.Update()
        except Exception as error:
            raise BuildFailed(f"the final part update failed: {error}", part["id"]) from error
        final = [self.bodies[name] for name in (part.get("bodies") or self.bodies)]
        record.volume_mm3 = round(sum(self._volume(body) for body in final), 4)

    def _check_part_volume(self, record: PartRecord) -> None:
        want, got = record.expected_volume_mm3, record.volume_mm3
        if abs(got - want) > max(1e-3, PART_REL_TOL * abs(want)):
            raise BuildFailed(
                f"the part measures {got:.4f} mm3 in CATIA, expected {want:.4f} mm3",
                record.id)

    # ── Features ──

    def _feature_sketch(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        # Drawn when an extrude consumes it, in the body that extrude builds.
        self.sketches[feature["id"]] = feature
        entry["note"] = "drawn with the feature that consumes it"

    def _feature_extrude(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        sketch = self.sketches[feature["sketch"]]
        operation = feature["operation"]
        if operation == "new_body":
            body = self._new_body(use_main=True)
            pad = self._verified_pad(body, sketch, float(feature["distance"]),
                                     bool(feature.get("symmetric")), entry)
            self.bodies[feature["creates"][0]] = body
            if self.first_pad is None:
                # Its body survives to the end, so editing it moves the part.
                self.first_pad = (pad, body)
            return
        (target,) = feature["bodies"]
        tool = self._new_body(use_main=False)
        self._verified_pad(tool, sketch, float(feature["distance"]),
                           bool(feature.get("symmetric")), entry)
        self._combine(self.bodies[target], tool, operation, entry)

    def _feature_combine(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        (target,) = feature["targets"]
        for name in feature["tools"]:
            self._combine(self.bodies[target], self.bodies.pop(name), feature["operation"], entry)

    def _feature_hole(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        (target,) = feature["bodies"]
        for matrix in feature["frames"]:
            origin, x_dir, z_dir = frame_axes(matrix)
            for radius, top, bottom in hole_segments(feature):
                cylinder = {
                    "plane": {"origin": list(add(origin, scale(z_dir, top))),
                              "x_dir": list(x_dir), "normal": list(z_dir)},
                    "entities": [{"type": "circle", "center": [0.0, 0.0], "radius": radius,
                                  "id": "c"}],
                    "profiles": [{"outer": ["c"], "inner": []}],
                }
                tool = self._new_body(use_main=False)
                self._verified_pad(tool, cylinder, -(top - bottom), False, entry)
                self._combine(self.bodies[target], tool, "cut", entry)

    # ── Bodies, pads, combines ──

    def _new_body(self, use_main: bool) -> Any:
        if use_main and not self.main_body_used:
            self.main_body_used = True
            return self.part.MainBody
        body = self.part.Bodies.Add()
        self.part.Update()
        return body

    def _verified_pad(self, body: Any, sketch: dict[str, Any], dist: float, symmetric: bool,
                      entry: dict[str, Any]) -> Any:
        frame = Frame.from_tree(sketch["plane"])
        area, centroid = profile_area_centroid(sketch)
        want_volume, want_cog = prism_expectation(frame, area, centroid, dist, symmetric)
        catia_sketch, along = self._draw(body, sketch, frame)
        part = self.part
        _force_solid_inwork(part, catia_sketch)
        part.InWorkObject = body
        pad = part.ShapeFactory.AddNewPad(catia_sketch, abs(dist))
        entry["catia"].append(str(pad.Name))
        if symmetric:
            if not _set_prop(pad, "IsSymmetric", True):
                raise BuildFailed(f"CATIA refused the symmetric extent of '{pad.Name}'")
        elif dist * along < 0 and not _set_prop(pad, "DirectionOrientation", 1):
            raise BuildFailed(f"CATIA refused the direction of '{pad.Name}'")
        part.UpdateObject(pad)
        volume, cog = self._volume(body), self._cog(body)
        size = math.sqrt(area) + abs(dist)
        tol = max(1e-3, FEATURE_REL_TOL * size)
        if abs(volume - want_volume) > max(1e-3, FEATURE_REL_TOL * want_volume):
            raise BuildFailed(
                f"'{pad.Name}' measures {volume:.4f} mm3, expected {want_volume:.4f} mm3: "
                "the sketch profile was not read as drawn")
        if distance(cog, want_cog) > tol and not symmetric:
            mirrored = add(want_cog, scale(frame.normal, -dist))
            if distance(cog, mirrored) > tol:
                raise BuildFailed(
                    f"'{pad.Name}' has its centre of gravity at {_fmt(cog)}, expected "
                    f"{_fmt(want_cog)}: the sketch is not where the tree puts it")
            current = _read_prop(pad, "DirectionOrientation") or 0
            if not _set_prop(pad, "DirectionOrientation", 1 - current):
                raise BuildFailed(f"CATIA refused to flip the direction of '{pad.Name}'")
            part.UpdateObject(pad)
            cog = self._cog(body)
            if distance(cog, want_cog) > tol:
                raise BuildFailed(f"'{pad.Name}' still points the wrong way after one flip")
            entry.setdefault("notes", []).append(f"{pad.Name}: direction flipped to match the tree")
        return pad

    def _combine(self, target: Any, tool: Any, operation: str, entry: dict[str, Any]) -> None:
        part = self.part
        before_target, before_tool = self._volume(target), self._volume(tool)
        part.InWorkObject = target
        feature = getattr(part.ShapeFactory, COMBINE_METHODS[operation])(tool)
        entry["catia"].append(str(feature.Name))
        part.UpdateObject(feature)
        after = self._volume(target)
        slack = max(1e-3, FEATURE_REL_TOL * (before_target + before_tool))
        bounds = {
            "join": (max(before_target, before_tool) - slack, before_target + before_tool + slack),
            "cut": (before_target - before_tool - slack, before_target + slack),
            "intersect": (-slack, min(before_target, before_tool) + slack),
        }[operation]
        if not bounds[0] <= after <= bounds[1]:
            raise BuildFailed(
                f"'{feature.Name}' left {after:.4f} mm3, outside what a {operation} of "
                f"{before_target:.4f} and {before_tool:.4f} mm3 can give")

    # ── Sketches ──

    def _construction_set(self) -> Any:
        hybrid = self.part.HybridBodies
        for i in range(1, hybrid.Count + 1):
            if str(hybrid.Item(i).Name) == CONSTRUCTION_SET:
                return hybrid.Item(i)
        geoset = hybrid.Add()
        geoset.Name = CONSTRUCTION_SET
        return geoset

    def _support(self, frame: Frame) -> tuple[Any, tuple[Vec, Vec, Vec] | None]:
        """A plane reference for the sketch, with the sketch frame CATIA is
        known to give it (None when it must be read back)."""
        part = self.part
        origin = part.OriginElements
        for attr, (h, v) in ORIGIN_PLANES.items():
            normal = cross(h, v)
            if not _parallel(frame.normal, normal):
                continue
            plane = getattr(origin, attr)
            offset = dot(frame.origin, normal)
            if abs(offset) < EPS:
                return part.CreateReferenceFromObject(plane), ((0.0, 0.0, 0.0), h, v)
            offset_plane = part.HybridShapeFactory.AddNewPlaneOffset(
                part.CreateReferenceFromObject(plane), offset, False)
            self._construction_set().AppendHybridShape(offset_plane)
            part.UpdateObject(offset_plane)
            return part.CreateReferenceFromObject(offset_plane), None
        a, b, c = frame.normal
        plane = part.HybridShapeFactory.AddNewPlaneEquation(a, b, c, dot(frame.normal, frame.origin))
        self._construction_set().AppendHybridShape(plane)
        part.UpdateObject(plane)
        return part.CreateReferenceFromObject(plane), None

    def _sketch_frame(self, sketch: Any, known) -> tuple[Vec, Vec, Vec]:
        if known is not None:
            return known
        data = call_fill_array(self.conn.app, sketch, "GetAbsoluteAxisData", 9)
        return tuple(data[0:3]), unit(tuple(data[3:6])), unit(tuple(data[6:9]))

    def _draw(self, body: Any, sketch: dict[str, Any], frame: Frame) -> tuple[Any, float]:
        """Draw the tree sketch in a new CATIA sketch of `body`. Returns the
        sketch and +1 / -1 for whether its normal agrees with the tree's."""
        ref, known = self._support(frame)
        catia_sketch = body.Sketches.Add(ref)
        origin, h, v = self._sketch_frame(catia_sketch, known)
        along = _parallel(cross(h, v), frame.normal)
        if not along:
            raise BuildFailed("the CATIA sketch is not on the tree's sketch plane")

        def local(uv) -> tuple[float, float]:
            world = add(frame.world(*uv), scale(origin, -1))
            return dot(world, h), dot(world, v)

        factory = catia_sketch.OpenEdition()
        if factory is None:  # V5-6R2020: the first OpenEdition can return None
            catia_sketch.CloseEdition()
            factory = catia_sketch.OpenEdition()
            if factory is None:
                raise BuildFailed("CATIA could not open the sketch for edition")
        for entity in sketch["entities"]:
            kind = entity["type"]
            if kind == "line":
                (u0, v0), (u1, v1) = local(entity["start"]), local(entity["end"])
                factory.CreateLine(u0, v0, u1, v1)
            elif kind == "circle":
                cu, cv = local(entity["center"])
                factory.CreateClosedCircle(cu, cv, float(entity["radius"]))
            else:
                arc = Arc.through(local(entity["start"]), local(entity["mid"]), local(entity["end"]))
                factory.CreateCircle(arc.center[0], arc.center[1], arc.radius, arc.start, arc.end)
        catia_sketch.CloseEdition()
        self.part.UpdateObject(catia_sketch)
        return catia_sketch, along

    # ── Measures and checks ──

    def _ensure_home(self) -> None:
        if not _same(self.conn.get_active_part(), self.part):
            raise BuildFailed("the active CATIA window changed during the build: keep the "
                              "new part's window active until the build ends")

    def _volume(self, body: Any) -> float:
        self._ensure_home()
        return self.measure.volume_mm3(self.part, body)

    def _cog(self, body: Any) -> Vec:
        self._ensure_home()
        return tuple(self.measure.cog_mm(self.part, body))

    def _check_editability(self, record: PartRecord) -> None:
        """Change the first pad's length, check the volume moves, restore it."""
        if self.first_pad is None:
            record.editable, record.editability_note = None, "no pad to edit"
            return
        pad, body = self.first_pad
        try:
            dimension = pad.FirstLimit.Dimension
            original = float(dimension.Value)
            before = self._volume(body)
            dimension.Value = original + 1.0
            self.part.Update()
            moved = self._volume(body)
            dimension.Value = original
            self.part.Update()
            restored = self._volume(body)
        except Exception as error:
            record.editable, record.editability_note = False, f"edit failed: {error}"
            return
        changed = abs(moved - before) > max(1e-3, FEATURE_REL_TOL * before)
        back = abs(restored - before) <= max(1e-3, PART_REL_TOL * before)
        record.editable = changed and back
        record.editability_note = (
            f"{pad.Name} length {original:g} -> {original + 1:g} -> {original:g} mm: volume "
            f"{before:.1f} -> {moved:.1f} -> {restored:.1f} mm3")


def _fmt(p: Vec) -> str:
    return "(" + ", ".join(f"{c:.3f}" for c in p) + ")"
