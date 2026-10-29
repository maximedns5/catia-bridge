"""Tolerance comparison of expected (build123d) and measured (CATIA) properties."""

from __future__ import annotations

import math
from typing import Any

# Relative tolerance on volume and area. Prismatic and revolved solids agree
# to rounding between kernels; freeform surfaces are parameterized
# differently, so they get a looser bound.
REL_EXACT = 1e-5
REL_FREEFORM = 1e-3
# Absolute floor (mm, mm2, mm3) so values near zero compare sanely.
ABS_FLOOR = 1e-3


def _scale(expected: dict[str, Any]) -> float:
    """A length scale for position tolerances: the bounding-box diagonal."""
    box = expected["bbox"]
    return math.dist(box["min"], box["max"]) or 1.0


def _close(expected: float, actual: float, rel: float, absolute: float) -> tuple[bool, float]:
    tolerance = max(abs(expected) * rel, absolute)
    return abs(actual - expected) <= tolerance, tolerance


def compare(
    expected: dict[str, Any], measured: dict[str, Any], checks: tuple[str, ...], freeform: bool
) -> list[dict[str, Any]]:
    rel = REL_FREEFORM if freeform else REL_EXACT
    position_tol = max(_scale(expected) * rel, ABS_FLOOR)
    rows = []
    for prop in checks:
        want, got = expected[prop], measured.get(prop)
        if got is None or isinstance(got, str):
            rows.append({"property": prop, "expected": want, "actual": None, "ok": False,
                         "note": got or "not measured"})
            continue
        if prop in ("volume", "area"):
            ok, tolerance = _close(want, got, rel, ABS_FLOOR)
            rows.append({"property": prop, "expected": round(want, 4), "actual": got,
                         "tolerance": round(tolerance, 6), "ok": ok})
        elif prop == "cog":
            deltas = [abs(a - b) for a, b in zip(got, want)]
            rows.append({"property": prop, "expected": [round(v, 4) for v in want],
                         "actual": got, "tolerance": round(position_tol, 6),
                         "ok": max(deltas) <= position_tol})
        elif prop == "bbox":
            deltas = [abs(a - b) for key in ("min", "max") for a, b in zip(got[key], want[key])]
            rows.append({
                "property": prop,
                "expected": {k: [round(v, 4) for v in want[k]] for k in ("min", "max")},
                "actual": got, "tolerance": round(position_tol, 6),
                "ok": max(deltas) <= position_tol,
            })
    return rows
