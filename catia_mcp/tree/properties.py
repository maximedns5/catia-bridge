"""Part properties after the geometry: appearance, material and mass,
product properties (the BOM), and Knowledgeware as a designer would set it.

Every field is optional and additive to tree v1 (docs/materials-properties.md):

    "color": [r, g, b]                    sRGB 0..1 (the STEP's colours too)
    "material": {"name", "density_kg_m3", "catalog_name"?, "family"?}
    "properties": {"part_number"?, "revision"?, "definition"?, "nomenclature"?,
                   "description"?, "source"?: made | bought | unknown,
                   "user"?: {name: value}}
    "parameters": [{"name", "type", "value", "comment"?}]
    "configurations": [{"name", "params": {parameter: value}}]
    "checks": [{"name", "parameter", "min"?, "max"?, "comment"?}]
    features[i]."color": [r, g, b]

The builder runs this step once a part is built and checked (its hook is
`TreeBuilder.properties`). Each sub-step tries its COM routes in order,
records which worked, and never fails the part: a property is a bonus. The
part's report gets a `properties` section.

The tree gives data only: checks are bounds, written into a check expression
by the bridge; no rule text from the tree is ever run.
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Callable
from typing import Any

from catia_mcp.tree.parameters import parameter_name, spec_for

PARAMETER_TYPES = ("length", "angle", "real", "integer", "boolean", "string")
PROPERTY_KEYS = ("part_number", "revision", "definition", "nomenclature", "description",
                 "source", "user")
SOURCES = ("made", "bought", "unknown")
FIELDS = ("color", "material", "properties", "parameters", "configurations", "checks")
MODES = ("auto", "on", "off")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# ── Contract ──

def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def color_problem(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3 \
            or not all(_number(c) and 0.0 <= c <= 1.0 for c in value):
        return "a color is three sRGB numbers between 0 and 1"
    return None


def value_problem(kind: str, value: Any) -> str | None:
    if kind in ("length", "angle", "real"):
        return None if _number(value) else f"a {kind} value is a number"
    if kind == "integer":
        return None if _number(value) and float(value).is_integer() \
            else "an integer value is a whole number"
    if kind == "boolean":
        return None if isinstance(value, bool) else "a boolean value is true or false"
    return None if isinstance(value, str) else "a string value is text"


def script_parameter_names(part: dict[str, Any]) -> set[str]:
    """The user parameters the builder makes from the script's numbers
    (catia_mcp/tree/parameters.py), as a configuration may name them."""
    names: set[str] = set()
    for feature in part.get("features") or []:
        for key in (feature.get("params") or {}):
            spec = spec_for(feature, key)
            if spec is not None:
                names.add(parameter_name(feature, spec, key))
    return names


def problems(part: dict[str, Any]) -> list[str]:
    """Every reason the part's property fields are malformed (empty: fine)."""
    out: list[str] = []
    if "color" in part and (why := color_problem(part["color"])):
        out.append(why)
    for feature in part.get("features") or []:
        if isinstance(feature, dict) and "color" in feature \
                and (why := color_problem(feature["color"])):
            out.append(f"feature {feature.get('id')}: {why}")
    material = part.get("material")
    if material is not None:
        if not isinstance(material, dict) or not isinstance(material.get("name"), str) \
                or not material["name"].strip():
            out.append("a material has a name")
        elif not _number(material.get("density_kg_m3")) or material["density_kg_m3"] <= 0:
            out.append("a material has a positive density_kg_m3")
        else:
            for key in ("catalog_name", "family"):
                if key in material and not isinstance(material[key], str):
                    out.append(f"a material's {key} is text")
    props = part.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            out.append("properties is an object")
        else:
            for key, value in props.items():
                if key not in PROPERTY_KEYS:
                    out.append(f"unknown property '{key}' (one of {', '.join(PROPERTY_KEYS)})")
                elif key == "user":
                    if not isinstance(value, dict) or not all(
                            isinstance(k, str) and k.strip() and
                            (isinstance(v, (str, bool)) or _number(v))
                            for k, v in value.items()):
                        out.append("user properties map names to text, numbers or booleans")
                elif key == "source" and value not in SOURCES:
                    out.append(f"source is one of {', '.join(SOURCES)}")
                elif not isinstance(value, str):
                    out.append(f"property {key} is text")
    declared: dict[str, str] = {}
    parameters = part.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, list):
            out.append("parameters is a list")
            parameters = []
        script = script_parameter_names(part)
        for spec in parameters:
            if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) \
                    or not _NAME.match(spec["name"]):
                out.append("a parameter name starts with a letter and holds letters, digits "
                           "and underscores")
                continue
            name, kind = spec["name"], spec.get("type")
            if kind not in PARAMETER_TYPES:
                out.append(f"parameter {name}: type is one of {', '.join(PARAMETER_TYPES)}")
                continue
            if name in declared or name in script:
                out.append(f"parameter {name} is declared twice (or is a script parameter)")
            if (why := value_problem(kind, spec.get("value"))):
                out.append(f"parameter {name}: {why}")
            if "comment" in spec and not isinstance(spec["comment"], str):
                out.append(f"parameter {name}: a comment is text")
            declared[name] = kind
    configurations = part.get("configurations")
    if configurations is not None:
        known = {**{n: "length" for n in script_parameter_names(part)}, **declared}
        if not isinstance(configurations, list) or not configurations:
            out.append("configurations is a non-empty list")
            configurations = []
        seen: set[str] = set()
        for row in configurations:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) \
                    or not row["name"].strip() or not isinstance(row.get("params"), dict) \
                    or not row["params"]:
                out.append("a configuration has a name and params")
                continue
            if row["name"] in seen:
                out.append(f"configuration {row['name']} is declared twice")
            seen.add(row["name"])
            for name, value in row["params"].items():
                if name not in known:
                    out.append(f"configuration {row['name']}: unknown parameter '{name}'")
                elif (why := value_problem(known[name], value)):
                    out.append(f"configuration {row['name']}: {name}: {why}")
    checks = part.get("checks")
    if checks is not None:
        if not isinstance(checks, list):
            out.append("checks is a list")
            checks = []
        known = {**{n: "length" for n in script_parameter_names(part)}, **declared}
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("name"), str) \
                    or not _NAME.match(check["name"]):
                out.append("a check has a name of letters, digits and underscores")
                continue
            target = check.get("parameter")
            if known.get(target) not in ("length", "angle", "real", "integer"):
                out.append(f"check {check['name']}: 'parameter' names a numeric parameter")
            bounds = [check.get(k) for k in ("min", "max") if k in check]
            if not bounds or not all(_number(b) for b in bounds):
                out.append(f"check {check['name']}: give a numeric min, max or both")
    return out


