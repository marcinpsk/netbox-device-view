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
import re

from dcim.models import Device

from ..components import (
    AMBIGUOUS,
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
from ..layout.model import Face
from ..layout.parser import LayoutParseError
from ..layout.renderers.svg import face_views, render_api_view, svg_dims
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

_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


class UnavailableReason:
    NO_LAYOUT = "no_layout"
    SVG_LAYOUT_REQUIRED = "svg_layout_required"
    INVALID_LAYOUT = "invalid_layout"


def _fill_for(state) -> str:
    if state.cable_color is not None:
        if state.cable_color == "":
            return NO_COLOR_FILL
        if _HEX_COLOR_RE.match(state.cable_color):
            return f"#{state.cable_color}"
        # Malformed value somehow stored on the model — don't trust it raw
        # in hand-built SVG; fall through to the state colour instead.
        logger.warning(
            "Ignoring non-hex cable color %r on %s",
            state.cable_color,
            state.component,
        )

    return {
        CONNECTED: COLOUR_CONNECTED,
        PARTIALLY_CONNECTED: COLOUR_PARTIAL,
        ENABLED: COLOUR_ENABLED,
        DISABLED: COLOUR_DISABLED,
    }[state.connection_state]


def _panel_key(face, view) -> str:
    if face:
        return face
    return "rear" if view.face == Face.REAR else "front"


def _reference(state) -> dict:
    return {
        "object_type": state.object_type,
        "object_id": state.component.pk,
        "name": state.component.name,
    }


def _hotspot(cell: dict, binding) -> dict:
    geometry = {name: cell[name] for name in ("x", "y", "width", "height")}
    state = binding.state
    if state is None:
        return {
            "object_type": None,
            "object_id": None,
            "name": binding.key,
            **geometry,
            "enabled": None,
            "connection_state": AMBIGUOUS,
            "cable_color": None,
            "peer_labels": [],
            "trace_supported": False,
            "candidates": [_reference(candidate) for candidate in binding.candidates],
        }
    return {
        **_reference(state),
        **geometry,
        "enabled": state.enabled is not False,
        "connection_state": state.connection_state,
        "cable_color": state.cable_color,
        "peer_labels": state.peer_labels,
        "trace_supported": state.trace_supported,
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

    # First pass: resolve every member's DeviceView. Any missing/CSS-only
    # DeviceView fails the whole response (matches prepare_svg()'s
    # all-or-nothing ObjectDoesNotExist behaviour for virtual chassis).
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
            device_view = member_views[member.pk]
            normalized = parse_layout(device_view.yaml_layout)
            member_modules = list(member.modules.restrict(user, "view"))
            variant = _detect_variant(member_modules, device_view)
            states = device_components(member, user)

            member_prefix = f"member-{member.pk}-" if len(members) > 1 else ""
            member_suffix = f" ({member.name})" if len(members) > 1 else ""

            for face, view in face_views(normalized):
                bindings = bind_layout(states, face)
                face_key = _panel_key(face, view)
                face_label = face_key.capitalize()
                svg, geometry = render_api_view(
                    view,
                    fills={
                        key: _fill_for(binding.state)
                        for key, binding in bindings.items()
                        if binding.state
                    },
                    variant_name=variant,
                )
                hotspots = [
                    _hotspot(cell, bindings[cell["key"]])
                    for cell in geometry
                    if cell["key"] in bindings
                ]
                width, height = svg_dims(view.canvas)
                panels.append(
                    {
                        "key": f"{member_prefix}{face_key}",
                        "label": f"{face_label}{member_suffix}",
                        "width": width,
                        "height": height,
                        "svg": svg,
                        "hotspots": hotspots,
                    }
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
