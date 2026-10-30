import re
import unicodedata
from dataclasses import dataclass

from dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    FrontPortTemplate,
    InterfaceTemplate,
    RearPortTemplate,
    VirtualChassis,
)
from django.core.exceptions import ObjectDoesNotExist
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

from .components import (
    AMBIGUOUS,
    CONNECTED,
    DISABLED,
    ENABLED,
    PARTIALLY_CONNECTED,
    bind_layout,
    derive_interface_key,
    derive_port_key,
    device_components,
    panel_members,
    viewable_device_view,
)
from .layout import (
    LayoutParseError,
    get_css_for_device_view,
    get_svg_for_device_view,
)
from .layout import (
    parse as parse_layout,
)
from .layout.renderers.svg import PortCell
from .models import DeviceView


def normalize_grid_template_area(value: str) -> str:
    """
    Replace every Unicode space separator (category Zs) with an ASCII space; keep newlines.

    Pasted text often holds U+00A0 and similar spaces, which grid-template-areas rejects.
    """
    return "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in value)


def get_stylenames_for_device_type(device_type_id):
    """Return a list of dicts with name, stylename, and kind for all component
    templates of the given DeviceType.  Mirrors the stylename logic used at
    render-time so the Add/Edit form can preview CSS grid-area names before a
    DeviceView is saved.
    """
    results = []

    for tmpl in InterfaceTemplate.objects.filter(
        device_type_id=device_type_id
    ).order_by("name"):
        if tmpl.type in ("virtual", "lag"):
            continue
        results.append(
            {
                "name": tmpl.name,
                "stylename": derive_interface_key(tmpl.name),
                "kind": "Interface",
                "type": tmpl.get_type_display(),
            }
        )

    for tmpl in ConsolePortTemplate.objects.filter(
        device_type_id=device_type_id
    ).order_by("name"):
        results.append(
            {
                "name": tmpl.name,
                "stylename": derive_port_key(tmpl.name),
                "kind": "Console Port",
                "type": tmpl.get_type_display(),
            }
        )

    for tmpl in ConsoleServerPortTemplate.objects.filter(
        device_type_id=device_type_id
    ).order_by("name"):
        results.append(
            {
                "name": tmpl.name,
                "stylename": derive_port_key(tmpl.name),
                "kind": "Console Server Port",
                "type": tmpl.get_type_display(),
            }
        )

    for tmpl in FrontPortTemplate.objects.filter(
        device_type_id=device_type_id
    ).order_by("name"):
        results.append(
            {
                "name": tmpl.name,
                "stylename": derive_port_key(tmpl.name),
                "kind": "Front Port",
                "type": tmpl.get_type_display(),
            }
        )

    for tmpl in RearPortTemplate.objects.filter(device_type_id=device_type_id).order_by(
        "name"
    ):
        results.append(
            {
                "name": tmpl.name,
                "stylename": derive_port_key(tmpl.name),
                "kind": "Rear Port",
                "type": tmpl.get_type_display(),
            }
        )

    return results


def device_height_px(device_type):
    """Return the CSS pixel height for a device grid based on u_height.

    Each U is rendered as two rows of 20 px with 2 px of spacing per U.
    """
    return device_type.u_height * 2 * 20 + device_type.u_height * 2


def active_panel_key(obj):
    """Return the ports_chassis/svg_views key that corresponds to ``obj``,
    or ``None`` for a standalone (non-virtual-chassis) device.

    Mirrors the key scheme used internally by ``prepare()``/``prepare_svg()``:
    a virtual chassis member is keyed by its ``vc_position``. Used to
    highlight the specific device the user navigated to when viewing a
    virtual chassis, whose device page shows every member's panel stacked
    together. Returns ``None`` for a standalone device since there is only
    ever one panel in that case, making a highlight redundant.
    """
    if obj.virtual_chassis is None:
        return None
    return obj.vc_position


