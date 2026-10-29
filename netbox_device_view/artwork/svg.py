"""Validation of artwork faces against the bounded SVG profile."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

import tinycss2
from lxml import etree

from ..layout.parser import one_css_color
from .errors import ArtworkError, Issue
from .limits import (
    MAX_ISSUES,
    MAX_NUMBER,
    MAX_PATH_DATA_BYTES,
    MAX_SVG_BYTES,
    MAX_SVG_DEPTH,
    MAX_SVG_ELEMENTS,
    MAX_TRANSFORM_BYTES,
    MAX_USE_DEPTH,
    MAX_USE_EXPANSION_NODES,
    MAX_VIEWBOX_SIZE,
    MIN_VIEWBOX_SIZE,
)
from .manifest import SCHEMA

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
ALLOWED_NAMESPACES = frozenset({SVG_NS, XLINK_NS})

SHAPES = frozenset({"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"})
GRADIENTS = frozenset({"linearGradient", "radialGradient"})
TEXT_ELEMENTS = frozenset({"text", "tspan", "title", "desc"})
USE_TARGETS = SHAPES | {"symbol", "g", "text"}
CONNECTOR_ELEMENTS = SHAPES | {"g", "use", "text"}
DEFINITIONS = GRADIENTS | {"defs", "symbol", "clipPath"}

MAX_DASHES = 32
MAX_FONT_NAME = 64


@dataclass(frozen=True)
class Face:
    """The facts of one valid face that the bundle import uses."""

    view_box: tuple[float, float, float, float]
    connector_ids: frozenset[str]
    bay_ids: frozenset[str]
    id_lines: dict[str, int]


class _Invalid(ValueError):
    """A value that does not match its grammar; the message says why."""


class _Bound(_Invalid):
    """A value over a resource bound; validation stops at once."""


class _Reference(NamedTuple):
    """A same-file reference that a value makes, and the element types it may name."""

    target: str
    kinds: frozenset[str]
    expands: bool = False


@dataclass(frozen=True)
class _Place:
    """What the ancestors of an element are."""

    definition: str | None = None
    connector: str | None = None
    plain: bool = True  # every ancestor is the root svg or a g without transform


_SPACE = " \t\r\n"
_WSP = re.compile(r"[ \t\r\n]*")
_COMMA_WSP = re.compile(r"[ \t\r\n]*,?[ \t\r\n]*")
_LIST_SEPARATOR = re.compile(r"[ \t\r\n]*,[ \t\r\n]*|[ \t\r\n]+")
_TRANSFORM_SEPARATOR = re.compile(r"[ \t\r\n,]*")
_NAME = re.compile(r"[A-Za-z]*")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_MARKER_IDS = {
    kind: re.compile(SCHEMA["properties"][section]["propertyNames"]["pattern"][1:-1])
    for kind, section in (("con-", "connectors"), ("bay-", "bays"))
}
_HREF = re.compile(r"#(.+)")
# Each digit belongs to one group only, so a failed match backtracks in linear time.
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_LENGTH = re.compile(rf"({_NUMBER.pattern})(px|mm|%)?")
_URL = re.compile(r"url\(#([^)]*)\)")
_ASPECT = re.compile(
    r"(?:defer[ \t\r\n]+)?(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max))"
    r"(?:[ \t\r\n]+(?:meet|slice))?"
)


def _bounded(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or abs(value) > MAX_NUMBER:
        raise _Invalid(f"number {text} is outside -{MAX_NUMBER}..{MAX_NUMBER}")
    return value


class _Scanner:
    """A cursor over one attribute value for the hand-written grammars."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def at_end(self) -> bool:
        return self.pos == len(self.text)

    def peek(self) -> str:
        return self.text[self.pos : self.pos + 1]

    def take(self) -> str:
        char = self.peek()
        self.pos += 1
        return char

    def where(self) -> str:
        return f"at position {self.pos + 1}"

    def skip(self, pattern: re.Pattern) -> str:
        match = pattern.match(self.text, self.pos)
        self.pos = match.end()
        return match.group()

    def comma_wsp(self) -> bool:
        """Skip an optional comma-wsp; return True when it held a comma."""
        return "," in self.skip(_COMMA_WSP)

    def starts_number(self) -> bool:
        return _NUMBER.match(self.text, self.pos) is not None

    def number(self) -> float:
        match = _NUMBER.match(self.text, self.pos)
        if match is None:
            raise _Invalid(f"expected a number {self.where()}")
        self.pos = match.end()
        return _bounded(match.group())


