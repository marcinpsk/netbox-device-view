"""Find the components of a device and their connectivity facts, for the device view pages and the API.

Every object is read through ``restrict(user, "view")``. A hidden device,
component or peer is treated as an absent one; a hidden cable gives no cable
facts.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from dcim.models import (
    Cable,
    ConsolePort,
    ConsolePortTemplate,
    ConsoleServerPort,
    ConsoleServerPortTemplate,
    FrontPort,
    FrontPortTemplate,
    Interface,
    InterfaceTemplate,
    Module,
    PowerOutlet,
    PowerOutletTemplate,
    PowerPort,
    PowerPortTemplate,
    RearPort,
    RearPortTemplate,
)
from dcim.models.device_components import PathEndpoint
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db.models import Q

from .models import DeviceView

# The component types that a layout key binds to.
LAYOUT_MODELS = (Interface, ConsolePort, ConsoleServerPort, FrontPort, RearPort)
BINDABLE_MODELS = (*LAYOUT_MODELS, PowerPort, PowerOutlet)

_TEMPLATE_MODELS = {
    Interface: InterfaceTemplate,
    ConsolePort: ConsolePortTemplate,
    ConsoleServerPort: ConsoleServerPortTemplate,
    FrontPort: FrontPortTemplate,
    RearPort: RearPortTemplate,
    PowerPort: PowerPortTemplate,
    PowerOutlet: PowerOutletTemplate,
}
logger = logging.getLogger(__name__)
_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_EXCLUDED_TYPES = {Interface: ("virtual", "lag")}
_FACES = {FrontPort: "front", RearPort: "rear"}

CONNECTED = "connected"
PARTIALLY_CONNECTED = "partially_connected"
ENABLED = "enabled"
DISABLED = "disabled"
AMBIGUOUS = "ambiguous"


def derive_interface_key(name):
    regex = r"^(?P<type>([a-zA-Z\-_]*))(\/|(?P<dev>[0-9]+).|\s)?((?P<module>[0-9]+).|\s)?((?P<port>[0-9]+))$"
    matches = re.search(regex, name.lower())
    if matches:
        if not matches["type"]:
            key = matches["port"]
        else:
            key = matches["type"] + (matches["module"] or "") + "-" + matches["port"]
    else:
        key = re.sub(r"[^.a-zA-Z\d]", "-", name.lower())
    if key.isdigit():
        key = f"p{key}"
    return key


def derive_port_key(name):
    key = re.sub(r"[^.a-zA-Z\d]", "-", name.lower())
    if key.isdigit():
        key = f"p{key}"
    return key


def layout_key(component):
    """Return the layout key (the stylename) of ``component``."""
    if isinstance(component, Interface):
        return derive_interface_key(component.name)
    return derive_port_key(component.name)


@dataclass
class ComponentState:
    """One component and the connectivity facts that the requesting user can see."""

    component: object
    # None where the component type has no enabled field.
    enabled: bool | None
    # The cable facts come only from a cable that the user can view.
    cable_status: str | None
    # None without a visible cable, "" for a visible cable without a colour.
    cable_color: str | None
    marked_connected: bool
    # None where the component type has no cable path.
    path_complete: bool | None
    peer_labels: list[str]
    connection_state: str

    @property
    def key(self):
        return layout_key(self.component)

    @property
    def face(self):
        """Return "front" or "rear" for a pass-through port, else None."""
        return _FACES.get(type(self.component))

    @property
    def cable_fill(self):
        """Return the visible cable colour as "#rrggbb", or None without one."""
        if not self.cable_color:
            return None
        if not _HEX_COLOR_RE.match(self.cable_color):
            logger.warning(
                "Ignoring non-hex cable color %r on %s",
                self.cable_color,
                self.component,
            )
            return None
        return f"#{self.cable_color}"

    @property
    def occupied(self):
        return self.cable_status is not None

    @property
    def object_type(self):
        return f"dcim.{type(self.component)._meta.model_name}"

    @property
    def trace_supported(self):
        return isinstance(self.component, PathEndpoint)


@dataclass
class KeyBinding:
    """The components that one layout key binds to; more than one is ambiguous."""

    key: str
    candidates: list[ComponentState]

    @property
    def state(self):
        """Return the one bound component, or None when the binding is ambiguous."""
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def connection_state(self):
        return self.state.connection_state if self.state else AMBIGUOUS


def panel_members(obj, user):
    """Return the devices of the panels of ``obj`` that ``user`` can view, in rack order."""
    if obj.virtual_chassis is None:
        return [obj]
    return sorted(
        obj.virtual_chassis.members.restrict(user, "view").select_related("rack"),
        key=_rack_sort_key,
    )


def _rack_sort_key(member):
    """Sort members top of rack first; respect ``desc_units``; unracked members go last."""
    pos = member.position
    vc = member.vc_position if member.vc_position is not None else 0
    if pos is None:
        return (1, float("inf"), vc)
    if member.rack and member.rack.desc_units:
        return (0, float(pos), vc)
    return (0, -float(pos), vc)


def viewable_device_view(device_type, user):
    """Return the DeviceView of ``device_type``; a DeviceView that ``user`` cannot view does not exist."""
    return DeviceView.objects.restrict(user, "view").get(device_type=device_type)


def _peer_querysets():
    return [model.objects.select_related("device", "cable") for model in LAYOUT_MODELS]


def _queryset(model, device, user):
    queryset = (
        model.objects.restrict(user, "view")
        .filter(device=device)
        .filter(
            Q(module__isnull=True) | Q(module__in=Module.objects.restrict(user, "view"))
        )
        .exclude(type__in=_EXCLUDED_TYPES.get(model, ("virtual",)))
        .select_related("cable")
        .prefetch_related(
            GenericPrefetch("cable__terminations__termination", _peer_querysets())
        )
    )
    if issubclass(model, PathEndpoint):
        queryset = queryset.prefetch_related(
            GenericPrefetch("_path__path_objects", _peer_querysets())
        )
    return queryset


def device_components(device, user, models=LAYOUT_MODELS):
    """Return a ComponentState for each component of ``models`` on ``device`` that ``user`` can view."""
    components = [
        component for model in models for component in _queryset(model, device, user)
    ]
    return component_states(components, user)


def _peer_label(peer):
    device = getattr(peer, "device", None)
    name = getattr(peer, "name", None)
    if device and name:
        return f"{device} | {name}"
    return str(peer)


def _visible(pks_by_model, user):
    return {
        (model, pk)
        for model, pks in pks_by_model.items()
        for pk in model.objects.restrict(user, "view")
        .filter(pk__in=pks)
        .values_list("pk", flat=True)
    }


def component_states(components, user):
    """Return a ComponentState for each of ``components``, with only the cables and peers that ``user`` can view.

    A peer label needs view permission on the peer only, not on the cable.
    """
    components = list(components)
    visible_cables = _visible(
        {Cable: {c.cable_id for c in components if c.cable_id is not None}}, user
    )
    links = []
    for component in components:
        link_peers = list(component.link_peers)
        endpoints = (
            list(component.connected_endpoints or [])
            if isinstance(component, PathEndpoint)
            else []
        )
        links.append((component, link_peers, endpoints))

    pks_by_model = defaultdict(set)
    for _component, link_peers, endpoints in links:
        for peer in endpoints or link_peers:
            pks_by_model[type(peer)].add(peer.pk)
    visible_peers = _visible(pks_by_model, user)

    return [
        _state(*link, (Cable, link[0].cable_id) in visible_cables, visible_peers)
        for link in links
    ]


def _state(component, link_peers, endpoints, cable_visible, visible_peers):
    cable = component.cable if cable_visible else None
    # A hidden cable is no cable: it does not connect the component.
    linked = cable is not None or component.cable_id is None
    enabled = getattr(component, "enabled", None)
    if isinstance(component, Interface):
        connected = linked and bool(endpoints)
        partially_connected = linked and bool(link_peers) and not connected
    else:
        connected = linked and bool(link_peers)
        partially_connected = False
    if connected:
        connection_state = CONNECTED
    elif partially_connected:
        connection_state = PARTIALLY_CONNECTED
    elif enabled is not False:
        connection_state = ENABLED
    else:
        connection_state = DISABLED
    path = getattr(component, "_path", None)
    return ComponentState(
        component=component,
        enabled=enabled,
        cable_status=cable.status if cable else None,
        cable_color=cable.color if cable else None,
        marked_connected=component.mark_connected,
        path_complete=(
            bool(linked and path and path.is_complete)
            if isinstance(component, PathEndpoint)
            else None
        ),
        peer_labels=[
            _peer_label(peer)
            for peer in endpoints or link_peers
            if (type(peer), peer.pk) in visible_peers
        ],
        connection_state=connection_state,
    )


def bind_layout(states, face=None):
    """Return {layout key: KeyBinding} for ``states``; a face ("front" or "rear") leaves out the pass-through ports of the other face."""
    bindings = {}
    for state in states:
        if face is not None and state.face not in (None, face):
            continue
        bindings.setdefault(state.key, KeyBinding(state.key, [])).candidates.append(
            state
        )
    return bindings


def find(states, model, name, module=None):
    """Return the states of ``model`` components named ``name`` in the scope of ``module``.

    Without ``module`` the scope is the components with no module. With
    ``module``, ``name`` is a NetBox template name and its ``{module}`` tokens
    resolve the way NetBox names the components of that module.
    """
    if module is not None:
        name = _TEMPLATE_MODELS[model](name=name).resolve_name(module)
    module_id = module.pk if module is not None else None
    return [
        state
        for state in states
        if type(state.component) is model
        and state.component.name == name
        and state.component.module_id == module_id
    ]
