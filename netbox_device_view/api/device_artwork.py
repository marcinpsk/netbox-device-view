"""Compose the artwork API envelope: one entry per member of a device that the user can view.

Active artwork comes from ``artwork.render``, as on the Device View tab. A
member without it gets the panels of the rendered-layout API, or a reason.
"""

import logging

from dcim.models import Device

from ..artwork.render import API, DAMAGED, FOUND, active_revisions, resolve_device
from ..components import panel_members, viewable_device_view
from ..layout.parser import LayoutParseError
from ..models import DeviceView
from .rendered_layout import UnavailableReason, component_fields, member_panels

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ARTWORK = "artwork"
LAYOUT = "layout"
UNAVAILABLE = "unavailable"
# Reserved for a generic schematic (#47); no member gets it yet.
SCHEMATIC = "schematic"
DAMAGED_ARTWORK = "damaged_artwork"


def compose_device_artwork(device: Device, user) -> dict:
    """Return the artwork envelope of ``device`` with only the objects that ``user`` can view."""
    members = panel_members(device, user)
    revisions = active_revisions({member.device_type_id for member in members})
    return {
        "schema_version": SCHEMA_VERSION,
        "device_id": device.pk,
        "members": [
            {
                "device_id": member.pk,
                "label": str(member),
                "position": member.vc_position,
                "current": member.pk == device.pk,
                **_representation(member, revisions.get(member.device_type_id), user),
            }
            for member in members
        ],
    }


def _representation(member, revision, user) -> dict:
    if revision is not None:
        part = resolve_device(member, revision, user, view=API, plain=True)
        if part.representation == DAMAGED:
            return _unavailable(DAMAGED_ARTWORK)
        return {
            "representation": ARTWORK,
            "reason": None,
            "artwork": _artwork(part),
            "panels": [],
        }
    try:
        device_view = viewable_device_view(member.device_type, user)
    except DeviceView.DoesNotExist:
        return _unavailable(UnavailableReason.NO_LAYOUT)
    if not device_view.has_yaml_layout:
        return _unavailable(UnavailableReason.SVG_LAYOUT_REQUIRED)
    try:
        panels = member_panels(member, device_view, user)
    except LayoutParseError:
        logger.warning("Invalid YAML layout for device %s", member.pk)
        return _unavailable(UnavailableReason.INVALID_LAYOUT)
    return {"representation": LAYOUT, "reason": None, "artwork": None, "panels": panels}


def _unavailable(reason: str) -> dict:
    return {
        "representation": UNAVAILABLE,
        "reason": reason,
        "artwork": None,
        "panels": [],
    }


def _artwork(part) -> dict:
    prefixes = {face.name: face.prefix for face in part.faces}
    return {
        "definition_key": part.revision.definition.key,
        "revision_id": part.revision.pk,
        "digest": part.revision.digest,
        "faces": [
            {"name": face.name, "label": face.label, "svg": str(face.svg)}
            for face in part.faces
        ],
        "connectors": [
            {
                "key": connector.key,
                "face": connector.face,
                "label": connector.label,
                "element_id": prefixes[connector.face] + connector.key,
                "bindings": [_binding(binding) for binding in connector.bindings],
            }
            for connector in part.connectors
        ],
    }


def _binding(binding) -> dict:
    if binding.status == FOUND:
        return {
            "status": binding.status,
            **component_fields(binding.state),
            "occupied": binding.state.occupied,
        }
    return {
        "status": binding.status,
        "object_type": binding.model._meta.label_lower,
        "object_id": None,
        "name": binding.name,
        "enabled": None,
        "connection_state": None,
        "cable_color": None,
        "peer_labels": [],
        "trace_supported": False,
        "occupied": False,
    }
