import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    Device,
    FrontPortTemplate,
    InterfaceTemplate,
    Module,
    RearPortTemplate,
    VirtualChassis,
)

from .artwork.render import Part, legend, resolve_device, resolve_schematic
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
)
from .details import Details, key_details
from .layout import (
    LayoutParseError,
    get_css_for_device_view,
    get_svg_for_device_view,
)
from .layout import (
    parse as parse_layout,
)
from .layout.renderers.svg import PortCell, drawn_keys
from .members import resolve_members


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


# The text of each layout state, for a cell that shows its state only as a colour and an icon.
_STATE_LABELS = {
    CONNECTED: "connected",
    PARTIALLY_CONNECTED: "partially connected",
    ENABLED: "not connected",
    DISABLED: "disabled",
    AMBIGUOUS: "several components",
}
_STATE_CLASSES = {
    CONNECTED: "bg-success",
    PARTIALLY_CONNECTED: "bg-warning",
    ENABLED: "bg-secondary",
    DISABLED: "bg-danger",
    AMBIGUOUS: "dv-ambiguous",
}


@dataclass(frozen=True)
class PageCell:
    """The page markup of one layout key: its state, link and details."""

    stylename: str
    name: str
    connection_state: str
    # None for an ambiguous key: it links to no single component.
    href: str | None
    classes: tuple[str, ...]
    # The cable colour, when the Cable Colors option is on.
    fill: str | None
    details: Details

    @property
    def partial(self):
        return self.connection_state == PARTIALLY_CONNECTED

    @property
    def disabled(self):
        return self.connection_state == DISABLED

    @property
    def label(self):
        """Return the name and the state, for example "Gi1: connected"."""
        return f"{self.name}: {_STATE_LABELS[self.connection_state]}"

    def port_cell(self):
        """Return the SVG state markup of this cell."""
        attributes = {
            "data-connection-state": self.connection_state,
            "data-dv-details": self.details.ident,
        }
        if self.href:
            attributes |= {"data-href": self.href, "role": "link"}
        else:
            attributes["aria-haspopup"] = "dialog"
        return PortCell(
            classes=self.classes,
            attributes=attributes,
            fill=self.fill,
            title=self.name,
            dot=self.partial,
        )


def _page_cell(binding, cable_colors, prefix):
    state = binding.state
    details = key_details(prefix, binding)
    if state is None:
        return PageCell(
            stylename=binding.key,
            name=binding.key,
            connection_state=binding.connection_state,
            href=None,
            classes=(_STATE_CLASSES[AMBIGUOUS],),
            fill=None,
            details=details,
        )
    component = state.component
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
        href=component.get_absolute_url(),
        classes=classes,
        fill=fill,
        details=details,
    )


def page_cells(states, cable_colors, prefix, face=None):
    """Return a PageCell for each layout key that ``states`` bind to; ``prefix`` makes the details ids unique."""
    return [
        _page_cell(binding, cable_colors, prefix)
        for binding in bind_layout(states, face).values()
    ]


def _css_grids(member, user, cable_colors):
    """Return {grid key: PageCells}: one grid for a virtual chassis member, and the device, Front and Rear grids for a device outside one."""
    states = device_components(member, user)
    if member.virtual_chassis_id is None:
        keys = ((member.name, None), ("Front", "front"), ("Rear", "rear"))
    else:
        keys = ((member.vc_position, None),)
    groups = {}
    for key, face in keys:
        group = [state for state in states if state.face == face]
        if group:
            groups[key] = group
    return {
        key: page_cells(group, cable_colors, f"dv-c{member.pk}-{index}")
        for index, (key, group) in enumerate(groups.items())
    }


def prepare(member, device_view, user, cable_colors=False):
    """Return (css, modules, grids) of the CSS layout of ``member``, with the objects that ``user`` can view.

    The CSS of a virtual chassis member is scoped with ``.d<vc_position>``.
    ``grids`` maps each grid key to its PageCells. Raises LayoutParseError
    for a YAML layout that does not parse.
    """
    css = get_css_for_device_view(device_view)
    if member.virtual_chassis_id is not None:
        css = re.sub(r"\.area\b", f".area.d{member.vc_position}", css)
    modules = list(member.modules.restrict(user, "view").select_related("module_type"))
    return css, modules, _css_grids(member, user, cable_colors)


