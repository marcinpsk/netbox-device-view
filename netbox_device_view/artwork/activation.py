"""Activate a definition revision for a device type: the one writer of associations."""

from dcim.models import Device
from django.core.exceptions import PermissionDenied
from django.db import DEFAULT_DB_ALIAS, IntegrityError, router, transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from ..models import Association, BindingOverride
from .overrides import depending_on
from .type_lock import lock_types

ADD_PERMISSION = "netbox_device_view.add_association"
CHANGE_PERMISSION = "netbox_device_view.change_association"
RACED_MESSAGE = _(
    "Another activation for this device type ran at the same time. Try again."
)
MISSING_CONNECTORS = _(
    "These binding overrides name a connector that the revision does not have:"
    " {overrides}. Delete them, or select another revision."
)
HIDDEN_OVERRIDES = _(
    "Binding overrides name a connector that the revision does not have. Your"
    " permissions do not cover every affected object."
)


class ActivationRejected(Exception):
    """The activation did not apply; the message says why."""


def can_activate(user) -> bool:
    """A first association needs add permission, a later one change permission."""
    return user.has_perm(ADD_PERMISSION) or user.has_perm(CHANGE_PERMISSION)


def activate(user, revision, device_type) -> Association:
    """Make the revision the active one for the device type; return the association."""
    # NetBox Branching routes Association to the branch and its ChangeDiff rows to main.
    try:
        with (
            transaction.atomic(using=DEFAULT_DB_ALIAS),
            transaction.atomic(using=router.db_for_write(Association)),
        ):
            return _activate(user, revision, device_type)
    except IntegrityError as exc:
        # Only a concurrent first activation of the type is expected; other errors stay visible.
        if not Association.objects.filter(device_type=device_type).exists():
            raise
        raise ActivationRejected(RACED_MESSAGE) from exc


def _activate(user, revision, device_type) -> Association:
    # The writers of binding overrides take the same lock.
    lock_types([device_type], router.db_for_write(Association))
    association = (
        Association.objects.select_for_update().filter(device_type=device_type).first()
    )
    if association is None:
        association, action = Association(device_type=device_type), "add"
    else:
        _require(user, "change", association)
        if association.revision_id == revision.pk:
            return association
        association.snapshot()
        action = "change"
    _check_overrides(user, revision, device_type)
    association.revision = revision
    # A concurrent first activation fails on the unique constraint instead.
    association.full_clean(validate_unique=False)
    association.save()
    # Constraints of the permission apply to the saved row, as in NetBox's edit views.
    _require(user, action, association)
    return association


def _require(user, action, association):
    if not (
        Association.objects.restrict(user, action).filter(pk=association.pk).exists()
    ):
        raise PermissionDenied


def _check_overrides(user, revision, device_type):
    """Refuse a revision without the connector of an override that depends on the type."""
    missing = (
        BindingOverride.objects.filter(
            depending_on(device_type), definition=revision.definition_id
        )
        .exclude(connector_key__in=list(revision.manifest["connectors"]))
        .select_related("device_type__manufacturer", "device")
    )
    count = missing.count()
    if not count:
        return
    # A name needs view permission on the override and on its device.
    visible = list(
        missing.restrict(user, "view").filter(
            Q(device__isnull=True) | Q(device__in=Device.objects.restrict(user, "view"))
        )
    )
    if len(visible) != count:
        raise ActivationRejected(HIDDEN_OVERRIDES)
    raise ActivationRejected(
        MISSING_CONNECTORS.format(overrides=", ".join(map(str, visible)))
    )
