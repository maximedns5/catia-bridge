"""Drafts, extrudes up to a limit, tapped holes and lofts to a point in CATIA.

The TreeBuilder's companion for the features of catia_mcp/tree/partdesign.py.
Each is one native, editable Part Design feature, built as a designer would:

- a Draft (AddNewDraft) on the faces the tree's anchors find, about the
  neutral plane, pulled along its normal, its angle an editable parameter.
  Which way CATIA turns a positive angle is not documented: the draft is
  tried as given, then pulled the other way, then with the angle negated,
  and kept when the body changes as build123d's did (or, with no reference,
  as the first-order estimate says);
- a Pad or Pocket straight into its body, limited up to next, up to last,
  up to a plane or up to a face (FirstLimit.LimitMode, LimitingElement):
  "up to next" needs the body's material, so it never goes in a tool body.
  The CATIA limit modes are tried in order (up to next, then up through
  next; up to a face, then up to the plane holding it), each with one
  direction flip;
- a Hole's thread (ThreadingMode, the standard and its designation, or "No
  Standard" with the thread's diameter and pitch, the depth, the side): the
  thread is cosmetic, so the hole keeps the tree's tap-drill diameter and the
  volume checks still apply;
- a Multi-sections Solid from a sketch to a point (a construction point), or,
  for a right circular cone CATIA will not loft, a Shaft of its triangle.

None of this is validated live yet: scripts/probe_part_design.py records,
on a running CATIA, which calls and enum values work (docs/part-design.md).
"""

from __future__ import annotations

import contextlib
from typing import Any

from catia_mcp.introspect import find_enum
from catia_mcp.tools.part_design import _force_solid_inwork, _read_prop, _set_prop
from catia_mcp.tree.anchors import match_faces
from catia_mcp.tree.geometry import Frame, Vec, distance, scale
from catia_mcp.tree.partdesign import (
    UntilSpec,
    cone_expectation,
    cone_profile,
    draft_estimate,
    draft_plan,
    plane_frame,
    point_loft,
    right_cone,
    thread_spec,
    until_spec,
)

# Declared order of the enums (CAA V5 help), used when the type library
# cannot be read.
FALLBACK_ENUMS = {
    "CatLimitMode": {"catOffsetLimit": 0, "catUpToNextLimit": 1, "catUpToLastLimit": 2,
                     "catUpToPlaneLimit": 3, "catUpToSurfaceLimit": 4,
                     "catUpThruNextLimit": 5},
    "CatHoleThreadSide": {"catRightThreadSide": 0, "catLeftThreadSide": 1},
}
# The CATIA limit modes tried for each tree limit, in order.
LIMIT_WAYS = {
    "next": ("uptonext", "upthrunext"),
    "previous": ("uptonext", "upthrunext"),
    "last": ("uptolast",),
    "first": ("uptolast",),
    "plane": ("uptoplane",),
    "face": ("uptosurface", "uptoplane"),
}
# A Pad's or Pocket's first length before its limit replaces it (mm).
PLACEHOLDER_LENGTH = 10.0
# CatDraftNeutralPropagationMode none, CatDraftMode standard,
# CatDraftMultiselectionMode by neutral face (first of each, CAA help).
DRAFT_NO_PROPAGATION, DRAFT_STANDARD, DRAFT_MULTISELECTION = 0, 0, 0
# Hole thread standards, in the order tried: the ISO designation, then no
# standard with the thread's own diameter and pitch.
COARSE_STANDARD, FINE_STANDARD, NO_STANDARD = "Metric Thick Pitch", "Metric Thin Pitch", \
    "No Standard"
V5_THREADED_MODE = 0  # CatHoleThreadingMode "threaded" on V5-6R2020 (B30)
FEATURE_REL_TOL = 1e-3


class PartDesignFailed(Exception):
    pass


