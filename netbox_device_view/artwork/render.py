"""Draw the active artwork of a device, with each installed module in its bay and each connector bound to a component.

Each face is parsed from its verified blob and serialized again, so no stored
bytes reach the page as they are. Every id gets the prefix of its occurrence,
``dv-<view>-d<device pk>[-m<module pk>]...-<face>-``, where the module part
lists each module on the placement path, and the references are rewritten on
the tree. A module without drawable artwork shows as a generated schematic of
its components and module bays, and a bay without a module that the user can
view shows that its inventory is not recorded. A device without artwork can
show as a schematic of the same kind, on its one face ``schematic``. A focus
view draws one module again in its own coordinates.
"""

import functools
import logging
import math
import re
from dataclasses import dataclass, replace

import tinycss2
from dcim.models import Interface, Module, ModuleBay
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import capfirst
from lxml import etree
from utilities.ordering import naturalize, naturalize_interface

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
from ..models import DefinitionRevision, active_revisions
from .overrides import connector_bindings, part_overrides
from .placement import Rect, fit, place, transform_rect
from .schematic import layout
from .storage import DamagedBlob, read_blob
from .svg import (
    BAY_ATTRIBUTES,
    IDENTITY,
    SVG_NS,
    XLINK_NS,
    invert,
    multiply,
    parse_view_box,
    safe_parser,
    transform_matrix,
)

logger = logging.getLogger(__name__)

ARTWORK = "artwork"
DAMAGED = "damaged"
# A device or an installed module without drawable artwork: a schematic of its components; also the name of its face.
SCHEMATIC = "schematic"
# A device schematic is as wide as rack equipment (17.5 in), and one rack unit high per unit of its type.
SCHEMATIC_WIDTH = 444.5
RACK_UNIT = 44.45
# The state of a drawn bay.
INSTALLED = "installed"
NOT_RECORDED = "not_recorded"


class WarningCode:
    """The code of each warning of a placed module."""

    SIZE_MISMATCH = "size_mismatch"
    NOT_FINITE = "placement_not_finite"
    DAMAGED_ARTWORK = "damaged_artwork"
    DUPLICATE_BAY = "duplicate_bay"
    TOO_DEEP = "too_deep"


# A finite bay can still overflow: x + width, or the transforms of the parents.
_PLACEMENT_NOT_FINITE = (
    "the placement in bay {key} is not a finite number, so the module shows as a"
    " schematic."
)
# Modules on one placement path; it keeps the composed face below the nesting depth of HTML parsers.
MAX_MODULE_DEPTH = 8
# The view part of an id: "o" for the overview of a device, "a" for the artwork API, "f<n>" for the n-th focus view.
OVERVIEW = "o"
API = "a"
FOCUS = "f"
_URL = re.compile(r"url\(#([^)]*)\)")
_URL_PROPERTIES = ("fill", "stroke", "clip-path")
_XLINK_HREF = f"{{{XLINK_NS}}}href"
_TITLE = f"{{{SVG_NS}}}title"
# XML 1.0 does not allow these characters, and lxml refuses them; NetBox names can hold them.
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_G, _A, _RECT = (f"{{{SVG_NS}}}{tag}" for tag in ("g", "a", "rect"))
# The attributes of a root svg that place it in its parent; a placed module face gets its own.
_VIEWPORT_ATTRIBUTES = ("x", "y", "width", "height")
# The initial value of each inherited property of the profile: a module does not take them from its parent.
# font-family is not reset, because its initial value depends on the browser.
INHERITED_INITIAL = {
    "fill": "black",
    "fill-opacity": "1",
    "fill-rule": "nonzero",
    "stroke": "none",
    "stroke-width": "1",
    "stroke-opacity": "1",
    "stroke-linecap": "butt",
    "stroke-linejoin": "miter",
    "stroke-dasharray": "none",
    "stroke-dashoffset": "0",
    "stroke-miterlimit": "4",
    "visibility": "visible",
    "clip-rule": "nonzero",
    "font-size": "medium",
    "font-weight": "normal",
    "font-style": "normal",
    "text-anchor": "start",
    "dominant-baseline": "auto",
    "letter-spacing": "normal",
}
# A generated local id starts with "-"; an author id starts with a letter.
_MARKS = "-marks-"
# The width of a connector outline as a part of the larger face dimension.
_OUTLINE_WIDTH = 1 / 300
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
    # The component model and name that the binding list gives, with {module} resolved for a module.
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
    # The bay key in the manifest of the parent; None for a slot of a schematic.
    bay_key: str | None
    # The face of the device that shows the module.
    face: str
    representation: str
    # The active revision of the module type, or None.
    revision: DefinitionRevision | None
    # The bay in the coordinates of the device face; None when it is not finite.
    bay: Rect | None
    # The rotation from the module face to the device face.
    rotate: int
    # The id of the group that draws the module, or None; and the prefix of the ids of its face.
    element_id: str | None
    prefix: str
    # The connectors of its artwork, or of its schematic: one for each component.
    connectors: tuple[Connector, ...]
    # The connectors of its revision, also when it shows as a schematic; none without a revision.
    revision_connectors: tuple[Connector, ...]
    # The ids of the module face or schematic that the group draws, without the prefix.
    ids: frozenset[str]
    warnings: tuple[ArtworkWarning, ...]

    @property
    def label(self):
        return part_label(self.module)

    @property
    def damaged(self) -> bool:
        """Return True when a stored file of its revision is damaged, so it shows as a schematic."""
        return any(w.code == WarningCode.DAMAGED_ARTWORK for w in self.warnings)

    def connector_id(self, key) -> str | None:
        return self.prefix + key if key in self.ids else None