def check(part: dict[str, Any], refuse: Callable[[str, str], None], where: str) -> None:
    """Preflight hook: refuse a part whose property fields are malformed."""
    for why in problems(part):
        refuse(f"part properties: {why}", where)


def carries_fields(part: dict[str, Any]) -> bool:
    return any(key in part for key in FIELDS) or any(
        isinstance(f, dict) and "color" in f for f in part.get("features") or [])


def colours_from_step(tree: dict[str, Any], structure: Any) -> list[str]:
    """Give each tree part without a colour its STEP instance's (XCAF)
    colour; returns the ids coloured."""
    by_id = {instance.id: instance for instance in getattr(structure, "instances", [])}
    done = []
    for part in tree.get("parts") or []:
        instance = by_id.get(part.get("id"))
        if "color" not in part and instance is not None and instance.color is not None:
            part["color"] = [float(c) for c in instance.color]
            part["color_source"] = "step"
            done.append(part["id"])
    return done


# ── The step ──

def to_rgb255(color: Any) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(float(c) * 255))) for c in color)


class PartProperties:
    """Applies a part's properties after its build. Attach an instance to
    `TreeBuilder.properties`.

    mode: auto (only parts carrying property fields), on (every part: its
    part number and tree id at least), off. `knowledge_advisor`: whether
    checks may be created (Knowledge Advisor licence proven); None or False
    skips them with a note, since a refused licence can open a dialog.
    """

    def __init__(self, mode: str = "auto", catalog: str | None = None,
                 sheet_dir: str | None = None, knowledge_advisor: bool | None = None,
                 log: Callable[[str], None] = lambda _: None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode is one of {', '.join(MODES)}")
        self.mode = mode
        self.catalog = catalog
        self.sheet_dir = sheet_dir
        self.knowledge_advisor = knowledge_advisor
        self.log = log

    def wanted(self, part: dict[str, Any]) -> bool:
        return self.mode == "on" or (self.mode == "auto" and carries_fields(part))

    def apply(self, builder: Any, part: dict[str, Any], record: Any) -> None:
        if not self.wanted(part):
            return
        from catia_mcp.tree.appearance import apply_colours
        from catia_mcp.tree.attributes import apply_attributes
        from catia_mcp.tree.knowledge import KnowledgeStep
        from catia_mcp.tree.materials import apply_material

        try:
            context = Context(builder, part, record)
            knowledge = KnowledgeStep(context)
        except Exception as error:  # never lose the part for its properties
            record.properties = {"ok": False, "error": str(error)}
            return
        section: dict[str, Any] = {}
        steps: list[tuple[str, Callable[[], dict[str, Any] | None]]] = [
            ("parameters", knowledge.parameters),
            ("attributes", lambda: apply_attributes(context, knowledge)),
            ("material", lambda: apply_material(context, knowledge, self.catalog)),
            ("appearance", lambda: apply_colours(context)),
            ("design_table", lambda: knowledge.design_table(self.sheet_dir)),
            ("checks", lambda: knowledge.checks(self.knowledge_advisor)),
        ]
        for name, step in steps:
            builder.log(f"  properties: {name}")
            try:
                result = step()
            except Exception as error:  # a property is a bonus: never lose the part
                result = {"status": "failed", "error": str(error)}
            if result is not None:
                section[name] = result
            context.restore_window()
        # A fallback (a material kept as parameters) is a planned outcome.
        section["ok"] = not any(s.get("status") in ("failed", "partial")
                                for s in section.values() if isinstance(s, dict))
        record.properties = section


class Context:
    """What the sub-steps share: the built part, its final bodies, the tree
    part and the measures, all through the builder."""

    def __init__(self, builder: Any, part: dict[str, Any], record: Any) -> None:
        self.builder = builder
        self.conn = builder.conn
        self.part = builder.part
        self.tree_part = part
        self.record = record
        names = part.get("bodies") or list(builder.bodies)
        self.bodies = [builder.bodies[n] for n in names if n in builder.bodies]

    @property
    def is_3dx(self) -> bool:
        return bool(getattr(self.conn, "is_3dx", False))

    @property
    def app(self) -> Any:
        return getattr(self.conn, "app", None)

    def selection(self) -> Any:
        return self.conn.hso

    def volume_mm3(self) -> float:
        return float(sum(self.builder._volume(body) for body in self.bodies))

    def mass_kg(self) -> float:
        measure = self.builder.measure
        return float(sum(measure.inertia(self.part, body)["mass_kg"] for body in self.bodies))

    def update(self) -> None:
        self.part.Update()

    def restore_window(self) -> None:
        """Opening a material document can change the active window; the
        builder measures in the part's own window only."""
        with contextlib.suppress(Exception):
            if getattr(self.builder, "adopting", False):
                return
            from catia_mcp.tools.document import _same

            if not _same(self.conn.get_active_part(), self.part):
                self.part.Parent.Activate()


def route(name: str, fn: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    """Run one COM route: its record ({route, ok, error?}) and its result."""
    try:
        result = fn()
    except Exception as error:
        return {"route": name, "ok": False, "error": str(error)}, None
    return {"route": name, "ok": True}, result


def summary(section: dict[str, Any] | None) -> str:
    """One line for the command line."""
    if not section:
        return ""
    bits = []
    appearance = section.get("appearance") or {}
    if appearance.get("color_read"):
        bits.append(f"colour {tuple(appearance['color_read'])}")
    elif appearance:
        bits.append(f"colour {appearance.get('status')}")
    material = section.get("material") or {}
    if material:
        bits.append(f"material {material.get('name')} ({material.get('route')})")
        mass = material.get("mass") or {}
        if mass.get("measured_kg") is not None:
            bits.append(f"mass {mass['measured_kg']:.6g} kg (expected "
                        f"{mass.get('expected_kg', 0):.6g})")
    attributes = section.get("attributes") or {}
    if attributes:
        bits.append(f"{len(attributes.get('set') or {})} attribute(s)")
    parameters = section.get("parameters") or {}
    if parameters.get("created"):
        bits.append(f"{len(parameters['created'])} parameter(s) in "
                    f"{parameters.get('set') or 'the part'}")
    table = section.get("design_table") or {}
    if table:
        bits.append(f"design table {table.get('status')}")
    checks = section.get("checks") or {}
    if checks:
        bits.append(f"checks {checks.get('status')}")
    return ", ".join(bits)
