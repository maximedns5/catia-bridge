"""Part placement: move a part's features from its own frame into the world.

A tree part may carry `placement`, a 3x4 matrix of rows [R|t] (t in mm) from
the part's frame to the world, when its parent has a location. Its features
are then given in the part's frame. `place_part` returns the part with every
3D coordinate of its features moved into the world, so the builder, the
reference and the preflight all work on world coordinates only.

What moves: sketch planes (sketch entities stay in their plane's (u, v)),
and, in every other feature, the 3D values of anchor fields: points
(origin, point, start, mid, end, center, samples, positions, poles) and
directions (normal, direction, x_dir, y_dir, z_dir), hole frames and copy
matrices. Lengths, angles and the right-hand rule are unchanged by a rigid
motion, so only proper rotations are accepted.
"""

from __future__ import annotations

import copy
import math
from typing import Any

Matrix = tuple[tuple[float, float, float], ...]

POINT_KEYS = ("origin", "point", "start", "mid", "end", "center", "samples", "positions", "poles")
VECTOR_KEYS = ("normal", "direction", "x_dir", "y_dir", "z_dir")
# Not geometry: kept as they are.
SKIPPED_KEYS = ("source", "params", "node", "id", "name")
RIGID_TOL = 1e-6


def placement_transform(placement: Any) -> tuple[Matrix, tuple[float, float, float]]:
    """(R, t) of a 3x4 [R|t] placement. Raises ValueError unless R is a
    rotation (orthonormal, determinant +1)."""
    try:
        rows = [[float(c) for c in row] for row in placement]
    except (TypeError, ValueError) as error:
        raise ValueError("the placement is not a 3x4 matrix of numbers") from error
    if len(rows) != 3 or any(len(row) != 4 for row in rows) \
            or not all(math.isfinite(c) for row in rows for c in row):
        raise ValueError("the placement is not a 3x4 matrix of numbers")
    rotation = tuple(tuple(row[:3]) for row in rows)
    for i in range(3):
        for j in range(3):
            column_dot = sum(rotation[k][i] * rotation[k][j] for k in range(3))
            if abs(column_dot - (1.0 if i == j else 0.0)) > RIGID_TOL:
                raise ValueError("the placement is not a rigid motion (its 3x3 part is not "
                                 "a rotation)")
    (a, b, c), (d, e, f), (g, h, i) = rotation
    if a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g) < 0:
        raise ValueError("the placement mirrors the part")
    return rotation, tuple(row[3] for row in rows)


def _is_vec3(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in value))


class _Motion:
    def __init__(self, rotation: Matrix, translation) -> None:
        self.r, self.t = rotation, translation

    def vector(self, v) -> list[float]:
        return [sum(self.r[i][k] * float(v[k]) for k in range(3)) for i in range(3)]

    def point(self, p) -> list[float]:
        return [c + self.t[i] for i, c in enumerate(self.vector(p))]

    def frame(self, matrix) -> list[list[float]]:
        """A 3x4 frame whose columns are X, Y, Z and its origin."""
        columns = [[float(matrix[i][j]) for i in range(3)] for j in range(4)]
        moved = [self.vector(c) for c in columns[:3]] + [self.point(columns[3])]
        return [[moved[j][i] for j in range(4)] for i in range(3)]

    def conjugate(self, matrix) -> list[list[float]]:
        """A rigid motion given in the part's frame, as the same motion in the
        world: R A R^T, and R b + t - R A R^T t."""
        a = [[float(matrix[i][j]) for j in range(3)] for i in range(3)]
        b = [float(matrix[i][3]) for i in range(3)]
        r = self.r
        ra = [[sum(r[i][k] * a[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        rart = [[sum(ra[i][k] * r[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
        shift = [sum(rart[i][k] * self.t[k] for k in range(3)) for i in range(3)]
        offset = [c + self.t[i] - shift[i] for i, c in enumerate(self.vector(b))]
        return [rart[i] + [offset[i]] for i in range(3)]

    def walk(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.walk(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        out = {}
        for key, item in value.items():
            if key in SKIPPED_KEYS:
                out[key] = copy.deepcopy(item)
            elif key == "frames" and isinstance(item, list):
                out[key] = [self.frame(m) for m in item]
            elif key == "matrix" and isinstance(item, list) and len(item) == 3:
                out[key] = self.conjugate(item)
            elif key in POINT_KEYS and _is_vec3(item):
                out[key] = self.point(item)
            elif key in POINT_KEYS and isinstance(item, list) and item and all(map(_is_vec3, item)):
                out[key] = [self.point(p) for p in item]
            elif key in VECTOR_KEYS and _is_vec3(item):
                out[key] = self.vector(item)
            else:
                out[key] = self.walk(item)
        return out

    def feature(self, feature: dict[str, Any]) -> dict[str, Any]:
        if feature.get("type") == "sketch":
            # Entities are 2D, in the plane's own (u, v): only the plane moves.
            moved = copy.deepcopy(feature)
            if isinstance(feature.get("plane"), dict):
                moved["plane"] = self.walk(feature["plane"])
            return moved
        return self.walk(feature)


def place_part(part: dict[str, Any]) -> dict[str, Any]:
    """The part with its features in world coordinates and no placement left.
    A part without a placement is returned as it is. Raises ValueError for a
    placement that is not a rigid motion."""
    if part.get("placement") is None:
        return part
    motion = _Motion(*placement_transform(part["placement"]))
    placed = {key: value for key, value in part.items() if key != "placement"}
    placed["features"] = [motion.feature(f) if isinstance(f, dict) else f
                          for f in part.get("features") or []]
    return placed
