"""Place a child face in a bay: at true scale, rotated by a multiple of 90 degrees."""

import math
from dataclasses import dataclass

# A size difference above this many millimetres is a warning; it does not block.
SIZE_TOLERANCE = 1
# The (a, b, c, d) part of the matrix of each bay rotation; exact, so no rounding error.
_ROTATIONS = {
    0: (1, 0, 0, 1),
    90: (0, 1, -1, 0),
    180: (-1, 0, 0, -1),
    270: (0, -1, 1, 0),
}


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Placement:
    # The matrix (a, b, c, d, e, f) from child face coordinates to bay coordinates; None when not finite.
    matrix: tuple[float, ...] | None
    # The size of the rotated child face.
    size: tuple[float, float]
    size_mismatch: bool


def place(view_box, bay: Rect, rotate: int) -> Placement:
    """Normalize the child viewBox to its origin, rotate it, and put its top-left corner at the bay origin."""
    min_x, min_y, width, height = view_box
    a, b, c, d = _ROTATIONS[rotate]
    corners = [(x, y) for x in (0, width) for y in (0, height)]
    left = min(a * x + c * y for x, y in corners)
    top = min(b * x + d * y for x, y in corners)
    matrix = (
        a,
        b,
        c,
        d,
        bay.x - left - (a * min_x + c * min_y),
        bay.y - top - (b * min_x + d * min_y),
    )
    size = (height, width) if rotate in (90, 270) else (width, height)
    return Placement(
        matrix=matrix if all(map(math.isfinite, matrix)) else None,
        size=size,
        size_mismatch=abs(size[0] - bay.width) > SIZE_TOLERANCE
        or abs(size[1] - bay.height) > SIZE_TOLERANCE,
    )


def transform_rect(matrix, rect: Rect) -> Rect:
    """Return the bounds of ``rect`` after ``matrix``; exact for a rotation by a multiple of 90 degrees."""
    a, b, c, d, e, f = matrix
    points = [
        (a * x + c * y + e, b * x + d * y + f)
        for x in (rect.x, rect.x + rect.width)
        for y in (rect.y, rect.y + rect.height)
    ]
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
