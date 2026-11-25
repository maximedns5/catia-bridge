"""The second Part Design slice in CATIA: cosmetic threads on a shaft, user
patterns of holes, bodies moved or mirrored in place, and (through
catia_mcp/tree/helix_com.py) sweeps along a helix.

A mixin of the TreeBuilder: `_feature_thread`, `_feature_move` and
`_feature_mirror_in_place` are dispatched like every feature, and the
builder's sweep and hole code call `_helix_sweep` and `_user_pattern`.
Each is one native, editable Part Design feature:

- a Thread on the matched cylindrical face, limited by the matched planar
  face (AddNewThreadWithRef, else AddNewThreadWithOutRef and its faces set),
  its side, diameter, depth and pitch set, the ISO standard loaded from its
  table (CreateStandardThreadDesignTable, the R428 route for holes) when it
  keeps the tree's values, else "No Standard". Cosmetic: the body's volume
  must not change, and the thread must read back the tree's values;
- one seed Hole and a User Pattern (AddNewUserPattern, located by a sketch of
  points: AddFeatureToLocatePositions), for holes in one plane that form no
  grid or circle. Live on R428 a user pattern of a Hole was refused on
  update (catia_mcp/tools/part_design.py), so the anchor point and the count
  are varied, and the caller falls back to one Hole per position;
- a Rotate and a Translate of the body in work (AddNewRotate2,
  AddNewTranslate2: the `copy` feature's code, without the paste), checked
  by the volume (unchanged) and the centre of gravity (R c + t);
- a Symmetry of the body in work (AddNewSymmetry2) about the tree's plane,
  checked by the volume and the mirrored centre of gravity.

None of this has run on a live CATIA yet: scripts/probe_part_design.py
runs the m11 fixtures (docs/part-design.md).
"""

from __future__ import annotations

import contextlib
from typing import Any

from catia_mcp.introspect import find_enum
from catia_mcp.tools.part_design import _read_prop
from catia_mcp.tree.anchors import match_faces
from catia_mcp.tree.geometry import Frame, Vec, distance, norm
from catia_mcp.tree.helix_com import HelixSweeps
from catia_mcp.tree.partdesign import ThreadSpec, plane_frame
from catia_mcp.tree.partdesign_com import (
    COARSE_STANDARD,
    FINE_STANDARD,
    NO_STANDARD,
    PartDesignFailed,
    _relation_names,
)
from catia_mcp.tree.partdesign_extra import (
    UserLayout,
    mirrored_point,
    positions_sketch,
    shaft_thread,
    user_layout,
    wants_user_pattern,
)

FEATURE_REL_TOL = 1e-3
# A cosmetic thread may not change the body's volume by more than this
# (relative): it adds no solid.
COSMETIC_REL_TOL = 1e-6
# CatThreadSide, and the thread standards' table kinds (CatHoleThreadStandard,
# read live on R428 for holes: thin pitch 0, thick pitch 1), used when the
# type library cannot be read.
FALLBACK_THREAD_SIDE = {"catRightSide": 0, "catLeftSide": 1}
FALLBACK_THREAD_STANDARD = {"catMetricThinPitch": 0, "catMetricThickPitch": 1}
VALUE_TOL = 1e-6


