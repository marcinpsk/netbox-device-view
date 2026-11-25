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

from ..models import ONE_SCOPE, SCOPES, BindingOverride


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


def _instance_scope(model):
    """Return the instance scope of a part model (a device or module) or of its type model."""
    return next(
        spec
        for spec in SCOPES.values()
        if spec.instance and model in (spec.model, SCOPES[spec.type_field].model)
    )


def part_overrides(parts) -> list[BindingOverride]:
    """Return the overrides of the devices and installed modules ``parts`` and of their types, in one query."""
    ids = defaultdict(set)
    for part in parts:
        spec = _instance_scope(type(part))
        ids[SCOPES[spec.type_field]].add(SCOPES[spec.type_field].id_of(part))
        ids[spec].add(part.pk)
    condition = functools.reduce(
        operator.or_, (spec.among(values) for spec, values in ids.items())
    )
    return list(BindingOverride.objects.filter(condition))


def connector_bindings(revision, part, overrides) -> dict[str, list]:
    """Return {connector key: bindings} of ``revision`` for one device or installed module.

    ``overrides`` comes from part_overrides() for parts that include ``part``.
    """
    spec = _instance_scope(type(part))
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
    spec = _instance_scope(type(hardware_type))
    return Q(**{spec.type_field: hardware_type}) | Q(
        **{f"{spec.field}__{spec.type_field}": hardware_type}
    )
