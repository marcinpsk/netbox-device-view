"""The placement of a child face in a bay: worked examples of each rotation."""

import pytest

from netbox_device_view.artwork.placement import Rect, place, transform_rect

# A child face whose viewBox does not start at the origin.
VIEW_BOX = (5, 7, 100, 30)


def apply(matrix, x, y):
    a, b, c, d, e, f = matrix
    return (round(a * x + c * y + e, 9), round(b * x + d * y + f, 9))


@pytest.mark.parametrize(
    ("rotate", "bay", "corners"),
    [
        # Each case maps the child corners top-left, top-right, bottom-left, bottom-right.
        (0, Rect(300, 5, 100, 30), [(300, 5), (400, 5), (300, 35), (400, 35)]),
        (90, Rect(300, 5, 30, 100), [(330, 5), (330, 105), (300, 5), (300, 105)]),
        (180, Rect(300, 5, 100, 30), [(400, 35), (300, 35), (400, 5), (300, 5)]),
        (270, Rect(300, 5, 30, 100), [(300, 105), (300, 5), (330, 105), (330, 5)]),
    ],
)
def test_the_rotated_child_box_fills_the_bay(rotate, bay, corners):
    placement = place(VIEW_BOX, bay, rotate)
    child = [(5, 7), (105, 7), (5, 37), (105, 37)]
    assert [apply(placement.matrix, x, y) for x, y in child] == corners
    assert not placement.size_mismatch


@pytest.mark.parametrize(
    ("bay", "rotate", "mismatch"),
    [
        (Rect(0, 0, 101, 29), 0, False),
        (Rect(0, 0, 101.01, 30), 0, True),
        (Rect(0, 0, 100, 28.9), 0, True),
        # A rotation of 90 or 270 swaps the width and the height of the child.
        (Rect(0, 0, 100, 30), 90, True),
        (Rect(0, 0, 30, 100), 270, False),
        (Rect(0, 0, 100, 30), 180, False),
    ],
)
def test_a_size_difference_of_more_than_one_millimetre_is_a_mismatch(
    bay, rotate, mismatch
):
    assert place(VIEW_BOX, bay, rotate).size_mismatch is mismatch


def test_a_placement_that_is_not_finite_has_no_matrix():
    assert place(VIEW_BOX, Rect(float("inf"), 0, 100, 30), 0).matrix is None
    assert place((float("nan"), 0, 100, 30), Rect(0, 0, 100, 30), 0).matrix is None


def test_the_rotated_size_is_the_size_of_the_child_in_the_bay():
    assert place(VIEW_BOX, Rect(0, 0, 30, 100), 90).size == (30, 100)
    assert place(VIEW_BOX, Rect(0, 0, 100, 30), 180).size == (100, 30)


def test_a_rect_in_a_rotated_module_maps_to_its_bounds_in_the_parent():
    # A 14 x 10 bay at (40, 10) of a 100 x 30 face, placed in a bay turned by 90 degrees.
    matrix = place((0, 0, 100, 30), Rect(300, 5, 30, 100), 90).matrix
    assert transform_rect(matrix, Rect(40, 10, 14, 10)) == Rect(310, 45, 10, 14)
