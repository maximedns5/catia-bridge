"""STEP fallback tools: import a STEP solid and paste it into a part as a
body without history ("dead" geometry), then report which bodies are dead.

CATIA V5 imports a STEP file by opening it as a new document; the import is
checked (a failed import still opens, with an empty PartBody and FaceKO
geometrical sets). 3DEXPERIENCE documents no scripted import: the user
imports the file by hand and catia_paste_dead_body takes it from its window.
Nothing here saves a document. The bridge closes a STEP document only when it
opened it. See catia_mcp/step.py and docs/step.md.
"""

from __future__ import annotations

import json
from typing import Any

from catia_mcp.automation import com_name, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import InvalidArgument, ObjectNotFound, after_mutation
from catia_mcp.step import (
    DEAD_BODY_PREFIX,
    ImportedSolid,
    StepExchange,
    StepImportFailed,
    volume_matches,
)

OPERATIONS = {"add": "join", "remove": "cut", "intersect": "intersect", "none": None}


class StepTools:
    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.exchange = StepExchange(connection)
        # STEP documents this session opened, by document name: only these
        # may be closed by the bridge.
        self.opened: dict[str, ImportedSolid] = {}
        self.previous_active: str | None = None

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_import_step",
                "description": (
                    "CATIA V5: import a STEP file (.stp/.step, one part) as a NEW Part "
                    "document, with file alerts suppressed, and check the import: the "
                    "main body must hold a solid, no FaceKO geometrical set (faces that "
                    "failed), a volume above zero, and expected_volume_mm3 when given. "
                    "Returns the document name, the solid's volume, area and centre of "
                    "gravity. The document stays open and unsaved; a failed import is "
                    "closed. Needs the STEP interface licence (ST1). On 3DEXPERIENCE "
                    "this fails with UNSUPPORTED_CAPABILITY and the manual import steps "
                    "(no scripted import exists); then use catia_paste_dead_body."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path of the STEP file (AP203/AP214, mm). It is "
                                           "copied to a temporary folder before opening.",
                        },
                        "expected_volume_mm3": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Optional: fail unless the solid measures this "
                                           "volume (relative tolerance 1e-4).",
                        },
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "catia_paste_dead_body",
                "description": (
                    "Copy the solid of an imported STEP part (source) and paste it into "
                    "a target part as a NEW body without history and without link "
                    "(Selection.PasteSpecial 'CATPrtResultWithOutLink'), checked by "
                    "volume, then optionally Add / Remove / Intersect it into a body of "
                    "the target, checked against what that operation can give. V5: "
                    "source and target are document names; the target defaults to the "
                    "document that was active before the last catia_import_step. "
                    "3DEXPERIENCE: source and target are window titles (import the STEP "
                    "by hand first); the target is required. Nothing is saved. The pasted "
                    "body is named STEP_<name> and stays dead geometry: it does not follow "
                    "edits of the native features."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "V5: the imported STEP document's name. "
                                           "3DEXPERIENCE: the imported part's window title "
                                           "(or part of it).",
                        },
                        "target": {
                            "type": "string",
                            "description": "Document name (V5) or window title "
                                           "(3DEXPERIENCE) of the part receiving the solid.",
                        },
                        "operation": {
                            "type": "string",
                            "enum": ["add", "remove", "intersect", "none"],
                            "default": "add",
                            "description": "What to do with the pasted body: Add it to "
                                           "into_body (default), Remove it from it, "
                                           "Intersect, or keep it as a separate body (none).",
                        },
                        "into_body": {
                            "type": "string",
                            "description": "Target body for the operation (default: the "
                                           "part's main body).",
                        },
                        "name": {
                            "type": "string",
                            "description": "Name for the pasted body, prefixed with STEP_ "
                                           "(default: the source's name).",
                        },
                        "expected_volume_mm3": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Optional: fail unless the source solid "
                                           "measures this volume.",
                        },
                        "close_source": {
                            "type": "boolean",
                            "default": False,
                            "description": "V5: close the STEP document afterwards (without "
                                           "saving). Only a document catia_import_step "
                                           "opened is closed; any other source stays open.",
                        },
                    },
                    "required": ["source"],
                },
            },
            {
                "name": "catia_list_dead_bodies",
                "description": (
                    "List the bodies of the active part and which are dead geometry (no "
                    "history): bodies named STEP_* by the STEP fallback, and bodies whose "
                    "features are all isolated solids (a STEP import or a paste as "
                    "result). Read-only."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_import_step":
                return self._import(arguments["file_path"], arguments.get("expected_volume_mm3"))
            case "catia_paste_dead_body":
                return self._paste(arguments)
            case "catia_list_dead_bodies":
                return self._list_dead()
            case _:
                raise ValueError(f"Unknown STEP tool: {tool_name}")

    def _import(self, file_path: str, expected_volume: float | None) -> str:
        self.conn.ensure_connected()
        previous = None
        if not self.conn.is_3dx:
            app = self.conn.app
            active = safe_get(app, "ActiveDocument") if int(self.conn.documents.Count) else None
            previous = com_name(active) if active is not None else None
        imported = self.exchange.import_file(file_path, expected_volume)
        if imported.opened_by_bridge:
            self.opened[imported.label] = imported
        self.previous_active = previous
        result = {"document": imported.label, **imported.summary(),
                  "previous_active_document": previous,
                  "note": "Open and unsaved. Paste it with catia_paste_dead_body."}
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _source(self, name: str, expected_volume: float | None) -> ImportedSolid:
        """A STEP document this session imported (still open), else the shape
        in the named document / window, checked like an import."""
        known = self.opened.get(name)
        if known is not None and not self.conn.is_3dx:
            try:
                self.exchange.find_document(name)
            except ObjectNotFound:
                self.opened.pop(name, None)
                known = None
        if known is None:
            return self.exchange.adopt(name, expected_volume)
        if expected_volume is not None and not volume_matches(known.volume_mm3, expected_volume):
            raise StepImportFailed(
                f"The STEP solid of {known.label} measures {known.volume_mm3:.4f} mm3, "
                f"expected {expected_volume:.4f} mm3.")
        return known

    def _target(self, name: str | None) -> tuple[Any, Any, Any, str]:
        """(part, selection owner, activate, label) of the target part."""
        if self.conn.is_3dx:
            if not name:
                raise InvalidArgument("On 3DEXPERIENCE pass target: the title of the window "
                                      "of the part receiving the solid.")
            window = self.exchange.find_window(name)
            window.Activate()
            return (self.conn.get_active_part(), self.conn.active_editor, window.Activate,
                    str(safe_get(window, "Caption", name)))
        name = name or self.previous_active
        if not name:
            raise InvalidArgument("Pass target: the name of the Part document receiving the "
                                  "solid.")
        doc = self.exchange.find_document(name)
        doc.Activate()
        try:
            part = doc.Part
        except Exception as error:
            raise InvalidArgument(f"Target '{name}' is not a Part document.") from error
        return part, doc, doc.Activate, com_name(doc)

    def _paste(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        operation = OPERATIONS[args.get("operation", "add")]
        source = self._source(args["source"], args.get("expected_volume_mm3"))
        part, owner, activate, label = self._target(args.get("target"))
        into = None
        if operation is not None:
            into_name = args.get("into_body")
            if into_name:
                try:
                    into = part.Bodies.Item(into_name)
                except Exception:
                    raise ObjectNotFound(f"No body named '{into_name}' in {label}.") from None
            else:
                into = part.MainBody
        base = args.get("name") or source.label.rsplit(".", 1)[0]
        body_name = base if base.startswith(DEAD_BODY_PREFIX) else DEAD_BODY_PREFIX + base
        body, volume, notes = self.exchange.paste_as_result(source, part, owner, activate,
                                                            body_name)
        result: dict[str, Any] = {"source": source.label, "target": label,
                                  "dead_body": com_name(body), "volume_mm3": round(volume, 4)}
        with after_mutation():
            if operation is not None:
                result["combine"] = self.exchange.combine(part, into, body, operation, volume)
            if args.get("close_source"):
                if self.exchange.close(source):
                    self.opened.pop(source.label, None)
                    result["source_closed"] = True
                    activate()
                else:
                    notes.append("the source was not closed: this session did not open it")
        result["notes"] = notes
        self.conn.refresh_display()
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _list_dead(self) -> str:
        part = self.conn.get_active_part()
        rows = self.exchange.dead_bodies(part)
        return json.dumps({"part": com_name(part), "bodies": rows,
                           "dead": [r["name"] for r in rows if r["dead"]]},
                          indent=2, ensure_ascii=False)