@dataclass(frozen=True)
class DrawnBay:
    """A bay that a face draws: with its installed module, or with inventory that is not recorded."""

    # The installed module that has the bay; None for a bay of the device.
    parent: Module | None
    # The bay key in the manifest of the parent; None for a slot of a schematic.
    key: str | None
    # None when NetBox has no module bay of that name that the user can view.
    module_bay: ModuleBay | None
    face: str
    # None when no module is recorded, or the user cannot view it.
    module: Module | None
    # The id of the group that draws the module or the mark, or None when the bay is not finite.
    element_id: str | None

    @property
    def state(self) -> str:
        return NOT_RECORDED if self.module is None else INSTALLED


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

    @property
    def view(self) -> str:
        """Return the view part of the ids of the face."""
        return self.prefix.split("-")[1]

    def element_id(self, key) -> str | None:
        """Return the id of the element ``key`` in the face, or None when the face does not draw it."""
        return self.prefix + key if key in self.ids else None


@dataclass(frozen=True)
class Focus:
    """A drawn module in a view of its own: its face or schematic in its own coordinates."""

    module: PlacedModule
    ident: str
    # The id of the overview of the device.
    overview: str
    face: RenderedFace
    # The focus views of the modules on the placement path above the module, outermost first.
    parents: tuple["Focus", ...]

    @property
    def parent_ident(self) -> str:
        """Return the id of the view that Escape goes back to."""
        return self.parents[-1].ident if self.parents else self.overview


@dataclass(frozen=True)
class AssemblyNode:
    """A placed module in the assembly navigation, with its focus view or None when it is not drawn."""

    module: PlacedModule
    focus: Focus | None
    children: tuple["AssemblyNode", ...]


@dataclass(frozen=True)
class Part:
    """The drawing of one device: its artwork faces in physical order, its schematic face, or no face when a blob is damaged."""

    device: object
    representation: str
    # None for a schematic.
    revision: DefinitionRevision | None
    # The id of the overview of the device.
    ident: str
    faces: tuple[RenderedFace, ...]
    connectors: tuple[Connector, ...]
    # Each installed module that a face shows, parents before their children.
    modules: tuple[PlacedModule, ...] = ()
    # Each bay that a face or a schematic draws, in drawing order.
    bays: tuple[DrawnBay, ...] = ()
    # The focus view of each module that a face draws; none for a plain face.
    focus_views: tuple[Focus, ...] = ()

    @property
    def damaged(self):
        return self.representation == DAMAGED

    @property
    def assembly(self) -> tuple[AssemblyNode, ...]:
        """Return the tree of the placed modules below the device."""
        views = {focus.module.module.pk: focus for focus in self.focus_views}
        children = {}
        for placed in self.modules:
            parent = None if placed.parent is None else placed.parent.pk
            children.setdefault(parent, []).append(placed)

        def nodes(parent):
            return tuple(
                AssemblyNode(
                    placed, views.get(placed.module.pk), nodes(placed.module.pk)
                )
                for placed in children.get(parent, ())
            )

        return nodes(None)