def _number_list(value: str) -> list[float]:
    """Return the numbers of a list separated by whitespace and/or commas."""
    scanner = _Scanner(value)
    numbers = [scanner.number()]
    while not scanner.at_end():
        scanner.comma_wsp()
        numbers.append(scanner.number())
    return numbers


def _number(value: str) -> float:
    if _NUMBER.fullmatch(value) is None:
        raise _Invalid("must be a number")
    return _bounded(value)


def _length(value: str) -> float:
    match = _LENGTH.fullmatch(value)
    if match is None:
        raise _Invalid("must be a number with an optional unit px, mm or %")
    return _bounded(match.group(1))


def _size(value: str):
    if _length(value) < 0:
        raise _Invalid("must be a non-negative length")


def _fraction(value: str):
    number = _number(value[:-1]) / 100 if value.endswith("%") else _number(value)
    if not 0 <= number <= 1:
        raise _Invalid("must be a number from 0 to 1 or a percentage from 0% to 100%")


def _miter_limit(value: str):
    if _number(value) < 1:
        raise _Invalid("must be a number of at least 1")


def _keywords(words: str) -> Callable[[str], None]:
    allowed = frozenset(words.split())

    def check(value: str):
        if value not in allowed:
            raise _Invalid(f"must be one of: {words}")

    return check


def _keywords_or(words: str, other: Callable[[str], object]) -> Callable[[str], None]:
    allowed = frozenset(words.split())

    def check(value: str):
        if value not in allowed:
            try:
                other(value)
            except _Invalid as error:
                raise _Invalid(f"must be one of: {words}; or {error}") from None

    return check


def _view_box(value: str) -> tuple[float, float, float, float]:
    numbers = _number_list(value)
    if len(numbers) != 4:
        raise _Invalid("must be four numbers: min-x, min-y, width, height")
    if not all(MIN_VIEWBOX_SIZE <= size <= MAX_VIEWBOX_SIZE for size in numbers[2:]):
        raise _Invalid(
            f"width and height must be {MIN_VIEWBOX_SIZE}..{MAX_VIEWBOX_SIZE} mm"
        )
    return tuple(numbers)


def _aspect(value: str):
    if _ASPECT.fullmatch(value) is None:
        raise _Invalid("must match [defer] <align> [meet|slice]")


def _points(value: str):
    numbers = _number_list(value)
    if len(numbers) < 4 or len(numbers) % 2:
        raise _Invalid("must be an even count of at least 4 numbers")


# Argument kinds per path command: c = number, r = non-negative number, f = flag.
_PATH_ARGUMENTS = {
    "M": "cc",
    "L": "cc",
    "T": "cc",
    "H": "c",
    "V": "c",
    "C": "cccccc",
    "S": "cccc",
    "Q": "cccc",
    "A": "rrcffcc",
    "Z": "",
}


def _path_group(scanner: _Scanner, kinds: str):
    for index, kind in enumerate(kinds):
        if index:
            scanner.comma_wsp()
        if kind == "f":
            if scanner.peek() not in ("0", "1"):
                raise _Invalid(f"expected an arc flag 0 or 1 {scanner.where()}")
            scanner.take()
        elif scanner.number() < 0 and kind == "r":
            raise _Invalid(f"arc radius must be non-negative {scanner.where()}")