class ExtraFeatures:
    """TreeBuilder mixin (self is the TreeBuilder)."""

    # ── Sweeps along a helix ──

    def _helix_sweep(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._plus(HelixSweeps(self).sweep, feature, entry)

    # ── Cosmetic threads ──

    def _feature_thread(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._plus(self._thread, feature, entry)

    def _thread(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        dressups = self.dressups
        (target,) = feature["bodies"]
        body = self.bodies[target]
        spec = shaft_thread(feature)
        candidates = dressups.face_candidates(body, entry)
        anchors = [feature["face"], feature["limit_face"]]
        matched = dressups._match(anchors, candidates, match_faces)
        lateral, limit = (dressups._face_reference(c, a, body, entry)
                          for c, a in zip(matched, anchors, strict=True))
        before = self._volume(body)
        record: dict[str, Any] = {"designation": spec.designation, "nominal": spec.nominal,
                                  "pitch": spec.pitch, "length": spec.depth, "side": spec.side}
        entry["thread"] = record
        radius = feature["face"].get("radius")
        if radius is not None and abs(2 * float(radius) - spec.nominal) > VALUE_TOL:
            entry.setdefault("notes", []).append(
                f"the shaft is {2 * float(radius):g} mm, the thread {spec.designation} "
                f"({spec.nominal:g} mm): the thread keeps its nominal diameter")
        tried: list[str] = []
        for route, make in self._thread_routes(lateral, limit):
            thread = None
            try:
                self.part.InWorkObject = body
                thread = make()
                standard = self._thread_values(thread, spec, tried)
                got = self._volume(body)
                if abs(got - before) > max(1e-6, COSMETIC_REL_TOL * before):
                    raise PartDesignFailed(f"the cosmetic thread changed the body by "
                                           f"{got - before:+.6f} mm3")
                record.update(route=route, standard=standard,
                              read_back=_thread_values(thread))
                if tried:
                    record["tried"] = tried
                entry["catia"].append(str(thread.Name))
                self.plan.drive(feature, "length", lambda t=thread: t.Depth)
                return
            except Exception as error:  # the next route
                tried.append(f"{route}: {error}")
                if thread is not None:
                    with contextlib.suppress(Exception):
                        self._delete(thread)
        raise PartDesignFailed("CATIA made no Thread with the tree's values ("
                               + "; ".join(tried) + ")")

    def _thread_routes(self, lateral: Any, limit: Any) -> list[tuple[str, Any]]:
        factory = self.part.ShapeFactory

        def without_refs():
            thread = factory.AddNewThreadWithOutRef()
            thread.LateralFaceElement = lateral
            thread.LimitFaceElement = limit
            return thread

        return [("AddNewThreadWithRef", lambda: factory.AddNewThreadWithRef(lateral, limit)),
                ("AddNewThreadWithOutRef", without_refs)]

    def _thread_values(self, thread: Any, spec: ThreadSpec, tried: list[str]) -> str:
        """Side, diameter, depth and pitch, with the ISO standard's table when
        it keeps them, else "No Standard". Returns the standard kept."""
        standards = [(COARSE_STANDARD if spec.coarse else FINE_STANDARD, True),
                     (NO_STANDARD, False)]
        for standard, table in standards:
            loaded: list[str] = []
            try:
                if table:
                    loaded = self._thread_table(thread, spec)
                self._set_thread(thread, spec)
                self._update_thread(thread)
                mismatch = _thread_mismatch(thread, spec)
                if mismatch:
                    raise PartDesignFailed(mismatch)
                return standard
            except Exception as error:  # the next standard
                tried.append(f"{standard}: {error}")
                relations = self.part.Relations
                for name in loaded:
                    with contextlib.suppress(Exception):
                        relations.Remove(name)
        raise PartDesignFailed("; ".join(tried[-len(standards):]))

    def _thread_table(self, thread: Any, spec: ThreadSpec) -> list[str]:
        """Load the ISO table as on holes (live on R428 the Hole's standard
        could only be set this way). Returns the relations it added."""
        wanted = "thick" if spec.coarse else "thin"
        values = None
        with contextlib.suppress(Exception):
            values = find_enum([self.part.ShapeFactory, self.conn.app], "CatThreadStandard") \
                or find_enum([self.part.ShapeFactory, self.conn.app], "CatHoleThreadStandard")
        kinds = [v for k, v in (values or FALLBACK_THREAD_STANDARD).items()
                 if wanted in k.lower()]
        relations = self.part.Relations
        before = _relation_names(relations)
        thread.CreateStandardThreadDesignTable(kinds[0] if kinds else
                                               FALLBACK_THREAD_STANDARD[
                                                   f"catMetric{wanted.title()}Pitch"])
        return [n for n in _relation_names(relations) if n not in before]

    def _set_thread(self, thread: Any, spec: ThreadSpec) -> None:
        from catia_mcp.tree.builder import _set_value

        side = self._thread_side(spec.side)
        if _read_prop(thread, "Side") != side:
            thread.Side = side
        for member, value in (("Diameter", spec.nominal), ("Depth", spec.depth),
                              ("Pitch", spec.pitch)):
            current = _read_value(thread, member)
            if current is None or abs(current - value) > VALUE_TOL:
                _set_value(thread, member, value)

    def _update_thread(self, thread: Any) -> None:
        """Update; a thread CATIA runs off its face the other way is reversed
        once."""
        try:
            self.part.UpdateObject(thread)
        except Exception as first:
            try:
                thread.ReverseDirection()
                self.part.UpdateObject(thread)
            except Exception as error:
                raise PartDesignFailed(f"{first}; reversed: {error}") from error

    def _thread_side(self, side: str) -> int:
        values = None
        with contextlib.suppress(Exception):
            values = find_enum([self.part.ShapeFactory, self.conn.app], "CatThreadSide")
        hits = [v for k, v in (values or FALLBACK_THREAD_SIDE).items() if side in k.lower()]
        return hits[0] if len(hits) == 1 else FALLBACK_THREAD_SIDE[f"cat{side.title()}Side"]

    # ── User patterns of holes ──

    def _user_layout(self, feature: dict[str, Any], pattern: Any) -> UserLayout | None:
        """A User Pattern for the hole when the tree asks for one, or when its
        frames share a plane and form no grid or circle (three or more)."""
        layout = user_layout(feature)
        if layout is None:
            return None
        if wants_user_pattern(feature) or (pattern is None and len(layout.positions) >= 2):
            return layout
        return None

    def _user_pattern(self, body: Any, seed: Any, layout: UserLayout, target: str,
                      before: tuple[float, Vec], made: list[Any],
                      entry: dict[str, Any]) -> None:
        """A User Pattern of the seed Hole on a sketch of the other positions,
        checked against build123d's body. Tried as recorded on V5 (as many
        instances as points), then with the seed's position as its anchor
        point, then counting the seed among the instances."""
        part = self.part
        factory = part.ShapeFactory
        points = positions_sketch(layout)
        sketch, _, _ = self._draw(body, points, Frame.from_tree(points["plane"]), entry,
                                  constrain=self.style == "human")
        n = len(layout.positions)
        ways = (("as many instances as points", n, False),
                ("anchored at the seed", n, True),
                ("the seed counted", n + 1, False))
        tried: list[str] = []
        for label, count, anchored in ways:
            pattern = None
            try:
                part.InWorkObject = body
                pattern = factory.AddNewUserPattern(seed, count)
                pattern.AddFeatureToLocatePositions(sketch)
                if anchored:
                    pattern.AnchorPoint = self.datums.point(layout.origin)
                part.UpdateObject(pattern)
                error = self._snapshot_error(target, before)
                if error is None:
                    made.append(pattern)
                    entry["catia"].append(str(pattern.Name))
                    entry.setdefault("notes", []).append(
                        f"{pattern.Name}: User Pattern, {label}"
                        + (f" ({'; '.join(tried)})" if tried else ""))
                    return
                tried.append(f"{label}: {error}")
            except Exception as error:  # CATIA refused this way: the next one
                tried.append(f"{label}: {error}")
            if pattern is not None:
                with contextlib.suppress(Exception):
                    self._delete(pattern)
        # The positions sketch is hidden, not deleted: live on R428 deleting
        # a sketch after refused features opened a blocking dialog.
        self.plus._hide(sketch)
        raise PartDesignFailed("no User Pattern of the Hole matched the tree ("
                               + "; ".join(tried) + ")")

    # ── Bodies moved or mirrored in place ──

    def _feature_move(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._plus(self._move, feature, entry)

    def _move(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        """A body moved in place: a Rotate and a Translate in the body itself,
        the `copy` feature's transformations without the paste."""
        from catia_mcp.tree.builder import _is_identity
        from catia_mcp.tree.placement import placement_transform

        (name,) = feature["bodies"]
        body = self.bodies[name]
        rotation, t = placement_transform(feature["matrix"])
        volume, cog = self._volume(body), self._cog(body)
        turned = tuple(sum(rotation[i][k] * cog[k] for k in range(3)) for i in range(3))
        want = tuple(turned[i] + t[i] for i in range(3))
        tol = max(1e-3, FEATURE_REL_TOL * max(volume, 1e-9) ** (1 / 3))
        tried: list[str] = []
        self.part.InWorkObject = body
        try:
            made = [] if _is_identity(rotation) else \
                [self._rotate_body(body, rotation, turned, tol, tried)]
            if norm(t) > 1e-9:
                made.append(self._translate_body(body, t, want, tol, tried))
        except Exception as error:
            raise PartDesignFailed(f"CATIA did not move the body ({error}"
                                   + (f"; {'; '.join(tried)}" if tried else "") + ")") from error
        self._rigid_check(body, volume, want, tol, "moved")
        entry["catia"] += [str(m.Name) for m in made]
        if distance(cog, want) <= tol and made:
            entry.setdefault("notes", []).append(
                "the body's centre of gravity does not move: the transformation is checked "
                "by its volume only")
        if tried:
            entry.setdefault("notes", []).append("; ".join(tried))

    def _feature_mirror_in_place(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        self._plus(self._mirror_in_place, feature, entry)

    def _mirror_in_place(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        """A Symmetry of the body in work about the tree's plane: the body
        replaced by its mirror image (not united with it, as a Mirror is)."""
        (name,) = feature["bodies"]
        body = self.bodies[name]
        plane = feature["plane"]
        reference, _ = self._support(plane_frame(plane))
        volume, cog = self._volume(body), self._cog(body)
        want = mirrored_point(plane, cog)
        tol = max(1e-3, FEATURE_REL_TOL * max(volume, 1e-9) ** (1 / 3))
        factory = self.part.ShapeFactory

        def later():
            made = factory.AddNewSymmetry2(self.part.CreateReferenceFromName(""))
            made.HybridShape.Reference = reference
            return made

        tried: list[str] = []
        for label, make in (("AddNewSymmetry2", lambda: factory.AddNewSymmetry2(reference)),
                            ("AddNewSymmetry2, its plane set after", later)):
            made = None
            try:
                self.part.InWorkObject = body
                made = make()
                self.part.UpdateObject(made)
                self._rigid_check(body, volume, want, tol, "mirrored")
                entry["catia"].append(str(made.Name))
                notes = entry.setdefault("notes", [])
                notes.append(f"{made.Name}: {label}" + (f" ({'; '.join(tried)})" if tried
                                                        else ""))
                if distance(cog, want) <= tol:
                    notes.append("the body's centre of gravity lies on the mirror plane: the "
                                 "symmetry is checked by its volume only")
                return
            except Exception as error:  # the next way
                tried.append(f"{label}: {error}")
                if made is not None:
                    with contextlib.suppress(Exception):
                        self._delete(made)
        raise PartDesignFailed("CATIA did not mirror the body in place (" + "; ".join(tried)
                               + ")")

    def _rigid_check(self, body: Any, volume: float, want: Vec, tol: float, what: str) -> None:
        got_volume, got = self._volume(body), self._cog(body)
        if abs(got_volume - volume) > max(1e-3, FEATURE_REL_TOL * volume):
            raise PartDesignFailed(f"the {what} body measures {got_volume:.4f} mm3, "
                                   f"{volume:.4f} mm3 before")
        if distance(got, want) > tol:
            raise PartDesignFailed(f"the {what} body's centre of gravity is at {_fmt(got)}, "
                                   f"expected {_fmt(want)}")


def _read_value(obj: Any, member: str) -> float | None:
    try:
        return float(getattr(obj, member).Value)
    except Exception:  # not readable on this release
        return None


def _thread_values(thread: Any) -> dict[str, Any]:
    return {"diameter": _read_value(thread, "Diameter"), "depth": _read_value(thread, "Depth"),
            "pitch": _read_value(thread, "Pitch"), "side": _read_prop(thread, "Side")}


def _thread_mismatch(thread: Any, spec: ThreadSpec) -> str | None:
    """Which value the Thread does not read back as the tree's, if any."""
    got = _thread_values(thread)
    wrong = [f"{name} {got[name]:g} (not {want:g})"
             for name, want in (("diameter", spec.nominal), ("depth", spec.depth),
                                ("pitch", spec.pitch))
             if got[name] is not None and abs(got[name] - want) > VALUE_TOL]
    if wrong:
        return "the Thread reads " + ", ".join(wrong)
    return None


def _fmt(p: Vec) -> str:
    return "(" + ", ".join(f"{c:.3f}" for c in p) + ")"
