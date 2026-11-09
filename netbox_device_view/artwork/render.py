"""Draw the active artwork of a device, with each connector bound to a component of that device.

Each face is parsed from its verified blob and serialized again, so no stored
bytes reach the page as they are. Every id gets the prefix of its occurrence,
``dv-<view>-d<device pk>-<face>-``, and the references are rewritten on the tree.
"""

import functools
import logging
import math
import re
from copy import deepcopy
from dataclasses import dataclass

import tinycss2
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import capfirst
from lxml import etree

from ..components import (
    BINDABLE_MODELS,
    BINDING_MODELS,
    ComponentState,
    component_label,
    device_components,
    find,
)
from ..models import Association, DefinitionRevision
from .storage import DamagedBlob, read_blob
from .svg import (
    IDENTITY,
    SVG_NS,
    XLINK_NS,
    multiply,
    parse_view_box,
    safe_parser,
    transform_matrix,
)

logger = logging.getLogger(__name__)

ARTWORK = "artwork"
DAMAGED = "damaged"
FOUND = "found"
MISSING = "missing"
# The view part of an id: "o" for the overview of a device, "a" for the artwork API.
OVERVIEW = "o"
API = "a"
_URL = re.compile(r"url\(#([^)]*)\)")
_URL_PROPERTIES = ("fill", "stroke", "clip-path")
_XLINK_HREF = f"{{{XLINK_NS}}}href"
_TITLE = f"{{{SVG_NS}}}title"
_USE = f"{{{SVG_NS}}}use"
# XML 1.0 does not allow these characters, and lxml refuses them; NetBox names can hold them.
_NOT_XML = dict.fromkeys(
    [*range(0x9), 0xB, 0xC, *range(0xE, 0x20), *range(0xD800, 0xE000), 0xFFFE, 0xFFFF]
)
# A generated local id starts with "-"; an author id starts with a letter.
_MARKS = "-marks-"
_FOCUS = "-focus-"
_SHAPE = "-shape-"
# The properties that can hide paint; the focus ring copy drops them and paints black.
_PAINT = frozenset(
    {"fill", "stroke", "opacity", "fill-opacity", "stroke-opacity", "visibility"}
)
# The outline width as a part of the larger face dimension.
_OUTLINE = 1 / 300
# The outer mark of a connector: its cable status, else marked connected, else unresolved.
_CABLE_MARKS = {
    "connected": "cable-connected",
    "planned": "cable-planned",
    "decommissioning": "cable-decommissioning",
}
MARKED = "marked"
UNRESOLVED = "unresolved"
# The inner mark of a connector.
DISABLED = "disabled"
# The ring of a connector that has the keyboard focus.
FOCUS_RING = "focus"
LEGEND = (
    ("cable-connected", "Cable connected"),
    ("cable-planned", "Cable planned"),
    ("cable-decommissioning", "Cable decommissioning"),
    (MARKED, "Marked connected"),
    (DISABLED, "Disabled"),
    (UNRESOLVED, "No component found"),
)


@dataclass(frozen=True)
class Binding:
    # The component model and name that the manifest binds.
    model: type
    name: str
    status: str
    # None for a missing binding.
    state: ComponentState | None


@dataclass(frozen=True)
class Connector:
    key: str
    face: str
    label: str | None
    bindings: tuple[Binding, ...]

    @property
    def target(self) -> ComponentState | None:
        """Return the component that a click opens: the one found binding, else None."""
        found = [binding.state for binding in self.bindings if binding.status == FOUND]
        return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class RenderedFace:
    name: str
    # The prefix of every id in the face.
    prefix: str
    svg: SafeString
    # The ids of the stored face that the face draws, without the prefix.
    ids: frozenset[str]

    @property
    def label(self):
        return capfirst(self.name)

    def element_id(self, key) -> str | None:
        """Return the id of the element ``key`` in the face, or None when the face does not draw it."""
        return self.prefix + key if key in self.ids else None


@dataclass(frozen=True)
class Part:
    """The artwork of one device: its faces in physical order, or none when a blob is damaged."""

    device: object
    representation: str
    revision: DefinitionRevision
    faces: tuple[RenderedFace, ...]
    connectors: tuple[Connector, ...]

    @property
    def damaged(self):
        return self.representation == DAMAGED


def has_active_artwork(device_type_id) -> bool:
    return Association.objects.filter(device_type=device_type_id).exists()


