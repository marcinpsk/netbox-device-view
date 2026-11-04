"""Compose the artwork API envelope: one entry per member of a device that the user can view.

Active artwork and its placed modules come from ``artwork.render``, as on the
Device View tab. A member without it gets the panels of the rendered-layout
API, or a reason.
"""

import logging

from dcim.models import Device

from ..artwork.render import (
    API,
    ARTWORK,
    DAMAGED,
    DAMAGED_ARTWORK,
    active_revisions,
    resolve_device,
)
from ..components import panel_members, viewable_device_view
from ..layout.parser import LayoutParseError
from ..models import DeviceView
from .rendered_layout import UnavailableReason, member_panels

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LAYOUT = "layout"
UNAVAILABLE = "unavailable"
# Reserved for a generic schematic (#47); no member gets it yet.
SCHEMATIC = "schematic"
# The ComponentState facts that a binding gives as they are.
_FACTS = ("enabled", "cable_status", "cable_color", "marked_connected", "path_complete")


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
            return _unavailable_member(DAMAGED_ARTWORK)
        return {
            "representation": ARTWORK,
            "reason": None,
            "artwork": _artwork(part),
            "panels": [],
        }
    try:
        device_view = viewable_device_view(member.device_type, user)
    except DeviceView.DoesNotExist:
        return _unavailable_member(UnavailableReason.NO_LAYOUT)
    if not device_view.has_yaml_layout:
        return _unavailable_member(UnavailableReason.SVG_LAYOUT_REQUIRED)
    try:
        panels = member_panels(member, device_view, user)
    except LayoutParseError:
        logger.warning("Invalid YAML layout for device %s", member.pk)
        return _unavailable_member(UnavailableReason.INVALID_LAYOUT)
    except Exception:
        # As in compose_rendered_layout: one member that fails does not fail the others.
        logger.exception("Unexpected error composing panels for device %s", member.pk)
        return _unavailable_member(UnavailableReason.INVALID_LAYOUT)
    return {"representation": LAYOUT, "reason": None, "artwork": None, "panels": panels}


def _unavailable_member(reason: str) -> dict:
    return {
        "representation": UNAVAILABLE,
        "reason": reason,
        "artwork": None,
        "panels": [],
    }


def _revision(revision) -> dict:
    return {
        "definition_key": revision.definition.key,
        "revision_id": revision.pk,
        "digest": revision.digest,
    }


def _artwork(part) -> dict:
    faces = {face.name: face for face in part.faces}
    return {
        **_revision(part.revision),
        "faces": [
            {"name": face.name, "label": face.label, "svg": str(face.svg)}
            for face in part.faces
        ],
        "connectors": [
            _connector(connector, faces[connector.face].element_id(connector.key))
            for connector in part.connectors
        ],
        "modules": [_module(placed) for placed in part.modules],
    }


def _connector(connector, element_id) -> dict:
    return {
        "key": connector.key,
        "face": connector.face,
        "label": connector.label,
        "element_id": element_id,
        "bindings": [_binding(binding) for binding in connector.bindings],
    }


def _module(placed) -> dict:
    """Return one placed module; its bay is in the coordinates of the device face."""
    bay = placed.bay
    return {
        "module_id": placed.module.pk,
        "module_bay_id": placed.module.module_bay_id,
        "parent_module_id": None if placed.parent is None else placed.parent.pk,
        "bay_key": placed.bay_key,
        "face": placed.face,
        "representation": placed.representation,
        "artwork": None if placed.revision is None else _revision(placed.revision),
        "placement": None if bay is None else {**vars(bay), "rotate": placed.rotate},
        "element_id": placed.element_id,
        "connectors": [
            _connector(connector, placed.connector_id(connector.key))
            for connector in placed.connectors
        ],
        "warnings": [
            {"code": warning.code, "message": warning.message}
            for warning in placed.warnings
        ],
    }


def _binding(binding) -> dict:
    """Return one binding with each connectivity fact in its own field."""
    state = binding.state
    if state is None:
        return {
            "status": binding.status,
            "object_type": binding.model._meta.label_lower,
            "object_id": None,
            "name": binding.name,
            **dict.fromkeys(_FACTS),
            "peer_labels": [],
            "trace_supported": False,
        }
    return {
        "status": binding.status,
        "object_type": state.object_type,
        "object_id": state.component.pk,
        "name": state.component.name,
        **{fact: getattr(state, fact) for fact in _FACTS},
        "peer_labels": state.peer_labels,
        "trace_supported": state.trace_supported,
    }