def resolve_device(
    device, revision, user, view=OVERVIEW, plain=False, selection=None
) -> Part:
    """Return the artwork Part of ``device`` with the components and modules that ``user`` can view.

    The connectors bind with the overrides of each part and its type. A
    ``plain`` face has no class, links or marks. A ``selection`` gives the
    revisions of the types of a reviewed activation: see active_revisions().
    """
    composer = _Composer(device, user, plain, selection)
    connectors = composer.connectors(revision, device)
    part_id = f"dv-{view}-d{device.pk}"
    data = _read_faces(revision)
    if data is None:
        return Part(device, DAMAGED, revision, part_id, (), connectors)
    faces = tuple(
        composer.root_face(part_id, face, data[face], revision, connectors)
        for face in revision.face_names
    )
    modules, bays = tuple(composer.modules), tuple(composer.bays)
    focus_views = () if plain else composer.focus_views(device, part_id, modules)
    return Part(
        device,
        ARTWORK,
        revision,
        part_id,
        faces,
        connectors,
        modules,
        bays,
        focus_views,
    )


def resolve_schematic(device, user, view=OVERVIEW, plain=False, selection=None) -> Part:
    """Return the schematic Part of ``device``: one face with the components and module bays that ``user`` can view.

    A slot draws its installed module: its artwork scaled to fit, or its own
    schematic. A ``plain`` face has no class, links or marks. A ``selection``
    is as for resolve_device().
    """
    composer = _Composer(device, user, plain, selection)
    part_id = f"dv-{view}-d{device.pk}"
    # A type can have a height of 0 U or less than 1 U.
    height = max(float(device.device_type.u_height), 1) * RACK_UNIT
    face, connectors = composer.schematic_face(
        part_id, None, Rect(0, 0, SCHEMATIC_WIDTH, height)
    )
    modules, bays = tuple(composer.modules), tuple(composer.bays)
    focus_views = () if plain else composer.focus_views(device, part_id, modules)
    return Part(
        device,
        SCHEMATIC,
        None,
        part_id,
        (face,),
        connectors,
        modules,
        bays,
        focus_views,
    )


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
    # The number of modules on the placement path: 0 for the device.
    depth: int
    # The outline width of the connector marks, in the face coordinates of the part.
    outline_width: float


