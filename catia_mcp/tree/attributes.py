"""Product properties, so the bill of materials reads right.

CATIA V5 (the part document's root `Product`): PartNumber (the tree's
`part_number`, else the part's name), Revision, Definition, Nomenclature,
DescriptionRef, Source (`CatProductSource`: unknown 0, made 1, bought 2) and
user properties (`UserRefProperties.CreateString` / `CreateReal` /
`CreateBoolean`, or the existing one's value), each read back. The tree part
id goes into the user property `TreeId`.

3DEXPERIENCE: the part's PLM object (the 3D Shape's VPMRepReference) takes
`V_Name` (title: the part's name), `V_description` and `PLM_ExternalID`
(the part number, often refused: the platform names objects). What the
platform has no attribute for, or refuses (revision, definition,
nomenclature, source, user properties), becomes a string parameter in a
`Properties` parameter set, so it stays on the part, noted per field.
"""

from __future__ import annotations

import contextlib
from typing import Any

from catia_mcp.tree.properties import route

SOURCE_CODES = {"unknown": 0, "made": 1, "bought": 2}
V5_FIELDS = (("part_number", "PartNumber"), ("revision", "Revision"),
             ("definition", "Definition"), ("nomenclature", "Nomenclature"),
             ("description", "DescriptionRef"))
X3D_FIELDS = (("title", "V_Name"), ("description", "V_description"),
              ("part_number", "PLM_ExternalID"))
X3D_PARAMETER_NAMES = {"part_number": "PartNumber", "revision": "Revision",
                       "definition": "Definition", "nomenclature": "Nomenclature",
                       "description": "Description", "source": "Source"}
PROPERTIES_SET = "Properties"


def wanted(ctx: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The standard fields and the user properties to set."""
    part = ctx.tree_part
    props = part.get("properties") or {}
    name = str(part.get("name") or ctx.record.name)
    fields = {"part_number": props.get("part_number") or name, "title": name}
    for key in ("revision", "definition", "nomenclature", "description", "source"):
        if props.get(key):
            fields[key] = props[key]
    user = dict(props.get("user") or {})
    user.setdefault("TreeId", str(part.get("id")))
    return fields, user


def apply_attributes(ctx: Any, knowledge: Any) -> dict[str, Any]:
    fields, user = wanted(ctx)
    if ctx.is_3dx:
        return _apply_3dx(ctx, knowledge, fields, user)
    return _apply_v5(ctx, fields, user)


def _apply_v5(ctx: Any, fields: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"set": {}, "refused": {}}
    record, product = route("PartDocument.Product", lambda: ctx.part.Parent.Product)
    result["routes"] = [record]
    if product is None:
        result["status"] = "failed"
        return result
    asked = {attribute: str(fields[key]) for key, attribute in V5_FIELDS if key in fields}
    for attribute, value in asked.items():
        try:
            setattr(product, attribute, value)
            result["set"][attribute] = str(getattr(product, attribute))
        except Exception as error:
            result["refused"][attribute] = str(error)
    if "source" in fields:
        try:
            product.Source = SOURCE_CODES[fields["source"]]
            result["set"]["Source"] = int(product.Source)
        except Exception as error:
            result["refused"]["Source"] = str(error)
    user_set: dict[str, Any] = {}
    try:
        properties = product.UserRefProperties
        for name, value in user.items():
            try:
                user_set[name] = _user_property(properties, name, value)
            except Exception as error:
                result["refused"][f"user:{name}"] = str(error)
    except Exception as error:
        result["refused"]["UserRefProperties"] = str(error)
    if user_set:
        result["user"] = user_set
    # CATIA renames a part number already used in the session.
    mismatched = {a: v for a, v in result["set"].items() if a in asked and v != asked[a]}
    if mismatched:
        result["kept_otherwise"] = mismatched
    result["status"] = "ok" if not result["refused"] and not mismatched else (
        "partial" if result["set"] else "failed")
    if not result["refused"]:
        del result["refused"]
    return result


def _user_property(properties: Any, name: str, value: Any) -> Any:
    """Set (or create) one user property; the value read back."""
    existing = None
    with contextlib.suppress(Exception):
        existing = properties.Item(name)
    if existing is not None:
        existing.Value = value if not isinstance(value, str) else str(value)
        made = existing
    elif isinstance(value, bool):
        made = properties.CreateBoolean(name, value)
    elif isinstance(value, (int, float)):
        made = properties.CreateReal(name, float(value))
    else:
        made = properties.CreateString(name, str(value))
    with contextlib.suppress(Exception):
        return made.Value
    return value


def _apply_3dx(ctx: Any, knowledge: Any, fields: dict[str, Any],
               user: dict[str, Any]) -> dict[str, Any]:
    from catia_mcp.plm import PLMSession

    result: dict[str, Any] = {"set": {}, "refused": {}, "as_parameters": {}}
    plm = PLMSession(ctx.conn)
    record, owner = route("PLM object of the part", lambda: plm.plm_object_of(ctx.part))
    result["routes"] = [record]
    left = dict(fields)
    if owner is not None:
        for key, attribute in X3D_FIELDS:
            if key not in fields:
                continue
            try:
                owner.SetAttributeValue(attribute, str(fields[key]))
                result["set"][attribute] = plm.attribute(owner, attribute)
                if key != "title":
                    left.pop(key, None)
            except Exception as error:
                result["refused"][attribute] = str(error)
    left.pop("title", None)
    # The rest (and the user properties) as string parameters on the part.
    pending = {X3D_PARAMETER_NAMES[k]: v for k, v in left.items()}
    pending.update({_parameter_name(n): v for n, v in user.items()})
    failed: dict[str, str] = {}
    for name, value in pending.items():
        try:
            entry = knowledge.create(name, "string", str(value), set_name=PROPERTIES_SET)
            result["as_parameters"][name] = entry.get("read")
        except Exception as error:
            failed[name] = str(error)
    if failed:
        result["parameters_refused"] = failed
    result["status"] = "ok" if not failed and result["set"] else (
        "partial" if result["set"] or result["as_parameters"] else "failed")
    for key in ("refused", "as_parameters"):
        if not result[key]:
            del result[key]
    return result


def _parameter_name(name: str) -> str:
    from catia_mcp.tree.knowledge import safe_name

    return safe_name(name)