def active_revisions(device_type_ids) -> dict[int, DefinitionRevision]:
    """Return {device type id: active revision} for the device types that have an association."""
    # Rendering needs no permission on the association or the revision.
    return {
        association.device_type_id: association.revision
        for association in Association.objects.filter(
            device_type__in=device_type_ids
        ).select_related("revision__definition")
    }


def face_prefix(view, device, face) -> str:
    """Return the prefix of every id in one face of one device."""
    return f"dv-{view}-d{device.pk}-{face}-"


def resolve_device(device, revision, user, view=OVERVIEW, plain=False) -> Part:
    """Return the artwork Part of ``device`` with the components that ``user`` can view.

    A ``plain`` face has no class, connector links or marks.
    """
    states = device_components(device, user, BINDABLE_MODELS)
    connectors = tuple(
        _connector(key, item, states)
        for key, item in sorted(revision.manifest["connectors"].items())
    )
    blobs = {file.path: file.blob for file in revision.files.select_related("blob")}
    try:
        data = {
            face: read_blob(blobs[revision.manifest["faces"][face]["file"]])
            for face in revision.face_names
        }
    except DamagedBlob as exc:
        logger.warning("Artwork of revision %s is damaged: %s", revision.pk, exc)
        return Part(device, DAMAGED, revision, (), connectors)
    faces = []
    for face in revision.face_names:
        prefix = face_prefix(view, device, face)
        svg, ids = _face_svg(
            data[face],
            prefix,
            [connector for connector in connectors if connector.face == face],
            plain,
        )
        faces.append(RenderedFace(face, prefix, svg, ids))
    return Part(device, ARTWORK, revision, tuple(faces), connectors)


def _connector(key, item, states) -> Connector:
    bindings = []
    for binding in item["bindings"]:
        model = BINDING_MODELS[binding["component"]]
        found = find(states, model, binding["name"])
        # A device-scope name is unique per component type, so one match at most.
        bindings.append(
            Binding(model, binding["name"], FOUND, found[0])
            if found
            else Binding(model, binding["name"], MISSING, None)
        )
    return Connector(key, item["face"], item.get("label"), tuple(bindings))


def _face_svg(
    data: bytes, prefix: str, connectors: list[Connector], plain: bool
) -> tuple[SafeString, frozenset[str]]:
    """Return the face markup and the ids of the stored face that it draws."""
    root = etree.fromstring(data, safe_parser(remove_comments=True, remove_pis=True))
    # HTML parses the content of title and desc as HTML, so they keep only their text.
    for element in root.iter(f"{{{SVG_NS}}}title", f"{{{SVG_NS}}}desc"):
        text = "".join(element.itertext())
        element[:] = []
        element.text = _xml_text(text)
    elements = {}
    for element in root.iter(etree.Element):
        ident = element.get("id")
        if ident is not None:
            elements[ident] = element
            element.set("id", prefix + ident)
        _rewrite_references(element, prefix)
    for name in ("x", "y", "width", "height"):
        root.attrib.pop(name, None)
    if not plain:
        _mark_connectors(root, prefix, connectors, elements)
    # lxml escapes text and attribute values, so the markup holds no stored bytes as they are.
    svg = mark_safe(etree.tostring(_unprefixed(root), encoding="unicode"))
    return svg, frozenset(elements)


def _mark_connectors(root, prefix, connectors, elements):
    """Mark each connector with its facts, and link it to its component when it has one."""
    view_box = parse_view_box(root.get("viewBox").strip())
    root.set("class", "dv-art-face")
    defs = etree.Element(f"{{{SVG_NS}}}defs")
    for connector in connectors:
        # A connector in title or desc is gone: a browser does not draw it either.
        element = elements.get(connector.key)
        if element is None:
            continue
        outer, inner = _marks(connector)
        marks = ring = None
        space = _filter_space(view_box, _user_space(element))
        if space is not None:
            if outer or inner:
                marks = _MarkFilter(f"{prefix}{_MARKS}{connector.key}", *space).draw(
                    outer, inner
                )
                defs.append(marks)
            if connector.target is not None:
                ring = _MarkFilter(
                    f"{prefix}{_FOCUS}{connector.key}", *space
                ).focus_ring()
                defs.append(ring)
        _wrap(element, connector, outer, inner, marks, ring, prefix)
    if len(defs):
        root.insert(0, defs)


