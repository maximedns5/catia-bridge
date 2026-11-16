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
  (the contract's own hole profiles); countersinks are revolved profiles.
- Revolves are Shafts in a body of their own, checked by Pappus (volume and
  centre of gravity, which also tells the turning direction for a partial
  angle). Mirrors joined straight back are CATIA Mirrors (checked by the
  centre of gravity landing on the plane); splits are checked by which side
  of the plane the centre of gravity ends on, with one flip.
- Lofts are Multi-sections Solids between sections redrawn with the vertices
  build123d pairs them by (catia_mcp/tree/loft.py), coupled vertex to vertex
  from matching closing points, and checked against the ruled solid's volume
  and centre of gravity; a twisted result is deleted and rebuilt once with the
  second section's orientation reversed. Sweeps are Ribs along the path drawn
  as a sketch in its own plane, checked by Pappus.
- Tapered extrudes are built as build123d builds them: each loop lofted
  (with the verified loft above) to its 2D offset, inner loops cut, pieces
  joined (catia_mcp/tree/extrude.py), checked by the prismatoid formula.
  Oblique extrudes are Pads along a 3D line, checked like pads; whether
  CATIA measures their length along the line or the normal is read from the
  first volume. A placed part is moved into world coordinates first
  (catia_mcp/tree/placement.py).
- Fillets, chamfers and shells find their edges and faces by the tree's
  geometric anchors among the cells of the body's last feature
  (catia_mcp/tree/dressup.py), one CATIA feature per tree feature, checked by
  bounds on the volume change; an asymmetric chamfer is oriented by its centre
  of gravity, and its part's centre of gravity is compared with the reference.
- Spline sketch entities (exact rational NURBS, catia_mcp/tree/nurbs.py) are
  drawn exactly when they are circles or ellipses, else as sketch splines
  through sampled points: the feature's notes say so, and its volume checks,
  and the part's, allow the area between the curve and those points' chords.
- With --step-dir, a feature with no native rule (base_feature, or any
  unsupported feature modifying one body) is built from STEP files: each is
  imported (V5 only), pasted as a body without history, and added to or
  removed from its target (catia_mcp/step.py, catia_mcp/tree/step_source.py).
  The report lists those dead bodies apart from the native features.
- Nothing else is retried: the first failure stops the part and is reported
  with its feature, and the document may then hold a partial model.
"""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from catia_mcp.automation import call_fill_array, evaluate
from catia_mcp.connection import CATIAConnection
from catia_mcp.introspect import find_enum
from catia_mcp.measure import Measurer
from catia_mcp.step import DEAD_BODY_PREFIX, StepExchange
from catia_mcp.tools.document import DocumentTools, _same
from catia_mcp.tools.part_design import (
    _force_solid_inwork,
    _read_prop,
    _set_prop,
    _sketch_v_direction,
)
from catia_mcp.tree.dressup import DressUpFailed, DressUps
from catia_mcp.tree.extrude import TaperPlan, extrude_vector, oblique_expectation, taper_plan
from catia_mcp.tree.geometry import (
    Arc,
    Frame,
    Vec,
    add,
    approximation_area,
    cross,
    distance,
    dot,
    frame_axes,
    hole_profile_sketch,
    hole_segments,
    prism_expectation,
    profile_area_centroid,
    revolve_expectation,
    scale,
    unit,
)
from catia_mcp.tree.loft import LoftPlan, plan_loft, section_sketch
from catia_mcp.tree.nurbs import Nurbs, drawing
from catia_mcp.tree.parameters import ParameterPlan
from catia_mcp.tree.placement import place_part
from catia_mcp.tree.preflight import SUPPORTED, check
from catia_mcp.tree.step_source import StepSource
from catia_mcp.tree.sweep import path_sketch, sweep_expectation

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
# Hole.Type (CatHoleType), Hole.BottomType, Limit.LimitMode (CatLimitMode),
# Hole.CounterSunkMode and Hole.AnchorMode, from the Automation help.
HOLE_TYPES = {"simple": 0, "counterbore": 2, "countersink": 3}
HOLE_FLAT_BOTTOM = 0
LIMIT_DIMENSION, LIMIT_UP_TO_LAST = 0, 2
COUNTERSUNK_DEPTH_ANGLE = 0
HOLE_ANCHOR_EXTREME = 0


@dataclass
class HoleSpec:
    type: int
    diameter: float
    depth: float | None  # None: up to last (a through hole)
    head_diameter: float | None = None
    head_depth: float | None = None
    head_angle: float | None = None


def hole_spec(feature: dict[str, Any]) -> HoleSpec:
    """CATIA's Hole values for a tree hole (build123d's Hole, CounterBoreHole
    or CounterSinkHole). A countersink is given by its depth and angle (the
    documented mode): depth = (D_cs - D) / 2 / tan(angle / 2)."""
    kind = feature["kind"]
    depth = None if feature.get("depth") is None else float(feature["depth"])
    spec = HoleSpec(HOLE_TYPES[kind], float(feature["diameter"]), depth)
    if kind == "counterbore":
        spec.head_diameter = float(feature["counterbore_diameter"])
        spec.head_depth = float(feature["counterbore_depth"])
    elif kind == "countersink":
        angle = float(feature["countersink_angle"])
        spec.head_angle = angle
        spec.head_depth = (float(feature["countersink_diameter"]) - spec.diameter) / 2 \
            / math.tan(math.radians(angle) / 2)
    return spec


# How features are built. "human": a join or cut goes straight into its body
# (Pad, Pocket, Shaft, Groove), as a designer would model it, and is checked
# against build123d's body after that feature (the reference's per-feature
# snapshots). "exact": every solid is built in a tool body of its own, checked
# against a closed form, then combined. A part with no snapshots (build123d
# not installed, or no expected volumes) is built "exact".
STYLES = ("human", "exact")
# HybridShapeLoft.SectionCoupling: 1 ratio, 2 tangency, 3 tangency then
# curvature, 4 vertices.
LOFT_VERTEX_COUPLING = 4
# How a loft section's closing point is given, in the order tried: a 3D
# point on the section's vertex (live on R428 the only way that builds a loft
# between sections with corners, with connected contours and vertex
# coupling), then CATIA's own choice. A sketch vertex found by a topology
# search is refused, and a point drawn in the sketch fails to update.
LOFT_CLOSING_MODES = ("point", "automatic")
# The second section's orientation: as drawn, then reversed (on R428, 2
# builds the same loft as 1).
LOFT_ORIENTATIONS = (1, -1)
_LOFT_SECTION_VBS = (
    "Public Function catia_mcp_loft_section(o, r, s)\n"
    "    o.AddSectionToLoft r, s, Nothing\n"
    "    catia_mcp_loft_section = 0\n"
    "End Function\n"
)


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
    dead_bodies: list[dict[str, Any]] = field(default_factory=list)
    # What approximated splines may move the volume by (mm3), added to the
    # part check's tolerance.
    spline_allowance_mm3: float = 0.0
    # "human" when build123d's per-feature snapshots drove the build.
    style: str = "exact"
    # The user parameters made from the script's numbers, and why none were.
    parameters: list[dict[str, Any]] = field(default_factory=list)
    parameters_note: str = ""


def _parallel(a: Vec, b: Vec) -> float:
    """+1 / -1 when the unit vectors are (anti)parallel, 0 otherwise."""
    d = dot(a, b)
    return math.copysign(1.0, d) if abs(abs(d) - 1) < 1e-9 else 0.0


class HumanMismatch(BuildFailed):
    """A human-style feature that does not change its body as build123d's did,
    either way round. `items` are the CATIA features it made."""

    def __init__(self, message: str, items: list[Any]) -> None:
        super().__init__(message)
        self.items = items


class TreeBuilder:
    def __init__(self, conn: CATIAConnection, plm_type: str = "3DShape",
                 deadline_s: float = 170.0, log: Callable[[str], None] = lambda _: None,
                 ownership: str | None = None, step_dir: str | None = None,
                 style: str = "human") -> None:
        if style not in STYLES:
            raise ValueError(f"style is one of {', '.join(STYLES)}")
        self.conn = conn
        self.style = style
        self.step_dir = step_dir
        self.step_source: StepSource | None = None
        self.measure = Measurer(conn)
        self.documents = DocumentTools(conn)
        self.plm_type = plm_type
        self.deadline_s = deadline_s
        self.log = log
        self.dressups = DressUps(self, ownership)
        self.cog_checked = False

    # ── Entry point ──

    def build(self, tree: dict[str, Any], expected: dict[str, dict[str, Any]] | None = None,
              check_editability: bool = True) -> dict[str, Any]:
        if self.step_dir:
            self.step_source = StepSource(self.step_dir, "3dx" if self.conn.is_3dx else "v5")
        check(tree, step=self.step_source)
        self.started = time.monotonic()
        report: dict[str, Any] = {"success": False, "native_history": False, "editable": None,
                                  "parts": len(tree["parts"]), "features": 0, "parts_detail": []}
        # Read by the tree command when a dialog blocks the build mid-call.
        self.report, self.feature_id = report, None
        try:
            for part in tree["parts"]:
                part = place_part(part)  # features in world coordinates
                record = PartRecord(part["id"], part.get("name", part["id"]))
                report["parts_detail"].append(record)
                self._build_part(part, record, (expected or {}).get(part["id"]))
                if expected and part["id"] in expected:
                    record.expected_volume_mm3 = expected[part["id"]]["volume_mm3"]
                    self._check_part_volume(record)
                    if self.cog_checked and expected[part["id"]].get("cog_mm"):
                        self._check_part_cog(part, expected[part["id"]]["cog_mm"])
                if check_editability:
                    self._check_editability(record)
            report["success"] = True
            dead = any(r.dead_bodies for r in report["parts_detail"])
            report["native_history"] = "partial" if dead else True
            flags = [r.editable for r in report["parts_detail"]]
            report["editable"] = all(flags) if check_editability else None
        except BuildFailed as error:
            report["error"] = {"part": error.part, "feature": error.feature, "message": str(error),
                               "note": "The CATIA document may contain a partial model: check it "
                                       "before trying again."}
        report["features"] = sum(len(r.features) for r in report["parts_detail"])
        report["dead_bodies"] = [dict(d, part=r.id) for r in report["parts_detail"]
                                 for d in r.dead_bodies]
        report["elapsed_s"] = round(time.monotonic() - self.started, 2)
        report["parts_detail"] = [vars(r) for r in report["parts_detail"]]
        return report

    # ── Parts ──

    def _build_part(self, part: dict[str, Any], record: PartRecord,
                    expected: dict[str, Any] | None = None) -> None:
        self.part_id, self.feature_id = part["id"], None
        self.record = record
        # build123d's bodies after each feature, for the human style's checks.
        snapshots = (expected or {}).get("features") if self.style == "human" else None
        self.snapshots: dict[str, dict[str, Any]] = snapshots or {}
        self.expected_bodies: dict[str, dict[str, Any]] = {}
        record.style = "human" if self.snapshots else "exact"
        self.plan = ParameterPlan()
        self.edit_parameter = None
        self.first_pad_feature = None
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
        self.pending_mirror: dict[str, Any] | None = None
        self.cog_checked = False
        for feature in part["features"]:
            if time.monotonic() - self.started > self.deadline_s:
                raise BuildFailed(f"the build exceeded its {self.deadline_s:.0f} s budget",
                                  part["id"], feature.get("id"))
            entry = {"id": feature.get("id"), "type": feature["type"], "catia": [], "ok": False}
            self.feature_id = feature.get("id")
            record.features.append(entry)
            self.log(f"  {feature['type']} {feature.get('id')}")
            try:
                kind = feature["type"] if feature["type"] in SUPPORTED else "from_step"
                getattr(self, f"_feature_{kind}")(feature, entry)
            except BuildFailed as error:
                error.part, error.feature = part["id"], feature.get("id")
                raise
            except Exception as error:  # a COM error from CATIA
                raise BuildFailed(f"CATIA rejected the feature: {error}", part["id"],
                                  feature.get("id")) from error
            entry["ok"] = True
            if feature.get("id") in self.snapshots:
                self.expected_bodies = self.snapshots[feature["id"]]
        try:
            self.part.Update()
        except Exception as error:
            raise BuildFailed(f"the final part update failed: {error}", part["id"]) from error
        final = [self.bodies[name] for name in (part.get("bodies") or self.bodies)]
        record.volume_mm3 = round(sum(self._volume(body) for body in final), 4)
        if self.style == "human" and self.plan:
            self._parametrize(record, final)

    def _check_part_volume(self, record: PartRecord) -> None:
        want, got = record.expected_volume_mm3, record.volume_mm3
        if abs(got - want) > max(1e-3, PART_REL_TOL * abs(want)) + record.spline_allowance_mm3:
            raise BuildFailed(
                f"the part measures {got:.4f} mm3 in CATIA, expected {want:.4f} mm3",
                record.id)

    def _check_part_cog(self, part: dict[str, Any], want: list[float]) -> None:
        """An asymmetric chamfer can be swapped without changing the volume:
        compare the centre of gravity too."""
        final = [self.bodies[name] for name in (part.get("bodies") or self.bodies)]
        masses = [(self._volume(body), self._cog(body)) for body in final]
        total = sum(volume for volume, _ in masses)
        cog = tuple(sum(volume * c[i] for volume, c in masses) / total for i in range(3))
        tol = max(1e-3, PART_REL_TOL * total ** (1 / 3))
        if distance(cog, tuple(want)) > tol:
            raise BuildFailed(f"the part's centre of gravity is at {_fmt(cog)} in CATIA, "
                              f"expected {_fmt(tuple(want))}", part["id"])

    # ── Features ──

    def _feature_sketch(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        # Drawn when an extrude consumes it, in the body that extrude builds.
        self.sketches[feature["id"]] = feature
        entry["note"] = "drawn with the feature that consumes it"

    def _feature_extrude(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        sketch = self.sketches[feature["sketch"]]
        symmetric = bool(feature.get("symmetric"))
        if feature.get("taper"):
            plan = taper_plan(sketch, feature)
            self._into_body(feature, entry, lambda body: self._verified_taper(body, plan, entry))
            return
        if feature.get("direction") is not None:
            vector = extrude_vector(feature, Frame.from_tree(sketch["plane"]))

            def make(body):
                return self._verified_oblique_pad(body, sketch, vector, symmetric, entry)
        else:
            dist = float(feature["distance"])
            # CATIA's lengths are positive, the direction is set apart: the
            # script's signed number drives the length times its sign.
            scale_ = 1.0 if dist >= 0 else -1.0
            centred = self._centred_box(feature, sketch, symmetric)
            if centred is not None:
                # build123d's centred Box or Cylinder: a designer sketches on
                # the middle plane and pads both ways.
                sketch, dist, symmetric, scale_ = centred, dist / 2, True, scale_ / 2

            def make(body):
                pad = self._verified_pad(body, sketch, dist, symmetric, entry)
                self.plan.drive(feature, "distance", lambda: pad.FirstLimit.Dimension, scale_)
                return pad
        operation = feature["operation"]
        if operation in ("join", "cut") and feature.get("direction") is None \
                and self._human(feature):

            def build(body):
                prism = self._prism_into(body, sketch, dist, symmetric, operation, entry)
                self.plan.drive(feature, "distance", lambda: prism.FirstLimit.Dimension, scale_)
                return prism

            self._human_feature(feature, entry, build,
                                flip=None if symmetric else _flip_direction)
            return
        if operation == "new_body":
            body = self._new_body(use_main=True)
            pad = make(body)
            self.bodies[feature["creates"][0]] = body
            if self.first_pad is None:
                # Its body survives to the end, so editing it moves the part.
                self.first_pad = (pad, body)
                self.first_pad_feature = feature.get("id")
            return
        (target,) = feature["bodies"]
        tool = self._new_body(use_main=False)
        make(tool)
        self._combine(self.bodies[target], tool, operation, entry)

    def _feature_revolve(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        sketch = self.sketches[feature["sketch"]]
        axis = feature["axis"]
        origin = tuple(float(c) for c in axis["origin"])
        direction = unit(tuple(float(c) for c in axis["direction"]))
        angle = float(feature["angle"])
        if feature["operation"] == "new_body":
            body = self._new_body(use_main=True)
            self._verified_shaft(body, sketch, origin, direction, angle, entry)
            self.bodies[feature["creates"][0]] = body
            return
        if feature["operation"] in ("join", "cut") and self._human(feature):
            self._human_feature(
                feature, entry,
                lambda body: self._revolution_into(body, sketch, origin, direction, angle,
                                                   feature["operation"], entry),
                flip=_swap_angles)
            return
        (target,) = feature["bodies"]
        tool = self._new_body(use_main=False)
        self._verified_shaft(tool, sketch, origin, direction, angle, entry)
        self._combine(self.bodies[target], tool, feature["operation"], entry)

    def _feature_combine(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        (target,) = feature["targets"]
        mirror = self.pending_mirror
        self.pending_mirror = None
        if mirror is not None and feature["tools"] == mirror["creates"]:
            # Preflight guarantees this combine joins the mirror's copy
            # straight back into its source: one CATIA Mirror of that body.
            self.bodies.pop(feature["tools"][0])
            self._mirror_into(self.bodies[target], mirror["plane"], entry)
            return
        for name in feature["tools"]:
            self._combine(self.bodies[target], self.bodies.pop(name), feature["operation"], entry)

    def _feature_mirror(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self.pending_mirror = feature
        self.bodies[feature["creates"][0]] = None  # exists only until the join
        entry["note"] = "applied as a CATIA Mirror by the combine that joins it back"

    def _feature_split(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        (target,) = feature["bodies"]
        body = self.bodies[target]
        frame = _plane_frame(feature["plane"])
        ref, _ = self._support(frame)
        before = self._volume(body)
        keep_top = feature["keep"] == "top"
        part = self.part
        part.InWorkObject = body
        sides = self._split_sides()
        split = part.ShapeFactory.AddNewSplit(ref, sides["top" if keep_top else "bottom"])
        entry["catia"].append(str(split.Name))
        part.UpdateObject(split)
        if not self._split_on_side(body, frame, keep_top, before):
            current = _read_prop(split, "SplitSide")
            other = sides["bottom"] if current == sides["top"] else sides["top"]
            if not _set_prop(split, "SplitSide", other):
                raise BuildFailed(f"CATIA refused to flip the side of '{split.Name}'")
            part.UpdateObject(split)
            if not self._split_on_side(body, frame, keep_top, before):
                raise BuildFailed(f"'{split.Name}' keeps material on the wrong side of the "
                                  "plane after one flip")
            entry.setdefault("notes", []).append(f"{split.Name}: side flipped to match the tree")

    def _feature_hole(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        if self._human(feature):
            try:
                self._human_feature(feature, entry,
                                    lambda body: self._holes_into(body, feature, entry),
                                    flip=_reverse_hole)
                return
            except HumanMismatch as error:
                # A through hole whose frame is not on an outer face (build123d
                # drills both ways from it, a Hole only one): cut it as tool
                # bodies instead.
                for item in reversed(error.items):
                    self._delete(item)
                entry["catia"] = []
                entry.setdefault("notes", []).append(
                    f"as Hole features: {error}; built as cut cylinders instead")
        (target,) = feature["bodies"]
        for matrix in feature["frames"]:
            if feature["kind"] == "countersink":
                sketch, origin, z_dir = hole_profile_sketch(feature, matrix)
                tool = self._new_body(use_main=False)
                self._verified_shaft(tool, sketch, origin, z_dir, 360.0, entry)
                self._combine(self.bodies[target], tool, "cut", entry)
                continue
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

    def _feature_loft(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        sections = [self.sketches[name] for name in feature["sections"]]
        plan = plan_loft(*sections)
        self._into_body(feature, entry,
                        lambda body: self._verified_loft(body, sections, plan, entry))

    def _feature_sweep(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        sketch = self.sketches[feature["sketch"]]
        self._into_body(feature, entry,
                        lambda body: self._verified_rib(body, sketch, feature["path"], entry))

    def _feature_fillet(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._dressup(self.dressups.fillet, feature, entry)

    def _feature_chamfer(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._dressup(self.dressups.chamfer, feature, entry)

    def _feature_shell(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._dressup(self.dressups.shell, feature, entry)

    @staticmethod
    def _dressup(build: Callable[..., None], feature: dict[str, Any], entry: dict[str, Any]) -> None:
        try:
            build(feature, entry)
        except DressUpFailed as error:
            raise BuildFailed(str(error)) from error

    def _feature_from_step(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        """A feature preflight accepted because STEP files stand in for it:
        each file is imported, pasted as a dead body and combined."""
        exchange = self._exchange()
        home = self.conn.active_document
        entry["dead"] = True
        for path, role in self.step_source.files(self.part_id, feature):
            name = f"{DEAD_BODY_PREFIX}{feature['id']}" + ("" if role == "solid" else f"_{role}")
            imported = exchange.import_file(path)
            try:
                body, volume, notes = exchange.paste_as_result(imported, self.part, home,
                                                               home.Activate, name)
            finally:
                exchange.close(imported)
                home.Activate()
            entry["catia"].append(str(body.Name))
            entry.setdefault("notes", []).extend(notes)
            self.record.dead_bodies.append({"feature": feature["id"], "type": feature["type"],
                                            "role": role, "file": path, "body": str(body.Name),
                                            "volume_mm3": round(volume, 4)})
            if role == "solid" and (feature.get("operation") or "new_body") == "new_body":
                self.bodies[feature["creates"][0]] = body
                continue
            operation = feature["operation"] if role == "solid" else \
                {"add": "join", "remove": "cut"}[role]
            (target,) = feature["bodies"]
            before = self._volume(self.bodies[target])
            self._combine(self.bodies[target], body, operation, entry)
            if role == "solid":
                continue
            # A delta is disjoint from its body (add) or inside it (remove),
            # so the volume moves by the delta's, to 1 % of the delta.
            want = before + volume if role == "add" else before - volume
            got = self._volume(self.bodies[target])
            if abs(got - want) > max(1e-3, 1e-2 * volume, 1e-6 * before):
                raise BuildFailed(
                    f"the {role} delta '{body.Name}' left {got:.4f} mm3, expected {want:.4f} "
                    "mm3: CATIA's boolean on the delta's coincident faces went wrong")

    def _exchange(self) -> StepExchange:
        return StepExchange(self.conn, self.measure)

    # ── Bodies, pads, combines ──

    def _into_body(self, feature: dict[str, Any], entry: dict[str, Any],
                   make: Callable[[Any], Any]) -> None:
        """Build a solid with `make` in a body of its own: the new body of a
        new_body feature, or a tool combined into the feature's target."""
        if feature["operation"] == "new_body":
            body = self._new_body(use_main=True)
            make(body)
            self.bodies[feature["creates"][0]] = body
            return
        (target,) = feature["bodies"]
        tool = self._new_body(use_main=False)
        make(tool)
        self._combine(self.bodies[target], tool, feature["operation"], entry)

    # ── Human style ──

    def _human(self, feature: dict[str, Any]) -> bool:
        return feature.get("id") in self.snapshots

    def _human_feature(self, feature: dict[str, Any], entry: dict[str, Any],
                       build: Callable[[Any], Any], flip: Callable[[Any], bool] | None) -> None:
        """Build a join or cut straight into its body, then check that body
        against build123d's after this feature; flip once if it does not."""
        (target,) = feature["bodies"]
        body = self.bodies[target]
        before = (self._volume(body), self._cog(body))
        built = build(body)
        items = built if isinstance(built, list) else [built]
        name = ", ".join(str(item.Name) for item in items)
        error = self._snapshot_error(target, before)
        if error is None:
            return
        if flip is None or not all(flip(item) for item in items):
            raise HumanMismatch(f"'{name}' {error}", items)
        for item in items:
            self.part.UpdateObject(item)
        second = self._snapshot_error(target, before)
        if second is not None:
            raise HumanMismatch(f"'{name}' {error}; flipped, it {second}", items)
        entry.setdefault("notes", []).append(f"{name}: flipped to match the tree")

    def _snapshot_error(self, name: str, before: tuple[float, Vec]) -> str | None:
        """None when the body changed as build123d's did in this feature: the
        volume added or removed, and where its centre of gravity is (the
        change's own moment, so earlier small differences cancel out)."""
        want = self.snapshots[self.feature_id][name]
        prior = self.expected_bodies.get(name) or {"volume_mm3": 0.0, "cog_mm": [0.0, 0.0, 0.0]}
        volume, cog = self._volume(self.bodies[name]), self._cog(self.bodies[name])
        want_delta = float(want["volume_mm3"]) - float(prior["volume_mm3"])
        got_delta = volume - before[0]
        slack = max(1e-3, FEATURE_REL_TOL * abs(want_delta), 1e-7 * float(want["volume_mm3"])) \
            + self.record.spline_allowance_mm3
        if abs(got_delta - want_delta) > slack:
            return (f"changed the body by {got_delta:+.4f} mm3, build123d by "
                    f"{want_delta:+.4f} mm3")
        if abs(want_delta) <= slack:
            return None  # nothing moved: no centre to compare
        moment = [volume * cog[i] - before[0] * before[1][i] for i in range(3)]
        want_moment = [float(want["volume_mm3"]) * float(want["cog_mm"][i])
                       - float(prior["volume_mm3"]) * float(prior["cog_mm"][i]) for i in range(3)]
        got_centre = tuple(m / got_delta for m in moment)
        want_centre = tuple(m / want_delta for m in want_moment)
        size = abs(want_delta) ** (1 / 3)
        tol = max(1e-3, FEATURE_REL_TOL * size) + self.record.spline_allowance_mm3 / abs(want_delta)
        if distance(got_centre, want_centre) > tol:
            return (f"changed the body around {_fmt(got_centre)}, build123d around "
                    f"{_fmt(want_centre)}")
        return None

    def _centred_box(self, feature: dict[str, Any], sketch: dict[str, Any],
                     symmetric: bool) -> dict[str, Any] | None:
        """The sketch moved to the middle of a centred primitive's extrude
        (params distance base_shift 0.5: the plane was offset by -distance/2),
        or None."""
        spec = (feature.get("params") or {}).get("distance") or {}
        if self.style != "human" or symmetric or spec.get("base_shift") != 0.5 \
                or feature["operation"] not in ("new_body", "join", "cut"):
            return None
        plane = dict(sketch["plane"])
        normal = unit(tuple(float(c) for c in plane["normal"]))
        plane["origin"] = list(add(tuple(float(c) for c in plane["origin"]),
                                   scale(normal, float(feature["distance"]) / 2)))
        return {**sketch, "plane": plane}

    def _prism_into(self, body: Any, sketch: dict[str, Any], dist: float, symmetric: bool,
                    operation: str, entry: dict[str, Any]) -> Any:
        """A Pad (join) or a Pocket (cut) drawn in the body it changes."""
        frame = Frame.from_tree(sketch["plane"])
        catia_sketch, along, _ = self._draw(body, sketch, frame, entry)
        self.record.spline_allowance_mm3 += \
            approximation_area(sketch) * abs(dist) * (2 if symmetric else 1)
        part = self.part
        _force_solid_inwork(part, catia_sketch)
        part.InWorkObject = body
        method = part.ShapeFactory.AddNewPad if operation == "join" else \
            part.ShapeFactory.AddNewPocket
        prism = method(catia_sketch, abs(dist))
        entry["catia"].append(str(prism.Name))
        if symmetric:
            if not _set_prop(prism, "IsSymmetric", True):
                raise BuildFailed(f"CATIA refused the symmetric extent of '{prism.Name}'")
        elif dist * along < 0 and not _set_prop(prism, "DirectionOrientation", 1):
            raise BuildFailed(f"CATIA refused the direction of '{prism.Name}'")
        part.UpdateObject(prism)
        return prism

    def _revolution_into(self, body: Any, sketch: dict[str, Any], axis_origin: Vec,
                         axis_direction: Vec, angle: float, operation: str,
                         entry: dict[str, Any]) -> Any:
        """A Shaft (join) or a Groove (cut) drawn in the body it changes."""
        frame = Frame.from_tree(sketch["plane"])
        want = revolve_expectation(frame, sketch, axis_origin, axis_direction, angle)
        catia_sketch, _, catia_frame = self._draw(body, sketch, frame, entry)
        self.record.spline_allowance_mm3 += \
            approximation_area(sketch) * math.radians(angle) * want.max_radius
        part = self.part
        axis_ref = self._axis_reference(catia_sketch, catia_frame, axis_origin, axis_direction)
        _force_solid_inwork(part, catia_sketch)
        part.InWorkObject = body
        method = part.ShapeFactory.AddNewShaft if operation == "join" else \
            part.ShapeFactory.AddNewGroove
        revolution = method(catia_sketch)
        entry["catia"].append(str(revolution.Name))
        revolution.RevoluteAxis = axis_ref
        if not _set_prop(revolution, "FirstAngle", angle):
            raise BuildFailed(f"CATIA refused the angle of '{revolution.Name}'")
        _set_prop(revolution, "SecondAngle", 0.0)
        part.UpdateObject(revolution)
        return revolution

    def _holes_into(self, body: Any, feature: dict[str, Any], entry: dict[str, Any]) -> list[Any]:
        """One Hole feature per position, each placed by a sketch holding one
        point on the hole frame's XY plane. A through hole goes up to last."""
        part = self.part
        holes = []
        spec = hole_spec(feature)
        for matrix in feature["frames"]:
            origin, x_dir, z_dir = frame_axes(matrix)
            point = {"plane": {"origin": list(origin), "x_dir": list(x_dir),
                               "normal": list(z_dir)},
                     "entities": [{"type": "point", "at": [0.0, 0.0], "id": "p"}],
                     "profiles": []}
            catia_sketch, _, _ = self._draw(body, point, Frame.from_tree(point["plane"]))
            _force_solid_inwork(part, catia_sketch)
            part.InWorkObject = body
            hole = part.ShapeFactory.AddNewHoleFromSketch(catia_sketch, spec.depth or 1.0)
            entry["catia"].append(str(hole.Name))
            _apply_hole_spec(hole, spec)
            for key, member in (("diameter", "Diameter"), ("counterbore_diameter", "HeadDiameter"),
                                ("counterbore_depth", "HeadDepth"),
                                ("countersink_angle", "HeadAngle")):
                self.plan.drive(feature, key, lambda h=hole, m=member: getattr(h, m))
            if spec.depth is not None:
                self.plan.drive(feature, "depth", lambda h=hole: h.BottomLimit.Dimension)
            part.UpdateObject(hole)
            holes.append(hole)
        return holes

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
        catia_sketch, along, _ = self._draw(body, sketch, frame, entry)
        approximation = approximation_area(sketch)
        allowance = approximation * abs(dist) * (2 if symmetric else 1)
        self.record.spline_allowance_mm3 += allowance
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
        tol = max(1e-3, FEATURE_REL_TOL * size) + approximation / area * size
        if abs(volume - want_volume) > max(1e-3, FEATURE_REL_TOL * want_volume) + allowance:
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

    def _verified_oblique_pad(self, body: Any, sketch: dict[str, Any], vector: Vec,
                              symmetric: bool, entry: dict[str, Any]) -> Any:
        """A Pad along a 3D line through the profile's centroid. Whether CATIA
        measures the length along that line or along the sketch normal is
        read from the first volume; the direction is flipped once like a pad's."""
        frame = Frame.from_tree(sketch["plane"])
        area, centroid = profile_area_centroid(sketch)
        want_volume, want_cog = oblique_expectation(frame, area, centroid, vector, symmetric)
        catia_sketch, _, _ = self._draw(body, sketch, frame)
        start = frame.world(*centroid)
        line = self.part.HybridShapeFactory.AddNewLinePtPt(
            self._construction_point(start), self._construction_point(add(start, vector)))
        self._construction_set().AppendHybridShape(line)
        part = self.part
        part.UpdateObject(line)
        _force_solid_inwork(part, catia_sketch)
        part.InWorkObject = body
        length, height = math.sqrt(dot(vector, vector)), abs(dot(vector, frame.normal))
        pad = part.ShapeFactory.AddNewPad(catia_sketch, length)
        entry["catia"].append(str(pad.Name))
        pad.SetDirection(part.CreateReferenceFromObject(line))
        if symmetric and not _set_prop(pad, "IsSymmetric", True):
            raise BuildFailed(f"CATIA refused the symmetric extent of '{pad.Name}'")
        part.UpdateObject(pad)
        slack = max(1e-3, FEATURE_REL_TOL * want_volume)
        volume = self._volume(body)
        if abs(volume - want_volume) > slack and abs(volume * height / length - want_volume) <= slack:
            # CATIA measured the length along the sketch normal: give it the height.
            pad.FirstLimit.Dimension.Value = height
            part.UpdateObject(pad)
            volume = self._volume(body)
            entry.setdefault("notes", []).append(
                f"{pad.Name}: length measured along the sketch normal")
        if abs(volume - want_volume) > slack:
            raise BuildFailed(f"'{pad.Name}' measures {volume:.4f} mm3, expected "
                              f"{want_volume:.4f} mm3 (profile area times the height)")
        size = math.sqrt(area) + length
        tol = max(1e-3, FEATURE_REL_TOL * size)
        cog = self._cog(body)
        if distance(cog, want_cog) > tol:
            if symmetric or distance(cog, add(want_cog, scale(vector, -1))) > tol:
                raise BuildFailed(f"'{pad.Name}' has its centre of gravity at {_fmt(cog)}, "
                                  f"expected {_fmt(want_cog)}: it does not follow the tree's "
                                  "direction")
            current = _read_prop(pad, "DirectionOrientation") or 0
            if not _set_prop(pad, "DirectionOrientation", 1 - current):
                raise BuildFailed(f"CATIA refused to flip the direction of '{pad.Name}'")
            part.UpdateObject(pad)
            if distance(self._cog(body), want_cog) > tol:
                raise BuildFailed(f"'{pad.Name}' still points the wrong way after one flip")
            entry.setdefault("notes", []).append(f"{pad.Name}: direction flipped to match the tree")
        return pad

    def _verified_taper(self, body: Any, plan: TaperPlan, entry: dict[str, Any]) -> None:
        """A tapered extrude as build123d builds it: each outer loop lofted to
        its offset, minus its inner loops lofted to theirs, the pieces joined."""
        for index, piece in enumerate(plan.pieces):
            sections = [{"plane": piece.bottom}, {"plane": piece.top}]
            target = body if index == 0 else self._new_body(use_main=False)
            self._verified_loft(target, sections, piece.outer, entry)
            for inner in piece.inners:
                tool = self._new_body(use_main=False)
                self._verified_loft(tool, sections, inner, entry)
                self._combine(target, tool, "cut", entry)
            if index:
                self._combine(body, target, "join", entry)
        volume, cog = self._volume(body), self._cog(body)
        size = math.sqrt(max(plan.volume, 1e-9) ** (2 / 3))
        if abs(volume - plan.volume) > max(1e-3, FEATURE_REL_TOL * plan.volume) \
                or distance(cog, plan.cog) > max(1e-3, FEATURE_REL_TOL * size):
            raise BuildFailed(f"the tapered extrude gives {volume:.4f} mm3 at {_fmt(cog)}, "
                              f"expected {plan.volume:.4f} mm3 at {_fmt(plan.cog)} (prismatoid)")

    def _verified_shaft(self, body: Any, sketch: dict[str, Any], axis_origin: Vec,
                        axis_direction: Vec, angle: float, entry: dict[str, Any]) -> Any:
        frame = Frame.from_tree(sketch["plane"])
        want = revolve_expectation(frame, sketch, axis_origin, axis_direction, angle)
        catia_sketch, _, catia_frame = self._draw(body, sketch, frame, entry)
        approximation = approximation_area(sketch)
        allowance = approximation * math.radians(angle) * want.max_radius
        self.record.spline_allowance_mm3 += allowance
        part = self.part
        axis_ref = self._axis_reference(catia_sketch, catia_frame, axis_origin, axis_direction)
        _force_solid_inwork(part, catia_sketch)
        part.InWorkObject = body
        shaft = part.ShapeFactory.AddNewShaft(catia_sketch)
        entry["catia"].append(str(shaft.Name))
        shaft.RevoluteAxis = axis_ref
        if not _set_prop(shaft, "FirstAngle", angle):
            raise BuildFailed(f"CATIA refused the angle of '{shaft.Name}'")
        _set_prop(shaft, "SecondAngle", 0.0)
        part.UpdateObject(shaft)
        volume, cog = self._volume(body), self._cog(body)
        size = math.sqrt(max(want.volume, 1e-9) ** (2 / 3))
        tol = max(1e-3, FEATURE_REL_TOL * size) + allowance / max(want.volume, 1e-9) * size
        if abs(volume - want.volume) > max(1e-3, FEATURE_REL_TOL * want.volume) + allowance:
            raise BuildFailed(
                f"'{shaft.Name}' measures {volume:.4f} mm3, expected {want.volume:.4f} mm3 "
                "(Pappus): the profile or the axis was not read as the tree gives them")
        if distance(cog, want.cog) > tol:
            if distance(cog, want.cog_reversed) > tol:
                raise BuildFailed(
                    f"'{shaft.Name}' has its centre of gravity at {_fmt(cog)}, expected "
                    f"{_fmt(want.cog)}")
            # CATIA turned the other way about this axis: put the angle on
            # the other side of the profile.
            if not (_set_prop(shaft, "FirstAngle", 0.0) and _set_prop(shaft, "SecondAngle", angle)):
                raise BuildFailed(f"CATIA refused to turn '{shaft.Name}' the other way")
            part.UpdateObject(shaft)
            cog = self._cog(body)
            if distance(cog, want.cog) > tol:
                raise BuildFailed(f"'{shaft.Name}' still turns the wrong way after one swap")
            entry.setdefault("notes", []).append(f"{shaft.Name}: angle swapped to turn as the tree")
        return shaft

    def _verified_loft(self, body: Any, sections: list[dict[str, Any]], plan: LoftPlan,
                       entry: dict[str, Any]) -> Any:
        drawn = []
        for sketch, segments, height in ((sections[0], plan.first, 0.0),
                                         (sections[1], plan.second, plan.height)):
            redrawn = section_sketch(sketch, segments, plan.common, height)
            catia_sketch, _, _ = self._draw(body, redrawn, Frame.from_tree(sketch["plane"]),
                                            connect=True)
            closing = plan.common.world(segments[0].a[0], segments[0].a[1], height)
            drawn.append((catia_sketch, closing))
        want = plan.expectation
        size = math.sqrt(max(want.volume, 1e-9) ** (2 / 3))
        tol = max(1e-3, FEATURE_REL_TOL * size)
        part = self.part
        found = None
        refused: list[str] = []
        # The volume and centre of gravity decide which closing points and
        # orientation give the tree's loft.
        for mode in LOFT_CLOSING_MODES:
            closings = [self._closing_point(mode, point) for _, point in drawn]
            for orientation in LOFT_ORIENTATIONS:
                _force_solid_inwork(part, drawn[0][0])
                part.InWorkObject = body
                loft = part.ShapeFactory.AddNewLoft()
                try:
                    shape = loft.HybridShape
                except Exception:  # some releases take the sections on the Loft itself
                    shape = loft
                if not _set_prop(shape, "SectionCoupling", LOFT_VERTEX_COUPLING):
                    raise BuildFailed(f"CATIA refused vertex coupling on '{loft.Name}'")
                try:
                    for (catia_sketch, _), closing, sense in zip(drawn, closings, (1, orientation),
                                                                 strict=True):
                        _add_loft_section(self.conn.app, shape,
                                          part.CreateReferenceFromObject(catia_sketch), sense,
                                          closing)
                except Exception as error:  # this closing point or orientation is refused
                    refused.append(f"{mode}, orientation {orientation}: {error}")
                    self._delete(loft)
                    continue
                try:
                    part.UpdateObject(loft)
                    volume, cog = self._volume(body), self._cog(body)
                    found = (volume, cog)
                    if abs(volume - want.volume) <= max(1e-3, FEATURE_REL_TOL * want.volume) \
                            and distance(cog, want.cog) <= tol:
                        entry["catia"].append(str(loft.Name))
                        notes = entry.setdefault("notes", [])
                        notes.append(f"{loft.Name}: closing points given as {mode}")
                        if orientation != 1:
                            notes.append(f"{loft.Name}: second section reversed to match the "
                                         "tree")
                        return loft
                except Exception as error:  # CATIA rejects a twisted, self-crossing loft
                    refused.append(f"{mode}, orientation {orientation}: {error}")
                self._delete(loft)
        got = (f"{found[0]:.4f} mm3 with its centre of gravity at {_fmt(found[1])}"
               if found else "nothing CATIA would build")
        raise BuildFailed(f"the loft gives {got}, expected {want.volume:.4f} mm3 at "
                          f"{_fmt(want.cog)} (tried: {'; '.join(refused) or 'every way'})")

    def _closing_point(self, mode: str, point: Vec) -> Any:
        """A loft section's closing point: a 3D point at `point`, or None for
        CATIA's choice."""
        return None if mode == "automatic" else self._construction_point(point)

    def _verified_rib(self, body: Any, sketch: dict[str, Any], path: list[dict[str, Any]],
                      entry: dict[str, Any]) -> Any:
        frame, want = sweep_expectation(sketch, path)
        profile, _, _ = self._draw(body, sketch, Frame.from_tree(sketch["plane"]))
        center, _, _ = self._draw(body, path_sketch(path, frame), frame)
        part = self.part
        _force_solid_inwork(part, profile)
        part.InWorkObject = body
        rib = part.ShapeFactory.AddNewRib(profile, center)
        entry["catia"].append(str(rib.Name))
        part.UpdateObject(rib)
        volume = self._volume(body)
        if abs(volume - want) > max(1e-3, FEATURE_REL_TOL * want):
            raise BuildFailed(f"'{rib.Name}' measures {volume:.4f} mm3, expected {want:.4f} "
                              "mm3 (profile area times the length its centroid travels)")
        return rib

    def _delete(self, feature: Any) -> None:
        selection = self.conn.hso
        selection.Clear()
        selection.Add(feature)
        selection.Delete()
        selection.Clear()
        self.part.Update()

    def _construction_point(self, point: Vec) -> Any:
        """A reference to a 3D point in the construction set."""
        part = self.part
        created = part.HybridShapeFactory.AddNewPointCoord(*point)
        self._construction_set().AppendHybridShape(created)
        part.UpdateObject(created)
        return part.CreateReferenceFromObject(created)

    def _axis_reference(self, catia_sketch: Any, catia_frame, axis_origin: Vec,
                        axis_direction: Vec) -> Any:
        """The revolve axis: the sketch's own H or V axis when the tree's axis is
        one of them, else a 3D line in the construction set."""
        origin, h, v = catia_frame
        offset = add(axis_origin, scale(origin, -1))
        through_origin = distance(offset, scale(axis_direction, dot(offset, axis_direction))) < 1e-6
        part = self.part
        if through_origin and _parallel(axis_direction, v):
            return part.CreateReferenceFromObject(_sketch_v_direction(catia_sketch))
        if through_origin and _parallel(axis_direction, h):
            return part.CreateReferenceFromObject(_sketch_h_direction(catia_sketch))
        points = [self._construction_point(point)
                  for point in (axis_origin, add(axis_origin, scale(axis_direction, 100.0)))]
        line = part.HybridShapeFactory.AddNewLinePtPt(*points)
        self._construction_set().AppendHybridShape(line)
        part.UpdateObject(line)
        return part.CreateReferenceFromObject(line)

    def _mirror_into(self, body: Any, plane: dict[str, Any], entry: dict[str, Any]) -> None:
        frame = _plane_frame(plane)
        ref, _ = self._support(frame)
        before = self._volume(body)
        part = self.part
        part.InWorkObject = body
        mirror = part.ShapeFactory.AddNewMirror(ref)
        entry["catia"].append(str(mirror.Name))
        part.UpdateObject(mirror)
        after, cog = self._volume(body), self._cog(body)
        slack = max(1e-3, FEATURE_REL_TOL * before)
        if not before - slack <= after <= 2 * before + slack:
            raise BuildFailed(f"'{mirror.Name}' left {after:.4f} mm3 from {before:.4f} mm3, "
                              "which no mirror union gives")
        off_plane = dot(add(cog, scale(frame.origin, -1)), frame.normal)
        if abs(off_plane) > max(1e-3, FEATURE_REL_TOL * math.sqrt(max(after, 1e-9) ** (2 / 3))):
            raise BuildFailed(f"'{mirror.Name}' is not symmetric about the tree's plane "
                              f"(centre of gravity {off_plane:.4f} mm off it)")

    def _split_sides(self) -> dict[str, int]:
        """CatSplitSide values for keeping the side the plane normal points to
        (top) or the other (bottom), from the type library when it is
        readable. The side actually kept is checked after the update anyway."""
        values = find_enum([self.part.ShapeFactory, self.conn.app], "CatSplitSide") or {}
        top = [v for n, v in values.items() if "positive" in n.lower()]
        bottom = [v for n, v in values.items() if "negative" in n.lower()]
        if len(top) == 1 and len(bottom) == 1:
            return {"top": top[0], "bottom": bottom[0]}
        return {"top": 0, "bottom": 1}

    def _split_on_side(self, body: Any, frame: Frame, keep_top: bool, before: float) -> bool:
        after = self._volume(body)
        if after <= 0 or after > before + max(1e-3, FEATURE_REL_TOL * before):
            return False
        side = dot(add(self._cog(body), scale(frame.origin, -1)), frame.normal)
        return side > 0 if keep_top else side < 0

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

    def _draw(self, body: Any, sketch: dict[str, Any], frame: Frame,
              entry: dict[str, Any] | None = None, connect: bool = False):
        """Draw the tree sketch in a new CATIA sketch of `body`. Returns the
        sketch, +1 / -1 for whether its normal agrees with the tree's, and the
        CATIA sketch frame (origin, H, V). Approximated splines are noted in
        `entry`. With `connect`, lines and arcs meeting at a point share one
        sketch point as their ends, as a connected contour: a Multi-sections
        Solid needs that (live on R428 a rectangle of four lines that only
        touch made every loft fail to update, a whole circle worked), while a
        Pad finds the contour itself."""
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
        ends: list[tuple[Any, tuple[float, float], tuple[float, float]]] = []
        for entity in sketch["entities"]:
            kind = entity["type"]
            if kind == "line":
                (u0, v0), (u1, v1) = local(entity["start"]), local(entity["end"])
                line = factory.CreateLine(u0, v0, u1, v1)
                ends.append((line, (u0, v0), (u1, v1)))
            elif kind == "circle":
                cu, cv = local(entity["center"])
                factory.CreateClosedCircle(cu, cv, float(entity["radius"]))
            elif kind == "point":  # a Hole's position (not a tree entity)
                factory.CreatePoint(*local(entity["at"]))
            elif kind == "spline":
                note = _draw_spline(factory, entity, local)
                if note and entry is not None:
                    entry.setdefault("notes", []).append(note)
            else:
                arc = Arc.through(local(entity["start"]), local(entity["mid"]), local(entity["end"]))
                circle = factory.CreateCircle(arc.center[0], arc.center[1], arc.radius, arc.start,
                                              arc.end)
                (cu, cv), r = arc.center, arc.radius
                ends.append((circle, (cu + r * math.cos(arc.start), cv + r * math.sin(arc.start)),
                             (cu + r * math.cos(arc.end), cv + r * math.sin(arc.end))))
        if connect:
            _connect_ends(factory, ends)
        catia_sketch.CloseEdition()
        self.part.UpdateObject(catia_sketch)
        return catia_sketch, along, (origin, h, v)

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

    # ── Parameters ──

    def _parametrize(self, record: PartRecord, final: list[Any]) -> None:
        """One user parameter per script number, and a formula on every value
        it set. The part must keep its volume; if not, or if CATIA refuses,
        everything made here is removed and the report says why."""
        part = self.part
        before = sum(self._volume(body) for body in final)
        made_parameters: list[str] = []
        made_formulas: list[str] = []
        # Each driven value as it was, put back if the parameters are undone
        # (removing a formula leaves its last value in place).
        originals: list[tuple[Any, float]] = []
        try:
            parameters, relations = part.Parameters, part.Relations
            for script in self.plan.parameters.values():
                parameter = parameters.CreateDimension(script.name, script.magnitude, 0)
                made_parameters.append(script.name)
                parameter.ValuateFromString(f"{script.value:g}{script.unit}")
                _set_prop(parameter, "Comment", f"from {script.source}")
                for i, driven in enumerate(script.driven, 1):
                    name = f"{script.name}_f{i}"
                    target = driven.target()
                    originals.append((target, float(target.Value)))
                    relations.CreateFormula(name, f"from {script.source}", target,
                                            script.expression(driven.scale))
                    made_formulas.append(name)
            part.Update()
            after = sum(self._volume(body) for body in final)
            if abs(after - before) > max(1e-3, PART_REL_TOL * abs(before)):
                raise BuildFailed(f"the formulas moved the part from {before:.4f} to "
                                  f"{after:.4f} mm3")
        except Exception as error:  # a parameter is a bonus: never lose the part for it
            for name in made_formulas:
                with contextlib.suppress(Exception):
                    part.Relations.Remove(name)
            for name in made_parameters:
                with contextlib.suppress(Exception):
                    part.Parameters.Remove(name)
            for target, value in originals:
                with contextlib.suppress(Exception):
                    target.Value = value
            with contextlib.suppress(Exception):
                part.Update()
            record.parameters_note = f"no user parameters: {error}"
            return
        record.parameters = [
            {"name": s.name, "value": s.value, "unit": s.unit, "source": s.source,
             "drives": [s.expression(d.scale) for d in s.driven]}
            for s in self.plan.parameters.values()]
        self.edit_parameter = self.plan.by_key.get((str(self.first_pad_feature), "distance"))

    def _check_editability(self, record: PartRecord) -> None:
        """Change the first pad's length (through its user parameter when it
        has one), check the volume moves, restore it."""
        if self.first_pad is None:
            record.editable, record.editability_note = None, "no pad to edit"
            return
        pad, body = self.first_pad
        script = getattr(self, "edit_parameter", None)
        if script is not None:
            self._check_parameter_edit(record, script, body)
            return
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
        # Restored but unmoved: a later feature clips the pad (an intersect),
        # which proves nothing either way.
        record.editable = True if changed and back else (None if back else False)
        record.editability_note = (
            f"{pad.Name} length {original:g} -> {original + 1:g} -> {original:g} mm: volume "
            f"{before:.1f} -> {moved:.1f} -> {restored:.1f} mm3")

    def _check_parameter_edit(self, record: PartRecord, script: Any, body: Any) -> None:
        """Edit the user parameter as a designer would, through its value."""
        try:
            parameter = self.part.Parameters.Item(script.name)
            before = self._volume(body)
            parameter.ValuateFromString(f"{script.value + 1:g}{script.unit}")
            self.part.Update()
            moved = self._volume(body)
            parameter.ValuateFromString(f"{script.value:g}{script.unit}")
            self.part.Update()
            restored = self._volume(body)
        except Exception as error:
            record.editable, record.editability_note = False, f"edit failed: {error}"
            return
        changed = abs(moved - before) > max(1e-3, FEATURE_REL_TOL * before)
        back = abs(restored - before) <= max(1e-3, PART_REL_TOL * before)
        record.editable = changed and back
        record.editability_note = (
            f"parameter {script.name} {script.value:g} -> {script.value + 1:g} -> "
            f"{script.value:g} {script.unit}: volume {before:.1f} -> {moved:.1f} -> "
            f"{restored:.1f} mm3")


