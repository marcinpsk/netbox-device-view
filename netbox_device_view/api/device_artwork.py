"""Compose the artwork API envelope: one entry per member of a device that the user can view.

``members.resolve_members`` selects what draws each member, as on the Device
View tab. Active artwork, the generated schematic and their placed modules
come from ``artwork.render``. A member with a layout gets the panels of the
rendered-layout API, or a reason.
"""

import logging

from dcim.models import Device

from ..artwork.render import (
    API,
    ARTWORK,
    DAMAGED,
    SCHEMATIC,
    resolve_device,
    resolve_schematic,
)
from ..layout.parser import LayoutParseError
from ..members import resolve_members
from .rendered_layout import UnavailableReason, member_panels

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LAYOUT = "layout"
UNAVAILABLE = "unavailable"
DAMAGED_ARTWORK = "damaged_artwork"
ARTWORK_ERROR = "artwork_error"
# The ComponentState facts that a binding gives as they are.
_FACTS = ("enabled", "cable_status", "cable_color", "marked_connected", "path_complete")


def compose_device_artwork(device: Device, user) -> dict:
    """Return the artwork envelope of ``device`` with only the objects that ``user`` can view."""
    return {
        "schema_version": SCHEMA_VERSION,
        "device_id": device.pk,
        "members": [
            {
                "device_id": member.device.pk,
                "label": str(member.device),
                "position": member.device.vc_position,
                "current": member.device.pk == device.pk,
                **_representation(member, user),
            }
            for member in resolve_members(device, user)
        ],
    }


def _representation(member, user) -> dict:
    device, device_view = member.device, member.device_view
    if member.revision is not None:
        try:
            part = resolve_device(device, member.revision, user, view=API, plain=True)
        except Exception:
            # As for the panels: one member that fails does not fail the others.
            logger.exception(
                "Unexpected error drawing the artwork of device %s", device.pk
            )
            return _unavailable_member(ARTWORK_ERROR)
        if part.representation == DAMAGED:
            return _unavailable_member(DAMAGED_ARTWORK)
        return _drawn(ARTWORK, part)
    if member.schematic:
        return _drawn(SCHEMATIC, resolve_schematic(device, user, view=API, plain=True))
    if device_view is None:
        return _unavailable_member(UnavailableReason.NO_LAYOUT)
    if not device_view.has_yaml_layout:
        return _unavailable_member(UnavailableReason.SVG_LAYOUT_REQUIRED)
    try:
        panels = member_panels(device, device_view, user)
    except LayoutParseError:
        logger.warning("Invalid YAML layout for device %s", device.pk)
        return _unavailable_member(UnavailableReason.INVALID_LAYOUT)
    except Exception:
        # As in compose_rendered_layout: one member that fails does not fail the others.
        logger.exception("Unexpected error composing panels for device %s", device.pk)
        return _unavailable_member(UnavailableReason.INVALID_LAYOUT)
    return {"representation": LAYOUT, "reason": None, "artwork": None, "panels": panels}


def _drawn(representation, part) -> dict:
    return {
        "representation": representation,
        "reason": None,
        "artwork": _artwork(part),
        "panels": [],
    }


def _unavailable_member(reason: str) -> dict:
    return {
        "representation": UNAVAILABLE,
        "reason": reason,
        "artwork": None,
        "panels": [],
    }


def _revision(revision) -> dict:
    """Return the fields of ``revision``; a schematic has no revision, so each is null."""
    if revision is None:
        return {"definition_key": None, "revision_id": None, "digest": None}
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
        "bays": [_bay(drawn) for drawn in part.bays],
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


def _bay(drawn) -> dict:
    """Return one drawn bay: installed, or not recorded when no module that the user can view is in it."""
    return {
        "parent_module_id": None if drawn.parent is None else drawn.parent.pk,
        "bay_key": drawn.key,
        "module_bay_id": None if drawn.module_bay is None else drawn.module_bay.pk,
        "face": drawn.face,
        "state": drawn.state,
        "module_id": None if drawn.module is None else drawn.module.pk,
        "element_id": drawn.element_id,
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