class _Composer:
    """Draw the faces of one device, with each installed module that the user can view in its bay."""

    def __init__(self, device, user, plain, selection):
        self.device = device
        self.plain = plain
        self.states = device_components(device, user, BINDABLE_MODELS)
        modules = list(
            Module.objects.restrict(user, "view")
            .filter(device=device)
            .select_related("module_bay", "module_type__manufacturer")
        )
        bays = sorted(
            ModuleBay.objects.restrict(user, "view").filter(device=device),
            key=lambda bay: (naturalize(bay.name, max_length=100), bay.pk),
        )
        visible_bays = {bay.pk for bay in bays}
        # {(module pk of the bay or None, bay name): module bay}
        self.bay_named = {(bay.module_id, bay.name): bay for bay in bays}
        # {module pk of the bay or None: its module bays}, the slots of a schematic.
        self.slots = {}
        for bay in bays:
            self.slots.setdefault(bay.module_id, []).append(bay)
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
        # {module pk: revision}
        self.revisions = active_revisions(modules, selection)
        self.overrides = part_overrides([device, *modules])
        self.faces = {}
        self.modules = []
        self.bays = []

    def connectors(self, revision, part) -> tuple[Connector, ...]:
        """Return the connectors of ``revision`` for a device or an installed module."""
        bindings = connector_bindings(revision, part, self.overrides)
        module = part if isinstance(part, Module) else None
        return tuple(
            _connector(key, item, bindings[key], self.states, module)
            for key, item in sorted(revision.manifest["connectors"].items())
        )

    def focus_views(self, device, part_id, modules) -> tuple[Focus, ...]:
        """Return the focus view of each drawn module of ``modules``, in order."""
        views = {}
        for placed in modules:
            # A module that is not drawn, or whose placement is not finite, has no view.
            if placed.element_id is None or placed.bay is None:
                continue
            parent = None if placed.parent is None else views[placed.parent.pk]
            parents = () if parent is None else (*parent.parents, parent)
            path = [*(p.module.module for p in parents), placed.module]
            ident = f"dv-{FOCUS}{len(views) + 1}-d{device.pk}" + "".join(
                f"-m{module.pk}" for module in path
            )
            first, bays = len(self.modules), len(self.bays)
            if placed.representation == ARTWORK:
                [face] = placed.revision.face_names
                rendered = self.root_face(
                    ident,
                    face,
                    self.face_data(placed.revision)[face],
                    placed.revision,
                    placed.connectors,
                    placed.module,
                    len(path),
                )
            else:
                bay = Rect(0, 0, placed.bay.width, placed.bay.height)
                rendered, _ = self.schematic_face(ident, placed.module, bay, len(path))
            # The overview already lists the modules and bays that the focus view draws again.
            del self.modules[first:]
            del self.bays[bays:]
            views[placed.module.pk] = Focus(placed, ident, part_id, rendered, parents)
        return tuple(views.values())

    def root_face(
        self, part_id, face, data, revision, connectors, module=None, depth=0
    ) -> RenderedFace:
        """Draw ``face`` of the device, or of ``module`` at ``depth`` in its own coordinates, with the modules in its bays."""
        prefix = f"{part_id}-{face}-"
        root, elements = _parse_face(data, prefix)
        for name in _VIEWPORT_ATTRIBUTES:
            root.attrib.pop(name, None)
        view_box = parse_view_box(root.get("viewBox").strip())
        width = max(view_box[2:]) * _OUTLINE_WIDTH
        first = len(self.modules)
        self.compose(
            root,
            elements,
            revision,
            face,
            _Scope(module, part_id, face, IDENTITY, 0, depth, width),
        )
        connectors = [c for c in connectors if c.face == face and c.key in elements]
        if not self.plain:
            _mark_connectors(root, prefix, connectors, elements, view_box, width)
        owner = None if module is None else part_label(module)
        own = tuple(c.details(prefix, owner) for c in connectors)
        return self.finish(root, face, prefix, frozenset(elements), own, first)

    def schematic_face(
        self, part_id, module, rect, depth=0
    ) -> tuple[RenderedFace, tuple[Connector, ...]]:
        """Draw the schematic of ``module``, or of the device when it is None, in its own coordinates at the size of ``rect``.

        Return the face and the connectors of the schematic.
        """
        root = etree.Element(
            f"{{{SVG_NS}}}svg",
            viewBox=f"0 0 {_number(rect.width)} {_number(rect.height)}",
            nsmap={None: SVG_NS},
        )
        first = len(self.modules)
        width = max(rect.width, rect.height) * _OUTLINE_WIDTH
        scope = _Scope(module, part_id, SCHEMATIC, IDENTITY, 0, depth, width)
        prefix, ids, connectors = self.schematic(root, rect, module, scope, scope)
        owner = None if module is None else part_label(module)
        own = tuple(c.details(prefix, owner) for c in connectors)
        face = self.finish(root, SCHEMATIC, prefix, ids, own, first)
        return face, connectors

    def finish(self, root, face, prefix, ids, own, first) -> RenderedFace:
        """Serialize a root face with the details of its own connectors and of the modules placed after ``first``."""
        placed = self.modules[first:]
        details = ()
        if not self.plain:
            root.set("class", "dv-art-face")
            details = (
                *own,
                *(
                    c.details(child.prefix, child.label)
                    for child in placed
                    for c in child.connectors
                    if c.key in child.ids
                ),
            )
        # lxml escapes text and attribute values, so the markup holds no stored bytes as they are.
        svg = mark_safe(etree.tostring(_unprefixed(root), encoding="unicode"))
        warnings = tuple(w for child in placed for w in child.warnings)
        return RenderedFace(face, prefix, svg, ids, details, warnings)

    def compose(self, root, elements, revision, face, scope):
        """Draw the installed module, or the mark of no recorded module, in each bay of ``revision`` on ``face``."""
        # {module bay name: bay keys}; with {module}, two keys can name one module bay.
        mapped = {}
        for key, item in sorted(revision.manifest["bays"].items()):
            # A bay in title or desc is not drawn.
            if item["face"] != face or key not in elements:
                continue
            name = item["module_bay"]
            if scope.module is not None:
                name = resolve_name(ModuleBay, name, scope.module)
            mapped.setdefault(name, []).append(key)
        owner = None if scope.module is None else scope.module.pk
        for name, (key, *others) in mapped.items():
            module = self.installed.get((owner, name))
            rect = elements[key]
            if module is None:
                bay = _bay_rect(rect)
                element_id = None
                if _finite(*vars(bay).values()):
                    element_id = f"{scope.part_id}-{face}--not-recorded-{key}"
                    group = etree.Element(_G, id=element_id, **INHERITED_INITIAL)
                    _after_top(rect, group)
                    target, bay, _ = _upright(group, bay, scope.matrix)
                    self.not_recorded(target, bay, _own_width(bay, scope.outline_width))
                self.bays.append(
                    DrawnBay(
                        scope.module,
                        key,
                        self.bay_named.get((owner, name)),
                        scope.face,
                        None,
                        element_id,
                    )
                )
            else:
                rotate = revision.manifest["bays"][key]["rotate"]
                self.place(elements[key], key, rotate, module, scope, others)

    def place(self, rect, key, rotate, module, scope, others=(), slot=False):
        """Draw ``module`` over the bay ``rect`` of ``scope``, and its own bays inside it.

        ``others`` are the later bay keys that name the same module bay; they
        draw nothing. In a ``slot`` of a schematic, artwork is scaled to fit.
        """
        revision = self.revisions.get(module.pk)
        inner = _Scope(
            module,
            f"{scope.part_id}-m{module.pk}",
            scope.face,
            scope.matrix,
            (scope.rotate + rotate) % 360,
            scope.depth + 1,
            # Placement at true scale keeps the units; a slot scales them in artwork().
            scope.outline_width,
        )
        label = part_label(module)
        # A slot of a schematic has no bay key; its module bay names it.
        where = key if key is not None else module.module_bay.name
        warnings = []

        def warn(code, text):
            warnings.append(ArtworkWarning(code, f"{label}: {text}"))

        if others:
            warn(
                WarningCode.DUPLICATE_BAY,
                f"the bays {key} and {', '.join(others)} map to the module bay"
                f" {module.module_bay.name}, so the module shows only in {key}.",
            )
        artwork_connectors = (
            () if revision is None else self.connectors(revision, module)
        )
        # The children come after their parent, so the parent takes this index.
        index, bay_index = len(self.modules), len(self.bays)
        bay = _bay_rect(rect)
        absolute, element_id = None, None
        representation, prefix, ids = SCHEMATIC, "", frozenset()
        connectors = artwork_connectors
        if not _finite(*vars(bay).values()):
            warn(
                WarningCode.NOT_FINITE,
                f"the bay {where} has a position or size that is not a finite number,"
                " so the module is not drawn.",
            )
        else:
            group = self.module_group(rect, inner.part_id, bay, slot)
            element_id = inner.part_id
            absolute = transform_rect(scope.matrix, bay)
            if not _finite(*vars(absolute).values()):
                warn(
                    WarningCode.NOT_FINITE,
                    _PLACEMENT_NOT_FINITE.format(key=where),
                )
                absolute = None
            elif inner.depth > MAX_MODULE_DEPTH:
                warn(
                    WarningCode.TOO_DEEP,
                    f"the module is more than {MAX_MODULE_DEPTH} modules deep, so it"
                    " shows as a schematic, and the modules in it do not show.",
                )
            elif revision is not None:
                drawn = self.artwork(
                    group,
                    bay,
                    rotate,
                    revision,
                    artwork_connectors,
                    inner,
                    key,
                    warn,
                    slot,
                )
                if drawn is not None:
                    representation, (prefix, ids) = ARTWORK, drawn
            if representation == SCHEMATIC:
                prefix, ids, connectors = self.schematic(
                    group, bay, module, scope, inner
                )
        self.modules.insert(
            index,
            PlacedModule(
                module=module,
                parent=scope.module,
                bay_key=key,
                face=scope.face,
                representation=representation,
                revision=revision,
                bay=absolute,
                # A schematic is not turned, so its labels stay readable.
                rotate=inner.rotate if representation == ARTWORK else 0,
                element_id=element_id,
                prefix=prefix,
                connectors=connectors,
                revision_connectors=artwork_connectors,
                ids=ids,
                warnings=tuple(warnings),
            ),
        )
        self.bays.insert(
            bay_index,
            DrawnBay(
                scope.module, key, module.module_bay, scope.face, module, element_id
            ),
        )

    def module_group(self, rect, part_id, bay, slot=False):
        """Return the group of a module, clipped to its bay.

        The group follows the top-level element of the parent face that holds
        the bay, or the slot itself in a schematic.
        """
        clip = f"{part_id}--clip"
        group = etree.Element(
            _G, {"id": part_id, "clip-path": f"url(#{clip})", **INHERITED_INITIAL}
        )
        etree.SubElement(
            etree.SubElement(group, f"{{{SVG_NS}}}clipPath", id=clip),
            _RECT,
            _rect_attributes(bay),
        )
        if slot:
            rect.addnext(group)
        else:
            _after_top(rect, group)
        return group

    def artwork(
        self, group, bay, rotate, revision, connectors, scope, key, warn, slot=False
    ):
        """Draw the artwork of the module of ``scope`` into ``group``; return (prefix, ids), or None when it cannot."""
        data = self.face_data(revision)
        if data is None:
            warn(
                WarningCode.DAMAGED_ARTWORK,
                f"a stored file of the active {revision.definition.key} revision is"
                " missing or does not match its digest. Import the revision again.",
            )
            return None
        [face] = revision.face_names
        prefix = f"{scope.part_id}-{face}-"
        child, elements = _parse_face(data[face], prefix)
        view_box = parse_view_box(child.get("viewBox").strip())
        placement = fit(view_box, bay) if slot else place(view_box, bay, rotate)
        nested = (
            None
            if placement.matrix is None
            else multiply(scope.matrix, placement.matrix)
        )
        if nested is None or not _finite(*nested):
            warn(
                WarningCode.NOT_FINITE,
                _PLACEMENT_NOT_FINITE.format(key=key or scope.module.module_bay.name),
            )
            return None
        if placement.size_mismatch:
            warn(
                WarningCode.SIZE_MISMATCH,
                f"the artwork is {_number(placement.size[0])} ×"
                f" {_number(placement.size[1])} mm, but the bay {key} is"
                f" {_number(bay.width)} × {_number(bay.height)} mm.",
            )
        # A slot scales the module face, so the width in its units scales the other way.
        width = (
            scope.outline_width / placement.matrix[0] if slot else scope.outline_width
        )
        self.compose(
            child,
            elements,
            revision,
            face,
            replace(scope, matrix=nested, outline_width=width),
        )
        if not self.plain:
            drawn = [c for c in connectors if c.face == face]
            _mark_connectors(child, prefix, drawn, elements, view_box, width)
            body = self.body(group, scope.module, "dv-art-module-body")
            etree.SubElement(body, _RECT, {**_rect_attributes(bay), "fill": "none"})
        # The module root stays a viewport, so its percentage lengths keep their meaning.
        min_x, min_y, width, height = view_box
        child.attrib.update(
            zip(
                _VIEWPORT_ATTRIBUTES,
                ("0", "0", _number(width), _number(height)),
                strict=True,
            )
        )
        # The viewport maps the viewBox origin to 0, 0: the transform moves it back.
        matrix = multiply(placement.matrix, (1, 0, 0, 1, min_x, min_y))
        art = etree.SubElement(
            group, _G, transform=f"matrix({' '.join(map(_number, matrix))})"
        )
        if not self.plain:
            art.set("class", "dv-art-module-art")
        art.append(child)
        return prefix, frozenset(elements)

    def schematic(self, group, bay, module, scope, inner):
        """Draw a schematic of ``module``, or of the device when it is None, in ``bay`` of ``scope``; return (prefix, ids, connectors).

        It shows the components of the part that the user can view, by type
        in NetBox order, and a slot for each module bay. It is not turned.
        """
        prefix = f"{inner.part_id}-{SCHEMATIC}-"
        target, rect, matrix = _upright(group, bay, scope.matrix)
        # The key of the components and bays of the part.
        owner = None if module is None else module.pk
        states = sorted(
            (s for s in self.states if s.component.module_id == owner),
            key=lambda s: (
                BINDABLE_MODELS.index(type(s.component)),
                _natural(s.component),
            ),
        )
        connectors = tuple(
            Connector(
                f"{type(s.component)._meta.model_name}-{s.component.pk}",
                SCHEMATIC,
                s.component.name,
                (Binding(type(s.component), s.component.name, FOUND, s),),
            )
            for s in states
        )
        # A finite bay can have an edge that overflows; the schematic then has only its frame.
        drawable = _finite(rect.x + rect.width, rect.y + rect.height)
        # Past the depth limit, the modules in it do not show.
        slots = (
            self.slots.get(owner, [])
            if drawable and inner.depth <= MAX_MODULE_DEPTH
            else []
        )
        title, cells = layout(rect, len(connectors) + len(slots) if drawable else 0)
        width = _own_width(rect, scope.outline_width)
        if module is None:
            body = self.body(target, self.device, "dv-art-device-body dv-art-schematic")
            model = self.device.device_type.model
        else:
            body = self.body(target, module, "dv-art-module-body dv-art-schematic")
            model = module.module_type.model
        self.box(body, rect, _SCHEMATIC_FRAME, width)
        _label(body, title, model)
        parts = etree.Element(_G)
        elements = {}
        for connector, cell in zip(connectors, cells, strict=False):
            element = etree.SubElement(parts, _G, id=prefix + connector.key)
            if not self.plain:
                element.set("class", "dv-art-schematic-part")
            self.box(element, cell, _SCHEMATIC_PART, width)
            _label(element, cell, connector.label)
            elements[connector.key] = element
        # Drawn upright, the slots are not turned; drawn in the bay, they turn with the parents.
        upright = replace(
            inner,
            matrix=matrix,
            rotate=0 if matrix[:4] == (1, 0, 0, 1) else scope.rotate,
            outline_width=width,
        )
        for slot, cell in zip(slots, cells[len(connectors) :], strict=True):
            element = etree.SubElement(parts, _G, id=f"{prefix}slot-{slot.pk}")
            if not self.plain:
                element.set("class", "dv-art-slot")
            frame = self.box(element, cell, _SCHEMATIC_SLOT, width, dashed=True)
            name, area = _slot_areas(cell)
            _label(element, name, slot.name)
            child = self.installed.get((owner, slot.name))
            if child is None:
                self.not_recorded(element, area, width, frame=False)
                self.bays.append(
                    DrawnBay(module, None, slot, scope.face, None, element.get("id"))
                )
                continue
            # The child draws in the area under the slot name, after the slot frame.
            room = etree.Element(_RECT, _rect_attributes(area))
            frame.addnext(room)
            self.place(room, None, 0, child, upright, slot=True)
            element.remove(room)
        if not self.plain:
            _mark_connectors(
                parts,
                prefix,
                connectors,
                elements,
                (rect.x, rect.y, rect.width, rect.height),
                width,
            )
        target.append(parts)
        return prefix, frozenset(elements), connectors

    def not_recorded(self, target, rect, width, frame=True):
        """Draw the mark of a bay without a recorded module that the user can view."""
        if not self.plain:
            target.set(
                "class",
                " ".join(filter(None, (target.get("class"), "dv-art-not-recorded"))),
            )
        if frame:
            self.box(target, rect, _SCHEMATIC_SLOT, width, dashed=True)
        _label(target, rect, NOT_RECORDED_TEXT)

    @staticmethod
    def box(parent, rect, style, width, dashed=False):
        """Add a rect with ``style`` and the stroke ``width`` to ``parent``."""
        attributes = {**_rect_attributes(rect), **style, "stroke-width": _number(width)}
        if dashed:
            attributes["stroke-dasharray"] = (
                f"{_number(3 * width)} {_number(2 * width)}"
            )
        return etree.SubElement(parent, _RECT, attributes)

    def face_data(self, revision) -> dict[str, bytes] | None:
        if revision.pk not in self.faces:
            self.faces[revision.pk] = _read_faces(revision)
        return self.faces[revision.pk]

    def body(self, group, part, classes):
        """Return the link of the body of a module or the device to its record; a plain face has a group instead."""
        if self.plain:
            return etree.SubElement(group, _G)
        body = etree.SubElement(
            group, _A, {"class": classes, "href": part.get_absolute_url()}
        )
        title = (
            f"Module {part_label(part)}"
            if isinstance(part, Module)
            else f"Device {part}"
        )
        etree.SubElement(body, _TITLE).text = _xml_text(title)
        return body