def _draw_spline(factory: Any, entity: dict[str, Any], local) -> str | None:
    """Draw a tree spline, read in the CATIA sketch's own frame (an isometry
    of the tree's, so a conic stays the same conic). Returns a note when it is
    approximated. Factory2D conventions: CreateClosedEllipse(cx, cy, major
    direction x, y, major radius, minor radius) and CreateEllipse(..., start,
    end) with counter-clockwise angles from the major axis, as CreateCircle
    takes them; CreateSpline takes the control points it passes through. Not
    yet validated live."""
    plan = drawing(Nurbs.from_entity(entity, transform=local))
    for item in plan.items:
        kind = item[0]
        if kind == "line":
            (u0, v0), (u1, v1) = item[1], item[2]
            factory.CreateLine(u0, v0, u1, v1)
        elif kind == "circle":
            (cu, cv), r = item[1], item[2]
            factory.CreateClosedCircle(cu, cv, r)
        elif kind == "arc":
            (cu, cv), r, start, end = item[1:]
            factory.CreateCircle(cu, cv, r, start, end)
        elif kind == "ellipse":
            (cu, cv), (mx, my), a, b = item[1:]
            factory.CreateClosedEllipse(cu, cv, mx, my, a, b)
        elif kind == "elliptic_arc":
            (cu, cv), (mx, my), a, b, start, end = item[1:]
            factory.CreateEllipse(cu, cv, mx, my, a, b, start, end)
        else:
            points = tuple(factory.CreateControlPoint(u, v) for u, v in item[1])
            factory.CreateSpline(points)
    if plan.exact:
        return None
    count = sum(len(item[1]) for item in plan.items)
    return (f"spline {entity.get('id', '?')}: approximated by {len(plan.items)} CATIA sketch "
            f"spline(s) through {count} points (chords within {plan.deviation_area:.2g} mm2 "
            "of the curve)")


