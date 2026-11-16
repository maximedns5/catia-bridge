"""User parameters and formulas from the tree's `params`.

The tree says which number in the build123d script each feature value came
from: `params: {key: {arg, kind, value, factor, slope, intercept, file, line,
also?}}`, the feature value being factor * (slope * value + intercept). For
each `literal` script number the builder makes one CATIA user parameter
holding the script's value, named after the feature and the argument
(`Extrude1_height`, `Hole1_radius`), and drives every CATIA value that number
set with a formula. Editing the parameter in CATIA is then editing the number
in the script.

Values the script does not write (`computed`) get no parameter: they stay
editable in their feature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ANGLE_KEYS = {"angle", "taper", "countersink_angle"}


@dataclass
class Driven:
    """A CATIA value (Length or Angle object) a script number sets, times
    `scale` (0.5 for a centred box drawn as a symmetric pad)."""

    target: Any
    scale: float = 1.0


@dataclass
class ScriptParameter:
    name: str
    value: float
    unit: str  # "mm" or "deg"
    factor: float
    slope: float
    intercept: float
    source: str
    driven: list[Driven] = field(default_factory=list)

    @property
    def magnitude(self) -> str:
        return "ANGLE" if self.unit == "deg" else "LENGTH"

    def expression(self, scale: float = 1.0) -> str:
        """factor * scale * (slope * name + intercept), written as a designer
        would: `Plate_height`, `Hole1_radius*2`, `Extrude1_height/2`."""
        term = self.name if self.slope == 1 else f"{_num(self.slope)}*{self.name}"
        if self.intercept:
            sign = "+" if self.intercept > 0 else "-"
            term = f"({term}{sign}{_num(abs(self.intercept))}{self.unit})"
        k = self.factor * scale
        if k == 1:
            return term
        if k != 0 and abs(1 / k - round(1 / k)) < 1e-12 and round(1 / k) > 1:
            return f"{term}/{round(1 / k)}"
        return f"{term}*{_num(k)}"


def _num(x: float) -> str:
    return f"{x:g}"


def _literal(spec: Any) -> bool:
    return isinstance(spec, dict) and spec.get("kind") == "literal" \
        and isinstance(spec.get("value"), (int, float)) and not isinstance(spec["value"], bool)


def spec_for(feature: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The literal script number behind `key`, directly or through another
    key's `also` (a chamfer's distance2 set by its distance)."""
    params = feature.get("params") or {}
    spec = params.get(key)
    if _literal(spec):
        return spec
    return next((s for s in params.values() if _literal(s) and key in (s.get("also") or [])),
                None)


def parameter_name(feature: dict[str, Any], spec: dict[str, Any], key: str) -> str:
    """`<feature name>_<script argument>`: letters, digits and underscores,
    starting with a letter (CATIA's rule for names used in formulas)."""
    base = f"{feature.get('name') or feature.get('id') or 'Feature'}_{spec.get('arg') or key}"
    name = re.sub(r"[^A-Za-z0-9_]", "_", base)
    return name if name[:1].isalpha() else f"P_{name}"


class ParameterPlan:
    """The script parameters of one part, gathered while it is built."""

    def __init__(self) -> None:
        self.parameters: dict[tuple[str, int, int, str], ScriptParameter] = {}
        self.names: set[str] = set()
        # (feature id, key) -> the parameter driving that value.
        self.by_key: dict[tuple[str, str], ScriptParameter] = {}

    def drive(self, feature: dict[str, Any], key: str, target: Any, scale: float = 1.0) -> None:
        spec = spec_for(feature, key)
        if spec is None:
            return
        # One parameter per script number: the same number driving several
        # values (a chamfer's two lengths, every hole of a Hole call) shares it.
        identity = (str(spec.get("file", "model.py")), int(spec.get("line") or 0),
                    int(spec.get("col") or 0), str(feature.get("id")))
        parameter = self.parameters.get(identity)
        if parameter is None:
            name = parameter_name(feature, spec, key)
            unique, n = name, 2
            while unique in self.names:
                unique, n = f"{name}_{n}", n + 1
            self.names.add(unique)
            where = f"{spec.get('file', 'model.py')} line {spec.get('line')}" \
                if spec.get("line") else "the script"
            parameter = ScriptParameter(
                unique, float(spec["value"]), "deg" if key in ANGLE_KEYS else "mm",
                float(spec.get("factor", 1.0)), float(spec.get("slope", 1.0)),
                float(spec.get("intercept", 0.0)), where)
            self.parameters[identity] = parameter
        parameter.driven.append(Driven(target, scale))
        self.by_key[(str(feature.get("id")), key)] = parameter

    def __bool__(self) -> bool:
        return bool(self.parameters)
