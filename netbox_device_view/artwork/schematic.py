"""The geometry of a generated schematic: a title strip and a grid of cells that fill a bay."""

import math

from .placement import Rect

# The part of the height that the title takes when the schematic has cells.
TITLE = 0.2
# The part of each cell that stays empty around its content.
GAP = 0.1


def layout(rect: Rect, count: int) -> tuple[Rect, tuple[Rect, ...]]:
    """Return the title strip of ``rect`` and ``count`` cells in the grid below it.

    The grid takes the number of columns that gives the largest cells.
    """
    if count == 0:
        return rect, ()
    title = Rect(rect.x, rect.y, rect.width, rect.height * TITLE)
    top, height = rect.y + title.height, rect.height - title.height
    columns = max(
        range(1, count + 1),
        key=lambda n: min(rect.width / n, height / math.ceil(count / n)),
    )
    rows = math.ceil(count / columns)
    width, row = rect.width / columns, height / rows
    return title, tuple(
        Rect(
            rect.x + (index % columns) * width + width * GAP / 2,
            top + (index // columns) * row + row * GAP / 2,
            width * (1 - GAP),
            row * (1 - GAP),
        )
        for index in range(count)
    )