def _set_value(obj: Any, name: str, value: float) -> None:
    """Set a Length or Angle parameter object's value (Hole.Diameter...)."""
    getattr(obj, name).Value = value
    got = float(getattr(obj, name).Value)
    if abs(got - value) > 1e-6:
        raise BuildFailed(f"CATIA kept {name} = {got:g} on '{obj.Name}' after {value:g} was set")


def _apply_hole_spec(hole: Any, spec: HoleSpec) -> None:
    if not _set_prop(hole, "Type", spec.type):
        raise BuildFailed(f"CATIA refused the hole type of '{hole.Name}'")
    _set_value(hole, "Diameter", spec.diameter)
    hole.BottomType = HOLE_FLAT_BOTTOM
    limit = hole.BottomLimit
    if spec.depth is None:
        limit.LimitMode = LIMIT_UP_TO_LAST
    else:
        limit.LimitMode = LIMIT_DIMENSION
        # R428 ignores AddNewHoleFromSketch's depth: set and read back.
        _set_value(limit, "Dimension", spec.depth)
    if spec.head_diameter is not None:
        _set_value(hole, "HeadDiameter", spec.head_diameter)
    if spec.head_depth is not None:
        _set_value(hole, "HeadDepth", spec.head_depth)
    if spec.head_angle is not None:
        _set_prop(hole, "CounterSunkMode", COUNTERSUNK_DEPTH_ANGLE)
        _set_value(hole, "HeadAngle", spec.head_angle)
    if spec.type == HOLE_TYPES["counterbore"]:
        _set_prop(hole, "AnchorMode", HOLE_ANCHOR_EXTREME)


