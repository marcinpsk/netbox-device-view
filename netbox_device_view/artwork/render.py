"""Draw the active artwork of a device, with each connector bound to a component of that device.

Each face is parsed from its verified blob and serialized again, so no stored
bytes reach the page as they are. Every id gets the prefix of its occurrence,
``dv-<view>-d<device pk>-<face>-``, and the references are rewritten on the tree.
"""

import logging
import re
from dataclasses import dataclass

import tinycss2
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import capfirst
from lxml import etree

from ..components import BINDABLE_MODELS, ComponentState, device_components, find
from ..models import Association, DefinitionRevision
from .storage import DamagedBlob, read_blob
from .svg import SVG_NS, XLINK_NS, parse_view_box

logger = logging.getLogger(__name__)

ARTWORK = "artwork"
DAMAGED = "damaged"
FOUND = "found"
MISSING = "missing"
OVERVIEW = "o"
_MODELS = {model._meta.model_name: model for model in BINDABLE_MODELS}
_URL = re.compile(r"url\(#([^)]*)\)")
_URL_PROPERTIES = ("fill", "stroke", "clip-path")
_XLINK_HREF = f"{{{XLINK_NS}}}href"
# A generated local id starts with "-"; an author id starts with a letter.
_OCCUPIED = "-occupied"


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
        found = find(states, _MODELS[binding["component"]], binding["name"])
        # A device-scope name is unique per component type, so one match at most.
        bindings.append(Binding(FOUND, found[0]) if found else Binding(MISSING, None))
    return Connector(key, item["face"], item.get("label"), tuple(bindings))


def _parser():
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        remove_comments=True,
        remove_pis=True,
    )


def _face_svg(data: bytes, prefix: str, connectors: list[Connector]) -> SafeString:
    root = etree.fromstring(data, _parser())
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
    occupied = False
    for connector in connectors:
        if connector.target is not None:
            _link(elements[connector.key], connector.target, prefix)
            occupied = occupied or connector.target.occupied
    if occupied:
        root.insert(0, _occupied_filter(prefix, view_box))
    etree.cleanup_namespaces(root)
    # lxml escapes text and attribute values, so the markup holds no stored bytes as they are.
    return mark_safe(etree.tostring(root, encoding="unicode"))


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


def _link(element, state: ComponentState, prefix: str):
    """Wrap the connector element in a link to its component."""
    component = state.component
    link = etree.Element(f"{{{SVG_NS}}}a")
    link.set("href", component.get_absolute_url())
    link.set("class", "dv-art-connector")
    title = f"{capfirst(component._meta.verbose_name)} {component.name}"
    if state.occupied:
        link.set("class", "dv-art-connector dv-art-occupied")
        link.set("filter", f"url(#{prefix}{_OCCUPIED})")
        title += ", cable attached"
    etree.SubElement(link, f"{{{SVG_NS}}}title").text = title
    link.tail, element.tail = element.tail, None
    element.addprevious(link)
    link.append(element)


def _occupied_filter(prefix: str, view_box) -> etree._Element:
    """Return a defs element with the filter that outlines an occupied connector."""
    min_x, min_y, width, height = view_box
    defs = etree.Element(f"{{{SVG_NS}}}defs")
    region = {
        "x": f"{min_x:g}",
        "y": f"{min_y:g}",
        "width": f"{width:g}",
        "height": f"{height:g}",
    }
    outline = etree.SubElement(
        defs,
        f"{{{SVG_NS}}}filter",
        id=f"{prefix}{_OCCUPIED}",
        filterUnits="userSpaceOnUse",
        **region,
    )
    etree.SubElement(
        outline,
        f"{{{SVG_NS}}}feMorphology",
        {
            "in": "SourceAlpha",
            "operator": "dilate",
            "radius": f"{max(width, height) / 300:g}",
            "result": "grown",
        },
    )
    etree.SubElement(
        outline,
        f"{{{SVG_NS}}}feFlood",
        {"class": "dv-art-occupied-flood", "flood-color": "#2fb344"},
    )
    etree.SubElement(
        outline,
        f"{{{SVG_NS}}}feComposite",
        {"in2": "grown", "operator": "in", "result": "outline"},
    )
    merge = etree.SubElement(outline, f"{{{SVG_NS}}}feMerge")
    for source in ("outline", "SourceGraphic"):
        etree.SubElement(merge, f"{{{SVG_NS}}}feMergeNode", {"in": source})
    return defs
