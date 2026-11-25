"""Surface features in CATIA: Generative Shape Design, then Part Design.

A mixin of TreeBuilder (catia_mcp/tree/builder.py), dispatched like every
feature by `_feature_<type>`. The contract is in catia_mcp/tree/surfaces.py.

Each surface is one GSD feature in a `Surfaces` geometrical set, named after
the tree's surface: Extrude (`AddNewExtrude` along a direction), Revolve
(`AddNewRevol` about a named axis), Multi-sections Surface (`AddNewLoft`,
sections redrawn and coupled as the solid loft's), Sweep
(`AddNewSweepExplicit`, the path drawn as a sketch in its plane), Fill
(`AddNewFill`), Offset (`AddNewOffset`), Join (`AddNewJoin`) and Split
(`AddNewHybridSplit` by a named plane). Their sketches go in the same set
(`HybridSketches`) when CATIA allows it, else in the main body. `thicken` is
a Thick Surface (`AddNewThickSurface`) and `close_surface` a Close Surface
(`AddNewCloseSurface`), each in a body of its own, combined like an
extrude's tool body.

Checks: each surface's area (and its centre when CATIA measures it) against
build123d's (catia_mcp/tree/surface_reference.py, in the expected
properties' `surfaces`); each solid's volume and centre against build123d's
(`solids`). Where CATIA's sense is not documented (an extrude's direction, a
revolve's side, an offset's or a thick surface's side, a split's kept side,
a loft's closing points and orientation), the variants are tried in order
and the first that matches is kept; the report notes which. Without a
reference, the first variant CATIA builds is kept and the report says it was
not checked.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from catia_mcp.automation import call_fill_array
from catia_mcp.tree.curve_builder import SPLINE_REL_TOL, MoreSurfaceFeatures
from catia_mcp.tree.curves import vec
from catia_mcp.tree.extrude import extrude_vector
from catia_mcp.tree.geometry import Frame, Vec, distance, norm, scale, unit
from catia_mcp.tree.loft import plan_loft, section_sketch
from catia_mcp.tree.surfaces import sweep_frame
from catia_mcp.tree.sweep import path_sketch

SURFACE_SET = "Surfaces"
# A surface's area agrees within max(this mm2, AREA_REL_TOL of it); its
# centre within max(this mm, CENTRE_REL_TOL of the square root of its area).
AREA_ABS_TOL, AREA_REL_TOL = 1e-3, 1e-3
CENTRE_ABS_TOL, CENTRE_REL_TOL = 1e-3, 1e-3
SOLID_REL_TOL = 1e-3
# ThickSurface.iOffsetOrientation and HybridSplit orientations, in the order
# tried (the meaning of each is not documented).
THICK_ORIENTATIONS = (0, 1)
SPLIT_ORIENTATIONS = (1, -1)
M2_TO_MM2 = 1e6


class SurfaceFeatures(MoreSurfaceFeatures):
    """TreeBuilder's surface features (mixin); curves and the second slice's
    surfaces are in curve_builder.py."""

    # ── State ──

    def _surface_state(self) -> dict[str, Any]:
        state = getattr(self, "_surfaces", None)
        if state is None or state["part"] is not self.part:
            state = {"part": self.part, "shapes": {}, "set": None, "holder": None, "drive": []}
            self._surfaces = state
        return state

    def _surface_set(self) -> Any:
        state = self._surface_state()
        if state["set"] is None:
            hybrid = self.part.HybridBodies
            for i in range(1, hybrid.Count + 1):
                if str(hybrid.Item(i).Name) == SURFACE_SET:
                    state["set"] = hybrid.Item(i)
                    break
            else:
                state["set"] = hybrid.Add()
                state["set"].Name = SURFACE_SET
        return state["set"]

    def _surface_holder(self) -> Any:
        """Where surface sketches go: the Surfaces set when CATIA gives its
        sketches collection, else the main body."""
        state = self._surface_state()
        if state["holder"] is None:
            try:
                state["holder"] = SimpleNamespace(Sketches=self._surface_set().HybridSketches)
            except Exception:  # no sketches in a geometrical set on this CATIA
                state["holder"] = self.part.MainBody
        return state["holder"]

    def _curve_sketch(self, sketch: dict[str, Any], entry: dict[str, Any]) -> Any:
        """A tree sketch drawn as connected curves, as a reference."""
        frame = Frame.from_tree(sketch["plane"])
        catia_sketch, _, _ = self._draw(self._surface_holder(), sketch, frame, entry,
                                        connect=True)
        return self.part.CreateReferenceFromObject(catia_sketch)

    def _surface_ref(self, name: str) -> Any:
        return self.part.CreateReferenceFromObject(self._surface_state()["shapes"][name])

    def _expected_part(self) -> dict[str, Any]:
        return getattr(self, "expected_part", None) or {}

    # ── Measures ──

    def _surface_area(self, shape: Any) -> float:
        self._ensure_home()
        measure = self.measure
        if not self.conn.is_3dx:
            return measure.area_mm2(self.part, shape)
        error: Exception | None = None
        for kind in ("surface", "volume"):
            try:
                measurable = measure._measurable_any_target(self.part, shape, kind)
                return measure.scalar(measurable, ("GetArea", "Area")) * M2_TO_MM2
            except Exception as e:  # the other measurable kind
                error = e
        raise error  # type: ignore[misc]

    def _surface_cog(self, shape: Any) -> Vec | None:
        """The surface's centre of gravity, or None when CATIA does not give it."""
        measure = self.measure
        try:
            if not self.conn.is_3dx:
                ref = self.part.CreateReferenceFromObject(shape)
                return tuple(call_fill_array(self.conn.app, measure.spa_measurable(ref),
                                             "GetCOG", 3))
            for kind in ("surface", "volume"):
                with contextlib.suppress(Exception):
                    measurable = measure._measurable_any_target(self.part, shape, kind)
                    for method in ("GetCOfG", "GetCOG"):
                        with contextlib.suppress(Exception):
                            return tuple(measure._triple(measurable, method))
        except Exception:  # not measurable: the area alone is checked
            return None
        return None

    # ── Surfaces ──

    def _feature_surface(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        if feature["op"] == "loft" and len(feature["inputs"]) > 2:
            self._surface_ruled_loft(feature, entry)
            return
        variants = getattr(self, f"_surface_{feature['op']}")(feature, entry)
        self._keep_surface(feature, entry, variants)

    def _feature_join_surfaces(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        factory = self.part.HybridShapeFactory
        refs = [self._surface_ref(name) for name in feature["inputs"]]

        def join():
            made = factory.AddNewJoin(refs[0], refs[1])
            for ref in refs[2:]:
                made.AddElement(ref)
            return made

        self._keep_surface(feature, entry, [("join", join)])

    def _feature_split_surface(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        from catia_mcp.tree.builder import _plane_frame

        factory = self.part.HybridShapeFactory
        ref = self._surface_ref(feature["inputs"][0])
        frame = _plane_frame(feature["plane"])
        cutter, _ = self._support(frame)
        keep_top = feature["keep"] == "top"
        variants = [(f"orientation {o}", lambda o=o: factory.AddNewHybridSplit(ref, cutter, o))
                    for o in SPLIT_ORIENTATIONS]

        def side_check(shape: Any) -> str | None:
            """Without a reference, the kept side is read from the centre."""
            cog = self._surface_cog(shape)
            if cog is None:
                return None
            side = sum((c - o) * n for c, o, n in zip(cog, frame.origin, frame.normal,
                                                     strict=True))
            return None if (side > 0) == keep_top else "keeps the other side of the plane"

        self._keep_surface(feature, entry, variants, fallback_check=side_check)

    def _surface_extrude(self, feature: dict[str, Any], entry: dict[str, Any]):
        name = feature["inputs"][0]
        if self._is_shape(name):  # a 3D curve, along the tree's direction
            vector = vec(feature["direction"])
        else:
            vector = extrude_vector(feature, Frame.from_tree(self.sketches[name]["plane"]))
        length, direction = norm(vector), unit(vector)
        symmetric = bool(feature.get("symmetric"))
        ref = self._profile_ref(name, entry)
        factory = self.part.HybridShapeFactory

        def build(sign: float):
            made = factory.AddNewExtrude(ref, length, length if symmetric else 0.0,
                                         factory.AddNewDirectionByCoord(*scale(direction, sign)))
            if feature.get("direction") is None:
                dist = float(feature["distance"])
                self._drive_surface(feature, "distance", made, "BeginOffset",
                                    1.0 if dist >= 0 else -1.0)
                if symmetric:
                    self._drive_surface(feature, "distance", made, "EndOffset",
                                        1.0 if dist >= 0 else -1.0)
            return made

        variants = [("along the direction", lambda: build(1.0))]
        if not symmetric:
            variants.append(("against the direction", lambda: build(-1.0)))
        return variants

    def _surface_revolve(self, feature: dict[str, Any], entry: dict[str, Any]):
        axis = feature["axis"]
        origin = tuple(float(c) for c in axis["origin"])
        direction = unit(tuple(float(c) for c in axis["direction"]))
        angle = float(feature["angle"])
        ref = self._profile_ref(feature["inputs"][0], entry)
        axis_ref = self.datums.line(origin, direction)
        factory = self.part.HybridShapeFactory

        def build(first: float, second: float, member: str):
            made = factory.AddNewRevol(ref, first, second, axis_ref)
            self._drive_surface(feature, "angle", made, member, 1.0)
            return made

        variants = [("first angle", lambda: build(angle, 0.0, "FirstAngle"))]
        if angle < 360:
            variants.append(("second angle", lambda: build(0.0, angle, "SecondAngle")))
        return variants

    def _surface_fill(self, feature: dict[str, Any], entry: dict[str, Any]):
        ref = self._profile_ref(feature["inputs"][0], entry)

        def build():
            made = self.part.HybridShapeFactory.AddNewFill()
            made.AddBound(ref)
            return made

        return [("fill", build)]

    def _surface_offset(self, feature: dict[str, Any], entry: dict[str, Any]):
        ref = self._surface_ref(feature["inputs"][0])
        value = abs(float(feature["distance"]))
        factory = self.part.HybridShapeFactory
        return [(f"orientation {flag}", lambda flag=flag: factory.AddNewOffset(ref, value, flag, 0.0))
                for flag in (False, True)]

    def _surface_sweep(self, feature: dict[str, Any], entry: dict[str, Any]):
        if feature.get("guide") is not None:  # along a 3D curve
            profile = self._profile_ref(feature["inputs"][0], entry)
            guide = self._surface_ref(feature["guide"])
            factory = self.part.HybridShapeFactory
            return [("explicit sweep along the guide",
                     lambda: factory.AddNewSweepExplicit(profile, guide))]
        sketch = self.sketches[feature["inputs"][0]]
        frame = sweep_frame(sketch, feature["path"])
        profile = self._curve_sketch(sketch, entry)
        guide = self._curve_sketch(path_sketch(feature["path"], frame), entry)
        factory = self.part.HybridShapeFactory
        return [("explicit sweep", lambda: factory.AddNewSweepExplicit(profile, guide))]

    def _surface_loft(self, feature: dict[str, Any], entry: dict[str, Any]):
        """Sections redrawn with build123d's vertex pairing and coupled from
        3D closing points, as the solid loft (builder._verified_loft)."""
        from catia_mcp.tree.builder import (
            LOFT_CLOSING_MODES,
            LOFT_ORIENTATIONS,
            LOFT_VERTEX_COUPLING,
            _add_loft_section,
            _set_prop,
        )

        sections = [self.sketches[name] for name in feature["inputs"]]
        plan = plan_loft(*sections)
        drawn = []
        for sketch, segments, height in ((sections[0], plan.first, 0.0),
                                         (sections[1], plan.second, plan.height)):
            redrawn = section_sketch(sketch, segments, plan.common, height)
            drawn.append((self._curve_sketch(redrawn, entry),
                          plan.common.world(segments[0].a[0], segments[0].a[1], height)))
        factory = self.part.HybridShapeFactory

        def build(mode: str, orientation: int):
            closings = [self._closing_point(mode, point) for _, point in drawn]
            made = factory.AddNewLoft()
            if not _set_prop(made, "SectionCoupling", LOFT_VERTEX_COUPLING):
                raise RuntimeError("CATIA refused vertex coupling")
            for (ref, _), closing, sense in zip(drawn, closings, (1, orientation), strict=True):
                _add_loft_section(self.conn.app, made, ref, sense, closing)
            return made

        return [(f"closing points {mode}, orientation {orientation}",
                 lambda mode=mode, orientation=orientation: build(mode, orientation))
                for mode in LOFT_CLOSING_MODES for orientation in LOFT_ORIENTATIONS]

    def _drive_surface(self, feature: dict[str, Any], key: str, shape: Any, member: str,
                       scale_: float) -> None:
        """A user parameter on the surface's length or angle, when the tree
        gives the script's number and CATIA exposes the value; registered
        once the variant is kept."""
        try:
            target = getattr(shape, member)
            float(target.Value)
        except Exception:  # not a parameter object on this CATIA: stays editable as is
            return
        self._surface_state()["drive"].append((feature, key, target, scale_))

    def _keep_surface(self, feature: dict[str, Any], entry: dict[str, Any],
                      variants: list[tuple[str, Callable[[], Any]]],
                      fallback_check: Callable[[Any], str | None] | None = None) -> None:
        """Build each variant in turn until one matches build123d's surface;
        keep it, named after the tree's surface."""
        from catia_mcp.tree.builder import BuildFailed

        (name,) = feature["creates"]
        want = (self._expected_part().get("surfaces") or {}).get(name)
        loose = self._mark_loose(feature)  # made from a spline (curve_builder.py)
        tried: list[str] = []
        geoset = self._surface_set()
        state = self._surface_state()
        for label, build in variants:
            shape = None
            state["drive"] = []
            try:
                shape = build()
                geoset.AppendHybridShape(shape)
                self.part.UpdateObject(shape)
                area, cog = self._surface_area(shape), self._surface_cog(shape)
            except Exception as error:  # this variant is refused: the next
                tried.append(f"{label}: {error}")
                if shape is not None:
                    with contextlib.suppress(Exception):
                        self._delete(shape)
                continue
            problem = _surface_problem(area, cog, want, loose) if want else \
                (fallback_check(shape) if fallback_check else None)
            if problem is None:
                self._kept(feature, entry, name, shape, label, tried, want, loose)
                row = {"name": name, "feature": feature.get("id"), "catia": str(shape.Name),
                       "area_mm2": round(area, 4),
                       "expected_area_mm2": round(float(want["area_mm2"]), 4) if want else None,
                       "cog_mm": [round(c, 4) for c in cog] if cog else None}
                if loose and want:
                    row["deviation"] = round(abs(area - float(want["area_mm2"]))
                                             / float(want["area_mm2"]), 6)
                self.record.surfaces.append(row)
                return
            tried.append(f"{label}: {problem}")
            with contextlib.suppress(Exception):
                self._delete(shape)
        raise BuildFailed(f"no way of building surface '{name}' matched build123d's "
                          f"({'; '.join(tried)})")

    # ── Solids from surfaces ──

    def _feature_thicken(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        ref = self._surface_ref(feature["inputs"][0])
        thickness = abs(float(feature["thickness"]))
        factory = self.part.ShapeFactory
        variants = [(f"orientation {o}",
                     lambda o=o: factory.AddNewThickSurface(ref, o, thickness, 0.0))
                    for o in THICK_ORIENTATIONS]
        self._into_body(feature, entry, lambda body: self._solid_variants(body, feature, entry,
                                                                          variants))

    def _feature_close_surface(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        ref = self._surface_ref(feature["inputs"][0])
        factory = self.part.ShapeFactory
        self._into_body(feature, entry, lambda body: self._solid_variants(
            body, feature, entry, [("close", lambda: factory.AddNewCloseSurface(ref))]))

    def _solid_variants(self, body: Any, feature: dict[str, Any], entry: dict[str, Any],
                        variants: list[tuple[str, Callable[[], Any]]]) -> Any:
        """A Part Design feature on a surface, in `body` (alone in it), kept
        when its volume and centre are build123d's solid's."""
        from catia_mcp.tree.builder import BuildFailed

        want = (self._expected_part().get("solids") or {}).get(str(feature.get("id")))
        loose = feature["inputs"][0] in self._loose()
        part = self.part
        tried: list[str] = []
        for label, build in variants:
            made = None
            try:
                part.InWorkObject = body
                made = build()
                part.UpdateObject(made)
                volume, cog = self._volume(body), self._cog(body)
            except Exception as error:  # refused: the next way
                tried.append(f"{label}: {error}")
                if made is not None:
                    with contextlib.suppress(Exception):
                        self._delete(made)
                continue
            problem = _solid_problem(volume, cog, want, loose) if want else \
                (None if volume > 0 else "no volume")
            if problem is None:
                entry["catia"].append(str(made.Name))
                if loose and want:  # a spline's solid: the part check allows as much
                    self.record.spline_allowance_mm3 += SPLINE_REL_TOL * float(want["volume_mm3"])
                    entry.setdefault("notes", []).append(
                        f"{made.Name}: made from a spline, checked within {SPLINE_REL_TOL:.0%}")
                if tried or not want:
                    notes = entry.setdefault("notes", [])
                    if tried:
                        notes.append(f"{made.Name}: {label} ({'; '.join(tried)})")
                    if not want:
                        notes.append(f"{made.Name}: no build123d reference, its side and "
                                     f"volume ({volume:.4f} mm3) not checked")
                return made
            tried.append(f"{label}: {problem}")
            with contextlib.suppress(Exception):
                self._delete(made)
        raise BuildFailed(f"no way of building {feature['type']} {feature.get('id')} matched "
                          f"build123d's solid ({'; '.join(tried)})")


def _surface_problem(area: float, cog: Vec | None, want: dict[str, Any],
                     loose: bool = False) -> str | None:
    want_area = float(want["area_mm2"])
    rel = SPLINE_REL_TOL if loose else AREA_REL_TOL
    if abs(area - want_area) > max(AREA_ABS_TOL, rel * want_area):
        return f"area {area:.4f} mm2, build123d {want_area:.4f} mm2"
    if cog is not None and want.get("cog_mm"):
        rel = SPLINE_REL_TOL if loose else CENTRE_REL_TOL
        tol = max(CENTRE_ABS_TOL, rel * math.sqrt(want_area))
        if distance(cog, tuple(want["cog_mm"])) > tol:
            return (f"centre at ({', '.join(f'{c:.3f}' for c in cog)}), build123d at "
                    f"({', '.join(f'{c:.3f}' for c in want['cog_mm'])})")
    return None


def _solid_problem(volume: float, cog: Vec, want: dict[str, Any],
                   loose: bool = False) -> str | None:
    want_volume = float(want["volume_mm3"])
    rel = SPLINE_REL_TOL if loose else SOLID_REL_TOL
    if abs(volume - want_volume) > max(1e-3, rel * want_volume):
        return f"{volume:.4f} mm3, build123d {want_volume:.4f} mm3"
    tol = max(1e-3, rel * want_volume ** (1 / 3))
    if distance(cog, tuple(want["cog_mm"])) > tol:
        return (f"centre at ({', '.join(f'{c:.3f}' for c in cog)}), build123d at "
                f"({', '.join(f'{c:.3f}' for c in want['cog_mm'])})")
    return None