def _detect_variant(member_modules, device_view) -> str | None:
    """Return the first installed module model name that matches a known variant,
    or None if no variant applies."""
    if not device_view.has_yaml_layout:
        return None
    layout = parse_layout(device_view.yaml_layout)
    view = layout.front or layout.rear
    if view is None or not view.variants:
        return None
    installed_models = {m.module_type.model for m in member_modules}
    for variant_name in view.variants:
        if variant_name in installed_models:
            return variant_name
    return None


# format_html_join() escapes a plain-string separator.
_BREAK = mark_safe("<br>")
_STATE_CLASSES = {
    CONNECTED: "bg-success",
    PARTIALLY_CONNECTED: "bg-warning",
    ENABLED: "bg-secondary",
    DISABLED: "bg-danger",
    AMBIGUOUS: "dv-ambiguous",
}


@dataclass(frozen=True)
class PageCell:
    """The page markup of one layout key: its state, link and popover."""

    stylename: str
    name: str
    connection_state: str
    # None for an ambiguous key: it links to no single component.
    href: str | None
    classes: tuple[str, ...]
    # The cable colour, when the Cable Colors option is on.
    fill: str | None
    popover_title: str
    popover_content: str

    @property
    def partial(self):
        return self.connection_state == PARTIALLY_CONNECTED

    @property
    def disabled(self):
        return self.connection_state == DISABLED

    def port_cell(self):
        """Return the SVG state markup of this cell."""
        attributes = {
            "data-connection-state": self.connection_state,
            "data-bs-toggle": "popover",
            "data-bs-title": self.popover_title,
            "data-bs-content": self.popover_content,
        }
        if self.href:
            attributes |= {"data-href": self.href, "role": "link"}
        return PortCell(
            classes=self.classes,
            attributes=attributes,
            fill=self.fill,
            title=self.name,
            dot=self.partial,
        )


def _page_cell(binding, cable_colors):
    state = binding.state
    if state is None:
        return PageCell(
            stylename=binding.key,
            name=binding.key,
            connection_state=binding.connection_state,
            href=None,
            classes=(_STATE_CLASSES[AMBIGUOUS],),
            fill=None,
            popover_title=escape(binding.key),
            popover_content=format_html_join(
                _BREAK,
                '<a href="{}">{}</a> ({})',
                (
                    (
                        candidate.component.get_absolute_url(),
                        candidate.component.name,
                        candidate.component._meta.verbose_name,
                    )
                    for candidate in binding.candidates
                ),
            ),
        )
    component = state.component
    url = component.get_absolute_url()
    title = escape(component.name)
    if state.peer_labels:
        title += format_html(
            "<hr>{}",
            format_html_join(_BREAK, "{}", ((label,) for label in state.peer_labels)),
        )
    if cable_colors and state.cable_fill:
        classes, fill = (), state.cable_fill
    elif cable_colors and state.cable_color == "":
        classes, fill = ("nocolor",), None
    else:
        classes, fill = (_STATE_CLASSES[state.connection_state],), None
    return PageCell(
        stylename=binding.key,
        name=component.name,
        connection_state=binding.connection_state,
        href=url,
        classes=classes,
        fill=fill,
        popover_title=title,
        popover_content=format_html('<a href="{}trace/">Trace</a>', url),
    )


def page_cells(states, cable_colors, face=None):
    """Return a PageCell for each layout key that ``states`` bind to."""
    return [
        _page_cell(binding, cable_colors)
        for binding in bind_layout(states, face).values()
    ]


def _css_groups(obj, members, user):
    """Return {grid key: states}: one grid per member, and Front and Rear grids for a device outside a virtual chassis."""
    groups = {}
    for member in members:
        states = device_components(member, user)
        if obj.virtual_chassis is None:
            for key, face in ((obj.name, None), ("Front", "front"), ("Rear", "rear")):
                group = [state for state in states if state.face == face]
                if group:
                    groups[key] = group
        else:
            group = [state for state in states if state.face is None]
            if group:
                groups[member.vc_position] = group
    return groups


