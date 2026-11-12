"""The artwork updates that the device page offers to review."""

from dcim.models import DeviceType, Module, ModuleType
from django.db.models import Q

from ..models import Association, DefinitionRevision, InstancePin, newest_revisions
from . import review


def device_update_pairs(user, device) -> list[tuple]:
    """Return the (type, newest revision) pairs of the unpinned device and installed modules that the user can review."""
    if not review.can_activate(user):
        return []
    modules = list(
        Module.objects.restrict(user, "view")
        .filter(device=device)
        .only("pk", "module_type_id")
    )
    pins = InstancePin.objects.filter(
        Q(device=device) | Q(module__in=[module.pk for module in modules])
    )
    pinned = {(pin.device_id, pin.module_id) for pin in pins}
    scope = Q(
        module_type__in={
            module.module_type_id
            for module in modules
            if (None, module.pk) not in pinned
        }
    )
    if (device.pk, None) not in pinned:
        scope |= Q(device_type=device.device_type_id)
    # The review rejects a type that the user cannot view.
    viewable_types = Q(device_type__in=DeviceType.objects.restrict(user, "view")) | Q(
        module_type__in=ModuleType.objects.restrict(user, "view")
    )
    associations = (
        Association.objects.restrict(user, "view")
        .filter(scope & viewable_types)
        .select_related(
            "device_type__manufacturer", "module_type__manufacturer", "revision"
        )
        .defer("revision__manifest")
    )
    newest = newest_revisions({a.revision.definition_id for a in associations})
    viewable = set(
        DefinitionRevision.objects.restrict(user, "view")
        .filter(pk__in=[revision.pk for revision in newest.values()])
        .values_list("pk", flat=True)
    )
    pairs = []
    # The device type first, then the module types by name; a review holds MAX_PAIRS.
    for association in sorted(
        associations, key=lambda a: (a.module_type_id is not None, str(a.hardware_type))
    ):
        candidate = newest[association.revision.definition_id]
        if candidate.pk != association.revision_id and candidate.pk in viewable:
            pairs.append((association.hardware_type, candidate))
    return pairs[: review.MAX_PAIRS]