def _path(value: str):
    if len(value.encode()) > MAX_PATH_DATA_BYTES:
        raise _Bound(f"path data is longer than {MAX_PATH_DATA_BYTES} bytes")
    scanner = _Scanner(value)
    if scanner.peek() not in ("M", "m"):
        raise _Invalid("path data must start with M or m")
    while not scanner.at_end():
        where = scanner.where()
        command = scanner.take()
        kinds = _PATH_ARGUMENTS.get(command.upper())
        if kinds is None:
            raise _Invalid(f"unknown path command {command} {where}")
        scanner.skip(_WSP)
        while kinds:
            _path_group(scanner, kinds)
            comma = scanner.comma_wsp()
            if not scanner.starts_number():
                if comma:
                    raise _Invalid(f"trailing comma {scanner.where()}")
                break


_TRANSFORM_ARGUMENTS = {
    "matrix": (6,),
    "translate": (1, 2),
    "scale": (1, 2),
    "rotate": (1, 3),
    "skewX": (1,),
    "skewY": (1,),
}


def _transform_arguments(scanner: _Scanner) -> list[float]:
    if scanner.take() != "(":
        raise _Invalid(f'expected "(" {scanner.where()}')
    scanner.skip(_WSP)
    numbers = [scanner.number()]
    while True:
        comma = scanner.comma_wsp()
        if scanner.peek() == ")":
            if comma:
                raise _Invalid(f"trailing comma {scanner.where()}")
            scanner.take()
            return numbers
        if not comma and not scanner.starts_number():
            raise _Invalid(f'expected ")" {scanner.where()}')
        numbers.append(scanner.number())


def _transform(value: str):
    if len(value.encode()) > MAX_TRANSFORM_BYTES:
        raise _Bound(f"transform list is longer than {MAX_TRANSFORM_BYTES} bytes")
    scanner = _Scanner(value)
    while not scanner.at_end():
        where = scanner.where()
        name = scanner.skip(_NAME)
        counts = _TRANSFORM_ARGUMENTS.get(name)
        if counts is None:
            raise _Invalid(f"unknown transform {name or scanner.peek()} {where}")
        scanner.skip(_WSP)
        count = len(_transform_arguments(scanner))
        if count not in counts:
            allowed = " or ".join(map(str, counts))
            raise _Invalid(f"{name} takes {allowed} numbers, not {count}")
        separator = scanner.skip(_TRANSFORM_SEPARATOR)
        if scanner.at_end():
            if "," in separator:
                raise _Invalid("transform list must not end with a comma")
        elif not separator:
            raise _Invalid(f"separate transforms with whitespace or commas {where}")


def _url(value: str, kinds: frozenset[str]) -> _Reference | None:
    match = _URL.fullmatch(value)
    return None if match is None else _Reference(match.group(1), kinds)


def _css(function, *args, **kwargs):
    """Call a tinycss2 function; it recurses into nested blocks, so deep nesting fails."""
    try:
        return function(*args, **kwargs)
    except RecursionError:
        raise _Invalid("CSS blocks are nested too deeply") from None


def _is_color(value: str) -> bool:
    tokens = _css(tinycss2.parse_component_value_list, value)
    return one_css_color(tokens) is not None


def _paint(value: str) -> _Reference | None:
    reference = _url(value, GRADIENTS)
    if reference is None and value != "none" and not _is_color(value):
        raise _Invalid(
            "must be none, currentColor, a colour, or url(#id) of a gradient"
        )
    return reference


def _stop_color(value: str):
    if not _is_color(value):
        raise _Invalid("must be a colour or currentColor")


def _clip_path(value: str) -> _Reference | None:
    reference = _url(value, frozenset({"clipPath"}))
    if reference is None and value != "none":
        raise _Invalid("must be none or url(#id) of a clipPath")
    return reference


