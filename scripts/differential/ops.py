"""Operations a differential test case is written in.

One case is a list of these operations. The build123d interpreter
(reference.py) turns it into the expected solid; the CATIA interpreter
(catia.py) turns it into MCP tool calls. Each operation means exactly one
thing in both, so a mismatch is a finding about CATIA or the MCP tools, not
about two differently written models.

Sketch coordinates are (H, V) in the sketch plane. The planes follow the
build123d convention (first letter = H axis): xy is H=X, V=Y, normal +Z; yz is
H=Y, V=Z, normal +X; zx is H=Z, V=X, normal +Y. Whether CATIA's origin planes
agree is one of the hypotheses the plane cases check.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rect:
    width: float
    height: float
    at: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class Circle:
    radius: float
    at: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class Point:
    at: tuple[float, float]


@dataclass(frozen=True)
class Sketch:
    plane: str
    shapes: tuple = ()


@dataclass(frozen=True)
class Pad:
    """Extrude the last sketch along its normal; symmetric means `height` on
    each side of the sketch plane (live on R428: CATIA's mirrored extent, and
    Fab's feature tree uses the same convention)."""

    height: float
    symmetric: bool = False


@dataclass(frozen=True)
class Pocket:
    """Cut the last sketch `depth` deep on the side where there is material.
    Cases must have material on one side only: the MCP tool auto-flips to
    reach material, and the reference refuses an ambiguous side."""

    depth: float


@dataclass(frozen=True)
class Shaft:
    """Revolve the last sketch around its V axis (the sketch's own axis)."""

    angle: float = 360.0


@dataclass(frozen=True)
class Groove:
    angle: float = 360.0


@dataclass(frozen=True)
class Hole:
    """Flat-bottom hole at the last sketch's single Point, drilled into the
    side with material (the MCP tool reverses once to reach it)."""

    diameter: float
    depth: float


@dataclass(frozen=True)
class RectPattern:
    """Copies of the last feature along +X (count1) and +Y (count2)."""

    count1: int
    spacing1: float
    count2: int = 1
    spacing2: float = 0.0


@dataclass(frozen=True)
class CircPattern:
    """`count` instances of the last feature, evenly around an axis."""

    count: int
    axis: str = "z"
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Mirror:
    """The active body united with its mirror image about an origin plane."""

    plane: str


@dataclass(frozen=True)
class NewBody:
    name: str


@dataclass(frozen=True)
class ActiveBody:
    """Make a body active; None is the main body."""

    name: str | None = None


@dataclass(frozen=True)
class Boolean:
    """Combine `body` into the active body: add, remove or intersect."""

    operation: str
    body: str


# Properties compared between build123d and CATIA. "bbox" is only meaningful
# when the solid's extreme points are vertices: CATIA's box is a vertex sweep.
ALL_CHECKS = ("volume", "area", "cog", "bbox")


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    ops: tuple
    checks: tuple[str, ...] = ("volume", "area", "cog")
    # Which assumption about CATIA a mismatch would point to.
    hypothesis: str = ""
    # Curved freeform geometry gets a looser tolerance (different kernels
    # parameterize such surfaces differently).
    freeform: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
