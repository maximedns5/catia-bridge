"""3D curves and the second slice of surface features in CATIA.

A mixin of surface_builder.SurfaceFeatures (so of TreeBuilder), dispatched
like every feature by `_feature_<type>`; the contract is in curves.py.

Curves go in a `Curves` geometrical set, named after the tree's curve:
Helix (`AddNewHelix` about a named axis, from a named start point), Spline
(`AddNewSpline` through named points, `AddPointWithConstraintExplicit` where
the tree gives tangents), Polyline (`AddNewPolyline`, `InsertElement`),
Circle (`AddNewCircleCtrRad` on a named plane), Projection (`AddNewProject`,
along a direction or normal) and Intersection (`AddNewIntersection`, with a
surface or a named plane). Surfaces of this slice go in the `Surfaces` set:
Sphere (`AddNewSphere`), Cylinder (`AddNewCylinder`), Translate, Rotate,
Symmetry, Scaling (`AddNewHybridScaling`, else an Affinity), Affinity, Trim
(`AddNewHybridTrim`), Extract (`AddNewExtract` of the face a face anchor
names, found as a shell's faces are), and ruled lofts through three or more
sections as one Multi-sections Surface per pair joined.

Checks: each curve's length (and its centre when CATIA measures it) against
build123d's (curve_reference.py, the expected properties' `curves`), each
surface's area and centre as in the first slice. Undocumented senses are
variants tried in order: a helix's axis and turning sense (and taper side),
a cylinder's direction, a rotation's sense, a trim's two sides, an extract's
propagation, a projection's solutions. A spline is CATIA's own interpolation
through the tree's points, not OpenCascade's: it, and what is made from it,
is checked within SPLINE_REL_TOL, and the report says by how much it differs.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from catia_mcp.automation import call_fill_array
from catia_mcp.tree.curves import band_name, helix_spec, occ_x_dir, vec
from catia_mcp.tree.geometry import Frame, Vec, distance, scale, unit

CURVE_SET = "Curves"
LENGTH_ABS_TOL, LENGTH_REL_TOL = 1e-3, 1e-3
# A spline (CATIA's interpolation through the tree's points) and anything made
# from it agree with build123d within this, relative.
SPLINE_REL_TOL = 2e-2
M_TO_MM = 1000.0
# HybridShapeExtract.PropagationType, in the order tried: no propagation,
# tangent continuity, CATIA's default.
EXTRACT_PROPAGATIONS = (3, 2, None)
# HybridShapeProject.SolutionType, in the order tried (None: CATIA's default).
PROJECT_SOLUTIONS = (None, 0, 1)


class MoreSurfaceFeatures:
    """TreeBuilder's curves and second-slice surface features (mixin)."""

    # ── State ──

    def _named_set(self, name: str) -> Any:
        state = self._surface_state()
        sets = state.setdefault("sets", {})
        if name not in sets:
            hybrid = self.part.HybridBodies
            for i in range(1, hybrid.Count + 1):
                if str(hybrid.Item(i).Name) == name:
                    sets[name] = hybrid.Item(i)
                    break
            else:
                sets[name] = hybrid.Add()
                sets[name].Name = name
        return sets[name]

    def _loose(self) -> set[str]:
        """The curves and surfaces made from a spline (checked within
        SPLINE_REL_TOL)."""
        return self._surface_state().setdefault("loose", set())

    def _mark_loose(self, feature: dict[str, Any]) -> bool:
        used = list(feature.get("inputs") or []) + [feature.get("guide")]
        loose = (feature.get("type") == "curve" and feature.get("op") == "spline") or \
            any(name in self._loose() for name in used if name)
        if loose:
            self._loose().update(feature.get("creates") or [])
        return loose

    def _is_shape(self, name: str) -> bool:
        return name in self._surface_state()["shapes"]

    def _profile_ref(self, name: str, entry: dict[str, Any]) -> Any:
        """A curve's reference, or a tree sketch drawn as a curve."""
        if self._is_shape(name):
            return self._surface_ref(name)
        return self._curve_sketch(self.sketches[name], entry)

    # ── Measures ──

    def _curve_length(self, shape: Any) -> float:
        """The curve's length in mm. 3DEXPERIENCE's unit is not documented:
        both readings are returned to the check, which keeps the one that
        fits (and remembers it)."""
        self._ensure_home()
        measure = self.measure
        if not self.conn.is_3dx:
            ref = self.part.CreateReferenceFromObject(shape)
            return float(measure.spa_measurable(ref).Length) * M_TO_MM
        measurable = measure._measurable_any_target(self.part, shape, "curve")
        raw = measure.scalar(measurable, ("GetLength", "Length"))
        factor = self._surface_state().get("length_factor")
        return raw * factor if factor else -raw  # negative: unit to be decided

    def _curve_cog(self, shape: Any) -> Vec | None:
        measure = self.measure
        try:
            if not self.conn.is_3dx:
                ref = self.part.CreateReferenceFromObject(shape)
                return tuple(call_fill_array(self.conn.app, measure.spa_measurable(ref),
                                             "GetCOG", 3))
            for kind in ("curve", "volume", "surface"):
                with contextlib.suppress(Exception):
                    measurable = measure._measurable_any_target(self.part, shape, kind)
                    for method in ("GetCOfG", "GetCOG"):
                        with contextlib.suppress(Exception):
                            return tuple(measure._triple(measurable, method))
        except Exception:  # not measurable: the length alone is checked
            return None
        return None

    def _length_mm(self, raw: float, want: float | None) -> float:
        """A length read by _curve_length, its unit decided (negative raw:
        m or mm, the one that fits build123d's, else m as areas are)."""
        if raw >= 0:
            return raw
        raw = -raw
        state = self._surface_state()
        for factor in (M_TO_MM, 1.0):
            if want is not None and abs(raw * factor - want) <= max(LENGTH_ABS_TOL,
                                                                    SPLINE_REL_TOL * want):
                state["length_factor"] = factor
                return raw * factor
        return raw * M_TO_MM

    # ── Curves ──

    def _feature_curve(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        variants = getattr(self, f"_curve_{feature['op']}")(feature, entry)
        self._keep_curve(feature, entry, variants)

    def _curve_helix(self, feature: dict[str, Any], entry: dict[str, Any]):
        spec = helix_spec(feature)
        axis_ref = self.datums.line(spec["origin"], spec["direction"], "Helix axis")
        start_ref = self.datums.point(spec["start"])
        factory = self.part.HybridShapeFactory
        taper = abs(spec["taper"])

        def build(invert: bool, clockwise: bool, outward: bool):
            made = factory.AddNewHelix(axis_ref, invert, start_ref, spec["pitch"],
                                       spec["height"], clockwise, 0.0, taper, outward)
            if not taper:
                self._drive_surface(feature, "pitch", made, "Pitch", 1.0)
            self._drive_surface(feature, "height", made, "Height", 1.0)
            return made

        variants = []
        outwards = [spec["taper"] > 0] + ([spec["taper"] <= 0] if taper else [])
        for invert in (False, True):
            for clockwise in (spec["left"], not spec["left"]):
                for outward in outwards:
                    label = (f"axis {'inverted' if invert else 'as given'}, "
                             f"{'clockwise' if clockwise else 'counter-clockwise'}"
                             + (f", taper {'outward' if outward else 'inward'}" if taper else ""))
                    variants.append((label, lambda i=invert, c=clockwise, o=outward:
                                     build(i, c, o)))
        return variants

    def _curve_spline(self, feature: dict[str, Any], entry: dict[str, Any]):
        points = [vec(p) for p in feature["points"]]
        tangents = [vec(t) for t in feature.get("tangents") or []]
        if len(tangents) == 2 and len(points) != 2:
            tangents = [tangents[0]] + [None] * (len(points) - 2) + [tangents[1]]
        refs = [self.datums.point(p) for p in points]
        factory = self.part.HybridShapeFactory

        def build(explicit: bool):
            made = factory.AddNewSpline()
            for i, ref in enumerate(refs):
                tangent = tangents[i] if explicit and tangents else None
                if tangent is None:
                    made.AddPoint(ref)
                else:
                    made.AddPointWithConstraintExplicit(
                        ref, factory.AddNewDirectionByCoord(*unit(tangent)), 1.0, 1, None, 0.0)
            if feature.get("closed"):
                made.SetClosing(1)
            return made

        variants = [("through the points" + (" with their tangents" if tangents else ""),
                     lambda: build(True))]
        if tangents:
            variants.append(("through the points, tangents left to CATIA", lambda: build(False)))
        return variants

    def _curve_polyline(self, feature: dict[str, Any], entry: dict[str, Any]):
        refs = [self.datums.point(vec(p)) for p in feature["points"]]
        factory = self.part.HybridShapeFactory

        def build():
            made = factory.AddNewPolyline()
            for i, ref in enumerate(refs, 1):
                made.InsertElement(ref, i)
            if feature.get("closed"):
                made.Closure = True
            return made

        return [("polyline", build)]

    def _curve_circle(self, feature: dict[str, Any], entry: dict[str, Any]):
        center, normal = vec(feature["center"]), unit(vec(feature["normal"]))
        frame = Frame.from_tree({"origin": list(center), "x_dir": list(occ_x_dir(normal)),
                                 "normal": list(normal)})
        plane_ref, _ = self._support(frame)
        center_ref = self.datums.point(center)
        radius = float(feature["radius"])
        factory = self.part.HybridShapeFactory

        def build():
            made = factory.AddNewCircleCtrRad(center_ref, plane_ref, False, radius)
            made.SetLimitation(1)  # the whole circle
            self._drive_surface(feature, "radius", made, "Radius", 1.0)
            return made

        return [("circle", build)]

    def _curve_project(self, feature: dict[str, Any], entry: dict[str, Any]):
        source, target = feature["inputs"]
        source_ref = self._profile_ref(source, entry)
        target_ref = self._surface_ref(target)
        direction = vec(feature["direction"]) if feature.get("direction") else None
        factory = self.part.HybridShapeFactory

        def build(solution):
            made = factory.AddNewProject(source_ref, target_ref)
            if direction is None:
                made.Normal = True
            else:
                made.Normal = False
                along = factory.AddNewDirectionByCoord(*direction)
                try:
                    made.Direction = along
                except Exception:  # the method form
                    made.SetDirection(along)
            if solution is not None:
                made.SolutionType = solution
            return made

        return [(f"solution type {s if s is not None else 'default'}", lambda s=s: build(s))
                for s in PROJECT_SOLUTIONS]

    def _curve_intersect(self, feature: dict[str, Any], entry: dict[str, Any]):
        from catia_mcp.tree.builder import _plane_frame

        first = self._surface_ref(feature["inputs"][0])
        if feature.get("plane") is not None:
            second, _ = self._support(_plane_frame(feature["plane"]))
        else:
            second = self._surface_ref(feature["inputs"][1])
        factory = self.part.HybridShapeFactory
        return [("intersection", lambda: factory.AddNewIntersection(first, second))]

    def _keep_curve(self, feature: dict[str, Any], entry: dict[str, Any],
                    variants: list[tuple[str, Callable[[], Any]]]) -> None:
        """Build each variant in turn until one matches build123d's curve
        (length, centre); keep it, named after the tree's curve."""
        from catia_mcp.tree.builder import BuildFailed

        (name,) = feature["creates"]
        loose = self._mark_loose(feature)
        want = (self._expected_part().get("curves") or {}).get(name)
        want_length = float(want["length_mm"]) if want else None
        tried: list[str] = []
        geoset = self._named_set(CURVE_SET)
        state = self._surface_state()
        for label, build in variants:
            shape = None
            state["drive"] = []
            try:
                shape = build()
                geoset.AppendHybridShape(shape)
                self.part.UpdateObject(shape)
                length = self._length_mm(self._curve_length(shape), want_length)
                cog = self._curve_cog(shape)
            except Exception as error:  # this variant is refused: the next
                tried.append(f"{label}: {error}")
                if shape is not None:
                    with contextlib.suppress(Exception):
                        self._delete(shape)
                continue
            problem = _measure_problem("length", length, cog, want_length,
                                       want.get("cog_mm") if want else None, loose, linear=True)
            if problem is None:
                self._kept(feature, entry, name, shape, label, tried, want, loose)
                deviation = abs(length - want_length) / want_length if want else None
                self._curve_rows().append({
                    "name": name, "feature": feature.get("id"), "catia": str(shape.Name),
                    "op": feature.get("op"), "length_mm": round(length, 4),
                    "expected_length_mm": round(want_length, 4) if want else None,
                    "cog_mm": [round(c, 4) for c in cog] if cog else None,
                    **({"deviation": round(deviation, 6)} if loose and want else {})})
                return
            tried.append(f"{label}: {problem}")
            with contextlib.suppress(Exception):
                self._delete(shape)
        raise BuildFailed(f"no way of building curve '{name}' matched build123d's "
                          f"({'; '.join(tried)})")

    def _curve_rows(self) -> list[dict[str, Any]]:
        return self.record.curves

    def _kept(self, feature, entry, name, shape, label, tried, want, loose) -> None:
        """A variant kept: its parameters driven, named, noted."""
        state = self._surface_state()
        state["shapes"][name] = shape
        for driven_feature, key, target, scale_ in state["drive"]:
            self.plan.drive(driven_feature, key, lambda target=target: target, scale_)
        with contextlib.suppress(Exception):
            shape.Name = name
        entry["catia"].append(str(shape.Name))
        if tried or not want or loose:
            notes = entry.setdefault("notes", [])
            if tried:
                notes.append(f"{name}: {label} ({'; '.join(tried)})")
            if not want:
                notes.append(f"{name}: no build123d reference, not checked")
            elif loose:
                notes.append(f"{name}: made from a spline, CATIA's interpolation through the "
                             f"tree's points: checked within {SPLINE_REL_TOL:.0%}")

    # ── Surfaces of this slice ──

    def _surface_sphere(self, feature: dict[str, Any], entry: dict[str, Any]):
        center_ref = self.datums.point(vec(feature["center"]))
        radius = float(feature["radius"])
        factory = self.part.HybridShapeFactory

        def build(empty_axis: bool, method: bool):
            # No axis system: Nothing, else an empty reference (as a circular
            # pattern's centre, live on R428).
            axis = self.part.CreateReferenceFromName("") if empty_axis else None
            made = factory.AddNewSphere(center_ref, axis, radius, -90.0, 90.0, 0.0, 360.0)
            if method:
                made.SetLimitation(1)
            else:
                made.Limitation = 1  # the whole sphere
            self._drive_surface(feature, "radius", made, "Radius", 1.0)
            return made

        return [(f"whole sphere, {'empty' if e else 'no'} axis"
                 + (", SetLimitation" if m else ""), lambda e=e, m=m: build(e, m))
                for e in (False, True) for m in (False, True)]

    def _surface_cylinder(self, feature: dict[str, Any], entry: dict[str, Any]):
        axis = feature["axis"]
        origin, direction = vec(axis["origin"]), unit(vec(axis["direction"]))
        radius, length = float(feature["radius"]), float(feature["length"])
        center_ref = self.datums.point(origin)
        factory = self.part.HybridShapeFactory

        def build(sign: float):
            along = factory.AddNewDirectionByCoord(*scale(direction, sign * math.copysign(1, length)))
            made = factory.AddNewCylinder(center_ref, radius, abs(length), 0.0, along)
            self._drive_surface(feature, "radius", made, "Radius", 1.0)
            return made

        return [("along the direction", lambda: build(1.0)),
                ("against the direction", lambda: build(-1.0))]

    def _surface_translate(self, feature: dict[str, Any], entry: dict[str, Any]):
        ref = self._surface_ref(feature["inputs"][0])
        vector = vec(feature["vector"])
        length = math.dist(vector, (0, 0, 0))
        factory = self.part.HybridShapeFactory

        def build(sign: float):
            along = factory.AddNewDirectionByCoord(*scale(unit(vector), sign))
            made = factory.AddNewTranslate(ref, along, length)
            self._drive_surface(feature, "distance", made, "Distance", 1.0)
            return made

        return [("along the vector", lambda: build(1.0)),
                ("against the vector", lambda: build(-1.0))]

    def _surface_rotate(self, feature: dict[str, Any], entry: dict[str, Any]):
        ref = self._surface_ref(feature["inputs"][0])
        axis = feature["axis"]
        axis_ref = self.datums.line(vec(axis["origin"]), unit(vec(axis["direction"])))
        angle = float(feature["angle"])
        factory = self.part.HybridShapeFactory

        def build(sign: float):
            made = factory.AddNewRotate(ref, axis_ref, sign * angle)
            self._drive_surface(feature, "angle", made, "Angle", sign)
            return made

        return [("angle as given", lambda: build(1.0)),
                ("angle reversed", lambda: build(-1.0))]

    def _surface_mirror(self, feature: dict[str, Any], entry: dict[str, Any]):
        from catia_mcp.tree.builder import _plane_frame

        ref = self._surface_ref(feature["inputs"][0])
        plane_ref, _ = self._support(_plane_frame(feature["plane"]))
        factory = self.part.HybridShapeFactory
        return [("symmetry", lambda: factory.AddNewSymmetry(ref, plane_ref))]

    def _surface_scale(self, feature: dict[str, Any], entry: dict[str, Any]):
        ref = self._surface_ref(feature["inputs"][0])
        center = vec(feature["center"])
        ratio = float(feature["ratio"])
        factory = self.part.HybridShapeFactory

        def scaling():
            made = factory.AddNewHybridScaling(ref, self.datums.point(center), ratio)
            self._drive_surface(feature, "ratio", made, "Ratio", 1.0)
            return made

        return [("scaling", scaling),
                ("affinity", lambda: self._affinity(ref, center, (ratio, ratio, ratio)))]

    def _surface_affinity(self, feature: dict[str, Any], entry: dict[str, Any]):
        ref = self._surface_ref(feature["inputs"][0])
        origin = vec(feature.get("origin") or (0, 0, 0))
        ratios = vec(feature["ratios"])
        return [("affinity", lambda: self._affinity(ref, origin, ratios))]

    def _affinity(self, ref: Any, origin: Vec, ratios: Vec) -> Any:
        """A GSD Affinity along the absolute axes, its origin a named point
        when it is not the part's origin."""
        made = self.part.HybridShapeFactory.AddNewAffinity(ref, *ratios)
        if any(abs(c) > 1e-12 for c in origin):
            made.Origin = self.datums.point(origin)
        return made

    def _surface_ruled_loft(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        """A ruled loft through three or more sections: one Multi-sections
        Surface per pair (each checked against build123d's band), joined."""
        (name,) = feature["creates"]
        bands = []
        for index, pair in enumerate(pairwise(feature["inputs"]), 1):
            band = dict(feature, inputs=list(pair), creates=[band_name(name, index)])
            self._keep_surface(band, entry, self._surface_loft(band, entry))
            bands.append(band_name(name, index))
        refs = [self._surface_ref(band) for band in bands]
        factory = self.part.HybridShapeFactory

        def join():
            made = factory.AddNewJoin(refs[0], refs[1])
            for ref in refs[2:]:
                made.AddElement(ref)
            return made

        self._keep_surface(feature, entry, [("bands joined", join)])

    def _feature_trim_surfaces(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        first, second = (self._surface_ref(name) for name in feature["inputs"])
        factory = self.part.HybridShapeFactory
        variants = [(f"orientations {a}, {b}",
                     lambda a=a, b=b: factory.AddNewHybridTrim(first, a, second, b))
                    for a in (1, -1) for b in (1, -1)]
        self._keep_surface(feature, entry, variants)

    def _feature_extract(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        from catia_mcp.tree.anchors import match_faces

        dressups = self.dressups
        body = self.bodies[feature["bodies"][0]]
        self._ensure_home()
        anchor = feature["face"]
        (matched,) = dressups._match([anchor], dressups.face_candidates(body, entry),
                                     match_faces)
        ref = dressups._face_reference(matched, anchor, body, entry)
        factory = self.part.HybridShapeFactory

        def build(propagation):
            made = factory.AddNewExtract(ref)
            if propagation is not None:
                made.PropagationType = propagation
            return made

        self._keep_surface(feature, entry, [
            (f"propagation {p if p is not None else 'default'}", lambda p=p: build(p))
            for p in EXTRACT_PROPAGATIONS])


def _measure_problem(kind: str, value: float, cog: Vec | None, want: float | None,
                     want_cog: Any, loose: bool, linear: bool = False) -> str | None:
    """Why a measured length (or area) and centre are not build123d's, or
    None. Without a reference, nothing to compare."""
    if want is None:
        return None
    rel = SPLINE_REL_TOL if loose else LENGTH_REL_TOL
    if abs(value - want) > max(LENGTH_ABS_TOL, rel * want):
        unit_ = "mm" if linear else "mm2"
        return f"{kind} {value:.4f} {unit_}, build123d {want:.4f} {unit_}"
    if cog is not None and want_cog:
        size = want if linear else math.sqrt(want)
        tol = max(LENGTH_ABS_TOL, rel * size)
        if distance(cog, tuple(want_cog)) > tol:
            return (f"centre at ({', '.join(f'{c:.3f}' for c in cog)}), build123d at "
                    f"({', '.join(f'{c:.3f}' for c in want_cog)})")
    return None
