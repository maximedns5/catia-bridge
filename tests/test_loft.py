"""Loft section pairing (catia_mcp/tree/loft.py) against closed forms and,
when build123d is installed, against OpenCascade's own lofts."""

from __future__ import annotations

import math
import random

import pytest

from catia_mcp.tree.geometry import Frame
from catia_mcp.tree.loft import (
    AmbiguousPairing,
    _smooth,
    loft_expectation,
    pair_sections,
    section_loop,
)

XY = {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "normal": [0, 0, 1]}


def _sketch(entities, plane=XY):
    for i, e in enumerate(entities):
        e.setdefault("id", f"e{i}")
    return {"plane": plane, "entities": entities,
            "profiles": [{"outer": [e["id"] for e in entities], "inner": []}]}


def _polygon(points):
    return [{"type": "line", "start": list(points[i]), "end": list(points[(i + 1) % len(points)])}
            for i in range(len(points))]


def _circle(center, radius):
    return [{"type": "circle", "center": list(center), "radius": radius}]


def _at(height):
    return {"origin": [0, 0, height], "x_dir": [1, 0, 0], "normal": [0, 0, 1]}


def _loft(s1, s2, height):
    common = Frame.from_tree(s1["plane"])
    p, q = pair_sections(section_loop(s1, common), section_loop(s2, common), height)
    return p, q, loft_expectation(common, height, p, q)


def test_circle_to_square_pairs_as_opencascade_does():
    """A circle to a square: the square's corners land on the circle at 45 deg,
    and the circle's vertex (on x_dir) lands mid-side on the square."""
    s1 = _sketch(_circle((30, 0), 10))
    s2 = _sketch(_polygon([(26, 4), (26, -4), (34, -4), (34, 4)]), _at(30))
    p, q, want = _loft(s1, s2, 30)
    r = 10 / math.sqrt(2)
    want_pairs = [((40, 0), (34, 0)), ((30 + r, r), (34, 4)), ((30 - r, r), (26, 4)),
                  ((30 - r, -r), (26, -4)), ((30 + r, -r), (34, -4))]
    assert len(p) == len(q) == 5
    for (a, b), (wa, wb) in zip(zip(p, q), want_pairs, strict=True):
        assert a.a == pytest.approx(wa, abs=1e-9) and b.a == pytest.approx(wb, abs=1e-9)
    assert want.volume == pytest.approx(5222.09875885885, rel=1e-8)  # build123d's loft
    assert want.cog == pytest.approx((30.0, 0.0, 11.4072), abs=1e-4)


def test_ruled_closed_forms():
    _, _, frustum = _loft(_sketch(_circle((0, 0), 10)), _sketch(_circle((0, 0), 4), _at(30)), 30)
    assert frustum.volume == pytest.approx(math.pi * 30 / 3 * (100 + 40 + 16), rel=1e-9)
    rect1 = _sketch(_polygon([(-10, -5), (10, -5), (10, 5), (-10, 5)]))
    rect2 = _sketch(_polygon([(-5, -3), (5, -3), (5, 3), (-5, 3)]), _at(30))
    _, _, prismatoid = _loft(rect1, rect2, 30)
    assert prismatoid.volume == pytest.approx(30 / 6 * (200 + 4 * 15 * 8 + 60), rel=1e-12)


def test_the_second_section_may_start_anywhere_and_run_either_way():
    rect1 = _sketch(_polygon([(-10, -5), (10, -5), (10, 5), (-10, 5)]))
    for points in ([(5, 3), (-5, 3), (-5, -3), (5, -3)], [(5, -3), (5, 3), (-5, 3), (-5, -3)]):
        _, _, got = _loft(rect1, _sketch(_polygon(points), _at(30)), 30)
        assert got.volume == pytest.approx(3700.0, rel=1e-12)


