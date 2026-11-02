"""Activate a definition revision for a device type: the one writer of associations."""

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, router, transaction
from django.utils.translation import gettext_lazy as _

from ..models import Association

ADD = "netbox_device_view.add_association"
CHANGE = "netbox_device_view.change_association"
RACED = _("Another activation for this device type ran at the same time. Try again.")


class ActivationRejected(Exception):
    """The activation did not apply; the message says why."""


def can_activate(user) -> bool:
    """A first association needs add permission, a later one change permission."""
    return user.has_perm(ADD) or user.has_perm(CHANGE)


def activate(user, revision, device_type) -> Association:
    """Make the revision the active one for the device type; return the association."""
    # NetBox Branching routes Association to the connection of the active branch.
    try:
        with transaction.atomic(using=router.db_for_write(Association)):
            return _activate(user, revision, device_type)
    except IntegrityError as exc:
        raise ActivationRejected(RACED) from exc


def _activate(user, revision, device_type) -> Association:
    association = (
        Association.objects.select_for_update().filter(device_type=device_type).first()
    )
    if association is None:
        association, action = Association(device_type=device_type), "add"
    else:
        _check(user, "change", association)
        if association.revision_id == revision.pk:
            return association
        association.snapshot()
        action = "change"
    association.revision = revision
    # A concurrent first activation fails on the unique constraint instead.
    association.full_clean(validate_unique=False)
    association.save()
    # Constraints of the permission apply to the saved row, as in NetBox's edit views.
    _check(user, action, association)
    return association


def _check(user, action, association):
    if not (
        Association.objects.restrict(user, action).filter(pk=association.pk).exists()
    ):
        raise PermissionDenied
