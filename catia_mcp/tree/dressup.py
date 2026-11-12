"""Fillets, chamfers and shells in CATIA, their edges and faces found by geometry.

A tree fillet, chamfer or shell names its edges and faces by anchors
(catia_mcp/tree/anchors.py), never by index. For each one the builder:

1. Enumerates the edges or faces of the body's final shape with the
   repository's localized topology search (references.search_final_topology).
2. Drops the sketches' own wire cells (Selection_WireREdge, WireFVertex):
   the body search returns the edges and vertices of every sketch in the
   body too (live on R428: a block gives its 12 edges plus its sketch's 4).
   Those were the extra cells first taken for the tool body's after a
   Remove. Then keeps the cells the body's last feature owns: each cell's
   Reference.DisplayName ends with the result it belongs to
   ("...;Remove.1_ResultOUT;Z0;G1234)"), named by CATIA's internal,
   English feature name (Pad.1 for a pad shown as Extrusion.1 in French), so
   when no cell names the displayed name and they all name one same result,
   that result is the last feature's. The filter is switchable (auto,
   strict, off) and its census goes into the build report.
3. Measures every kept cell and matches each anchor to exactly one cell,
   with the contract's tolerances. V5: an edge's length and start, middle
   and end points. 3DEXPERIENCE has no GetPointsOnCurve (R428): an edge's
   length, then, for the edges of an anchor's length only, its distance to
   construction points at the anchor's sample points (the contract's test);
   the matched edge's ends are the anchor's. A face: area and centre of
   gravity.
4. Builds the references from the cells' DisplayName with
   Part.CreateReferenceFromBRepName (also unvalidated live), checks that each
   resolved reference still measures as the anchor says, and falls back to
   the selection's own Reference otherwise.
5. Creates one CATIA feature per tree feature (corners are solved jointly, so
   splitting or merging features would change them), with tangency
   propagation, as OpenCascade fillets and chamfers the whole tangent chain of
   each edge given: one edge per tangent chain is passed.
6. Checks the volume change against bounds (the strict check is the per-part
   volume against the build123d reference). An asymmetric chamfer is built
   both ways round and keeps the orientation whose centre of gravity sits
   where `distance` on the reference face puts it.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from catia_mcp.automation import call_fill_array, call_out_values, name_exists
from catia_mcp.introspect import find_enum
from catia_mcp.references import search_final_topology
from catia_mcp.tree.anchors import (
    AnchorError,
    EdgeCandidate,
    FaceCandidate,
    edge_from_points,
    edge_length_tol,
    match_edge,
    match_edges,
    match_face,
    match_faces,
    plane_of,
    representatives,
    tangent_chains,
)
from catia_mcp.tree.geometry import Vec, distance, dot

OWNERSHIP_MODES = ("auto", "strict", "off")
OWNERSHIP_ENV = "CATIA_MCP_TOPOLOGY_OWNERSHIP"
# How forum macros turn a selected cell's DisplayName into a label
# CreateReferenceFromBRepName accepts: drop "Selection_" and the trailing
# result context, append these flags. Undocumented; MFBRepVersion may vary.
BREP_FLAGS = ";WithPermanentBody;WithoutBuildError;WithSelectingFeatureSupport;MFBRepVersion_CXR15)"
_CONTEXT = re.compile(r";([^;()]+?)_ResultOUT(?=[;)])")
# A sketch's own cells, which the body search returns with the solid's.
_WIRE = re.compile(r"^(Selection_)?Wire")
# Declared order of the enums (CAA V5 help), used when the type library
# cannot be read.
FALLBACK_ENUMS = {
    "CatFilletEdgePropagation": {"catMinimalFilletEdgePropagation": 0,
                                 "catTangencyFilletEdgePropagation": 1},
    "CatChamferPropagation": {"catTangencyChamfer": 0, "catMinimalChamfer": 1},
    "CatChamferMode": {"catTwoLengthChamfer": 0, "catLengthAngleChamfer": 1},
    "CatChamferOrientation": {"catNoReverseChamfer": 0, "catReverseChamfer": 1},
}
# Interior angles down to about 35 degrees remove less than this times r^2
# per mm of a filleted edge (r^2 (cot(a/2) - (pi - a)/2), 0.215 at 90 deg).
FILLET_SECTION_MAX = 3.0
M_TO_MM, M2_TO_MM2 = 1000.0, 1e6


def is_sketch_wire(display_name: str) -> bool:
    return bool(_WIRE.match(display_name or ""))


def context_of(display_name: str) -> str | None:
    """The feature whose result a selected cell belongs to, from its
    DisplayName ("..._ResultOUT"), or None."""
    found = _CONTEXT.findall(display_name or "")
    return found[-1] if found else None


def canonical_brep_name(display_name: str) -> str | None:
    """The CreateReferenceFromBRepName label for a selected cell's
    DisplayName, or None when the name has no result context to replace."""
    name = display_name or ""
    name = name.removeprefix("Selection_")
    matches = list(_CONTEXT.finditer(name))
    if not matches:
        return None
    return name[: matches[-1].start()] + BREP_FLAGS


def length_mm(raw: float, start: Vec, mid: Vec, end: Vec) -> float:
    """An edge length read in m or in mm, told apart by the measured points:
    a line's length is its two chords, an arc's at most pi/2 times them."""
    chords = distance(start, mid) + distance(mid, end)
    for value in (raw * M_TO_MM, raw):
        if chords > 0 and chords * (1 - 1e-6) - 1e-9 <= value <= chords * math.pi / 2 * (1 + 1e-6):
            return value
    raise ValueError(f"the edge length {raw!r} agrees with its points ({chords:.6f} mm of "
                     "chords) in neither m nor mm")


def ownership_mode(value: str | None = None) -> str:
    mode = (value or os.environ.get(OWNERSHIP_ENV) or "auto").strip().lower()
    if mode not in OWNERSHIP_MODES:
        raise ValueError(f"topology ownership filter must be one of {', '.join(OWNERSHIP_MODES)}, "
                         f"got {mode!r}")
    return mode


@dataclass
class Cell:
    index: int
    reference: Any
    display_name: str
    context: str | None


class DressUpFailed(Exception):
    pass


class DressUps:
    """Fillet, chamfer and shell features of a TreeBuilder."""

    def __init__(self, builder: Any, ownership: str | None = None,
                 brep_references: bool = True) -> None:
        self.builder = builder
        self.ownership = ownership_mode(ownership)
        self.brep_references = brep_references
        self._enums: dict[str, dict[str, int]] = {}
        # 3DEXPERIENCE: what an edge length reads in (m or mm), and the
        # construction points at anchor samples, by position.
        self._length_factor = 1.0
        self._sample_points: dict[tuple[float, ...], Any] = {}

    # ── Features ──

    def _edges(self, feature: dict[str, Any], body: Any, entry: dict[str, Any]):
        """The tangent chains of the feature's edges, and a reference to one
        anchored edge of each."""
        everything = self.edge_candidates(body, entry)
        if self.builder.conn.is_3dx:
            self._scale_lengths(everything, feature["edges"])
        matched = self._match(feature["edges"], everything, match_edges)
        if self.builder.conn.is_3dx:
            # Only the matched edges' ends are known (the anchors'): chains
            # are found among them, and propagation adds the rest.
            matched = [self._from_anchor(candidate, anchor)
                       for candidate, anchor in zip(matched, feature["edges"], strict=True)]
            chains = tangent_chains(matched, matched)
        else:
            chains = tangent_chains(matched, everything)
        refs = [self._edge_reference(candidate, anchor, body, entry)
                for candidate, anchor in self._anchors_of(representatives(matched, chains),
                                                          matched, feature["edges"])]
        self._note_chains(entry, matched, chains)
        return chains, refs

    def fillet(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        body = self._body(feature)
        radius = float(feature["radius"])
        chains, refs = self._edges(feature, body, entry)
        propagation = self._enum("CatFilletEdgePropagation", "tangency")
        part = self.builder.part
        before = self.builder._volume(body)
        part.InWorkObject = body
        fillet = part.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius(
            refs[0], propagation, radius)
        for ref in refs[1:]:
            fillet.AddObjectToFillet(ref)
        entry["catia"].append(str(fillet.Name))
        part.UpdateObject(fillet)
        after = self.builder._volume(body)
        bound = sum(FILLET_SECTION_MAX * radius ** 2 * e.length * (1 + radius / (e.radius or math.inf))
                    for chain in chains for e in chain.edges)
        bound += 8 * len(chains) * radius ** 3
        self._check_change(fillet, before, after, bound, "fillet")

    def chamfer(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        body = self._body(feature)
        first = float(feature["distance"])
        second = float(feature.get("distance2") or first)
        chains, refs = self._edges(feature, body, entry)
        part = self.builder.part
        before = self.builder._volume(body)
        straight = self._enum("CatChamferOrientation", "noreverse")
        part.InWorkObject = body
        chamfer = part.ShapeFactory.AddNewChamfer(
            refs[0], self._enum("CatChamferPropagation", "tangency"),
            self._enum("CatChamferMode", "twolength"), straight, first, second)
        for ref in refs[1:]:
            chamfer.AddElementToChamfer(ref)
        entry["catia"].append(str(chamfer.Name))
        part.UpdateObject(chamfer)
        after = self.builder._volume(body)
        longest = max(first, second)
        bound = sum(first * second / 2 * e.length * (1 + longest / (e.radius or math.inf))
                    for chain in chains for e in chain.edges)
        bound += 4 * len(chains) * longest ** 3
        self._check_change(chamfer, before, after, bound, "chamfer")
        if abs(second - first) > 1e-9:
            self._orient_chamfer(feature, chamfer, body, first, second, straight, entry)

    def shell(self, feature: dict[str, Any], entry: dict[str, Any]) -> None:
        body = self._body(feature)
        thickness = float(feature["thickness"])
        candidates = self.face_candidates(body, entry)
        matched = self._match(feature["faces"], candidates, match_faces)
        refs = [self._face_reference(c, a, body, entry)
                for c, a in zip(matched, feature["faces"], strict=True)]
        inside, outside = (-thickness, 0.0) if thickness < 0 else (0.0, thickness)
        part = self.builder.part
        before, area = self.builder._volume(body), self._area(body)
        part.InWorkObject = body
        shell = part.ShapeFactory.AddNewShell(refs[0], inside, outside)
        for ref in refs[1:]:
            shell.AddFaceToRemove(ref)
        entry["catia"].append(str(shell.Name))
        part.UpdateObject(shell)
        after = self.builder._volume(body)
        t = abs(thickness)
        slack = max(1e-3, 1e-3 * before)
        if thickness < 0:
            # The wall is about the area times the thickness, less at convex
            # edges, a little more at concave ones.
            ok = 0 < after < before and after <= 1.5 * area * t + slack
            want = f"between 0 and {min(before, 1.5 * area * t):.4f} mm3 (about the area times " \
                   "the thickness)"
        else:
            ok = before < after <= before + 2 * area * t + 32 / 3 * math.pi * t ** 3 + slack
            want = f"more than {before:.4f} mm3 and less than twice the area times the thickness"
        if not ok:
            raise DressUpFailed(f"'{shell.Name}' left {after:.4f} mm3 from {before:.4f} mm3, "
                                f"expected {want}")

    # ── Topology ──

    def _body(self, feature: dict[str, Any]) -> Any:
        (target,) = feature["bodies"]
        self.builder._ensure_home()
        return self.builder.bodies[target]

    def last_feature_name(self, body: Any) -> str | None:
        try:
            shapes = body.Shapes
            return str(shapes.Item(int(shapes.Count)).Name)
        except Exception:
            return None

    def _context_feature(self, body: Any) -> Any:
        try:
            shapes = body.Shapes
            return shapes.Item(int(shapes.Count))
        except Exception:
            return body

    def cells(self, body: Any, kind: str) -> list[Cell]:
        """Every cell of `kind` the topology search returns for `body`."""
        conn = self.builder.conn
        sel = conn.hso
        found: list[Cell] = []
        try:
            count = search_final_topology(sel, body, kind, is_3dx=conn.is_3dx)
            for index in range(1, count + 1):
                reference = sel.Item(index).Reference
                try:
                    name = str(reference.DisplayName)
                except Exception:
                    name = ""
                found.append(Cell(index, reference, name, context_of(name)))
        finally:
            with contextlib.suppress(Exception):
                sel.Clear()
        return found

    def owned_cells(self, body: Any, kind: str, entry: dict[str, Any]) -> list[Cell]:
        """The cells of the body's last feature, with the census logged and
        recorded in the entry."""
        everything = self.cells(body, kind)
        found = [cell for cell in everything if not is_sketch_wire(cell.display_name)]
        last = self.last_feature_name(body)
        census = Counter(cell.context or "<no context>" for cell in found)
        record = {"kind": kind, "found": len(everything),
                  "sketch_wires": len(everything) - len(found), "last_feature": last,
                  "contexts": dict(census), "filter": self.ownership}
        kept = found
        if self.ownership != "off":
            owned = [cell for cell in found if last is not None and cell.context == last]
            contexts = set(census)
            if owned:
                kept = owned
            elif len(contexts) == 1 and "<no context>" not in contexts:
                # CATIA names results by its internal, English feature name
                # (Pad.1 for Extrusion.1): one result for every cell is the
                # last feature's.
                record["note"] = (f"every {kind.lower()} names the result "
                                  f"'{next(iter(contexts))}', taken as the last feature "
                                  f"'{last}' under its internal name")
            else:
                why = (f"no {kind.lower()} found names the body's last feature '{last}' "
                       f"(contexts seen: {dict(census)})")
                if self.ownership == "strict":
                    raise DressUpFailed(f"the topology ownership filter kept nothing: {why}")
                record["note"] = f"filter not applied: {why}"
        record["kept"] = len(kept)
        entry.setdefault("topology", []).append(record)
        self.builder.log(f"    {kind.lower()} cells: {len(everything)} found, "
                         f"{record['sketch_wires']} sketch wire(s) dropped, {len(kept)} kept "
                         f"(last feature {last}, contexts {dict(census)})")
        if not kept:
            raise DressUpFailed(f"the topology search found no {kind.lower()} on the body")
        return kept

    def edge_candidates(self, body: Any, entry: dict[str, Any]) -> list[EdgeCandidate]:
        candidates = []
        for cell in self.owned_cells(body, "Edge", entry):
            try:
                candidates.append(self.edge_candidate(cell.reference, cell))
            except Exception as error:
                raise DressUpFailed(f"could not measure edge {cell.index} "
                                    f"({cell.display_name or 'no name'}): {error}") from error
        return candidates

    def face_candidates(self, body: Any, entry: dict[str, Any]) -> list[FaceCandidate]:
        candidates = []
        for cell in self.owned_cells(body, "Face", entry):
            try:
                candidates.append(self.face_candidate(cell.reference, cell))
            except Exception as error:
                raise DressUpFailed(f"could not measure face {cell.index} "
                                    f"({cell.display_name or 'no name'}): {error}") from error
        return candidates

    def edge_candidate(self, reference: Any, handle: Any = None) -> EdgeCandidate:
        if self.builder.conn.is_3dx:
            return EdgeCandidate(handle if handle is not None else reference,
                                 self._edge_length_raw(reference) * self._length_factor,
                                 self._distance_to(reference),
                                 label=getattr(handle, "display_name", ""))
        length, start, mid, end, center = self._edge_geometry(reference)
        return edge_from_points(handle if handle is not None else reference, length, start, mid,
                                end, center=center, label=getattr(handle, "display_name", ""))

    def face_candidate(self, reference: Any, handle: Any = None) -> FaceCandidate:
        area, centroid = self._face_geometry(reference)
        return FaceCandidate(handle if handle is not None else reference, area, centroid,
                             getattr(handle, "display_name", ""))

    def _scale_lengths(self, candidates: list[EdgeCandidate], anchors) -> None:
        """3DEXPERIENCE edge lengths read without points to tell m from mm:
        keep the unit under which more anchors find an edge of their length."""
        def fits(factor: float) -> int:
            return sum(any(abs(c.length * factor - float(a["length"])) <= edge_length_tol(
                float(a["length"])) for c in candidates) for a in anchors)

        factor = max((1.0, M_TO_MM), key=fits)
        for candidate in candidates:
            candidate.length *= factor
        self._length_factor = factor

    def _distance_to(self, reference: Any):
        """The distance (mm) from a point to the edge, measured against a
        construction point there; only called for edges of an anchor's length."""
        def measure(point: Vec) -> float:
            return self._point_distance(reference, point)

        return measure

    def _sample_point(self, point: Vec) -> Any:
        key = tuple(round(float(c), 9) for c in point)
        if key not in self._sample_points:
            self._sample_points[key] = self.builder._construction_point(key)
        return self._sample_points[key]

    @staticmethod
    def _from_anchor(candidate: EdgeCandidate, anchor: dict[str, Any]) -> EdgeCandidate:
        """A matched edge with the anchor's ends, which it fits within the
        contract's tolerance."""
        def point(key: str) -> Vec | None:
            value = anchor.get(key)
            return tuple(float(c) for c in value) if value else None

        start, mid, end = point("start"), point("point"), point("end")
        if start is None or mid is None or end is None:
            return candidate  # ends unknown: a chain of its own
        radius = float(anchor["radius"]) if anchor.get("radius") else None
        return edge_from_points(candidate.handle, candidate.length, start, mid, end,
                                center=point("center"), radius=radius, normal=point("normal"),
                                label=candidate.label)

    # ── Measures (COM) ──

    def _edge_length_raw(self, reference: Any) -> float:
        """3DEXPERIENCE: an edge's length as the curve measurable reads it."""
        measurable = self.builder.measure.measurable_3dx(reference, "curve")
        return self.builder.measure.scalar(measurable, ("GetLength", "Length"))

    def _point_distance(self, reference: Any, point: Vec) -> float:
        """3DEXPERIENCE: the distance (mm) from `point` to the cell, measured
        against a construction point there (DistanceMinTo, live on R428)."""
        return float(self.builder.measure.min_distance(
            reference, self._sample_point(point))["distance_mm"])

    def _edge_geometry(self, reference: Any) -> tuple[float, Vec, Vec, Vec, Vec | None]:
        """Length (mm), start, middle and end points, and the centre when
        CATIA gives one. V5: SPAWorkbench Measurable.Length and
        GetPointsOnCurve; 3DEXPERIENCE: the curve measurable, whose length
        unit is told apart from the points (length_mm)."""
        measurer, app = self.builder.measure, self.builder.conn.app
        if self.builder.conn.is_3dx:
            measurable = measurer.measurable_3dx(reference, "curve")
            raw = measurer.scalar(measurable, ("GetLength", "Length"))
        else:
            measurable = measurer.spa_measurable(reference)
            raw = float(measurable.Length)
        try:
            values = call_fill_array(app, measurable, "GetPointsOnCurve", 9)
        except Exception:
            values = [float(v) for v in call_out_values(app, measurable, "GetPointsOnCurve", 9)]
        start, mid, end = (tuple(values[i:i + 3]) for i in (0, 3, 6))
        center = None
        if distance(start, end) < 1e-9:  # a closed edge: ask for its centre
            try:
                center = tuple(call_fill_array(app, measurable, "GetCenter", 3))
            except Exception:
                center = None
        return length_mm(raw, start, mid, end), start, mid, end, center

    def _face_geometry(self, reference: Any) -> tuple[float, Vec]:
        measurer, app = self.builder.measure, self.builder.conn.app
        if self.builder.conn.is_3dx:
            measurable = measurer.measurable_3dx(reference, "surface")
            area = measurer.scalar(measurable, ("GetArea", "Area")) * M2_TO_MM2
            method = "GetCOfG" if name_exists(measurable, "GetCOfG") else "GetCOG"
            try:
                cog = [float(v) for v in call_out_values(app, measurable, method, 3)]
            except Exception:
                cog = call_fill_array(app, measurable, method, 3)
        else:
            measurable = measurer.spa_measurable(reference)
            area = float(measurable.Area) * M2_TO_MM2
            cog = call_fill_array(app, measurable, "GetCOG", 3)
        return area, tuple(cog)

    def _area(self, body: Any) -> float:
        self.builder._ensure_home()
        return self.builder.measure.area_mm2(self.builder.part, body)

    # ── References ──

    def _match(self, anchors, candidates, match) -> list[Any]:
        try:
            return match(anchors, candidates)
        except AnchorError as error:
            raise DressUpFailed(f"{error}. The anchors are matched against the cells of the "
                                "body's last feature; see the topology census in the report") \
                from error

    @staticmethod
    def _anchors_of(picked, matched, anchors):
        for candidate in picked:
            index = next(i for i, m in enumerate(matched) if m is candidate)
            yield candidate, anchors[index]

    def _edge_reference(self, candidate: EdgeCandidate, anchor: dict[str, Any], body: Any,
                        entry: dict[str, Any]) -> Any:
        return self._reference(candidate.handle, body, entry,
                               lambda ref: match_edge(anchor, [self.edge_candidate(ref)]))

    def _face_reference(self, candidate: FaceCandidate, anchor: dict[str, Any], body: Any,
                        entry: dict[str, Any]) -> Any:
        return self._reference(candidate.handle, body, entry,
                               lambda ref: match_face(anchor, [self.face_candidate(ref)]))

    def _reference(self, cell: Cell, body: Any, entry: dict[str, Any], still_fits) -> Any:
        """A reference to `cell` by its BRep name, checked to still measure as
        the anchor says, or else the selection's own reference."""
        counts = entry.setdefault("references", {"brep_name": 0, "selection": 0})
        label = canonical_brep_name(cell.display_name) if self.brep_references else None
        if label is not None:
            try:
                reference = self.builder.part.CreateReferenceFromBRepName(
                    label, self._context_feature(body))
                still_fits(reference)
                counts["brep_name"] += 1
                return reference
            except Exception as error:  # a COM error or a resolved cell that does not fit
                entry.setdefault("notes", []).append(
                    f"BRep name of cell {cell.index} not used ({error}): selection reference")
        counts["selection"] += 1
        return cell.reference

    # ── Checks ──

    def _enum(self, name: str, wanted: str) -> int:
        """The value of `name` whose member name contains `wanted` (tangency,
        twolength, noreverse, reverse), from the type library when it is
        readable, else from the declared order."""
        if name not in self._enums:
            try:
                values = find_enum([self.builder.part.ShapeFactory, self.builder.conn.app], name)
            except Exception:
                values = None
            self._enums[name] = values or FALLBACK_ENUMS[name]
        values = self._enums[name]
        hits = [value for key, value in values.items()
                if wanted in key.lower() and not (wanted == "reverse" and "noreverse" in key.lower())]
        if len(hits) != 1:
            raise DressUpFailed(f"cannot tell which {name} value means '{wanted}' "
                                f"(type library: {values})")
        return hits[0]

    @staticmethod
    def _note_chains(entry, matched, chains) -> None:
        propagated = sum(len(chain.edges) for chain in chains) - len(matched)
        if len(chains) < len(matched) or propagated:
            entry.setdefault("notes", []).append(
                f"{len(matched)} anchored edge(s) in {len(chains)} tangent chain(s) of "
                f"{sum(len(chain.edges) for chain in chains)} edge(s): one edge per chain, "
                "tangency propagation does the rest")

    @staticmethod
    def _check_change(feature: Any, before: float, after: float, bound: float, kind: str) -> None:
        change = after - before
        noise = max(1e-6, 1e-9 * before)
        if after <= 0 or abs(change) <= noise or abs(change) > bound * (1 + 1e-3) + noise:
            raise DressUpFailed(
                f"'{feature.Name}' changed the volume by {change:+.4f} mm3 (from {before:.4f}), "
                f"outside what a {kind} of these edges can (0 to {bound:.4f} mm3 either way)")

    def _orient_chamfer(self, feature, chamfer, body, first, second, straight, entry) -> None:
        """`distance` is measured on the reference face F. With the shorter
        length on F's side, the removed (or added) prism sits nearer to F's
        plane than with the lengths swapped, so the body's centre of
        gravity ends lower along F's outward normal: build both ways and keep
        the one that sits as the tree's lengths put it."""
        _, normal = plane_of(feature["face"])
        part = self.builder.part

        def height() -> float:
            return dot(self.builder._cog(body), normal)

        first_way = height()
        reverse = self._enum("CatChamferOrientation", "reverse")
        chamfer.Orientation = reverse
        part.UpdateObject(chamfer)
        other_way = height()
        if abs(first_way - other_way) <= 1e-9 * max(1.0, abs(first_way)):
            raise DressUpFailed(f"'{chamfer.Name}': both orientations put the centre of gravity "
                                "at the same height, so the face its first distance is measured "
                                "on cannot be told")
        # The tree's way has the lower centre of gravity when distance2 < distance.
        keep_reverse = (other_way < first_way) == (second < first)
        if not keep_reverse:
            chamfer.Orientation = straight
            part.UpdateObject(chamfer)
        else:
            entry.setdefault("notes", []).append(
                f"{chamfer.Name}: orientation reversed so that {first:g} mm is on the reference "
                "face")
        self.builder.cog_checked = True
