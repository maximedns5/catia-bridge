"""STEP fallback: bring a solid without history into a part.

A feature with no native rule (a build123d `base_feature`, or a fillet the
replay cannot build yet) can still reach CATIA as geometry: a STEP file holds
the solid, CATIA imports it, and the solid is pasted into the target part as
a "dead" body (an isolated solid, no history) that is then added to or
removed from a native body.

CATIA V5 (documented, V5 Interoperability guide, "STEP: VBScript Macros"):
`Documents.Open("x.stp")` imports a STEP file into a NEW document, a
CATPart when the file holds geometry only. A successful Open proves nothing:
a failed import still opens, with an empty PartBody and geometrical sets
named `FaceKO...` holding the faces that could not be sewn. So every import
is checked: PartBody has shapes, no FaceKO set, a volume above zero, and,
when one is given, the expected volume. The file is first copied to a fresh
temporary folder (the guide warns that a read-only input stops the import
on a dialog), and file alerts are off during Open and Close (restored
afterwards). The bridge closes the imported document only when it opened it,
and never saves it.

3DEXPERIENCE: no scripted STEP import is documented (DSYAutomation.chm lists
none, and macro recording does not capture the Import command). The import
fails with UNSUPPORTED_CAPABILITY and the manual steps; once the user has
imported the file, `adopt` takes the editor showing it and the paste
continues as on V5.

Paste as result: copy the source body, select the target part, and
`Selection.PasteSpecial "CATPrtResultWithOutLink"`. CATIA creates a new body
holding an isolated solid, with no link to the source (live-checked on
3DEXPERIENCE R428 SP6; V5 documents the same format). The new body is found
by comparing the part's bodies before and after, and its volume must match
the source's.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from catia_mcp.automation import com_name, iter_items, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import (
    AmbiguousTarget,
    CATIAToolError,
    InvalidArgument,
    ObjectNotFound,
    UnsupportedCapability,
    WrongDocumentType,
    after_mutation,
)
from catia_mcp.introspect import type_name
from catia_mcp.measure import Measurer

STEP_EXTENSIONS = (".stp", ".step")
PASTE_AS_RESULT = "CATPrtResultWithOutLink"
# Geometrical sets a failed STEP import leaves behind (V5 guide, "STEP: Best
# Practices"), one per face CATIA could not transfer.
FAILED_FACES_PREFIX = "FaceKO"
# Bodies the STEP fallback creates are named with this prefix.
DEAD_BODY_PREFIX = "STEP_"
# An isolated solid's feature name: Solid.1 in English, Solide.1 in French
# (inferred; the type name "Solid" is checked first where readable).
_ISOLATED_SOLID = re.compile(r"^(Solid|Solide)\.\d+$", re.IGNORECASE)
# Relative volume tolerance between a STEP solid and what CATIA measures
# (the tree builder's per-part tolerance).
VOLUME_REL_TOL = 1e-4
MIN_VOLUME_MM3 = 1e-6
COMBINE_METHODS = {"join": "AddNewAdd", "cut": "AddNewRemove", "intersect": "AddNewIntersect"}

MANUAL_IMPORT_3DX = (
    "3DEXPERIENCE has no scripted STEP import: DSYAutomation.chm documents none, and "
    "macro recording does not capture the Import command. Import the file by hand, "
    "then let the bridge continue:\n"
    "1. In the top bar, click + (Add; 'Ajouter' in French) > Import ('Importer').\n"
    "2. Format: STEP. Source: File on disk. Pick {path}.\n"
    "3. Click Validate Import. CATIA saves the imported 3D Part to the database "
    "(your role needs the 'Converter for STEP' app and write access to the "
    "collaborative space) and titles it with the STEP product name plus a date "
    "suffix.\n"
    "4. Open it in its own window (catia_search_database / catia_open_from_database "
    "find it by title), check that it shows a solid, then call catia_paste_dead_body "
    "with source = that window's title and target = the window of the part to "
    "receive it."
)


def _same(a: Any, b: Any) -> bool:
    """COM identity (pywin32 compares the underlying IUnknown)."""
    try:
        return bool(a == b)
    except Exception:  # an object without comparison
        return a is b


class StepImportFailed(CATIAToolError):
    code = "STEP_IMPORT_FAILED"


@dataclass
class ImportedSolid:
    """A STEP solid open in CATIA, ready to paste."""

    label: str  # V5 document name, or 3DEXPERIENCE window caption
    part: Any
    body: Any
    owner: Any  # the V5 document or 3DEXPERIENCE editor holding the Selection
    activate: Callable[[], None]
    volume_mm3: float
    area_mm2: float | None
    cog_mm: list[float] | None
    shapes: list[str] = field(default_factory=list)
    geometrical_sets: list[str] = field(default_factory=list)
    path: str | None = None
    close: Callable[[], None] | None = None  # set only when the bridge opened it

    @property
    def opened_by_bridge(self) -> bool:
        return self.close is not None

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.label,
            "file": self.path,
            "body": com_name(self.body),
            "features": self.shapes,
            "geometrical_sets": self.geometrical_sets,
            "volume_mm3": round(self.volume_mm3, 4),
            "area_mm2": None if self.area_mm2 is None else round(self.area_mm2, 4),
            "cog_mm": None if self.cog_mm is None else [round(c, 4) for c in self.cog_mm],
            "opened_by_bridge": self.opened_by_bridge,
        }


def volume_matches(got: float, want: float) -> bool:
    return abs(got - want) <= max(1e-3, VOLUME_REL_TOL * abs(want))


def combine_bounds(operation: str, target: float, tool: float) -> tuple[float, float]:
    """The volumes an Add / Remove / Intersect of these two solids can give."""
    slack = max(1e-3, 1e-3 * (target + tool))
    return {
        "join": (max(target, tool) - slack, target + tool + slack),
        "cut": (target - tool - slack, target + slack),
        "intersect": (-slack, min(target, tool) + slack),
    }[operation]


class StepExchange:
    """Import STEP files (V5), adopt imported shapes (both platforms), and
    paste them into a part as bodies without history."""

    def __init__(self, conn: CATIAConnection, measure: Measurer | None = None) -> None:
        self.conn = conn
        self.measure = measure or Measurer(conn)

    # ── Import (V5) ──

    def import_file(self, path: str, expected_volume_mm3: float | None = None) -> ImportedSolid:
        """Open a STEP file as a new V5 document and check the solid."""
        self.conn.ensure_connected()
        if self.conn.is_3dx:
            raise UnsupportedCapability(MANUAL_IMPORT_3DX.format(path=path))
        if os.path.splitext(path)[1].lower() not in STEP_EXTENSIONS:
            raise InvalidArgument(f"{path} is not a STEP file (.stp or .step).")
        if not os.path.isfile(path):
            raise InvalidArgument(f"No STEP file at {path}.")
        # A fresh, writable copy: a read-only input stops the import on a
        # dialog, and the copy's unique folder keeps document names apart.
        copy = os.path.join(tempfile.mkdtemp(prefix="catia_step_"), os.path.basename(path))
        shutil.copyfile(path, copy)
        documents = self.conn.documents
        before = int(documents.Count)
        doc = self._quietly(lambda: documents.Open(copy))
        opened = int(documents.Count) > before
        close = (lambda: self._quietly(doc.Close)) if opened else None
        label = com_name(doc)
        try:
            part = doc.Part
        except Exception as error:  # a CATProduct: the file holds an assembly
            if close:
                close()
            raise WrongDocumentType(
                f"{os.path.basename(path)} imports as an assembly ({label}), not a single "
                "part. Write one STEP file per part.") from error
        try:
            return self._checked(label, part, doc, doc.Activate, expected_volume_mm3,
                                 path=path, close=close)
        except StepImportFailed:
            if close:
                close()
            raise

    def _quietly(self, action: Callable[[], Any]) -> Any:
        """Run with CATIA's file alerts off, then restore the previous setting."""
        app = self.conn.app
        previous = safe_get(app, "DisplayFileAlerts")
        if previous is not None:
            app.DisplayFileAlerts = False
        try:
            return action()
        finally:
            if previous is not None:
                app.DisplayFileAlerts = previous

    # ── Adopt (an imported shape the bridge did not open) ──

    def adopt(self, name: str | None = None,
              expected_volume_mm3: float | None = None) -> ImportedSolid:
        """The shape in a V5 document (by name) or a 3DEXPERIENCE window (by
        caption), or the active one; checked like an import. The bridge never
        closes it."""
        self.conn.ensure_connected()
        if self.conn.is_3dx:
            window = self.find_window(name) if name else self.conn.active_window
            window.Activate()
            editor = self.conn.active_editor
            part = self.conn.get_active_part()
            label = str(safe_get(window, "Caption", name or ""))
            return self._checked(label, part, editor, window.Activate, expected_volume_mm3)
        doc = self.find_document(name) if name else self.conn.active_document
        doc.Activate()
        try:
            part = doc.Part
        except Exception as error:
            raise WrongDocumentType(f"'{com_name(doc)}' is not a Part document.") from error
        return self._checked(com_name(doc), part, doc, doc.Activate, expected_volume_mm3)

    def find_document(self, name: str) -> Any:
        documents = self.conn.documents
        try:
            return documents.Item(name)
        except Exception:
            names = ", ".join(com_name(d) for d in iter_items(documents))
            raise ObjectNotFound(
                f"No open document named '{name}'. Open: {names or 'none'}.") from None

    def find_window(self, caption: str) -> Any:
        windows = list(iter_items(self.conn.app.Windows))
        captions = [str(safe_get(w, "Caption", "")) for w in windows]
        exact = [w for w, c in zip(windows, captions) if c == caption]
        matches = exact or [w for w, c in zip(windows, captions)
                            if caption.lower() in c.lower()]
        if not matches:
            raise ObjectNotFound(f"No window matches '{caption}'. Windows: "
                                 f"{', '.join(captions) or 'none'}.")
        if len(matches) > 1:
            raise AmbiguousTarget(f"'{caption}' matches several windows: " + ", ".join(
                c for w, c in zip(windows, captions) if any(w is m for m in matches))
                + ". Pass more of the caption.")
        return matches[0]

    # ── Checks ──

    def inspect(self, part: Any) -> dict[str, Any]:
        """What a STEP import left in a part: PartBody's features, the
        geometrical sets, and the FaceKO sets of a failed import."""
        body = part.MainBody
        shapes = [com_name(s) for s in iter_items(body.Shapes)]
        sets = [com_name(s) for s in iter_items(part.HybridBodies)]
        failed = [s for s in sets if s.startswith(FAILED_FACES_PREFIX)]
        return {"body": body, "shapes": shapes, "geometrical_sets": sets, "failed_sets": failed}

    def _checked(self, label: str, part: Any, owner: Any, activate: Callable[[], None],
                 expected_volume_mm3: float | None, path: str | None = None,
                 close: Callable[[], None] | None = None) -> ImportedSolid:
        found = self.inspect(part)
        body = found["body"]
        problems = []
        if not found["shapes"]:
            problems.append(f"its main body '{com_name(body)}' is empty")
        if found["failed_sets"]:
            problems.append(f"{len(found['failed_sets'])} face set(s) failed to transfer "
                            f"({', '.join(found['failed_sets'][:5])})")
        volume = 0.0
        if found["shapes"]:
            try:
                volume = self.measure.volume_mm3(part, body)
            except Exception as error:  # nothing measurable: no solid
                problems.append(f"its main body cannot be measured ({error})")
        if found["shapes"] and volume <= MIN_VOLUME_MM3 and not problems:
            problems.append("its main body has no volume (surfaces only)")
        if problems:
            where = os.path.basename(path) if path else label
            raise StepImportFailed(
                f"The STEP import of {where} failed: " + "; ".join(problems) + ". CATIA "
                "writes what went wrong to <file>.rpt and <file>.err in its CATReport "
                "folder. Check the solid in build123d (valid, tolerances under 1e-4 mm) "
                "and export it again as AP214 in mm.")
        if expected_volume_mm3 is not None and not volume_matches(volume, expected_volume_mm3):
            raise StepImportFailed(
                f"The STEP solid of {label} measures {volume:.4f} mm3 in CATIA, expected "
                f"{expected_volume_mm3:.4f} mm3.")
        # Area and centre of gravity are informative; the volume is the check.
        try:
            area = self.measure.area_mm2(part, body)
        except Exception:  # not measurable on this installation
            area = None
        try:
            cog = [float(c) for c in self.measure.cog_mm(part, body)]
        except Exception:  # not measurable on this installation
            cog = None
        return ImportedSolid(label=label, part=part, body=body, owner=owner, activate=activate,
                             volume_mm3=volume, area_mm2=area, cog_mm=cog,
                             shapes=found["shapes"], geometrical_sets=found["geometrical_sets"],
                             path=path, close=close)

    # ── Paste and combine ──

    def paste_as_result(self, source: ImportedSolid, target_part: Any, target_owner: Any,
                        activate_target: Callable[[], None],
                        body_name: str | None = None) -> tuple[Any, float, list[str]]:
        """Paste the source solid into a new body of the target part, without
        history and without a link. Returns the body, its volume, notes."""
        if _same(source.part, target_part):
            raise InvalidArgument("The source and the target are the same part.")
        source.activate()
        selection = source.owner.Selection
        selection.Clear()
        selection.Add(source.body)
        selection.Copy()
        selection.Clear()
        activate_target()
        before = list(iter_items(target_part.Bodies))
        target = target_owner.Selection
        target.Clear()
        target.Add(target_part)
        target.PasteSpecial(PASTE_AS_RESULT)
        target.Clear()
        notes: list[str] = []
        with after_mutation():
            target_part.Update()
            added = [b for b in iter_items(target_part.Bodies)
                     if not any(_same(b, old) for old in before)]
            if len(added) != 1:
                raise CATIAToolError(
                    f"PasteSpecial '{PASTE_AS_RESULT}' added {len(added)} bodies to the "
                    "target part instead of one. Inspect the part; nothing was rolled back.",
                    code="PASTE_FAILED", modified=None)
            body = added[0]
            if body_name:
                try:
                    body.Name = body_name
                except Exception as error:  # rename refused: keep CATIA's name
                    notes.append(f"body kept the name '{com_name(body)}' ({error})")
            volume = self.measure.volume_mm3(target_part, body)
            if not volume_matches(volume, source.volume_mm3):
                raise CATIAToolError(
                    f"The pasted body '{com_name(body)}' measures {volume:.4f} mm3, the "
                    f"source {source.volume_mm3:.4f} mm3. It stays in the part.",
                    code="WRONG_GEOMETRY_EFFECT", modified=None)
        return body, volume, notes

    def combine(self, part: Any, target: Any, tool: Any, operation: str,
                tool_volume_mm3: float | None = None) -> dict[str, Any]:
        """Add / Remove / Intersect a dead body into a target body, checked
        against what that operation can give."""
        if operation not in COMBINE_METHODS:
            raise InvalidArgument(f"Unknown operation '{operation}'.")
        # An empty body (a new part's PartBody) may not be measurable.
        before = 0.0 if _is_empty(target) else self.measure.volume_mm3(part, target)
        tool_volume = (tool_volume_mm3 if tool_volume_mm3 is not None
                       else self.measure.volume_mm3(part, tool))
        part.InWorkObject = target
        feature = getattr(part.ShapeFactory, COMBINE_METHODS[operation])(tool)
        with after_mutation():
            part.UpdateObject(feature)
            after = self.measure.volume_mm3(part, target)
            low, high = combine_bounds(operation, before, tool_volume)
            if not low <= after <= high:
                raise CATIAToolError(
                    f"'{com_name(feature)}' left {after:.4f} mm3 in '{com_name(target)}', "
                    f"outside what a {operation} of {before:.4f} and {tool_volume:.4f} mm3 can "
                    "give (coincident faces can make CATIA's boolean fail). It stays in "
                    "the part.", code="WRONG_GEOMETRY_EFFECT", modified=None)
        return {"feature": com_name(feature), "operation": operation, "target": com_name(target),
                "volume_before_mm3": round(before, 4), "volume_after_mm3": round(after, 4)}

    def close(self, source: ImportedSolid) -> bool:
        """Close the source document if the bridge opened it (never saved)."""
        if source.close is None:
            return False
        source.close()
        source.close = None
        return True

    # ── Dead geometry ──

    @staticmethod
    def dead_bodies(part: Any) -> list[dict[str, Any]]:
        """Each body of the part, and whether it is dead geometry: a body the
        STEP fallback named, or one whose features are all isolated solids."""
        rows = []
        for body in iter_items(part.Bodies):
            name = com_name(body)
            shapes = list(iter_items(body.Shapes))
            names = [com_name(s) for s in shapes]
            isolated = [n for s, n in zip(shapes, names) if _is_isolated_solid(s, n)]
            row: dict[str, Any] = {"name": name, "features": names}
            if name.startswith(DEAD_BODY_PREFIX):
                row["dead"], row["reason"] = True, f"named {DEAD_BODY_PREFIX}* by the STEP fallback"
            elif shapes and len(isolated) == len(shapes):
                row["dead"], row["reason"] = True, "only isolated solids (no history)"
            elif isolated:
                row["dead"] = False
                row["reason"] = f"partly dead: isolated solid(s) {', '.join(isolated)}"
            else:
                row["dead"] = False
            rows.append(row)
        return rows


def _is_empty(body: Any) -> bool:
    try:
        return int(body.Shapes.Count) == 0
    except Exception:  # unknown: measure it
        return False


def _is_isolated_solid(shape: Any, name: str) -> bool:
    kind = type_name(shape)
    if kind is not None:
        return kind == "Solid"
    return bool(_ISOLATED_SOLID.match(name))
