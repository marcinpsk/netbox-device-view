"""Resolve what draws each member of a device view: its pinned or active artwork, else its layout, else a generated schematic.

The Device View tab, the device page panel and the artwork API use this one
resolution, so they draw each member the same way.
"""

from dataclasses import dataclass

from dcim.models import Device

from .components import panel_members
from .models import DefinitionRevision, DeviceView, active_revisions


@dataclass(frozen=True)
class Member:
    """A panel member of a device view and what draws it."""

    device: Device
    # Its pin, else the active revision of its device type; it comes before a layout.
    revision: DefinitionRevision | None
    # The DeviceView of its device type that the user can view, when it has no active artwork.
    device_view: DeviceView | None
    # True when it has neither, and another visible member has one of them.
    schematic: bool

    @property
    def shown(self) -> bool:
        return (
            self.revision is not None or self.device_view is not None or self.schematic
        )


def resolve_members(device, user) -> list[Member]:
    """Return each panel member of ``device`` that ``user`` can view, in rack order, with what draws it.

    A member without artwork and without a DeviceView that ``user`` can view
    is a schematic when another visible member has one of them. Otherwise no
    member shows.
    """
    members = panel_members(device, user)
    type_ids = {member.device_type_id for member in members}
    revisions = active_revisions(members)
    views = {
        view.device_type_id: view
        for view in DeviceView.objects.restrict(user, "view").filter(
            device_type__in=type_ids
        )
    }
    shown = bool(revisions.keys() | views.keys())
    resolved = []
    for member in members:
        revision = revisions.get(member.pk)
        device_view = None if revision else views.get(member.device_type_id)
        resolved.append(
            Member(
                member,
                revision,
                device_view,
                shown and not revision and not device_view,
            )
        )
    return resolved