def _dash_array(value: str):
    if value == "none":
        return
    items = _LIST_SEPARATOR.split(value)
    if not 1 <= len(items) <= MAX_DASHES:
        raise _Invalid(f"must be none or 1 to {MAX_DASHES} lengths")
    for item in items:
        _size(item)


def _font_family(value: str):
    groups: list[list] = [[]]
    for token in _css(tinycss2.parse_component_value_list, value, skip_comments=True):
        if token.type == "literal" and token.value == ",":
            groups.append([])
        elif token.type != "whitespace":
            groups[-1].append(token)
    for group in groups:
        if len(group) == 1 and group[0].type == "string":
            name = group[0].value
        elif group and all(token.type == "ident" for token in group):
            name = " ".join(token.value for token in group)
        else:
            raise _Invalid("must be family names separated by commas")
        if len(name) > MAX_FONT_NAME:
            raise _Invalid(f"a family name is longer than {MAX_FONT_NAME} characters")


_UNITS = _keywords("userSpaceOnUse objectBoundingBox")
_FILL_RULE = _keywords("nonzero evenodd")

# Presentation properties, as attributes or style declarations. Opacities take
# the CSS Color 4 percentage form as well as a number.
PROPERTIES: dict[str, Callable[[str], object]] = {
    "fill": _paint,
    "fill-opacity": _fraction,
    "fill-rule": _FILL_RULE,
    "stroke": _paint,
    "stroke-width": _size,
    "stroke-opacity": _fraction,
    "stroke-linecap": _keywords("butt round square"),
    "stroke-linejoin": _keywords("miter round bevel"),
    "stroke-dasharray": _dash_array,
    "stroke-dashoffset": _length,
    "stroke-miterlimit": _miter_limit,
    "opacity": _fraction,
    "visibility": _keywords("visible hidden collapse"),
    "display": _keywords(
        "inline block list-item run-in compact marker table inline-table"
        " table-row-group table-header-group table-footer-group table-row"
        " table-column-group table-column table-cell table-caption none"
    ),
    "clip-path": _clip_path,
    "clip-rule": _FILL_RULE,
    "stop-color": _stop_color,
    "stop-opacity": _fraction,
    "font-family": _font_family,
    "font-size": _keywords_or(
        "xx-small x-small small medium large x-large xx-large larger smaller", _size
    ),
    "font-weight": _keywords(
        "normal bold bolder lighter 100 200 300 400 500 600 700 800 900"
    ),
    "font-style": _keywords("normal italic oblique"),
    "text-anchor": _keywords("start middle end"),
    "dominant-baseline": _keywords(
        "auto use-script no-change reset-size ideographic alphabetic hanging"
        " mathematical central middle text-after-edge text-before-edge"
    ),
    "letter-spacing": _keywords_or("normal", _length),
}

# Value grammar of the other attributes; id, style and href have their own checks.
ATTRIBUTES: dict[str, Callable[[str], object]] = {
    **dict.fromkeys(
        ("x", "y", "cx", "cy", "x1", "y1", "x2", "y2", "fx", "fy"), _length
    ),
    **dict.fromkeys(("width", "height", "r", "rx", "ry"), _size),
    "points": _points,
    "d": _path,
    "transform": _transform,
    "gradientTransform": _transform,
    "viewBox": _view_box,
    "preserveAspectRatio": _aspect,
    "offset": _fraction,
    "gradientUnits": _UNITS,
    "clipPathUnits": _UNITS,
    "spreadMethod": _keywords("pad reflect repeat"),
    "xml:space": _keywords("default preserve"),
}