def prepare(obj, user, cable_colors=False):
    """Return (dv, modules, ports_chassis, device_view) for CSS rendering, with the objects that ``user`` can view.

    ``ports_chassis`` maps each grid key to its PageCells. Returns
    (None, None, None, None) if no DeviceView is configured for the device
    type.
    """
    dv = {}
    modules = {}
    device_view = None
    members = panel_members(obj, user)
    try:
        if obj.virtual_chassis is None:
            device_view = viewable_device_view(obj.device_type, user)
            dv[1] = get_css_for_device_view(device_view)
            modules[1] = obj.modules.restrict(user, "view")
        else:
            for member in members:
                pos = member.vc_position
                member_css = get_css_for_device_view(
                    viewable_device_view(member.device_type, user)
                )
                dv[pos] = re.sub(r"\.area\b", f".area.d{pos}", member_css)
                modules[pos] = member.modules.restrict(user, "view")
    except ObjectDoesNotExist:
        return None, None, None, None

    ports_chassis = {
        key: page_cells(states, cable_colors)
        for key, states in _css_groups(obj, members, user).items()
    }
    return dv, modules, ports_chassis, device_view


def _svg_cells(states, cable_colors):
    return {
        face: {
            cell.stylename: cell.port_cell()
            for cell in page_cells(states, cable_colors, face)
        }
        for face in ("front", "rear")
    }


def prepare_svg(obj, user, cable_colors=False):
    """Return (svg_views, modules, device_view) for SVG rendering, with the objects that ``user`` can view.

    ``svg_views`` maps each panel key (the device name, or the
    ``vc_position`` of a virtual chassis member) to its SVG markup. A member
    without components has no panel. Returns (None, None, None) when a
    member has no DeviceView.
    """
    svg_views = {}
    modules = {}
    device_view = None
    try:
        for member in panel_members(obj, user):
            key = obj.name if obj.virtual_chassis is None else member.vc_position
            device_view = viewable_device_view(member.device_type, user)
            member_modules = list(member.modules.restrict(user, "view"))
            modules[key] = member_modules
            states = device_components(member, user)
            svg = get_svg_for_device_view(
                device_view,
                variant_name=_detect_variant(member_modules, device_view),
                cells=_svg_cells(states, cable_colors),
            )
            if states:
                svg_views[key] = svg
    except ObjectDoesNotExist:
        return None, None, None
    return svg_views, modules, device_view


def panel_title(obj, user):
    """Return the virtual chassis name of ``obj`` when ``user`` can view it, else the device name."""
    if (
        obj.virtual_chassis_id is not None
        and VirtualChassis.objects.restrict(user, "view")
        .filter(pk=obj.virtual_chassis_id)
        .exists()
    ):
        return obj.virtual_chassis.name
    return obj.name


def device_view_context(obj, user, cable_colors=False):
    """Return the render context for the device view of ``obj`` that ``user`` sees, or None without one.

    ``layout_error`` holds the parse error of a stored layout that does not parse.
    """
    context = {
        "layout_error": None,
        "device_view": None,
        "dv": None,
        "svg_panels": [],
        "use_svg": False,
        "modules": None,
        "ports_chassis": None,
        "height": device_height_px(obj.device_type),
        "active_panel": active_panel_key(obj),
        "panel_title": panel_title(obj, user),
    }
    try:
        use_svg = viewable_device_view(obj.device_type, user).use_svg
    except DeviceView.DoesNotExist:
        use_svg = False

    try:
        if use_svg:
            svg_views, modules, device_view = prepare_svg(obj, user, cable_colors)
            # prepare_svg() gives None when a virtual chassis member has no device view.
            use_svg = svg_views is not None
            context["svg_panels"] = list((svg_views or {}).items())
        if not use_svg:
            dv, modules, ports_chassis, device_view = prepare(obj, user, cable_colors)
            if dv is None:
                return None
            context.update(dv=dv, ports_chassis=ports_chassis)
    except LayoutParseError as exc:
        context["layout_error"] = str(exc)
        return context

    context.update(device_view=device_view, use_svg=use_svg, modules=modules)
    return context