def _marks(connector) -> tuple[str | None, bool]:
    """Return the outer mark of a connector and whether it has the inner disabled mark."""
    target = connector.target
    if target is None:
        return UNRESOLVED, False
    # A cable status that NetBox configuration adds has no mark.
    outer = _CABLE_MARKS.get(target.cable_status)
    if outer is None and target.marked_connected:
        outer = MARKED
    return outer, target.enabled is False


def _unprefixed(element, parent=None):
    """Return a copy in the default SVG namespace; HTML reads a prefixed name as an unknown element."""
    if parent is None:
        copy = etree.Element(element.tag, nsmap={None: SVG_NS})
    else:
        copy = etree.SubElement(parent, element.tag)
    copy.attrib.update(element.attrib)
    copy.text, copy.tail = element.text, element.tail
    for child in element:
        _unprefixed(child, copy)
    return copy


def _rewrite_references(element, prefix: str):
    href = element.attrib.pop(_XLINK_HREF, None) or element.get("href")
    if href is not None:
        element.set("href", f"#{prefix}{href[1:]}")
    for name in _URL_PROPERTIES:
        value = element.get(name)
        if value is not None:
            element.set(name, _URL.sub(rf"url(#{prefix}\1)", value))
    style = element.get("style")
    if style is not None:
        element.set("style", _rewrite_style(style, prefix))


def _rewrite_style(style: str, prefix: str) -> str:
    declarations = tinycss2.parse_declaration_list(
        style, skip_comments=True, skip_whitespace=True
    )
    return ";".join(
        declaration.lower_name
        + ":"
        + "".join(_style_token(token, prefix) for token in declaration.value)
        for declaration in declarations
    )


def _style_token(token, prefix: str) -> str:
    # The profile accepts only url(#id) as a URL in a style.
    if token.type == "url":
        return f"url(#{prefix}{token.value[1:]})"
    return tinycss2.serialize([token])


def _wrap(element, connector, outer, inner, marks, ring, prefix):
    """Wrap the connector element in a link to its component, or in a group when it has none."""
    target = connector.target
    if target is None:
        wrapper = etree.Element(f"{{{SVG_NS}}}g")
        wrapper.set("class", "dv-art-unresolved")
    else:
        wrapper = etree.Element(f"{{{SVG_NS}}}a")
        wrapper.set("href", target.component.get_absolute_url())
        classes = ["dv-art-connector"]
        if outer:
            classes.append(f"dv-art-{outer}")
        if inner:
            classes.append(f"dv-art-{DISABLED}")
        wrapper.set("class", " ".join(classes))
    if marks is not None:
        wrapper.set("filter", f"url(#{marks.get('id')})")
    title = _title(connector)
    # A browser shows the title nearest to the hovered element, so each title names the facts.
    for own in element.iter(_TITLE):
        own.text = _xml_text(f"{own.text} · {title}" if own.text else title)
    etree.SubElement(wrapper, _TITLE).text = _xml_text(title)
    wrapper.tail, element.tail = element.tail, None
    element.addprevious(wrapper)
    wrapper.append(element)
    if ring is not None:
        # WebKit paints no outline on an SVG element, so the page CSS shows this ring on focus.
        etree.SubElement(
            wrapper,
            f"{{{SVG_NS}}}g",
            {
                "class": "dv-art-focus-ring",
                "filter": f"url(#{ring.get('id')})",
                "fill": "#000",
                "stroke": "#000",
                "fill-opacity": "1",
                "stroke-opacity": "1",
                "visibility": "visible",
                "aria-hidden": "true",
            },
        ).append(_shape(element, ring.getparent(), prefix))


def _shape(element, defs, prefix):
    """Return a copy of the connector without ids, titles and paint; the ring draws its shape."""
    shape = deepcopy(element)
    shape.tail = None
    for text in list(shape.iterdescendants(_TITLE, f"{{{SVG_NS}}}desc")):
        _remove_keeping_tail(text)
    for node in shape.iter(etree.Element):
        for name in ("id", *_PAINT):
            node.attrib.pop(name, None)
        style = node.get("style")
        if style is not None:
            declarations = tinycss2.parse_declaration_list(
                style, skip_comments=True, skip_whitespace=True
            )
            kept = ";".join(
                f"{declaration.lower_name}:{tinycss2.serialize(declaration.value)}"
                for declaration in declarations
                if declaration.lower_name not in _PAINT
            )
            if kept:
                node.set("style", kept)
            else:
                del node.attrib["style"]
    # A use draws with the paint of its target, so the copy refers to a target without paint.
    root = element.getroottree().getroot()
    for use in shape.iter(_USE):
        href = use.get("href")
        if href is not None:
            use.set("href", f"#{_paintless(root, href[1:], defs, prefix)}")
    return shape


