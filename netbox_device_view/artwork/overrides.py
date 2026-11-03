"""Binding overrides: the one writer of a changed override, and the bindings that a part resolves.

Bindings resolve in this order: the artwork default, then the type override,
then the instance override. Each level replaces the list of the level below it.
"""

from dcim.models import DeviceType, Module
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from ..models import ONE_SCOPE, BindingOverride


def save(user, override) -> BindingOverride:
    """Validate and save the override with the lock of its type; return it.

    Raise ValidationError for an invalid override, and PermissionDenied when the
    permissions of ``user`` do not cover the saved row.
    """
    action = "add" if override._state.adding else "change"
    if override.scope_field is None:
        raise ValidationError(ONE_SCOPE)
    with override.locked():
        # The lock holds the row of a device or module scope, so its visibility cannot change.
        check_scope(user, override.scope_field, override.scope)
        override.full_clean()
        override.save()
        if (
            not BindingOverride.objects.restrict(user, action)
            .filter(pk=override.pk)
            .exists()
        ):
            raise PermissionDenied
    return override


def can_view_scope(user, scope) -> bool:
    return type(scope).objects.restrict(user, "view").filter(pk=scope.pk).exists()


def check_scope(user, field, scope) -> None:
    """Raise ValidationError unless ``user`` can view ``scope``."""
    # The API looks up a nested object without the permissions of the user.
    if not can_view_scope(user, scope):
        raise ValidationError(
            {
                field: _("Select a {scope} that you can view.").format(
                    scope=scope._meta.verbose_name
                )
            }
        )


def connector_bindings(revision, part) -> dict[str, list]:
    """Return {connector key: bindings} of ``revision`` for one device or installed module."""
    if isinstance(part, Module):
        type_scope, instance_scope = Q(module_type=part.module_type_id), Q(module=part)
    else:
        type_scope, instance_scope = Q(device_type=part.device_type_id), Q(device=part)
    result = {
        key: connector["bindings"]
        for key, connector in revision.manifest["connectors"].items()
    }
    overrides = BindingOverride.objects.filter(
        type_scope | instance_scope, definition=revision.definition_id
    )
    # The instance override comes last, so it replaces the type override.
    for override in sorted(
        overrides, key=lambda o: o.scope_field in ("device", "module")
    ):
        if override.connector_key in result:
            result[override.connector_key] = override.bindings
    return result


def depending_on(hardware_type) -> Q:
    """Return the overrides that depend on a type: those of the type and of its instances."""
    if isinstance(hardware_type, DeviceType):
        return Q(device_type=hardware_type) | Q(device__device_type=hardware_type)
    return Q(module_type=hardware_type) | Q(module__module_type=hardware_type)
