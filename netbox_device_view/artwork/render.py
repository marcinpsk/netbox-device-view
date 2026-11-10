"""Draw the active artwork of a device, with each connector bound to a component of that device.

Each face is parsed from its verified blob and serialized again, so no stored
bytes reach the page as they are. Every id gets the prefix of its occurrence,
``dv-<view>-d<device pk>-<face>-``, and the references are rewritten on the tree.
"""

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
# The view part of an id: "o" for the overview of a device.
OVERVIEW = "o"
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
_OCCUPIED = "-occupied-"
_FOCUS = "-focus-"
_SHAPE = "-shape-"
# The properties that can hide paint; the focus ring copy drops them and paints black.
_PAINT = frozenset(
    {"fill", "stroke", "opacity", "fill-opacity", "stroke-opacity", "visibility"}
)
# The outline width as a part of the larger face dimension.
_OUTLINE = 1 / 300


@dataclass(frozen=True)
class Binding:
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
    svg: SafeString

    @property
    def label(self):
        return capfirst(self.name)


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


def resolve_device(device, revision, user, view=OVERVIEW) -> Part:
    """Return the artwork Part of ``device`` with the components that ``user`` can view."""
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
    faces = tuple(
        RenderedFace(
            face,
            _face_svg(
                data[face],
                f"dv-{view}-d{device.pk}-{face}-",
                [connector for connector in connectors if connector.face == face],
            ),
        )
        for face in revision.face_names
    )
    return Part(device, ARTWORK, revision, faces, connectors)


def _connector(key, item, states) -> Connector:
    bindings = []
    for binding in item["bindings"]:
        found = find(states, BINDING_MODELS[binding["component"]], binding["name"])
        # A device-scope name is unique per component type, so one match at most.
        bindings.append(Binding(FOUND, found[0]) if found else Binding(MISSING, None))
    return Connector(key, item["face"], item.get("label"), tuple(bindings))


def _face_svg(data: bytes, prefix: str, connectors: list[Connector]) -> SafeString:
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
    view_box = parse_view_box(root.get("viewBox").strip())
    for name in ("x", "y", "width", "height"):
        root.attrib.pop(name, None)
    root.set("class", "dv-art-face")
    defs = etree.Element(f"{{{SVG_NS}}}defs")
    for connector in connectors:
        # A connector in title or desc is gone: a browser does not draw it either.
        element = elements.get(connector.key)
        if connector.target is None or element is None:
            continue
        outline = ring = None
        space = _filter_space(view_box, _user_space(element))
        if space is not None:
            if connector.target.occupied:
                outline = _outline(f"{prefix}{_OCCUPIED}{connector.key}", *space)
                defs.append(outline)
            ring = _focus_ring(f"{prefix}{_FOCUS}{connector.key}", *space)
            defs.append(ring)
        _link(element, connector.target, outline, ring, prefix)
    if len(defs):
        root.insert(0, defs)
    # lxml escapes text and attribute values, so the markup holds no stored bytes as they are.
    return mark_safe(etree.tostring(_unprefixed(root), encoding="unicode"))


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


def _link(element, state: ComponentState, outline, ring, prefix):
    """Wrap the connector element in a link to its component."""
    component = state.component
    link = etree.Element(f"{{{SVG_NS}}}a")
    link.set("href", component.get_absolute_url())
    link.set("class", "dv-art-connector")
    title = f"{capfirst(component._meta.verbose_name)} {component.name}"
    if state.occupied:
        link.set("class", "dv-art-connector dv-art-occupied")
        title += ", cable attached"
    if outline is not None:
        link.set("filter", f"url(#{outline.get('id')})")
    # A browser shows the title nearest to the hovered element, so each title names the component.
    for own in element.iter(_TITLE):
        own.text = _xml_text(f"{own.text} · {title}" if own.text else title)
    etree.SubElement(link, _TITLE).text = _xml_text(title)
    link.tail, element.tail = element.tail, None
    element.addprevious(link)
    link.append(element)
    if ring is not None:
        # WebKit paints no outline on an SVG element, so the page CSS shows this ring on focus.
        etree.SubElement(
            link,
            f"{{{SVG_NS}}}g",
            {
                "class": "dv-art-focus-ring",
                "filter": f"url(#{ring.get('id')})",
                "fill": "#000",
                "stroke": "#000",
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
            previous.tail = (previous.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
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


def _grown(ident: str, region, radii, flood_class, flood_color) -> etree._Element:
    """Return a filter whose result "outline" is the source grown by one outline width, in the flood colour."""
    element = etree.Element(
        f"{{{SVG_NS}}}filter",
        id=ident,
        filterUnits="userSpaceOnUse",
        **dict(
            zip(("x", "y", "width", "height"), (f"{v:g}" for v in region), strict=True)
        ),
    )
    etree.SubElement(
        element,
        f"{{{SVG_NS}}}feMorphology",
        {
            "in": "SourceAlpha",
            "operator": "dilate",
            "radius": f"{radii[0]:g} {radii[1]:g}",
            "result": "grown",
        },
    )
    # The page CSS gives the flood the colour of the theme.
    etree.SubElement(
        element,
        f"{{{SVG_NS}}}feFlood",
        {"class": flood_class, "flood-color": flood_color},
    )
    etree.SubElement(
        element,
        f"{{{SVG_NS}}}feComposite",
        {"in2": "grown", "operator": "in", "result": "outline"},
    )
    return element


def _outline(ident: str, region, radii) -> etree._Element:
    """Return the filter that draws a connector with its occupied outline."""
    outline = _grown(ident, region, radii, "dv-art-occupied-flood", "#2fb344")
    merge = etree.SubElement(outline, f"{{{SVG_NS}}}feMerge")
    for source in ("outline", "SourceGraphic"):
        etree.SubElement(merge, f"{{{SVG_NS}}}feMergeNode", {"in": source})
    return outline


def _focus_ring(ident: str, region, radii) -> etree._Element:
    """Return the filter that draws only the ring around a connector, without the connector."""
    ring = _grown(ident, region, radii, "dv-art-focus-flood", "#0d6efd")
    etree.SubElement(
        ring,
        f"{{{SVG_NS}}}feComposite",
        {"in": "outline", "in2": "SourceAlpha", "operator": "out"},
    )
    return ring