def _svg_cells(states, cable_colors, prefix, drawn):
    """Return ({face: {key: PortCell}}, the details of each cell that ``drawn`` ({face: keys}) has)."""
    cells = {
        face: page_cells(states, cable_colors, f"{prefix}-{face}", face)
        for face in ("front", "rear")
    }
    port_cells = {
        face: {cell.stylename: cell.port_cell() for cell in face_cells}
        for face, face_cells in cells.items()
    }
    details = [
        cell.details
        for face, face_cells in cells.items()
        for cell in face_cells
        if cell.stylename in drawn.get(face, ())
    ]
    return port_cells, details


def prepare_svg(member, device_view, user, cable_colors=False):
    """Return (svg, details) of the SVG layout of ``member``, with the objects that ``user`` can view.

    ``svg`` is None for a member without components. Raises LayoutParseError
    for a YAML layout that does not parse.
    """
    # The layout renders without components too, so its parse error shows.
    variant = _detect_variant(list(member.modules.restrict(user, "view")), device_view)
    drawn = drawn_keys(parse_layout(device_view.yaml_layout), variant)
    states = device_components(member, user)
    prefix = f"dv-s{member.pk}"
    cells, details = _svg_cells(states, cable_colors, prefix, drawn)
    svg = get_svg_for_device_view(
        device_view, variant_name=variant, cells=cells, id_prefix=prefix
    )
    return (svg, tuple(details)) if states else (None, ())


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


@dataclass(frozen=True)
class MemberPanel:
    """The device view of one member: a Part (artwork or schematic), an SVG layout, CSS grids, or the error of its layout."""

    device: Device
    current: bool
    part: Part | None = None
    layout_error: str | None = None
    use_svg: bool = False
    # An SVG layout: its markup and the details of its cells; None without components.
    svg: str | None = None
    details: tuple[Details, ...] = ()
    # A CSS layout: its CSS, its height in px, {grid key: PageCells}, and the modules that name its classes.
    css: str | None = None
    height: Decimal | None = None
    grids: dict[object, list[PageCell]] = field(default_factory=dict)
    modules: tuple[Module, ...] = ()

    @property
    def key(self):
        """Return the key of the panel: the device name, or the ``vc_position`` of a virtual chassis member."""
        if self.device.virtual_chassis_id is None:
            return self.device.name
        return self.device.vc_position

    @property
    def height_selector(self):
        """Return the CSS selector of the grids that ``height`` applies to."""
        if self.device.virtual_chassis_id is None:
            return ".deviceview"
        return f".deviceview.d{self.key}"


def _member_panel(obj, member, user, cable_colors):
    device = member.device
    current = device.pk == obj.pk
    if member.revision is not None:
        return MemberPanel(
            device, current, part=resolve_device(device, member.revision, user)
        )
    if member.schematic:
        return MemberPanel(device, current, part=resolve_schematic(device, user))
    device_view = member.device_view
    try:
        if device_view.use_svg:
            svg, details = prepare_svg(device, device_view, user, cable_colors)
            return MemberPanel(device, current, use_svg=True, svg=svg, details=details)
        css, modules, grids = prepare(device, device_view, user, cable_colors)
    except LayoutParseError as exc:
        return MemberPanel(device, current, layout_error=str(exc))
    return MemberPanel(
        device,
        current,
        css=css,
        height=device_height_px(device.device_type),
        grids=grids,
        modules=tuple(modules),
    )


def has_device_view(obj, user):
    """Return True when a member of ``obj`` that ``user`` can view has active artwork or a DeviceView that ``user`` can view."""
    return any(member.shown for member in resolve_members(obj, user))


def device_view_context(obj, user, cable_colors=False):
    """Return the render context for the device view of ``obj`` that ``user`` sees, or None without one.

    Each member resolves alone: its active artwork, else its layout, else a
    schematic. The ``layout_error`` of a member panel holds the parse error of
    a stored layout that does not parse.
    """
    members = resolve_members(obj, user)
    if not any(member.shown for member in members):
        return None
    panels = [_member_panel(obj, member, user, cable_colors) for member in members]
    uses_artwork = any(panel.part for panel in panels)
    return {
        "device_panels": panels,
        "chassis": obj.virtual_chassis_id is not None,
        "panel_title": panel_title(obj, user),
        "uses_artwork": uses_artwork,
        "uses_svg": any(panel.use_svg for panel in panels),
        "uses_css": any(panel.css is not None for panel in panels),
        "uses_layout": not all(panel.part for panel in panels),
        "artwork_legend": legend(),
    }