# The attributes of each profile element; the keys are the profile element set.
ELEMENT_ATTRIBUTES: dict[str, frozenset[str]] = {
    tag: frozenset(names.split())
    for tag, names in {
        "svg": "viewBox preserveAspectRatio x y width height",
        "g": "transform",
        "defs": "transform",
        "symbol": "",
        "use": "x y width height transform href",
        "path": "d transform",
        "rect": "x y width height rx ry transform",
        "circle": "cx cy r transform",
        "ellipse": "cx cy rx ry transform",
        "line": "x1 y1 x2 y2 transform",
        "polyline": "points transform",
        "polygon": "points transform",
        "text": "x y transform",
        "tspan": "x y",
        "title": "",
        "desc": "",
        "linearGradient": "x1 y1 x2 y2 gradientUnits gradientTransform spreadMethod href",
        "radialGradient": "cx cy r fx fy gradientUnits gradientTransform spreadMethod href",
        "stop": "offset",
        "clipPath": "clipPathUnits transform",
    }.items()
}
COMMON_ATTRIBUTES = frozenset({"id", "style", "xml:space", *PROPERTIES})
_CHECKS = {**ATTRIBUTES, **PROPERTIES}


def _attribute_name(key: str) -> str | None:
    """Return the profile name of an attribute key, or None for a foreign namespace."""
    qname = etree.QName(key)
    if qname.namespace is None:
        return qname.localname
    if qname.namespace == XLINK_NS:
        return "href" if qname.localname == "href" else f"xlink:{qname.localname}"
    if qname.namespace == XML_NS:
        return f"xml:{qname.localname}"
    return None


def _tag(element: etree._Element) -> str:
    return etree.QName(element).localname