def _reverse_hole(hole: Any) -> bool:
    try:
        hole.Reverse()
        return True
    except Exception:
        return False


def _flip_direction(prism: Any) -> bool:
    current = _read_prop(prism, "DirectionOrientation") or 0
    return _set_prop(prism, "DirectionOrientation", 1 - current)


def _read_angle(obj: Any, name: str) -> float | None:
    """An angle property (an Angle object on CATIA, whose Value is degrees)."""
    try:
        value = getattr(obj, name)
        return float(getattr(value, "Value", value))
    except Exception:
        return None


def _swap_angles(revolution: Any) -> bool:
    """Turn the other way: the angle on the other side of the profile."""
    first, second = _read_angle(revolution, "FirstAngle"), _read_angle(revolution, "SecondAngle")
    if first is None or second is None:
        return False
    return _set_prop(revolution, "FirstAngle", second) and \
        _set_prop(revolution, "SecondAngle", first)


def _connect_ends(factory: Any, ends) -> None:
    """Give curves that meet one shared sketch point at each meeting place
    (Curve2D.StartPoint / EndPoint, as CATIA's own macros do)."""
    points: dict[tuple[float, float], Any] = {}

    def shared(uv: tuple[float, float]) -> Any:
        key = (round(uv[0], 6), round(uv[1], 6))
        if key not in points:
            points[key] = factory.CreatePoint(uv[0], uv[1])
        return points[key]

    for curve, start, end in ends:
        curve.StartPoint = shared(start)
        curve.EndPoint = shared(end)


