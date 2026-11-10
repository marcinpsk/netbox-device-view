"""
Rendered-layout composition for the device rendered-layout API endpoint.

Bridges the layout engine (``netbox_device_view.layout``, pure and NetBox-
agnostic geometry) with the components that ``netbox_device_view.components``
resolves, to produce the versioned JSON envelope consumed by API clients (e.g.
the netbox-companion mobile app). The Device View tab and the device page
panel use the same resolver, so the page and the API bind the same components
in the same state.
"""

from __future__ import annotations

import logging

from dcim.models import Device

from ..components import (
    CONNECTED,
    DISABLED,
    ENABLED,
    PARTIALLY_CONNECTED,
    bind_layout,
    device_components,
    panel_members,
    viewable_device_view,
)
from ..layout import parse as parse_layout
from ..layout.parser import LayoutParseError
from ..layout.renderers.svg import drawn_face, face_views, render_api_view, svg_dims
from ..models import DeviceView
from ..utils import _detect_variant

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Status colours -- mirror device_view_svg.css's Bootstrap-derived palette.
# Baked in as explicit SVG fill attributes since flutter_svg (the
# netbox-companion mobile client's renderer) cannot resolve the CSS classes
# the web renderer relies on for the same states.
COLOUR_CONNECTED = "#198754"  # bg-success
COLOUR_ENABLED = "#6c757d"  # bg-secondary (enabled, not connected)
COLOUR_DISABLED = "#dc3545"  # bg-danger
COLOUR_PARTIAL = "#ffc107"  # bg-warning
NO_COLOR_FILL = "url(#dv-nocolor-pattern)"


class UnavailableReason:
    NO_LAYOUT = "no_layout"
    SVG_LAYOUT_REQUIRED = "svg_layout_required"
    INVALID_LAYOUT = "invalid_layout"


def _fill_for(state) -> str:
    if state.cable_color == "":
        return NO_COLOR_FILL
    if state.cable_fill:
        return state.cable_fill
    return {
        CONNECTED: COLOUR_CONNECTED,
        PARTIALLY_CONNECTED: COLOUR_PARTIAL,
        ENABLED: COLOUR_ENABLED,
        DISABLED: COLOUR_DISABLED,
    }[state.connection_state]


def _reference(state) -> dict:
    return {
        "object_type": state.object_type,
        "object_id": state.component.pk,
        "name": state.component.name,
    }


def _geometry(cell: dict) -> dict:
    return {name: cell[name] for name in ("x", "y", "width", "height")}


def component_fields(state) -> dict:
    """Return the identity and the state of one component, as the API clients get them."""
    return {
        **_reference(state),
        "enabled": state.enabled is not False,
        "connection_state": state.connection_state,
        "cable_color": state.cable_color,
        "peer_labels": state.peer_labels,
        "trace_supported": state.trace_supported,
    }


def _hotspot(cell: dict, state) -> dict:
    # The reference and the geometry come first; a merge keeps the first key order.
    return {**_reference(state), **_geometry(cell), **component_fields(state)}


def _ambiguous(cell: dict, binding) -> dict:
    return {
        "name": binding.key,
        **_geometry(cell),
        "candidates": [_reference(candidate) for candidate in binding.candidates],
    }


def _unavailable(device_id: int, reason: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "reason": reason,
        "device_id": device_id,
        "render_mode": "svg",
        "panels": [],
    }


def member_panels(
    member: Device, device_view: DeviceView, user, key_prefix="", label_suffix=""
) -> list[dict]:
    """Return the panels of one device from the YAML layout of ``device_view``.

    Raises LayoutParseError for a layout that does not parse.
    """
    normalized = parse_layout(device_view.yaml_layout)
    member_modules = list(member.modules.restrict(user, "view"))
    variant = _detect_variant(member_modules, device_view)
    states = device_components(member, user)
    panels = []
    for face, view in face_views(normalized):
        face_key = drawn_face(face, view)
        bindings = bind_layout(states, face_key)
        svg, geometry = render_api_view(
            view,
            fills={
                key: _fill_for(binding.state)
                for key, binding in bindings.items()
                if binding.state
            },
            variant_name=variant,
        )
        hotspots, ambiguous = [], []
        for cell in geometry:
            binding = bindings.get(cell["key"])
            if binding is None:
                continue
            if binding.state is None:
                ambiguous.append(_ambiguous(cell, binding))
            else:
                hotspots.append(_hotspot(cell, binding.state))
        width, height = svg_dims(view.canvas)
        panels.append(
            {
                "key": f"{key_prefix}{face_key}",
                "label": f"{face_key.capitalize()}{label_suffix}",
                "width": width,
                "height": height,
                "svg": svg,
                "hotspots": hotspots,
                "ambiguous": ambiguous,
            }
        )
    return panels


def compose_rendered_layout(device: Device, user) -> dict:
    """
    Compose the full rendered-layout JSON envelope for one device, with only
    the objects that ``user`` can view.

    Handles both plain devices and virtual chassis (one set of panels per
    member, in rack order) — mirrors utils.prepare_svg()'s branching.
    Returns a plain dict matching the API's documented schema; never
    raises for expected "no layout"/"invalid layout" conditions (those are
    reported via ``available``/``reason`` instead).
    """
    members = panel_members(device, user)

    # Unlike the page, one member without a YAML layout fails the whole response.
    member_views: dict[int, DeviceView] = {}
    for member in members:
        try:
            member_views[member.pk] = viewable_device_view(member.device_type, user)
        except DeviceView.DoesNotExist:
            return _unavailable(device.pk, UnavailableReason.NO_LAYOUT)

    for member in members:
        if not member_views[member.pk].has_yaml_layout:
            return _unavailable(device.pk, UnavailableReason.SVG_LAYOUT_REQUIRED)

    panels = []
    try:
        for member in members:
            if len(members) > 1:
                prefix, suffix = f"member-{member.pk}-", f" ({member.name})"
            else:
                prefix, suffix = "", ""
            panels += member_panels(
                member, member_views[member.pk], user, prefix, suffix
            )
    except LayoutParseError:
        logger.warning(
            "Invalid YAML layout for device %s (device_type %s)",
            device.pk,
            device.device_type_id,
        )
        return _unavailable(device.pk, UnavailableReason.INVALID_LAYOUT)
    except Exception:
        logger.exception(
            "Unexpected error composing rendered layout for device %s", device.pk
        )
        return _unavailable(device.pk, UnavailableReason.INVALID_LAYOUT)

    return {
        "schema_version": SCHEMA_VERSION,
        "available": True,
        "reason": None,
        "device_id": device.pk,
        "render_mode": "svg",
        "panels": panels,
    }