# The plain faces of the API have no page CSS, so the schematic has its own colours; the page CSS sets theme colours.
_SCHEMATIC_FRAME = {"fill": "#f8f9fa", "stroke": "#6c757d"}
_SCHEMATIC_PART = {"fill": "#dee2e6", "stroke": "#495057"}
_SCHEMATIC_SLOT = {"fill": "none", "stroke": "#6c757d"}
NOT_RECORDED_TEXT = "not recorded"


def _after_top(rect, element):
    """Put ``element`` after the top-level element of the face that holds ``rect``.

    The groups of the bay then do not give it their opacity, clip or display.
    """
    top = rect
    while top.getparent().getparent() is not None:
        top = top.getparent()
    top.addnext(element)


def _upright(group, rect, matrix):
    """Return a group in ``group`` that draws in device face coordinates, ``rect`` there, and the matrix of those coordinates.

    A schematic or a mark drawn there is not turned with the parents of its bay.
    When the coordinates are not finite, it draws in the coordinates of the bay.
    """
    # A parent that is not turned keeps the labels readable.
    if matrix[:4] == (1, 0, 0, 1):
        return group, rect, matrix
    inverse = invert(matrix)
    absolute = transform_rect(matrix, rect)
    if inverse is None or not _finite(*inverse, *vars(absolute).values()):
        return group, rect, matrix
    upright = etree.SubElement(
        group, _G, transform=f"matrix({' '.join(map(_number, inverse))})"
    )
    return upright, absolute, IDENTITY


