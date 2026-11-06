"""Activate revisions through the review of the activation, without the pages."""

from netbox_device_view.artwork import review


def activate(user, *pairs):
    """Review the (revision, type) ``pairs`` together and activate them; return the review."""
    built = review.build(
        user,
        [review.Pair(hardware_type, revision) for revision, hardware_type in pairs],
    )
    assert built.token, built
    review.apply(user, built.token)
    return built