def _line_at(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _reference_problem(
    element, reference: _Reference, target, holders: dict
) -> str | None:
    """Return why a reference is not valid, or None."""
    where = f"<{_tag(element)}> reference to #{reference.target}"
    if target is None:
        return f"{where} does not resolve"
    if _tag(target) not in reference.kinds:
        expected = ", ".join(sorted(reference.kinds))
        return f"{where} names a <{_tag(target)}>, not one of {expected}"
    if reference.expands and (target is element or target in element.iterancestors()):
        return f"{where} names its own ancestor"
    if reference.expands and target in holders:
        return f"{where} would draw connector or bay {holders[target]} twice"
    return None


class _Validator:
    """Collects the issues of one face file."""

    def __init__(self, path: str):
        self.path = path
        self.issues: list[Issue] = []
        self.references: list[tuple[etree._Element, _Reference]] = []
        self.ids: dict[str, etree._Element] = {}
        self.markers: dict[etree._Element, str] = {}
        self.use_targets: dict[etree._Element, etree._Element] = {}
        self.expansions: dict[etree._Element, tuple[int, int]] = {}

    def add(self, line: int | None, message: str):
        self.issues.append(Issue(self.path, line, message))
        if len(self.issues) >= MAX_ISSUES:
            raise ArtworkError(self.issues)

    def fail(self, line: int | None, message: str):
        """Stop at a resource bound."""
        self.issues.append(Issue(self.path, line, message))
        raise ArtworkError(self.issues)

    def parse(self, data: bytes) -> etree._Element:
        if len(data) > MAX_SVG_BYTES:
            self.fail(None, f"file is larger than {MAX_SVG_BYTES} bytes")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            self.fail(_line_at(data, error.start), "file must be UTF-8")
        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False
        )
        try:
            root = etree.fromstring(data, parser)
        except etree.XMLSyntaxError as error:
            self.fail(error.lineno, f"invalid XML: {error.msg}")
        docinfo = root.getroottree().docinfo
        if docinfo.encoding.upper() != "UTF-8":
            self.add(1, f"file must be UTF-8, not {docinfo.encoding}")
        if docinfo.doctype or docinfo.internalDTD is not None:
            offset = data.find(b"<!DOCTYPE")
            self.add(
                _line_at(data, offset) if offset >= 0 else 1, "DOCTYPE is not allowed"
            )
        self.bound_size(root)
        return root

    def bound_size(self, root: etree._Element):
        count = depth = 0
        for event, element in etree.iterwalk(root, events=("start", "end")):
            if not isinstance(element.tag, str):
                continue
            if event == "end":
                depth -= 1
                continue
            count += 1
            depth += 1
            if count > MAX_SVG_ELEMENTS:
                self.fail(element.sourceline, f"more than {MAX_SVG_ELEMENTS} elements")
            if depth > MAX_SVG_DEPTH:
                self.fail(element.sourceline, f"nesting depth over {MAX_SVG_DEPTH}")

    def outside(self, root: etree._Element):
        for node in (*root.itersiblings(preceding=True), *root.itersiblings()):
            if node.tag is etree.PI:
                self.add(node.sourceline, "processing instructions are not allowed")

    def element(
        self, element: etree._Element, parent: etree._Element | None, place: _Place
    ):
        qname = etree.QName(element)
        tag = qname.localname
        if qname.namespace != SVG_NS:
            self.add(
                element.sourceline,
                f"<{tag}> element from namespace {qname.namespace} is not allowed",
            )
            return
        if tag not in ELEMENT_ATTRIBUTES:
            self.add(element.sourceline, f"<{tag}> element is not allowed")
            return
        if tag == "svg" and parent is not None:
            self.add(element.sourceline, "<svg> is allowed only as the root element")
            return
        self.namespaces(element, parent)
        self.text(element.text, tag, element.sourceline)
        self.attributes(element, tag)
        ident = element.get("id")
        if ident is not None:
            self.identify(element, tag, ident, place)
        inner = _Place(
            definition=place.definition or (tag if tag in DEFINITIONS else None),
            connector=place.connector
            or (ident if self.markers.get(element, "").startswith("con-") else None),
            plain=place.plain
            and (tag == "svg" or (tag == "g" and element.get("transform") is None)),
        )
        for child in element:
            if child.tag is etree.PI:
                self.add(child.sourceline, "processing instructions are not allowed")
            elif child.tag is etree.Entity:
                self.add(child.sourceline, "entity references are not allowed")
            elif child.tag is not etree.Comment:
                self.element(child, element, inner)
            self.text(child.tail, tag, child.sourceline)

    def namespaces(self, element: etree._Element, parent: etree._Element | None):
        inherited = {} if parent is None else parent.nsmap
        for prefix, uri in element.nsmap.items():
            if uri not in ALLOWED_NAMESPACES and inherited.get(prefix) != uri:
                self.add(element.sourceline, f"namespace {uri} is not allowed")

    def text(self, text: str | None, tag: str, line: int):
        if text and text.strip(_SPACE) and tag not in TEXT_ELEMENTS:
            self.add(line, f"text is not allowed in <{tag}>")

    def identify(self, element: etree._Element, tag: str, ident: str, place: _Place):
        if _ID.fullmatch(ident) is None:
            self.add(
                element.sourceline, f"<{tag}> id {ident!r} must match ^{_ID.pattern}$"
            )
            return
        if ident in self.ids:
            self.add(element.sourceline, f"duplicate id {ident}")
            return
        self.ids[ident] = element
        prefix = ident[:4]
        if prefix not in _MARKER_IDS:
            return
        self.markers[element] = ident
        kind = "connector" if prefix == "con-" else "bay"
        if _MARKER_IDS[prefix].fullmatch(ident) is None:
            pattern = _MARKER_IDS[prefix].pattern
            self.add(element.sourceline, f"{kind} id {ident!r} must match ^{pattern}$")
        if place.connector is not None:
            self.add(
                element.sourceline,
                f"{kind} {ident} is inside connector {place.connector}",
            )
        elif place.definition is not None:
            self.add(
                element.sourceline, f"{kind} {ident} is inside <{place.definition}>"
            )
        if kind == "connector":
            if tag not in CONNECTOR_ELEMENTS:
                allowed = ", ".join(sorted(CONNECTOR_ELEMENTS))
                self.add(
                    element.sourceline,
                    f"connector {ident} must be one of {allowed}, not <{tag}>",
                )
        else:
            self.bay(element, tag, ident, place)

    def bay(self, element: etree._Element, tag: str, ident: str, place: _Place):
        if tag != "rect":
            self.add(element.sourceline, f"bay {ident} must be a <rect>, not <{tag}>")
            return
        for name in ("x", "y", "width", "height"):
            raw = element.get(name)
            if raw is None:
                if name in ("width", "height"):
                    self.add(element.sourceline, f"bay {ident} needs {name}")
            elif _NUMBER.fullmatch(raw.strip(_SPACE)) is None:
                self.add(
                    element.sourceline,
                    f"bay {ident} {name} must be a number in face units, without unit",
                )
            elif name in ("width", "height") and float(raw) < 1:
                self.add(element.sourceline, f"bay {ident} {name} must be at least 1")
        if element.get("transform") is not None:
            self.add(element.sourceline, f"bay {ident} must not have a transform")
        if place.connector is None and place.definition is None and not place.plain:
            self.add(
                element.sourceline,
                f"bay {ident}: every ancestor must be the root svg or a g without transform",
            )

    def attributes(self, element: etree._Element, tag: str):
        allowed = ELEMENT_ATTRIBUTES[tag] | COMMON_ATTRIBUTES
        if (
            element.get("href") is not None
            and element.get(f"{{{XLINK_NS}}}href") is not None
        ):
            self.add(element.sourceline, f"<{tag}> has both href and xlink:href")
        for key, raw in element.attrib.items():
            name = _attribute_name(key)
            value = raw.strip(_SPACE)
            label = f"<{tag}> attribute {name}"
            if name is None:
                self.add(
                    element.sourceline,
                    f"<{tag}> attribute from namespace "
                    f"{etree.QName(key).namespace} is not allowed",
                )
            elif name not in allowed:
                self.add(element.sourceline, f"{label} is not allowed")
            elif name == "style":
                self.style(element, tag, raw)
            elif name == "href":
                self.href(element, tag, raw)
            elif name in _CHECKS:
                self.value(element, label, name, value)

    def href(self, element: etree._Element, tag: str, raw: str):
        match = _HREF.fullmatch(raw)
        if match is None:
            self.add(
                element.sourceline,
                f"<{tag}> attribute href must be #id of an element in the same file",
            )
        elif tag == "use":
            self.references.append((element, _Reference(match[1], USE_TARGETS, True)))
        else:
            self.references.append((element, _Reference(match[1], GRADIENTS)))

    def style(self, element: etree._Element, tag: str, raw: str):
        try:
            declarations = _css(
                tinycss2.parse_declaration_list,
                raw,
                skip_comments=True,
                skip_whitespace=True,
            )
        except _Invalid as error:
            self.add(element.sourceline, f"<{tag}> style: {error}")
            return
        for item in declarations:
            if item.type == "at-rule":
                message = f"at-rule @{item.at_keyword} is not allowed"
            elif item.type != "declaration":
                message = f"invalid declaration ({getattr(item, 'message', item.type)})"
            elif item.important:
                message = f"!important is not allowed on {item.lower_name}"
            elif item.lower_name not in PROPERTIES:
                message = f"property {item.lower_name} is not allowed"
            else:
                try:
                    value = _css(tinycss2.serialize, item.value).strip(_SPACE)
                except _Invalid as error:
                    self.add(element.sourceline, f"<{tag}> style: {error}")
                    continue
                label = f"<{tag}> style {item.lower_name}"
                self.value(element, label, item.lower_name, value)
                continue
            self.add(element.sourceline, f"<{tag}> style: {message}")

    def value(self, element: etree._Element, label: str, name: str, value: str):
        # SVG 1.1 defines inherit for every presentation property.
        if value == "inherit" and name in PROPERTIES:
            return
        try:
            result = _CHECKS[name](value)
        except _Bound as error:
            self.fail(element.sourceline, f"{label}: {error}")
        except _Invalid as error:
            self.add(element.sourceline, f"{label}: {error}")
            return
        if isinstance(result, _Reference):
            self.references.append((element, result))

    def face(self, root: etree._Element) -> Face:
        self.outside(root)
        if root.tag != f"{{{SVG_NS}}}svg":
            self.fail(root.sourceline, "root element must be an SVG svg element")
        self.element(root, None, _Place())
        if root.get("viewBox") is None:
            self.add(root.sourceline, "root svg needs a viewBox")
        if not self.resolve():
            self.expand(root, 0)
        if self.issues:
            raise ArtworkError(self.issues)
        return Face(
            view_box=_view_box(root.get("viewBox").strip(_SPACE)),
            connector_ids=frozenset(i for i in self.ids if i.startswith("con-")),
            bay_ids=frozenset(i for i in self.ids if i.startswith("bay-")),
            id_lines={
                i: e.sourceline for i, e in self.ids.items() if i[:4] in _MARKER_IDS
            },
        )

    def resolve(self) -> bool:
        """Check every reference; return True when the references form a cycle."""
        holders = {}  # each element that holds a connector or bay, and that id
        for marker, ident in self.markers.items():
            for holder in (marker, *marker.iterancestors()):
                holders.setdefault(holder, ident)
        valid = []
        for element, reference in self.references:
            target = self.ids.get(reference.target)
            problem = _reference_problem(element, reference, target, holders)
            if problem is not None:
                self.add(element.sourceline, problem)
                continue
            valid.append((element, target))
            if reference.expands:
                self.use_targets[element] = target
        targets = {target for _, target in valid}
        edges: dict[etree._Element, list] = {}
        for element, target in valid:
            for holder in (element, *element.iterancestors()):
                if holder in targets:
                    edges.setdefault(holder, []).append((target, element))
        return self.cycles(edges)

    def cycles(self, edges: dict) -> bool:
        """Report each reference that closes a cycle; iterative depth-first search."""
        done: set = set()
        found = False
        for start, start_edges in edges.items():
            if start in done:
                continue
            path, stack = [start], [iter(start_edges)]
            while stack:
                for target, via in stack[-1]:
                    if target in path:
                        found = True
                        self.add(
                            via.sourceline,
                            f"<{_tag(via)}> reference to #{target.get('id')} "
                            "forms a cycle",
                        )
                    elif target not in done:
                        path.append(target)
                        stack.append(iter(edges.get(target, ())))
                        break
                else:
                    done.add(path.pop())
                    stack.pop()
        return found

    def expand(self, element: etree._Element, nesting: int) -> tuple[int, int]:
        """Return the node count and use depth of element with every use expanded."""
        known = self.expansions.get(element)
        if known is None:
            size, depth = 1, 0
            for child in element.iterchildren(etree.Element):
                child_size, child_depth = self.expand(child, nesting)
                size, depth = size + child_size, max(depth, child_depth)
                self.bound_expansion(element, nesting, size, depth)
            target = self.use_targets.get(element)
            if target is not None:
                self.bound_expansion(element, nesting, size, 1)
                target_size, target_depth = self.expand(target, nesting + 1)
                size, depth = size + target_size, max(depth, target_depth + 1)
            known = self.expansions[element] = (size, depth)
        self.bound_expansion(element, nesting, *known)
        return known

    def bound_expansion(self, element: etree._Element, nesting: int, size, depth):
        if size > MAX_USE_EXPANSION_NODES:
            self.fail(
                element.sourceline,
                f"use expansion makes more than {MAX_USE_EXPANSION_NODES} nodes",
            )
        if nesting + depth > MAX_USE_DEPTH:
            self.fail(element.sourceline, f"use nesting is deeper than {MAX_USE_DEPTH}")


def parse_face(path: str, data: bytes) -> Face:
    """Return the facts of one face file, or raise ArtworkError with every issue."""
    validator = _Validator(path)
    return validator.face(validator.parse(data))