def _bay_rect(element) -> Rect:
    return Rect(*(float(element.get(name, "0")) for name in BAY_ATTRIBUTES))


def _own_width(rect: Rect, outline_width) -> float:
    """Return the outline width of a schematic or a mark in ``rect``: as for a face of that size, so its marks fit its cells."""
    return min(outline_width, max(rect.width, rect.height) * _OUTLINE_WIDTH)


def _natural(component) -> str:
    """Return the sort key that NetBox gives the name of ``component``."""
    if isinstance(component, Interface):
        return naturalize_interface(component.name, max_length=100)
    return naturalize(component.name, max_length=100)


def _slot_areas(cell: Rect) -> tuple[Rect, Rect]:
    """Return the strip of the slot name at the top of ``cell``, and the area of the child under it."""
    strip = cell.height * 0.25
    return (
        Rect(cell.x, cell.y, cell.width, strip),
        Rect(cell.x, cell.y + strip, cell.width, cell.height - strip),
    )


def _label(parent, rect: Rect, name: str):
    """Write ``name`` in the centre of ``rect``, at the largest size that fits; nothing when that is not finite."""
    size = min(rect.height * 0.6, rect.width * 0.9 / (0.6 * max(len(name), 1)))
    x, y = rect.x + rect.width / 2, rect.y + rect.height / 2
    # A finite rect can have a centre that overflows.
    if not _finite(x, y, size) or size <= 0:
        return
    text = etree.SubElement(
        parent,
        f"{{{SVG_NS}}}text",
        {
            "x": _number(x),
            "y": _number(y),
            "font-size": _number(size),
            "font-family": "sans-serif",
            "text-anchor": "middle",
            "dominant-baseline": "central",
            "fill": "#212529",
        },
    )
    text.text = _xml_text(name)