def _add_loft_section(app: Any, shape: Any, section: Any, sense: int, closing: Any) -> None:
    """AddSectionToLoft; with `closing` None, no closing point. Python cannot
    pass VBScript's Nothing (R428: "invalid number of parameters"), so that
    call runs in VBScript."""
    if closing is not None:
        shape.AddSectionToLoft(section, sense, closing)
        return
    evaluate(app, _LOFT_SECTION_VBS, "catia_mcp_loft_section", [shape, section, sense])


def _sketch_h_direction(sketch: Any) -> Any:
    """The sketch's H axis, by name first (as _sketch_v_direction), then by the
    language-independent Sketch.AbsoluteAxis property."""
    try:
        return sketch.GeometricElements.Item("AbsoluteAxis").GetItem("HDirection")
    except Exception:
        return sketch.AbsoluteAxis.HorizontalReference


def _plane_frame(plane: dict[str, Any]) -> Frame:
    """A Frame for a mirror or split plane, which may come without x_dir."""
    normal = unit(tuple(float(c) for c in plane["normal"]))
    x_dir = plane.get("x_dir")
    if not x_dir:
        seed = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
        x_dir = unit(add(seed, scale(normal, -dot(seed, normal))))
    return Frame.from_tree({"origin": plane["origin"], "x_dir": list(x_dir), "normal": list(normal)})


def _fmt(p: Vec) -> str:
    return "(" + ", ".join(f"{c:.3f}" for c in p) + ")"
