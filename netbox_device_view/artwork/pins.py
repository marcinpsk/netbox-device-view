"""Instance pins: the one writer of a changed pin."""

from ..models import ONE_INSTANCE, InstancePin
from .overrides import check_scope, save_scoped


def save(user, pin) -> InstancePin:
    """Validate and save the pin with the lock of the type of its device or module; return it.

    Raise ValidationError for an invalid pin, and PermissionDenied when the
    permissions of ``user`` do not cover the saved row.
    """
    if pin.revision_id is not None:
        check_scope(user, "revision", pin.revision)
    return save_scoped(user, pin, ONE_INSTANCE)