def _finite(*values) -> bool:
    return all(map(math.isfinite, values))


def _number(value) -> str:
    # Adding 0 turns -0.0 into 0.0.
    return f"{value + 0:.12g}"


def _rect_attributes(rect: Rect) -> dict[str, str]:
    return {name: _number(getattr(rect, name)) for name in BAY_ATTRIBUTES}


def _connector(key, item, resolved, states, module=None) -> Connector:
    """Return the connector of manifest ``item`` with the ``resolved`` binding list, in the scope of ``module``."""
    bindings = []
    for binding in resolved:
        model = BINDING_MODELS[binding["component"]]
        found = find(states, model, binding["name"], module)
        # The page and the API name what NetBox calls the component of this module.
        name = binding["name"]
        if module is not None:
            name = resolve_name(model, name, module) or name
        # A component name is unique per type on one device, so one match at most.
        bindings.append(
            Binding(model, name, FOUND, found[0])
            if found
            else Binding(model, name, MISSING, None)
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


def _mark_connectors(root, prefix, connectors, elements, view_box, outline_width):
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
                outline_width,
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
    ident: str, view_box, matrix, outer, inner, outline_width
) -> etree._Element | None:
    """Return the filter that marks a connector, in the user space that ``matrix`` maps to the face.

    ``outline_width`` is in face units. None when the matrix
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
    radii = (outline_width / math.hypot(a, b), outline_width / math.hypot(c, d))
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