def _remove_keeping_tail(node) -> None:
    """Remove ``node``; lxml removes its tail too, and the tail is text that the parent draws."""
    parent, previous = node.getparent(), node.getprevious()
    if node.tail:
        if previous is not None:
            previous.tail = _xml_text((previous.tail or "") + node.tail)
        else:
            parent.text = _xml_text((parent.text or "") + node.tail)
    parent.remove(node)


def _paintless(root, ident: str, defs, prefix: str) -> str:
    """Return the id of the copy without paint of element ``ident`` in ``defs``; it makes one copy per element."""
    copy_id = f"{prefix}{_SHAPE}{ident.removeprefix(prefix)}"
    if not defs.xpath("*[@id=$ident]", ident=copy_id):
        [target] = root.xpath("//*[@id=$ident]", ident=ident)
        copy = _shape(target, defs, prefix)
        copy.set("id", copy_id)
        defs.append(copy)
    return copy_id


def _title(connector) -> str:
    """Return each fact of the connector as text, so no fact depends on colour alone."""
    target = connector.target
    if target is None:
        found = [b for b in connector.bindings if b.status == FOUND]
        names = ", ".join(
            component_label(b.model, b.name) for b in found or connector.bindings
        )
        return f"Two or more components: {names}" if found else f"No component: {names}"
    facts = []
    if target.enabled is not None:
        facts.append("enabled" if target.enabled else "disabled")
    facts.append(f"cable {target.cable_status}" if target.cable_status else "no cable")
    if target.marked_connected:
        facts.append("marked connected")
    if target.path_complete is not None:
        facts.append("path complete" if target.path_complete else "path incomplete")
    if target.peer_labels:
        peers = "peer" if len(target.peer_labels) == 1 else "peers"
        facts.append(f"{peers} {', '.join(target.peer_labels)}")
    component = target.component
    return f"{component_label(type(component), component.name)}: {', '.join(facts)}"


def _xml_text(value: str) -> str:
    """Return ``value`` without the characters that XML 1.0 does not allow."""
    return value.translate(_NOT_XML)


def _user_space(element):
    """Return the matrix from the user space of the element's parent to face coordinates."""
    matrix = IDENTITY
    for ancestor in reversed(list(element.iterancestors())):
        value = ancestor.get("transform")
        if value is not None:
            matrix = multiply(matrix, transform_matrix(value))
    return matrix


def _filter_space(view_box, matrix):
    """Return (region, radii) of a filter in the user space that ``matrix`` maps to the face.

    The region covers the face, and the radii are the outline width on each
    local axis. None when the matrix cannot be inverted: the connector is not drawn.
    """
    a, b, c, d, e, f = matrix
    determinant = a * d - b * c
    if not math.isfinite(determinant) or determinant == 0:
        return None
    min_x, min_y, width, height = view_box
    # The face corners in the local user space.
    corners = [
        (
            (d * (x - e) - c * (y - f)) / determinant,
            (a * (y - f) - b * (x - e)) / determinant,
        )
        for x in (min_x, min_x + width)
        for y in (min_y, min_y + height)
    ]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    region = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    # Each local axis keeps the face outline width: divide by the face length of its unit vector.
    width_on_face = max(width, height) * _OUTLINE
    radii = (width_on_face / math.hypot(a, b), width_on_face / math.hypot(c, d))
    if not all(math.isfinite(value) for value in (*region, *radii)):
        return None
    return region, radii