class PartDesignPlus:
    def __init__(self, builder: Any) -> None:
        self.builder = builder
        self._enums: dict[str, dict[str, int]] = {}

    # ── Draft ──

    def draft(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        b, dressups = self.builder, self.builder.dressups
        body = dressups._body(feature)
        (target,) = feature["bodies"]
        plan = draft_plan(feature)
        candidates = dressups.face_candidates(body, entry)
        matched = dressups._match(feature["faces"], candidates, match_faces)
        refs = [dressups._face_reference(c, a, body, entry)
                for c, a in zip(matched, feature["faces"], strict=True)]
        neutral, _ = b._support(plane_frame(feature["neutral_plane"]))
        before = (b._volume(body), b._cog(body))
        estimate = draft_estimate(plan, feature["faces"])
        pull = plan.pull
        ways = (("as given", pull, plan.angle, 1.0),
                ("pulled the other way", scale(pull, -1), plan.angle, 1.0),
                ("angle negated", pull, -plan.angle, -1.0))
        tried: list[str] = []
        for label, direction, angle, sign in ways:
            made = None
            try:
                made, domain, how = self._new_draft(refs, neutral, direction, angle)
                b.part.UpdateObject(made)
                error = self._draft_error(feature, target, before, estimate, plan)
                if error is None:
                    entry["catia"].append(str(made.Name))
                    notes = entry.setdefault("notes", [])
                    notes.append(f"{made.Name}: {how}")
                    if label != "as given":
                        notes.append(f"{made.Name}: {label} to match the tree")
                    if domain is not None:
                        b.plan.drive(feature, "angle", lambda d=domain: d.DraftAngle, sign)
                    return
                tried.append(f"{label}: {error}")
            except Exception as error:  # CATIA refused this way: the next one
                tried.append(f"{label}: {error}")
            if made is not None:
                with contextlib.suppress(Exception):
                    b._delete(made)
        raise PartDesignFailed("no Draft changed the body as the tree's does (" +
                               "; ".join(tried) + ")")

    def _new_draft(self, refs: list[Any], neutral: Any, pull: Vec, angle: float):
        """AddNewDraft(face, neutral, neutral mode, parting, pulling direction
        x, y, z, mode, angle, multiselection mode), the other faces added to
        its first domain, the pulling direction and angle set on the domain
        too. The parting element is none, or the neutral plane (as recorded
        macros give it)."""
        part = self.builder.part
        factory = part.ShapeFactory
        refused: list[str] = []
        for parting_label, parting in (("no parting element", lambda: part.CreateReferenceFromName("")),
                                       ("parting = neutral", lambda: neutral)):
            try:
                made = factory.AddNewDraft(refs[0], neutral, DRAFT_NO_PROPAGATION, parting(),
                                           pull[0], pull[1], pull[2], DRAFT_STANDARD, angle,
                                           DRAFT_MULTISELECTION)
            except Exception as error:  # the other parting element
                refused.append(f"{parting_label}: {error}")
                continue
            domain = None
            with contextlib.suppress(Exception):
                domain = made.DraftDomains.Item(1)
            holder = domain if domain is not None else made
            for ref in refs[1:]:
                holder.AddFaceToDraft(ref)
            with contextlib.suppress(Exception):
                holder.SetPullingDirection(pull[0], pull[1], pull[2])
            if domain is not None:
                with contextlib.suppress(Exception):
                    domain.DraftAngle.Value = angle
            how = f"AddNewDraft with {parting_label}" + (f" ({'; '.join(refused)})" if refused
                                                        else "")
            return made, domain, how
        raise PartDesignFailed("CATIA refused AddNewDraft (" + "; ".join(refused) + ")")

    def _draft_error(self, feature, target, before, estimate, plan) -> str | None:
        b = self.builder
        if b._human(feature):
            return b._snapshot_error(target, before)
        body = b.bodies[target]
        got = b._volume(body) - before[0]
        # Corners between drafted faces add a term in tan^2.
        second = sum(float(f["area"]) ** 1.5 for f in feature["faces"]) * plan.tan ** 2
        tol = max(1e-3, 0.25 * abs(estimate), second)
        if abs(got - estimate) > tol:
            return (f"changed the body by {got:+.4f} mm3, about {estimate:+.4f} mm3 expected "
                    "(first order, no build123d reference)")
        return None

    # ── Extrude up to a limit ──

    def extrude_until(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        from catia_mcp.tree.builder import _flip_direction

        b = self.builder
        sketch = b.sketches[feature["sketch"]]
        spec = until_spec(feature, sketch)
        (target,) = feature["bodies"]
        operation = feature["operation"]
        tried: list[str] = []
        for way in LIMIT_WAYS[spec.mode]:
            made: list[Any] = []
            first = len(entry["catia"])
            try:
                if b._human(feature):
                    b._human_feature(
                        feature, entry,
                        lambda body, way=way, made=made: self._limited_prism(
                            body, sketch, spec, way, operation, entry, made),
                        flip=_flip_direction)
                else:
                    body = b.bodies[target]
                    before = b._volume(body)
                    self._limited_prism(body, sketch, spec, way, operation, entry, made)
                    self._sign_check(before, b._volume(body), operation, made[-1])
                    entry.setdefault("notes", []).append(
                        f"{made[-1].Name}: checked by the sign of the volume change only (no "
                        "build123d reference)")
                entry.setdefault("notes", []).append(f"{made[-1].Name}: limit {way}")
                if tried:
                    entry["notes"].append("; ".join(tried))
                return
            except Exception as error:  # the next limit mode
                tried.append(f"{way}: {error}")
                del entry["catia"][first:]
                for item in reversed(made):
                    with contextlib.suppress(Exception):
                        b._delete(item)
        raise PartDesignFailed(f"no Pad or Pocket up to {spec.mode} changed the body as the "
                               "tree's does (" + "; ".join(tried) + ")")

    def _limited_prism(self, body: Any, sketch: dict[str, Any], spec: UntilSpec, way: str,
                       operation: str, entry: dict[str, Any], made: list[Any]) -> Any:
        b = self.builder
        part = b.part
        limiting = self._limiting_element(body, spec, way, entry)
        frame = Frame.from_tree(sketch["plane"])
        catia_sketch, along, _ = b._draw(body, sketch, frame, entry,
                                         constrain=b.style == "human")
        made.append(catia_sketch)
        _force_solid_inwork(part, catia_sketch)
        part.InWorkObject = body
        method = part.ShapeFactory.AddNewPad if operation == "join" else \
            part.ShapeFactory.AddNewPocket
        prism = method(catia_sketch, PLACEHOLDER_LENGTH)
        made.append(prism)
        entry["catia"].append(str(prism.Name))
        limit = prism.FirstLimit
        mode = self._enum("CatLimitMode", way)
        limit.LimitMode = mode
        if _read_prop(limit, "LimitMode") not in (None, mode):
            raise PartDesignFailed(f"CATIA kept LimitMode {_read_prop(limit, 'LimitMode')} on "
                                   f"'{prism.Name}' after {mode} ({way})")
        if limiting is not None:
            limit.LimitingElement = limiting
        if spec.sign * along < 0 and not _set_prop(prism, "DirectionOrientation", 1):
            raise PartDesignFailed(f"CATIA refused the direction of '{prism.Name}'")
        part.UpdateObject(prism)
        return prism

    def _limiting_element(self, body: Any, spec: UntilSpec, way: str,
                          entry: dict[str, Any]) -> Any:
        """The plane or face a limit stops at, found before the prism is made
        (the face among the cells of the body's last feature)."""
        b = self.builder
        if way == "uptoplane":
            point, normal = spec.plane
            return b._support(plane_frame({"origin": point, "normal": normal}))[0]
        if way == "uptosurface":
            dressups = b.dressups
            candidates = dressups.face_candidates(body, entry)
            (matched,) = dressups._match([spec.face], candidates, match_faces)
            return dressups._face_reference(matched, spec.face, body, entry)
        return None

    @staticmethod
    def _sign_check(before: float, after: float, operation: str, prism: Any) -> None:
        change = after - before
        noise = max(1e-3, 1e-9 * before)
        if (operation == "join" and change <= noise) or (operation == "cut" and change >= -noise):
            raise PartDesignFailed(f"'{prism.Name}' changed the body by {change:+.4f} mm3, "
                                   f"which no {operation} up to a limit gives")

    # ── Lofts to a point ──

    def point_loft(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self.builder._into_body(feature, entry, lambda body: self._cone_into(feature, body,
                                                                            entry))

    def _cone_into(self, feature: dict[str, Any], body: Any, entry: dict[str, Any]) -> None:
        b = self.builder
        part = b.part
        plan = point_loft(feature)
        sketch = b.sketches[plan.sketch]
        want = cone_expectation(sketch, plan.point)
        size = max(want.volume, 1e-9) ** (1 / 3)
        tol = max(1e-3, FEATURE_REL_TOL * size)
        catia_sketch, _, _ = b._draw(body, sketch, Frame.from_tree(sketch["plane"]), entry,
                                     connect=True)
        apex = b._construction_point(plan.point)
        section = part.CreateReferenceFromObject(catia_sketch)
        tried: list[str] = []
        orders = ((apex, section), (section, apex)) if plan.point_first else \
            ((section, apex), (apex, section))
        for n, order in enumerate(orders):
            loft = None
            try:
                _force_solid_inwork(part, catia_sketch)
                part.InWorkObject = body
                loft = part.ShapeFactory.AddNewLoft()
                try:
                    shape = loft.HybridShape
                except Exception:  # some releases take the sections on the Loft itself
                    shape = loft
                from catia_mcp.tree.builder import _add_loft_section

                for ref in order:
                    _add_loft_section(b.conn.app, shape, ref, 1, None)
                part.UpdateObject(loft)
                volume, cog = b._volume(body), b._cog(body)
                if abs(volume - want.volume) <= max(1e-3, FEATURE_REL_TOL * want.volume) \
                        and distance(cog, want.cog) <= tol:
                    entry["catia"].append(str(loft.Name))
                    entry.setdefault("notes", []).append(
                        f"{loft.Name}: Multi-sections Solid to a point"
                        + (" (sections in the other order)" if n else ""))
                    return
                tried.append(f"loft, order {n + 1}: {volume:.4f} mm3 at {_fmt(cog)}")
            except Exception as error:  # CATIA refused a point section this way
                tried.append(f"loft, order {n + 1}: {error}")
            if loft is not None:
                with contextlib.suppress(Exception):
                    b._delete(loft)
        cone = right_cone(sketch, plan.point)
        if cone is None:
            raise PartDesignFailed(f"no Multi-sections Solid to the point gave {want.volume:.4f} "
                                   f"mm3 at {_fmt(want.cog)} (" + "; ".join(tried) + ")")
        with contextlib.suppress(Exception):
            b._delete(catia_sketch)  # the section a Shaft does not use
        profile, origin, axis = cone_profile(*cone)
        try:
            b._verified_shaft(body, profile, origin, axis, 360.0, entry)
        except Exception as error:
            raise PartDesignFailed("neither a loft to the point nor a Shaft gave the cone ("
                                   + "; ".join(tried + [f"shaft: {error}"]) + ")") from error
        entry.setdefault("notes", []).append(
            "a right circular cone CATIA did not loft to its point: built as a Shaft of its "
            "triangle (" + "; ".join(tried) + ")")

    # ── Hole threads ──

    def thread(self, hole: Any, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        """Thread a Hole after its first update (V5-6R2020 breaks the rebuild
        otherwise). The hole keeps the tree's tap-drill diameter: a standard
        that changes it is undone for "No Standard". A thread CATIA refuses
        leaves the Hole smooth, never lost: the entry's `thread` says
        `threaded: false` and why, and a note says so."""
        from catia_mcp.tree.builder import _set_value

        spec = thread_spec(feature)
        if spec is None:
            return
        part = self.builder.part
        record: dict[str, Any] = {"designation": spec.designation, "pitch": spec.pitch,
                                  "depth": spec.depth, "threaded": False}
        entry["thread"] = record
        mode, how = self._threaded_mode(hole)
        record["threading_mode"] = f"{mode} ({how})"
        record["verified"] = how == "type library"
        if not _set_prop(hole, "ThreadingMode", mode):
            self._smooth(hole, record, entry, ["CATIA refused ThreadingMode"])
            return
        tap = float(feature["diameter"])
        standards = [(COARSE_STANDARD if spec.coarse else FINE_STANDARD, spec.designation),
                     (NO_STANDARD, None)]
        tried: list[str] = []
        for standard, description in standards:
            try:
                hole.HoleThreadStandard = standard
                if description is not None:
                    hole.HoleThreadDescription.Value = description
                else:
                    _set_value(hole, "ThreadDiameter", spec.nominal)
                    _set_value(hole, "ThreadPitch", spec.pitch)
                if abs(float(hole.Diameter.Value) - tap) > 1e-6:
                    _set_value(hole, "Diameter", tap)
                if spec.depth is not None:
                    _set_value(hole, "ThreadDepth", spec.depth)
                if spec.side == "left":
                    hole.ThreadSide = self._enum("CatHoleThreadSide", "left")
                part.UpdateObject(hole)
                if abs(float(hole.Diameter.Value) - tap) > 1e-6:
                    raise PartDesignFailed(f"the standard set the diameter to "
                                           f"{float(hole.Diameter.Value):g} mm")
                record.update(threaded=True, standard=standard)
                if tried:
                    record["tried"] = tried
                self.builder.plan.drive(feature, "thread_depth",
                                        lambda h=hole: h.ThreadDepth)
                return
            except Exception as error:  # the next standard
                tried.append(f"{standard}: {error}")
        self._smooth(hole, record, entry, tried)

    def _smooth(self, hole: Any, record: dict[str, Any], entry: dict[str, Any],
                tried: list[str]) -> None:
        """The Hole stays, smooth: its volume is the tree's either way."""
        record["tried"] = tried
        entry.setdefault("notes", []).append(
            f"{hole.Name}: left smooth, CATIA did not thread it as {record['designation']} "
            f"({'; '.join(tried)})")
        self.builder.log(f"    {hole.Name}: not threaded ({'; '.join(tried)})")

    def _threaded_mode(self, hole: Any) -> tuple[int, str]:
        values = find_enum([hole, self.builder.conn.app], "CatHoleThreadingMode") or {}
        threaded = {value for name, value in values.items()
                    if "thread" in name.lower() and "smooth" not in name.lower()}
        if len(threaded) == 1:
            return threaded.pop(), "type library"
        return V5_THREADED_MODE, "the V5 B30 value: type library unreadable, unverified"

    # ── Enums ──

    def _enum(self, name: str, wanted: str) -> int:
        """The value of `name` whose member name contains `wanted`, from the
        type library when it is readable, else from the declared order."""
        if name not in self._enums:
            try:
                values = find_enum([self.builder.part.ShapeFactory, self.builder.conn.app], name)
            except Exception:
                values = None
            self._enums[name] = values or FALLBACK_ENUMS[name]
        hits = [value for key, value in self._enums[name].items() if wanted in key.lower()]
        if len(hits) != 1:
            raise PartDesignFailed(f"cannot tell which {name} value means '{wanted}' "
                                   f"(type library: {self._enums[name]})")
        return hits[0]


def _fmt(p: Vec) -> str:
    return "(" + ", ".join(f"{c:.3f}" for c in p) + ")"

