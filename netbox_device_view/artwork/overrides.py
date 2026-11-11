"""Binding overrides: the one writer of a changed override, and the bindings that a part resolves.

Bindings resolve in this order: the artwork default, then the type override,
then the instance override. Each level replaces the list of the level below it.
"""

import functools
import operator
from collections import defaultdict

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from ..models import ONE_SCOPE, SCOPES, BindingOverride, instance_scope


def save(user, override) -> BindingOverride:
    """Validate and save the override with the lock of its type; return it."""
    return save_scoped(user, override, ONE_SCOPE)


def save_scoped(user, row, one_scope):
    """Validate and save a row of one scope (an override or a pin) with the lock of the scope's type; return it.

    Raise ValidationError for an invalid row, with ``one_scope`` when it has no
    single scope, and PermissionDenied when the permissions of ``user`` do not
    cover the saved row.
    """
    action = "add" if row._state.adding else "change"
    if row.scope_field is None:
        raise ValidationError(one_scope)
    with row.locked():
        # The lock holds the row of a device or module scope, so its visibility cannot change.
        check_scope(user, row.scope_field, row.scope)
        row.lock_references()
        row.full_clean()
        row.save()
        if not type(row).objects.restrict(user, action).filter(pk=row.pk).exists():
            raise PermissionDenied
    return row


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


def part_overrides(parts) -> list[BindingOverride]:
    """Return the overrides of the devices and installed modules ``parts`` and of their types, in one query."""
    return list(BindingOverride.objects.filter(of_parts(parts)))


def of_parts(parts) -> Q:
    """Return the filter of the overrides of the devices and installed modules ``parts`` and of their types."""
    ids = defaultdict(set)
    for part in parts:
        spec = instance_scope(type(part))
        ids[SCOPES[spec.type_field]].add(SCOPES[spec.type_field].id_of(part))
        ids[spec].add(part.pk)
    return functools.reduce(
        operator.or_, (spec.among(values) for spec, values in ids.items()), Q(pk__in=[])
    )


def connector_bindings(revision, part, overrides) -> dict[str, list]:
    """Return {connector key: bindings} of ``revision`` for one device or installed module.

    ``overrides`` comes from part_overrides() for parts that include ``part``.
    """
    spec = instance_scope(type(part))
    type_spec = SCOPES[spec.type_field]
    result = {
        key: connector["bindings"]
        for key, connector in revision.manifest["connectors"].items()
    }
    applying = [
        override
        for override in overrides
        if override.definition_id == revision.definition_id
        and (
            type_spec.id_of(override) == type_spec.id_of(part)
            or spec.id_of(override) == part.pk
        )
    ]
    # The instance override comes last, so it replaces the type override.
    for override in sorted(applying, key=lambda o: SCOPES[o.scope_field].instance):
        # A part that changed its type, or a branch merge, can keep a key that the revision lacks.
        if override.connector_key in result:
            result[override.connector_key] = override.bindings
    return result


def depending_on(hardware_type) -> Q:
    """Return the overrides that depend on a type: those of the type and of its instances."""
    spec = instance_scope(type(hardware_type))
    return Q(**{spec.type_field: hardware_type}) | Q(
        **{f"{spec.field}__{spec.type_field}": hardware_type}
    )
