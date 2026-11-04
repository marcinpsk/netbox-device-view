"""Draw the active artwork of a device, with each installed module in its bay and each connector bound to a component.

Each face is parsed from its verified blob and serialized again, so no stored
bytes reach the page as they are. Every id gets the prefix of its occurrence,
``dv-<view>-d<device pk>[-m<module pk>]...-<face>-``, where the module part
lists each module on the placement path, and the references are rewritten on
the tree.
"""

import functools
import logging
import math
import re
from dataclasses import dataclass, replace

import tinycss2
from dcim.models import Module, ModuleBay
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
    resolve_name,
)
from ..details import (
    FOUND,
    MISSING,
    SEVERAL,
    SINGLE,
    UNRESOLVED,
    Details,
    Entry,
    details_id,
    resolution,
)
from ..models import Association, DefinitionRevision
from .overrides import connector_bindings
from .placement import Rect, place, transform_rect
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
# An installed module without drawable artwork; this version draws an outline of its bay.
SCHEMATIC = "schematic"
# The warnings of a placed module.
SIZE_MISMATCH = "size_mismatch"
NOT_FINITE = "placement_not_finite"
DAMAGED_ARTWORK = "damaged_artwork"
# The view part of an id: "o" for the overview of a device, "a" for the artwork API.
OVERVIEW = "o"
API = "a"
_URL = re.compile(r"url\(#([^)]*)\)")
_URL_PROPERTIES = ("fill", "stroke", "clip-path")
_XLINK_HREF = f"{{{XLINK_NS}}}href"
_TITLE = f"{{{SVG_NS}}}title"
# XML 1.0 does not allow these characters, and lxml refuses them; NetBox names can hold them.
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_G, _A, _RECT = (f"{{{SVG_NS}}}{tag}" for tag in ("g", "a", "rect"))
_BAY_ATTRIBUTES = ("x", "y", "width", "height")
# The root attributes that place a face in a document; a placed face uses the transform of its bay.
_PLACEMENT_ATTRIBUTES = frozenset({"viewBox", "preserveAspectRatio", *_BAY_ATTRIBUTES})
# A generated local id starts with "-"; an author id starts with a letter.
_MARKS = "-marks-"
# The outline width as a part of the larger face dimension.
_OUTLINE = 1 / 300
# The outer mark of a connector: its cable status or marked connected, else several or unresolved.
_CABLE_MARKS = {
    "connected": "cable-connected",
    "planned": "cable-planned",
    "decommissioning": "cable-decommissioning",
}
MARKED = "marked"
# The inner mark of a connector.
DISABLED = "disabled"
LEGEND = (
    ("cable-connected", "Cable connected"),
    ("cable-planned", "Cable planned"),
    ("cable-decommissioning", "Cable decommissioning"),
    (MARKED, "Marked connected"),
    (DISABLED, "Disabled"),
    (SEVERAL, "Several components"),
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
    def resolution(self) -> str:
        return resolution(binding.status for binding in self.bindings)

    @property
    def target(self) -> ComponentState | None:
        """Return the component that a click opens: the only binding when it is found, else None."""
        return self.bindings[0].state if self.resolution == SINGLE else None

    def details(self, prefix, owner=None) -> Details:
        """Return the details of the connector; ``owner`` names the module that has it."""
        entries = tuple(
            Entry(
                component_label(binding.model, binding.name),
                binding.status,
                () if binding.state is None else (binding.state,),
            )
            for binding in self.bindings
        )
        title = f"Connector {self.label or self.key}"
        if owner is not None:
            title = f"{title} · {owner}"
        return Details(details_id(prefix, self.key), title, entries)


@dataclass(frozen=True)
class ArtworkWarning:
    code: str
    message: str


@dataclass(frozen=True)
class PlacedModule:
    """One installed module in a bay of its parent: drawn with its artwork, or as a schematic."""

    module: Module
    # The installed module that has the bay; None for a bay of the device.
    parent: Module | None
    bay_key: str
    # The face of the device that shows the module.
    face: str
    representation: str
    # The active revision of the module type, or None.
    revision: DefinitionRevision | None
    # The bay in the coordinates of the device face; None when it is not finite and nothing is drawn.
    bay: Rect | None
    # The rotation from the module face to the device face.
    rotate: int
    # The id of the group of the module, and the prefix of the ids of its face.
    part_id: str
    prefix: str
    connectors: tuple[Connector, ...]
    # The ids of the stored module face that the group draws, without the prefix.
    ids: frozenset[str]
    warnings: tuple[ArtworkWarning, ...]

    @property
    def label(self):
        return part_label(self.module)

    @property
    def element_id(self) -> str | None:
        return None if self.bay is None else self.part_id

    def connector_id(self, key) -> str | None:
        return self.prefix + key if key in self.ids else None


def part_label(module) -> str:
    """Return the name of an installed module, for example "ACME NM-2X in Network Module"."""
    return f"{module.module_type.full_name} in {module.module_bay.name}"


@dataclass(frozen=True)
class RenderedFace:
    name: str
    # The prefix of every id in the face.
    prefix: str
    svg: SafeString
    # The ids of the stored face that the face draws, without the prefix.
    ids: frozenset[str]
    # The details of each connector that the face draws; none for a plain face.
    details: tuple[Details, ...] = ()
    # The warnings of the modules that the face shows.
    warnings: tuple[ArtworkWarning, ...] = ()

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
    # Each installed module that a face shows, parents before their children.
    modules: tuple[PlacedModule, ...] = ()

    @property
    def damaged(self):
        return self.representation == DAMAGED


def has_active_artwork(device_type_id) -> bool:
    return Association.objects.filter(device_type=device_type_id).exists()


def active_revisions(type_ids, field="device_type") -> dict[int, DefinitionRevision]:
    """Return {type id: active revision} for the types that have an association; ``field`` is device_type or module_type."""
    # Rendering needs no permission on the association or the revision.
    return {
        getattr(association, f"{field}_id"): association.revision
        for association in Association.objects.filter(
            **{f"{field}__in": type_ids}
        ).select_related("revision__definition")
    }


def resolve_device(device, revision, user, view=OVERVIEW, plain=False) -> Part:
    """Return the artwork Part of ``device`` with the components and modules that ``user`` can view.

    The connectors bind with the overrides of each part and its type. A
    ``plain`` face has no class, links or marks.
    """
    composer = _Composer(device, user, plain)
    connectors = composer.connectors(revision, device)
    data = _read_faces(revision)
    if data is None:
        return Part(device, DAMAGED, revision, (), connectors)
    faces = tuple(
        composer.device_face(
            f"dv-{view}-d{device.pk}", face, data[face], revision, connectors
        )
        for face in revision.face_names
    )
    return Part(device, ARTWORK, revision, faces, connectors, tuple(composer.modules))


def _read_faces(revision) -> dict[str, bytes] | None:
    """Return {face: SVG bytes} of a revision, or None when a stored file is damaged."""
    blobs = {file.path: file.blob for file in revision.files.select_related("blob")}
    try:
        return {
            face: read_blob(blobs[revision.manifest["faces"][face]["file"]])
            for face in revision.face_names
        }
    except DamagedBlob as exc:
        logger.warning("Artwork of revision %s is damaged: %s", revision.pk, exc)
        return None


@dataclass(frozen=True)
class _Scope:
    """The part whose face has a bay: the device, or an installed module."""

    # None for the device.
    module: Module | None
    part_id: str
    # The face of the device that shows the part.
    face: str
    # From the face coordinates of the part to the device face.
    matrix: tuple
    rotate: int


class _Composer:
    """Draw the faces of one device, with each installed module that the user can view in its bay."""

    def __init__(self, device, user, plain):
        self.plain = plain
        self.states = device_components(device, user, BINDABLE_MODELS)
        modules = list(
            Module.objects.restrict(user, "view")
            .filter(device=device)
            .select_related("module_bay", "module_type__manufacturer")
        )
        visible_bays = set(
            ModuleBay.objects.restrict(user, "view")
            .filter(device=device)
            .values_list("pk", flat=True)
        )
        by_pk = {module.pk: module for module in modules}
        # {(module pk of the bay or None, bay name): installed module}
        self.installed = {}
        for module in modules:
            bay = module.module_bay
            if bay.pk not in visible_bays:
                continue
            # NetBox resolves {module} through these objects, so the walk makes no query.
            if bay.module_id in by_pk:
                bay.module = by_pk[bay.module_id]
            self.installed[(bay.module_id, bay.name)] = module
        self.revisions = active_revisions(
            {module.module_type_id for module in modules}, "module_type"
        )
        self.faces = {}
        self.modules = []

    def connectors(self, revision, part) -> tuple[Connector, ...]:
        """Return the connectors of ``revision`` for a device or an installed module."""
        bindings = connector_bindings(revision, part)
        module = part if isinstance(part, Module) else None
        return tuple(
            _connector(key, item, bindings[key], self.states, module)
            for key, item in sorted(revision.manifest["connectors"].items())
        )

    def device_face(self, part_id, face, data, revision, connectors) -> RenderedFace:
        prefix = f"{part_id}-{face}-"
        root, elements = _parse_face(data, prefix)
        for name in _BAY_ATTRIBUTES:
            root.attrib.pop(name, None)
        view_box = parse_view_box(root.get("viewBox").strip())
        # Module faces keep the outline width of the device face: placement does not scale.
        self.outline = max(view_box[2:]) * _OUTLINE
        first = len(self.modules)
        self.compose(
            root, elements, revision, face, _Scope(None, part_id, face, IDENTITY, 0)
        )
        placed = self.modules[first:]
        connectors = [c for c in connectors if c.face == face]
        details = ()
        if not self.plain:
            root.set("class", "dv-art-face")
            _mark_connectors(root, prefix, connectors, elements, view_box, self.outline)
            details = (
                *(c.details(prefix) for c in connectors if c.key in elements),
                *(
                    c.details(module.prefix, module.label)
                    for module in placed
                    for c in module.connectors
                    if c.key in module.ids
                ),
            )
        # lxml escapes text and attribute values, so the markup holds no stored bytes as they are.
        svg = mark_safe(etree.tostring(_unprefixed(root), encoding="unicode"))
        warnings = tuple(w for module in placed for w in module.warnings)
        return RenderedFace(face, prefix, svg, frozenset(elements), details, warnings)

    def compose(self, root, elements, revision, face, scope):
        """Draw the installed module of each bay of ``revision`` on ``face`` into ``root``."""
        for key, item in sorted(revision.manifest["bays"].items()):
            # A bay in title or desc is not drawn.
            if item["face"] != face or key not in elements:
                continue
            name = item["module_bay"]
            if scope.module is not None:
                name = resolve_name(ModuleBay, name, scope.module)
            owner = None if scope.module is None else scope.module.pk
            module = self.installed.get((owner, name))
            # An empty bay draws nothing more: the parent artwork shows the slot.
            if module is not None:
                self.place(elements[key], key, item["rotate"], module, scope)

    def place(self, rect, key, rotate, module, scope):
        """Draw ``module`` over the bay ``rect`` of ``scope``, and its own bays inside it."""
        revision = self.revisions.get(module.module_type_id)
        inner = _Scope(
            module,
            f"{scope.part_id}-m{module.pk}",
            scope.face,
            scope.matrix,
            (scope.rotate + rotate) % 360,
        )
        connectors = () if revision is None else self.connectors(revision, module)
        placed = functools.partial(
            PlacedModule,
            module=module,
            parent=scope.module,
            bay_key=key,
            face=scope.face,
            revision=revision,
            rotate=inner.rotate,
            part_id=inner.part_id,
            connectors=connectors,
        )
        label = part_label(module)
        bay = Rect(*(float(rect.get(name, "0")) for name in _BAY_ATTRIBUTES))
        if not all(map(math.isfinite, vars(bay).values())):
            message = (
                f"{label}: the bay {key} has a position or size that is not a finite"
                " number, so the module is not drawn."
            )
            self.modules.append(
                placed(
                    representation=SCHEMATIC,
                    bay=None,
                    prefix="",
                    ids=frozenset(),
                    warnings=(ArtworkWarning(NOT_FINITE, message),),
                )
            )
            return
        group = etree.Element(_G, id=inner.part_id)
        clip = f"{inner.part_id}--clip"
        group.set("clip-path", f"url(#{clip})")
        etree.SubElement(
            etree.SubElement(group, f"{{{SVG_NS}}}clipPath", id=clip),
            _RECT,
            _rect_attributes(bay),
        )
        rect.addnext(group)
        absolute = transform_rect(scope.matrix, bay)
        data = None if revision is None else self.face_data(revision)
        if data is None:
            warnings = ()
            if revision is not None:
                message = (
                    f"{label}: a stored file of the active {revision.definition.key}"
                    " revision is missing or does not match its digest. Import the"
                    " revision again."
                )
                warnings = (ArtworkWarning(DAMAGED_ARTWORK, message),)
            self.modules.append(
                placed(
                    representation=SCHEMATIC,
                    bay=absolute,
                    prefix="",
                    ids=frozenset(),
                    warnings=warnings,
                )
            )
            self.outline_part(group, bay, module)
            return
        [face] = revision.face_names
        prefix = f"{inner.part_id}-{face}-"
        child, elements = _parse_face(data[face], prefix)
        view_box = parse_view_box(child.get("viewBox").strip())
        placement = place(view_box, bay, rotate)
        warnings = ()
        if placement.size_mismatch:
            message = (
                f"{label}: the artwork is {_number(placement.size[0])} ×"
                f" {_number(placement.size[1])} mm, but the bay {key} is"
                f" {_number(bay.width)} × {_number(bay.height)} mm."
            )
            warnings = (ArtworkWarning(SIZE_MISMATCH, message),)
        self.modules.append(
            placed(
                representation=ARTWORK,
                bay=absolute,
                prefix=prefix,
                ids=frozenset(elements),
                warnings=warnings,
            )
        )
        self.compose(
            child,
            elements,
            revision,
            face,
            replace(inner, matrix=multiply(scope.matrix, placement.matrix)),
        )
        if not self.plain:
            drawn = [c for c in connectors if c.face == face]
            _mark_connectors(child, prefix, drawn, elements, view_box, self.outline)
            body = self.body(group, module, "dv-art-module-body")
            etree.SubElement(body, _RECT, {**_rect_attributes(bay), "fill": "none"})
        art = etree.SubElement(
            group,
            _G,
            {k: v for k, v in child.attrib.items() if k not in _PLACEMENT_ATTRIBUTES},
        )
        art.set("transform", f"matrix({' '.join(map(_number, placement.matrix))})")
        if not self.plain:
            art.set("class", "dv-art-module-art")
        art.text = child.text
        art.extend(child)

    def face_data(self, revision) -> dict[str, bytes] | None:
        if revision.pk not in self.faces:
            self.faces[revision.pk] = _read_faces(revision)
        return self.faces[revision.pk]

    def body(self, group, module, classes):
        """Return the link of a module body to its record; a plain face has a group instead."""
        if self.plain:
            return etree.SubElement(group, _G)
        body = etree.SubElement(
            group, _A, {"class": classes, "href": module.get_absolute_url()}
        )
        etree.SubElement(body, f"{{{SVG_NS}}}title").text = (
            f"Module {part_label(module)}"
        )
        return body

    def outline_part(self, group, bay, module):
        """Draw the module as an outline of its bay with the name of its type."""
        body = self.body(group, module, "dv-art-module-body dv-art-schematic")
        etree.SubElement(
            body,
            _RECT,
            {
                **_rect_attributes(bay),
                "fill": "#f8f9fa",
                "stroke": "#6c757d",
                "stroke-width": _number(self.outline),
            },
        )
        name = module.module_type.model
        # The label is not turned with the bay, so it stays readable.
        size = min(bay.height * 0.4, bay.width * 0.9 / (0.6 * max(len(name), 1)))
        text = etree.SubElement(
            body,
            f"{{{SVG_NS}}}text",
            {
                "x": _number(bay.x + bay.width / 2),
                "y": _number(bay.y + bay.height / 2),
                "font-size": _number(size),
                "font-family": "sans-serif",
                "text-anchor": "middle",
                "dominant-baseline": "central",
                "fill": "#212529",
            },
        )
        text.text = name


def _number(value) -> str:
    # Adding 0 turns -0.0 into 0.0.
    return f"{value + 0:.12g}"


def _rect_attributes(rect: Rect) -> dict[str, str]:
    return {name: _number(getattr(rect, name)) for name in _BAY_ATTRIBUTES}


def _connector(key, item, resolved, states, module=None) -> Connector:
    """Return the connector of manifest ``item`` with the ``resolved`` binding list, in the scope of ``module``."""
    bindings = []
    for binding in resolved:
        model = BINDING_MODELS[binding["component"]]
        found = find(states, model, binding["name"], module)
        # A component name is unique per type on one device, so one match at most.
        bindings.append(
            Binding(model, binding["name"], FOUND, found[0])
            if found
            else Binding(model, binding["name"], MISSING, None)
        )
    return Connector(key, item["face"], item.get("label"), tuple(bindings))


def _parse_face(data: bytes, prefix: str):
    """Return the parsed face with the prefix on each id and reference, and {stored id: element} of what it draws."""
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
    return root, elements


def _mark_connectors(root, prefix, connectors, elements, view_box, outline):
    """Mark each connector with its facts, and link it to its component when it has one."""
    defs = etree.Element(f"{{{SVG_NS}}}defs")
    for connector in connectors:
        # A connector in title or desc is gone: a browser does not draw it either.
        element = elements.get(connector.key)
        if element is None:
            continue
        outer, inner = _marks(connector)
        marks = None
        if outer or inner:
            marks = _mark_filter(
                f"{prefix}{_MARKS}{connector.key}",
                view_box,
                _user_space(element),
                outer,
                inner,
                outline,
            )
        if marks is not None:
            defs.append(marks)
        _wrap(element, connector, prefix, outer, inner, marks)
    if len(defs):
        root.insert(0, defs)


def _marks(connector) -> tuple[str | None, bool]:
    """Return the outer mark of a connector and whether it has the inner disabled mark."""
    target = connector.target
    if target is None:
        return connector.resolution, False
    if target.cable_status is not None:
        outer = _CABLE_MARKS[target.cable_status]
    elif target.marked_connected:
        outer = MARKED
    else:
        outer = None
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


def _wrap(element, connector, prefix, outer, inner, marks):
    """Wrap the connector element in a link to its component, or in a button that opens its details."""
    target = connector.target
    if target is None:
        wrapper = etree.Element(
            f"{{{SVG_NS}}}g", tabindex="0", role="button", **{"aria-haspopup": "dialog"}
        )
    else:
        wrapper = etree.Element(f"{{{SVG_NS}}}a")
        wrapper.set("href", target.component.get_absolute_url())
    classes = ["dv-art-connector"]
    if outer:
        classes.append(f"dv-art-{outer}")
    if inner:
        classes.append(f"dv-art-{DISABLED}")
    wrapper.set("class", " ".join(classes))
    wrapper.set("data-dv-details", details_id(prefix, connector.key))
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


def _title(connector) -> str:
    """Return each fact of the connector as text, so no fact depends on colour alone."""
    target = connector.target
    if target is None:
        if connector.resolution == SEVERAL:
            names = ", ".join(
                component_label(b.model, b.name) for b in connector.bindings
            )
            return f"Several components: {names}"
        names = ", ".join(
            component_label(b.model, b.name)
            for b in connector.bindings
            if b.status == MISSING
        )
        return f"No component: {names}"
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
    return _NOT_XML.sub("", value)


def _user_space(element):
    """Return the matrix from the user space of the element's parent to face coordinates."""
    matrix = IDENTITY
    for ancestor in reversed(list(element.iterancestors())):
        value = ancestor.get("transform")
        if value is not None:
            matrix = multiply(matrix, transform_matrix(value))
    return matrix


def _mark_filter(
    ident: str, view_box, matrix, outer, inner, outline
) -> etree._Element | None:
    """Return the filter that marks a connector, in the user space that ``matrix`` maps to the face.

    ``outline`` is the width of the outline in face units. None when the matrix
    cannot be inverted: the connector is not drawn.
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
    radii = (outline / math.hypot(a, b), outline / math.hypot(c, d))
    if not all(math.isfinite(value) for value in (*region, *radii)):
        return None
    return _MarkFilter(ident, region, radii).draw(outer, inner)


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
        if mark == SEVERAL:
            return far
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