class _MarkFilter:
    """Build the filter primitives of the marks; each length is a multiple of the outline width."""

    def __init__(self, ident, region, radii):
        self.region, self.radii = region, radii
        self.element = etree.Element(
            f"{{{SVG_NS}}}filter",
            id=ident,
            filterUnits="userSpaceOnUse",
            **_box(region),
        )
        self.results = 0

    def draw(self, outer, inner) -> etree._Element:
        layers = []
        if outer is not None:
            layers.append(self.paint(self.outer_ring(outer), outer))
        layers.append("SourceGraphic")
        if inner:
            band = self.cut("SourceAlpha", self.grow(1, "erode"))
            layers.append(self.paint(self.keep(band, self.checker(1.5)), DISABLED))
        merge = etree.SubElement(self.element, f"{{{SVG_NS}}}feMerge")
        for layer in layers:
            etree.SubElement(merge, f"{{{SVG_NS}}}feMergeNode", {"in": layer})
        return self.element

    def focus_ring(self) -> etree._Element:
        """Draw only the focus ring around the source, without the source."""
        self.paint(self.cut(self.grow(1), "SourceAlpha"), FOCUS_RING)
        return self.element

    def outer_ring(self, mark) -> str:
        edge = self.cut(self.grow(1), "SourceAlpha")
        if mark == "cable-connected":
            return edge
        if mark == "cable-planned":
            return self.keep(edge, self.checker(2))
        if mark == "cable-decommissioning":
            return self.keep(edge, self.checker(1))
        far = self.cut(self.grow(2.25), self.grow(1.5))
        if mark == MARKED:
            near = self.cut(self.grow(0.75), "SourceAlpha")
            return self.add("feComposite", {"in": near, "in2": far, "operator": "over"})
        if mark == UNRESOLVED:
            return self.keep(far, self.checker(2))
        raise ValueError(f"unknown mark {mark!r}")

    def add(self, tag, attributes) -> str:
        self.results += 1
        result = f"m{self.results}"
        etree.SubElement(
            self.element, f"{{{SVG_NS}}}{tag}", {**attributes, "result": result}
        )
        return result

    def grow(self, times, operator="dilate") -> str:
        rx, ry = (radius * times for radius in self.radii)
        return self.add(
            "feMorphology",
            {"in": "SourceAlpha", "operator": operator, "radius": f"{rx:g} {ry:g}"},
        )

    def cut(self, source, hole) -> str:
        return self.add("feComposite", {"in": source, "in2": hole, "operator": "out"})

    def keep(self, source, mask) -> str:
        return self.add("feComposite", {"in": source, "in2": mask, "operator": "in"})

    def checker(self, times) -> str:
        """Return a checkerboard of squares ``times`` outline widths wide; a ring through it is dashed."""
        x, y = self.region[:2]
        width, height = (radius * times for radius in self.radii)
        squares = [
            self.add(
                "feFlood",
                {"flood-color": "#000", **_box((sx, sy, width, height))},
            )
            for sx, sy in ((x, y), (x + width, y + height))
        ]
        tile = self.add(
            "feComposite",
            {
                "in": squares[0],
                "in2": squares[1],
                "operator": "over",
                **_box((x, y, 2 * width, 2 * height)),
            },
        )
        return self.add("feTile", {"in": tile})

    def paint(self, mask, mark) -> str:
        # The page CSS gives each mark the colour of the NetBox theme.
        flood = self.add("feFlood", {"class": f"dv-art-flood-{mark}"})
        return self.keep(flood, mask)


def _box(values) -> dict[str, str]:
    """Return the x, y, width and height attributes of a filter or a primitive."""
    return dict(
        zip(("x", "y", "width", "height"), (f"{v:g}" for v in values), strict=True)
    )


@functools.cache
def legend() -> tuple[tuple[str, SafeString], ...]:
    """Return (label, swatch SVG) for each mark, drawn with the filters of the faces."""
    items = []
    for mark, label in LEGEND:
        outer, inner = (None, True) if mark == DISABLED else (mark, False)
        marks = _MarkFilter(f"dv-legend-{mark}", (0, 0, 32, 20), (1.5, 1.5)).draw(
            outer, inner
        )
        svg = etree.Element(
            f"{{{SVG_NS}}}svg",
            {"class": "dv-art-swatch", "viewBox": "0 0 32 20", "aria-hidden": "true"},
            nsmap={None: SVG_NS},
        )
        etree.SubElement(svg, f"{{{SVG_NS}}}defs").append(marks)
        etree.SubElement(
            svg,
            f"{{{SVG_NS}}}rect",
            {
                "x": "8",
                "y": "5",
                "width": "16",
                "height": "10",
                "fill": "#6c757d",
                "filter": f"url(#dv-legend-{mark})",
            },
        )
        items.append((label, mark_safe(etree.tostring(svg, encoding="unicode"))))
    return tuple(items)