def test_symmetric_or_corner_sensitive_sections_are_refused():
    def hexagon(start, height=0):
        pts = [(8 * math.cos(start + i * math.pi / 3), 8 * math.sin(start + i * math.pi / 3))
               for i in range(6)]
        return _sketch(_polygon(pts), _at(height))

    with pytest.raises(AmbiguousPairing):  # turned 30 deg: two twists fit equally
        _loft(hexagon(0.0), hexagon(math.pi / 6, 20), 20)
    with pytest.raises(AmbiguousPairing):  # two corners compete for the circle's vertex
        _loft(_sketch(_circle((0, 0), 10)),
              _sketch(_polygon([(-6, -3), (6, -3), (6, 3), (-6, 3)]), _at(20)), 20)


def test_smoothness_follows_opencascade_parameters():
    slot = section_loop(_sketch([
        {"type": "line", "start": [-5, -3], "end": [5, -3]},
        {"type": "arc", "start": [5, -3], "mid": [8, 0], "end": [5, 3]},
        {"type": "line", "start": [5, 3], "end": [-5, 3]},
        {"type": "arc", "start": [-5, 3], "mid": [-8, 0], "end": [-5, -3]},
    ]), Frame.from_tree(XY))
    assert not _smooth(slot)  # a line meets a tangent arc only G1
    assert _smooth(section_loop(_sketch(_circle((0, 0), 5)), Frame.from_tree(XY)))


# ── Against OpenCascade ──


def _typical_section(kind, center, rng):
    cx, cy = center
    if kind == "circle":
        return _circle(center, rng.choice([4, 5, 6, 8, 10]))
    if kind == "rect":
        w, h = rng.choice([(10, 5), (12, 4), (8, 8), (6, 10)])
        return _polygon([(cx - w / 2, cy + h / 2), (cx - w / 2, cy - h / 2),
                         (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2)])
    if kind == "slot":
        half, r = rng.choice([(4, 3), (5, 2), (3, 4)])
        return [
            {"type": "line", "start": [cx - half, cy - r], "end": [cx + half, cy - r]},
            {"type": "arc", "start": [cx + half, cy - r], "mid": [cx + half + r, cy],
             "end": [cx + half, cy + r]},
            {"type": "line", "start": [cx + half, cy + r], "end": [cx - half, cy + r]},
            {"type": "arc", "start": [cx - half, cy + r], "mid": [cx - half - r, cy],
             "end": [cx - half, cy - r]},
        ]
    n = int(kind[4:])
    r, a0 = rng.choice([5, 7, 9]), rng.choice([0.0, math.pi / 2, math.pi / n])
    return _polygon([(cx + r * math.cos(a0 + 2 * math.pi * i / n),
                      cy + r * math.sin(a0 + 2 * math.pi * i / n)) for i in range(n)])


def test_pairing_reproduces_build123d_lofts_or_refuses():
    pytest.importorskip("build123d")
    from build123d import CenterOf, loft

    from catia_mcp.tree.reference import sketch_faces

    rng = random.Random(7)
    kinds = ["circle", "rect", "slot", "poly3", "poly5", "poly6", "poly8"]
    exact = refused = 0
    for _ in range(60):
        height = rng.choice([10, 20, 30, 50])
        offset = rng.choice([(0, 0), (0, 0), (2, 0), (0, 3), (1.5, -1)])
        s1 = _sketch(_typical_section(rng.choice(kinds), (0, 0), rng))
        s2 = _sketch(_typical_section(rng.choice(kinds), offset, rng), _at(height))
        solid = loft([sketch_faces(s1)[0], sketch_faces(s2)[0]])
        try:
            _, _, want = _loft(s1, s2, height)
        except AmbiguousPairing:
            refused += 1
            continue
        cog = solid.center(CenterOf.MASS)
        assert want.volume == pytest.approx(solid.volume, rel=1e-6)
        assert want.cog == pytest.approx((cog.X, cog.Y, cog.Z), abs=1e-5)
        exact += 1
    assert exact >= 45 and refused <= 15
