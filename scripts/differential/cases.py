"""Differential test cases (Route A: the same model built by build123d and
through the MCP tools in CATIA).

Each case is small and targets one tool or one convention. Expected values
come from build123d at run time; the closed-form value in each description is
only there to make the intent readable.
"""

from __future__ import annotations

from scripts.differential.ops import (
    ActiveBody,
    Boolean,
    Case,
    Circle,
    CircPattern,
    Groove,
    Hole,
    Mirror,
    NewBody,
    Pad,
    Pocket,
    Point,
    Rect,
    RectPattern,
    Shaft,
    Sketch,
)

BLOCK = (Sketch("xy", (Rect(100, 60),)), Pad(40))  # 100 x 60 x 40, z 0..40

CASES: tuple[Case, ...] = (
    Case(
        "block",
        "Centred 100 x 60 rectangle padded 40: 240000 mm3, the smoke-test baseline.",
        BLOCK,
        checks=("volume", "area", "cog", "bbox"),
        tags=("pad",),
    ),
    Case(
        "xy_offset_pad",
        "40 x 20 rectangle centred at (30, 10) on xy, padded 15: COG (30, 10, 7.5).",
        (Sketch("xy", (Rect(40, 20, at=(30, 10)),)), Pad(15)),
        checks=("volume", "area", "cog", "bbox"),
        hypothesis="xy sketch: H = +X, V = +Y, pad along +Z",
        tags=("pad", "plane"),
    ),
    Case(
        "yz_offset_pad",
        "40 x 20 rectangle centred at (10, 5) on yz, padded 30: COG (15, 10, 5) "
        "if H = +Y, V = +Z and the pad goes along +X.",
        (Sketch("yz", (Rect(40, 20, at=(10, 5)),)), Pad(30)),
        checks=("volume", "area", "cog", "bbox"),
        hypothesis="yz sketch: H = +Y, V = +Z, pad along +X",
        tags=("pad", "plane"),
    ),
    Case(
        "zx_offset_pad",
        "40 x 20 rectangle centred at (10, 5) on zx, padded 30: COG (5, 15, 10) "
        "if H = +Z, V = +X and the pad goes along +Y.",
        (Sketch("zx", (Rect(40, 20, at=(10, 5)),)), Pad(30)),
        checks=("volume", "area", "cog", "bbox"),
        hypothesis="zx sketch: H = +Z, V = +X, pad along +Y",
        tags=("pad", "plane"),
    ),
    Case(
        "pad_symmetric",
        "50 x 50 square padded 20 symmetric: 20 on each side, 100000 mm3 over "
        "z -20..20 (live R428; the schema used to say total = height).",
        (Sketch("xy", (Rect(50, 50),)), Pad(20, symmetric=True)),
        checks=("volume", "area", "cog", "bbox"),
        tags=("pad",),
    ),
    Case(
        "circular_pockets",
        "The smoke block with 4 blind pockets (r 4, depth 20) from the bottom "
        "face, circular pattern around Z: 235978.8 mm3.",
        BLOCK + (Sketch("xy", (Circle(4, at=(20, 0)),)), Pocket(20), CircPattern(4)),
        checks=("volume", "area", "cog", "bbox"),
        tags=("pocket", "pattern"),
    ),
    Case(
        "rect_pattern_pockets",
        "100 x 60 x 10 plate, pocket r 4 depth 5 at (-30, -15) from the bottom, "
        "patterned 3 x 2 at 30 mm along +X and +Y: 6 pockets inside the plate.",
        (
            Sketch("xy", (Rect(100, 60),)),
            Pad(10),
            Sketch("xy", (Circle(4, at=(-30, -15)),)),
            Pocket(5),
            RectPattern(3, 30, 2, 30),
        ),
        checks=("volume", "area", "cog", "bbox"),
        hypothesis="catia_rect_pattern default directions: dir1 = +X, dir2 = +Y",
        tags=("pocket", "pattern"),
    ),
    Case(
        "rect_pattern_dir1",
        "Plate with a pocket at its centre, patterned 2 x 1 at 20 mm: the copy at "
        "x = +20 moves the COG to x < 0; at x = -20 the COG sign flips.",
        (
            Sketch("xy", (Rect(100, 60),)),
            Pad(10),
            Sketch("xy", (Circle(4),)),
            Pocket(5),
            RectPattern(2, 20),
        ),
        checks=("volume", "area", "cog", "bbox"),
        hypothesis="catia_rect_pattern dir1 = +X (the COG shows the actual direction)",
        tags=("pocket", "pattern"),
    ),
    Case(
        "rect_pattern_dir2",
        "Plate with a pocket at its centre, patterned 1 x 2 at 20 mm: the copy at "
        "y = +20 moves the COG to y < 0; at y = -20 the COG sign flips.",
        (
            Sketch("xy", (Rect(100, 60),)),
            Pad(10),
            Sketch("xy", (Circle(4),)),
            Pocket(5),
            RectPattern(1, 0, 2, 20),
        ),
        checks=("volume", "area", "cog", "bbox"),
        hypothesis="catia_rect_pattern dir2 = +Y (the COG shows the actual direction)",
        tags=("pocket", "pattern"),
    ),
    Case(
        "shaft_ring",
        "10 x 30 rectangle at (20, 0) on xy revolved 360 about the sketch V "
        "axis (world Y): ring of 2*pi*20*300 = 37699.1 mm3, COG at the origin.",
        (Sketch("xy", (Rect(10, 30, at=(20, 0)),)), Shaft()),
        hypothesis="catia_shaft revolves about the sketch V axis",
        tags=("revolve",),
    ),
    Case(
        "groove_on_cylinder",
        "r 20 x 60 cylinder along Y (shaft), then a 4 x 10 groove profile at "
        "x 17..21: removes pi*(400-289)*10 = 3487.2 mm3.",
        (
            Sketch("xy", (Rect(20, 60, at=(10, 0)),)),
            Shaft(),
            Sketch("xy", (Rect(4, 10, at=(19, 0)),)),
            Groove(),
        ),
        hypothesis="catia_groove revolves about the sketch V axis",
        tags=("revolve",),
    ),
    Case(
        "hole_from_sketch",
        "Smoke block, D8 x 25 flat-bottom hole at (20, 10) on the bottom face: "
        "removes pi*16*25 = 1256.6 mm3.",
        BLOCK + (Sketch("xy", (Point((20, 10)),)), Hole(8, 25)),
        checks=("volume", "area", "cog", "bbox"),
        tags=("hole",),
    ),
    Case(
        "mirror_body",
        "40 x 20 x 15 box centred at (30, 10), mirrored about yz: two boxes, "
        "24000 mm3, COG (0, 10, 7.5).",
        (Sketch("xy", (Rect(40, 20, at=(30, 10)),)), Pad(15), Mirror("yz")),
        checks=("volume", "area", "cog", "bbox"),
        tags=("mirror",),
    ),
    Case(
        "boolean_remove",
        "Smoke block minus a 40 x 20 x 50 body centred at (40, 0) that sticks out "
        "of the x = 50 face: removes 30*20*40 = 24000 mm3.",
        BLOCK
        + (
            NewBody("Cutter"),
            Sketch("xy", (Rect(40, 20, at=(40, 0)),)),
            Pad(50),
            ActiveBody(),
            Boolean("remove", "Cutter"),
        ),
        checks=("volume", "area", "cog", "bbox"),
        tags=("boolean",),
    ),
    Case(
        "boolean_add",
        "Smoke block plus an r 5 x 60 cylinder body at the origin, 20 mm above "
        "the top face: adds pi*25*20 = 1570.8 mm3.",
        BLOCK
        + (
            NewBody("Boss"),
            Sketch("xy", (Circle(5),)),
            Pad(60),
            ActiveBody(),
            Boolean("add", "Boss"),
        ),
        tags=("boolean",),
    ),
    Case(
        "boolean_intersect",
        "Smoke block intersected with an r 25 x 50 cylinder body at the origin: "
        "pi*625*40 = 78539.8 mm3.",
        BLOCK
        + (
            NewBody("Core"),
            Sketch("xy", (Circle(25),)),
            Pad(50),
            ActiveBody(),
            Boolean("intersect", "Core"),
        ),
        tags=("boolean",),
    ),
)


def by_name(names: list[str] | None) -> tuple[Case, ...]:
    if not names:
        return CASES
    known = {case.name: case for case in CASES}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise SystemExit(f"Unknown case(s): {', '.join(unknown)}. Known: {', '.join(known)}")
    return tuple(known[name] for name in names)
