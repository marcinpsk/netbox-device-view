"""Tests for the bounded SVG profile of imported artwork faces (no database)."""

import time

import pytest

from netbox_device_view.artwork.errors import ArtworkError
from netbox_device_view.artwork.limits import (
    MAX_ISSUES,
    MAX_PATH_DATA_BYTES,
    MAX_SVG_BYTES,
    MAX_SVG_DEPTH,
    MAX_SVG_ELEMENTS,
    MAX_TRANSFORM_BYTES,
    MAX_USE_DEPTH,
    MAX_USE_EXPANSION_NODES,
)
from netbox_device_view.artwork.svg import Face, parse_face

PATH = "faces/front.svg"
OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
OPEN_XLINK = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 50">'
)


def svg(body: str, opening: str = OPEN) -> bytes:
    return f"{opening}\n{body}\n</svg>".encode()


def issues(data: bytes) -> list:
    with pytest.raises(ArtworkError) as caught:
        parse_face(PATH, data)
    assert all(issue.path == PATH for issue in caught.value.issues)
    return caught.value.issues


def rejected(data: bytes, match: str, line: int | None = None):
    found = issues(data)
    hits = [issue for issue in found if match in issue.message]
    assert hits, f"no issue matches {match!r}: {found}"
    if line is not None:
        assert hits[0].line == line, hits[0]


# Parsing and resource bounds.


def test_minimal_face():
    assert parse_face(PATH, svg("")) == Face(
        view_box=(0.0, 0.0, 100.0, 50.0),
        connector_ids=frozenset(),
        bay_ids=frozenset(),
        id_lines={},
    )


def test_xml_declaration_comments_and_utf8_text_pass():
    data = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!-- front -->\n'
        + svg("<!-- c --><text>Zaśłon</text>").decode()
    ).encode()
    assert parse_face(PATH, data).view_box == (0.0, 0.0, 100.0, 50.0)


def test_size_bound_fails_before_parsing():
    found = issues(b" " * (MAX_SVG_BYTES + 1))
    assert len(found) == 1
    assert found[0].line is None
    assert "bytes" in found[0].message


def test_size_at_bound_is_parsed():
    body = svg("")
    data = body + b" " * (MAX_SVG_BYTES - len(body))
    assert parse_face(PATH, data).view_box[2] == 100.0


def test_syntax_error_reports_line():
    rejected(svg("<g>\n<rect></g>"), "XML", line=3)


@pytest.mark.parametrize(
    "data, match, line",
    [
        (
            b'<!DOCTYPE svg [<!ENTITY a "b">]>\n' + svg("<text>&a;</text>"),
            "DOCTYPE",
            1,
        ),
        (
            b"<?xml version='1.0'?>\n<!DOCTYPE svg SYSTEM 'http://x/svg.dtd'>\n"
            + svg(""),
            "DOCTYPE",
            2,
        ),
        (
            b'<!DOCTYPE svg [<!ENTITY a "b">]>\n' + svg("<text>&a;</text>"),
            "entity",
            3,
        ),
        (svg("<text>&a;</text>"), "XML", None),
        (b'<?xml-stylesheet href="x.css"?>\n' + svg(""), "processing", 1),
        (svg("<g>\n<?php echo 1 ?></g>"), "processing", 3),
        (
            b"<?xml version='1.0' encoding='ISO-8859-1'?>\n" + svg(""),
            "UTF-8",
            1,
        ),
        ("<svg/>".encode("utf-16"), "UTF-8", 1),
        (svg("<text>\xff</text>").replace(b"\xc3\xbf", b"\xff"), "UTF-8", 2),
    ],
)
def test_document_level_rejections(data, match, line):
    rejected(data, match, line)


def test_element_count_bound():
    many = svg("<g/>" * MAX_SVG_ELEMENTS)
    found = issues(many)
    assert len(found) == 1
    assert "elements" in found[0].message
    parse_face(PATH, svg("<g/>" * (MAX_SVG_ELEMENTS - 1)))


def test_depth_bound():
    def nested(depth):
        return svg("<g>" * (depth - 1) + "</g>" * (depth - 1))

    parse_face(PATH, nested(MAX_SVG_DEPTH))
    found = issues(nested(MAX_SVG_DEPTH + 1))
    assert len(found) == 1
    assert "depth" in found[0].message


def test_issue_count_is_bounded():
    found = issues(svg('<rect class="a"/>\n' * (MAX_ISSUES + 10)))
    assert len(found) == MAX_ISSUES


# Namespaces, elements, attributes and text.


@pytest.mark.parametrize(
    "data, match, line",
    [
        (svg("<g/>", '<svg viewBox="0 0 100 50">'), "root element", 1),
        (svg("<svg viewBox='0 0 1 1'/>"), "<svg>", 2),
        (svg("\n<script>alert(1)</script>"), "<script>", 3),
        (svg("<foreignObject><p>x</p></foreignObject>"), "<foreignObject>", 2),
        (svg("<style>rect{fill:red}</style>"), "<style>", 2),
        (svg('<image href="x.png"/>'), "<image>", 2),
        (svg('<a href="#x"><rect/></a>'), "<a>", 2),
        (svg("<mask/>"), "<mask>", 2),
        (svg("<filter/>"), "<filter>", 2),
        (svg("<pattern/>"), "<pattern>", 2),
        (svg("<marker/>"), "<marker>", 2),
        (svg('<animate attributeName="x"/>'), "<animate>", 2),
        (svg("<set/>"), "<set>", 2),
        (
            svg(
                '<i:x xmlns:i="http://www.inkscape.org/namespaces/inkscape"/>',
            ),
            "namespace",
            2,
        ),
        (
            svg(
                "",
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/'
                'sodipodi-0.dtd" viewBox="0 0 100 50">',
            ),
            "namespace",
            1,
        ),
        (
            svg(
                '<g xmlns:i="http://www.inkscape.org/namespaces/inkscape" '
                'i:label="x"/>'
            ),
            "namespace",
            2,
        ),
        (svg('<rect onclick="alert(1)"/>'), "onclick", 2),
        (
            svg('<svg:rect xmlns:svg="http://www.w3.org/2000/svg" onload="x"/>'),
            "onload",
            2,
        ),
        (svg('<rect class="a"/>'), "class", 2),
        (svg('<rect d="M0 0"/>'), "attribute d", 2),
        (svg('<g x="1"/>'), "attribute x", 2),
        (svg('<symbol viewBox="0 0 1 1"/>'), "attribute viewBox", 2),
        (svg('<rect href="#a" id="a"/>'), "attribute href", 2),
        (svg('<g xml:lang="en"/>'), "xml:lang", 2),
        (svg('<use xlink:title="x"/>', OPEN_XLINK), "xlink", 2),
        (svg("<g>text</g>"), "text", 2),
        (svg("<g><rect/>tail</g>"), "text", 2),
        (svg("<g><!-- c -->tail</g>"), "text", 2),
        (svg("<rect>x</rect>"), "text", 2),
        (svg('<g version="1.1"/>'), "attribute version", 2),
    ],
)
def test_structure_rejections(data, match, line):
    rejected(data, match, line)


@pytest.mark.parametrize(
    "body",
    [
        "<g><rect/></g>",
        "<text>Port <tspan>1</tspan> tail</text>",
        "<title>Front</title><desc>Panel</desc>",
        '<g xml:space="preserve"><text xml:space="default">a</text></g>',
        "<defs><symbol><rect/></symbol><clipPath><rect/></clipPath></defs>",
        "<linearGradient><stop/></linearGradient><radialGradient/>",
        "<path/><circle/><ellipse/><line/><polyline/><polygon/><use/>",
        "\n  <g>\n    <rect/>\n  </g>\n",
    ],
)
def test_structure_passes(body):
    parse_face(PATH, svg(body, OPEN_XLINK))


# Value grammar.

GRADIENT = '<linearGradient id="g"/><clipPath id="c"/>'


@pytest.mark.parametrize(
    "opening, match",
    [
        ('<svg xmlns="http://www.w3.org/2000/svg">', "viewBox"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100">', "four"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50 1">', "four"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0.5 50">', "width"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 5001 50">', "width"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 -5">', "width"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 5e400">', "number"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0,0,100,">', "number"),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1mm 1">', "viewBox"),
        (
            (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9" '
                'preserveAspectRatio="xMidYMid cover">'
            ),
            "preserveAspectRatio",
        ),
        (
            (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9" '
                'width="10cm">'
            ),
            "width",
        ),
    ],
)
def test_root_rejections(opening, match):
    rejected(svg("", opening), match, line=1)


@pytest.mark.parametrize(
    "opening",
    [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0,0,1,5000">',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox=" -10 -1e1 .5e1 1E3 ">',
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9" '
            'preserveAspectRatio="defer xMinYMax slice" width="90mm" height="100%">'
        ),
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9" '
            'preserveAspectRatio="none">'
        ),
    ],
)
def test_root_passes(opening):
    parse_face(PATH, svg("", opening))


BAD_VALUES = [
    ('<rect x="1e400"/>', "outside"),
    ('<rect x="100001"/>', "outside"),
    ('<rect x="NaN"/>', "attribute x"),
    ('<rect x="inf"/>', "attribute x"),
    ('<rect x="5em"/>', "attribute x"),
    ('<rect x=""/>', "attribute x"),
    ('<rect x="1 2"/>', "attribute x"),
    ('<rect width="-1"/>', "non-negative"),
    ('<circle r="-0.5"/>', "non-negative"),
    ('<rect rx="-1mm"/>', "non-negative"),
    ('<polygon points="0 0 1"/>', "points"),
    ('<polygon points="0 0"/>', "points"),
    ('<polyline points="0,0 1,1,"/>', "points"),
    ('<path d=""/>', "M or m"),
    ('<path d="L 0 0"/>', "M or m"),
    ('<path d="M 0 0 X 5"/>', "command X"),
    ('<path d="M 0 0 L 1e400 0"/>', "outside"),
    ('<path d="M 0"/>', "number"),
    ('<path d="M 0 0 L 1 1,"/>', "comma"),
    ('<path d="M 0 0 C 1 1 2 2 3"/>', "number"),
    ('<path d="M 0 0 a1 1 0 2 0 1 1"/>', "flag"),
    ('<path d="M 0 0 a-1 1 0 0 0 1 1"/>', "non-negative"),
    ('<path d="M 0 0 Z 1 1"/>', "command 1"),
    ('<path d="M 0 0 L 1 1 #"/>', "command #"),
    ('<path d="M 0 0 L 1e 1"/>', "number"),
    ('<path d="M 0,,0"/>', "number"),
    ('<g transform="rotate(1 2)"/>', "rotate"),
    ('<g transform="matrix(1 0 0 1 0)"/>', "matrix"),
    ('<g transform="translate()"/>', "number"),
    ('<g transform="skewX(1, 2)"/>', "skewX"),
    ('<g transform="scale(1"/>', ")"),
    ('<g transform="translate(1) scale(2),"/>', "end"),
    ('<g transform="translate(1)scale(2)"/>', "separate"),
    ('<g transform="perspective(1)"/>', "perspective"),
    ('<g transform="translate(1e400)"/>', "outside"),
    ('<g transform="translate(1,)"/>', "comma"),
    ('<linearGradient gradientTransform="rotate(x)"/>', "gradientTransform"),
    ('<rect fill="url(http://evil/x)"/>', "fill"),
    ('<rect fill="url(#g) red"/>', "fill"),
    ('<rect fill="javascript:alert(1)"/>', "fill"),
    ('<rect fill="bluish"/>', "fill"),
    ('<rect stroke="url(&quot;#g&quot;)"/>', "stroke"),
    ('<rect clip-path="inset(1px)"/>', "clip-path"),
    ('<stop stop-color="none"/>', "stop-color"),
    ('<rect opacity="1.5"/>', "opacity"),
    ('<rect fill-opacity="-0.1"/>', "fill-opacity"),
    ('<rect stroke-opacity="101%"/>', "stroke-opacity"),
    ('<stop offset="1.1"/>', "offset"),
    ('<stop offset="120%"/>', "offset"),
    ('<rect stroke-miterlimit="0.5"/>', "stroke-miterlimit"),
    ('<rect stroke-width="-1"/>', "stroke-width"),
    ('<rect stroke-dasharray="1 -2"/>', "stroke-dasharray"),
    ('<rect stroke-dasharray=""/>', "stroke-dasharray"),
    ('<rect stroke-dasharray="' + " ".join(["1"] * 33) + '"/>', "stroke-dasharray"),
    ('<rect fill-rule="odd"/>', "fill-rule"),
    ('<rect stroke-linecap="flat"/>', "stroke-linecap"),
    ('<rect stroke-linejoin="arc"/>', "stroke-linejoin"),
    ('<rect visibility="gone"/>', "visibility"),
    ('<rect display="flex"/>', "display"),
    ('<text font-weight="950">x</text>', "font-weight"),
    ('<text font-style="slanted">x</text>', "font-style"),
    ('<text text-anchor="left">x</text>', "text-anchor"),
    ('<text dominant-baseline="top">x</text>', "dominant-baseline"),
    ('<text font-size="big">x</text>', "font-size"),
    ('<text letter-spacing="wide">x</text>', "letter-spacing"),
    ('<text font-family="">x</text>', "font-family"),
    ('<text font-family="Arial,">x</text>', "font-family"),
    ('<text font-family="' + "a" * 65 + '">x</text>', "font-family"),
    ('<text font-family="url(x)">x</text>', "font-family"),
    ('<linearGradient gradientUnits="screen"/>', "gradientUnits"),
    ('<clipPath clipPathUnits="px"/>', "clipPathUnits"),
    ('<linearGradient spreadMethod="wrap"/>', "spreadMethod"),
    ('<g xml:space="keep"/>', "xml:space"),
    ('<rect style="fill: url(http://evil/x)"/>', "fill"),
    ('<rect style="@import url(x.css)"/>', "at-rule"),
    ('<rect style="fill: red !important"/>', "important"),
    ('<rect style="behavior: url(x.htc)"/>', "behavior"),
    ('<rect style="transform: scale(2)"/>', "transform"),
    ('<rect style="fill red"/>', "style"),
    ('<rect style="fill: bluish"/>', "fill"),
    ('<rect style="stroke-width: -1px"/>', "stroke-width"),
    ('<rect style="fill: url(#g); {}"/>', "style"),
]


@pytest.mark.parametrize("body, match", BAD_VALUES)
def test_value_rejections(body, match):
    rejected(svg(GRADIENT + "\n" + body), match, line=3)


GOOD_VALUES = [
    '<rect x="-1.5" y="+.5e1" width="10mm" height="50%" rx="0" ry="1px"/>',
    '<rect x=" 1 "/>',
    '<circle cx="1" cy="2" r="3"/><ellipse cx="1" cy="1" rx="1" ry="2"/>',
    '<line x1="0" y1="0" x2="1e2" y2="1E-2"/>',
    '<polygon points="0,0 1,1 2,0"/><polyline points="0 0,1 1 -1-1"/>',
    '<path d="M0 0L1 1H2V3C1 1 2 2 3 3S1 1 2 2Q1 1 2 2T5 5A1 1 0 1 0 9 9Z"/>',
    '<path d="m0,0l1,1 2,2h1v1c1,1,2,2,3,3s1,1,2,2q1,1,2,2t5,5z m1 1"/>',
    '<path d="M0 0 a1 1 0 00 1 1"/>',
    '<path d="M0 0a1 1 0 1,1 1 1 2 2 0 0110 10"/>',
    '<path d="M.5.5.5.5L-1-1"/>',
    '<path d="  M 0 0  z  "/>',
    '<path d="M0 0 1 1 2 2"/>',
    '<g transform="translate(1) scale(2, 3) rotate(45 1 1)"/>',
    '<g transform=" matrix(1,0,0,1,0,0),skewX(10) skewY( -5 ) "/>',
    '<g transform="translate(1 2),,scale(1)"/>',
    '<g transform=""/>',
    (
        '<linearGradient gradientTransform="rotate(10)" gradientUnits="userSpaceOnUse"'
        ' spreadMethod="reflect" x1="0" y1="0" x2="100%" y2="0"/>'
    ),
    (
        '<radialGradient cx="50%" cy="50%" r="50%" fx="1" fy="1"'
        ' gradientUnits="objectBoundingBox"/>'
    ),
    '<stop offset="0.5" stop-color="#0a0" stop-opacity="0.3"/>',
    '<stop offset="50%" stop-color="currentColor"/>',
    '<rect fill="none" stroke="currentColor"/>',
    '<rect fill="url(#g)" stroke="rgb(0 0 0 / 50%)" clip-path="url(#c)"/>',
    '<rect fill="#abc" stroke="hsl(120 50% 50%)" clip-path="none"/>',
    '<rect opacity="0" fill-opacity="1" stroke-opacity="50%"/>',
    '<rect stroke-width="0.5" stroke-dasharray="none" stroke-dashoffset="-1"/>',
    '<rect stroke-dasharray="1, 2 3mm" stroke-miterlimit="1"/>',
    '<rect stroke-dasharray="' + " ".join(["1"] * 32) + '"/>',
    (
        '<rect fill-rule="evenodd" clip-rule="nonzero" stroke-linecap="round"'
        ' stroke-linejoin="bevel" visibility="hidden" display="none"/>'
    ),
    '<rect fill="inherit" display="inline"/>',
    (
        '<text font-family="sans-serif" font-size="3.5" font-weight="bold"'
        ' font-style="italic" text-anchor="middle" dominant-baseline="central"'
        ' letter-spacing="normal">x</text>'
    ),
    (
        "<text font-family=\"'DejaVu Sans', Liberation Mono, monospace\""
        ' font-size="small" font-weight="700" letter-spacing="0.2mm">x</text>'
    ),
    '<rect style="fill: url(#g); stroke: #000; stroke-width: 0.5px"/>',
    '<rect style="fill:red;;/* note */stroke:none;"/>',
    '<rect style=""/>',
    '<clipPath clipPathUnits="objectBoundingBox" transform="scale(2)"/>',
    '<use x="1" y="1" width="2" height="2" transform="translate(1)"/>',
]


@pytest.mark.parametrize("body", GOOD_VALUES)
def test_value_passes(body):
    parse_face(PATH, svg(GRADIENT + "\n" + body))


def test_path_data_bound_fails_at_once():
    long_path = "M0 0" + " L1 1" * (MAX_PATH_DATA_BYTES // 5)
    found = issues(
        svg(f'<rect class="a"/>\n<path d="{long_path}"/>\n<rect class="b"/>')
    )
    assert [issue.line for issue in found] == [2, 3]
    assert "longer than" in found[-1].message
    at_bound = "M0 0" + " L1 1" * ((MAX_PATH_DATA_BYTES - 4) // 5)
    parse_face(PATH, svg(f'<path d="{at_bound}"/>'))


def test_transform_bound_fails_at_once():
    long_transform = " ".join(["scale(1)"] * (MAX_TRANSFORM_BYTES // 9 + 1))
    found = issues(svg(f'<g transform="{long_transform}"/>\n<rect class="b"/>'))
    assert len(found) == 1
    assert "longer than" in found[0].message
    at_bound = " ".join(["scale(1)"] * (MAX_TRANSFORM_BYTES // 9))
    parse_face(PATH, svg(f'<g transform="{at_bound}"/>'))


# Ids and references.


@pytest.mark.parametrize(
    "body, match, line",
    [
        ('<g id="1a"/>', "id", 2),
        ('<g id="a b"/>', "id", 2),
        ('<g id=""/>', "id", 2),
        ('<g id="a:b"/>', "id", 2),
        ('<g id="' + "a" * 129 + '"/>', "id", 2),
        ('<g id="a"/>\n<rect id="a"/>', "duplicate", 3),
        ('<rect id="con-A"/>', "connector id", 2),
        ('<rect id="con-"/>', "connector id", 2),
        ('<rect id="con-a_b"/>', "connector id", 2),
        ('<rect id="con-' + "a" * 61 + '"/>', "connector id", 2),
        ('<rect id="bay-X" width="1" height="1"/>', "bay id", 2),
        ('<use href="javascript:alert(1)"/>', "href", 2),
        ('<use xlink:href="http://evil/x.svg#a"/>', "href", 2),
        ('<use href="a"/><rect id="a"/>', "href", 2),
        ('<use href="#"/>', "href", 2),
        ('<rect id="a"/>\n<use href="#a" xlink:href="#a"/>', "both", 3),
        ('<use href="#missing"/>', "#missing", 2),
        ('<rect fill="url(#missing)"/>', "#missing", 2),
        ('<rect style="stroke: url(#missing)"/>', "#missing", 2),
        ('<rect clip-path="url(#missing)"/>', "#missing", 2),
        ('<linearGradient href="#missing"/>', "#missing", 2),
        ('<linearGradient id="g"/>\n<use href="#g"/>', "<linearGradient>", 3),
        ('<defs id="d"/>\n<use href="#d"/>', "<defs>", 3),
        ('<clipPath id="c"/>\n<use href="#c"/>', "<clipPath>", 3),
        ('<g><stop id="s"/></g>\n<use href="#s"/>', "<stop>", 3),
        ('<rect id="r"/>\n<linearGradient href="#r"/>', "<rect>", 3),
        ('<clipPath id="c"/>\n<rect fill="url(#c)"/>', "<clipPath>", 3),
        (
            '<linearGradient id="g"/>\n<rect clip-path="url(#g)"/>',
            "<linearGradient>",
            3,
        ),
        ('<g id="a">\n<use href="#a"/></g>', "ancestor", 3),
        ('<symbol id="a"><g>\n<use href="#a"/></g></symbol>', "ancestor", 3),
    ],
)
def test_reference_rejections(body, match, line):
    rejected(svg(body, OPEN_XLINK), match, line)


@pytest.mark.parametrize(
    "body, line",
    [
        (
            (
                '<symbol id="a"><use href="#b"/></symbol>\n'
                '<symbol id="b"><use href="#a"/></symbol>'
            ),
            3,
        ),
        (
            (
                '<linearGradient id="g1" href="#g2"/>\n'
                '<radialGradient id="g2" xlink:href="#g1"/>'
            ),
            3,
        ),
        ('<linearGradient id="g" href="#g"/>', 2),
        ('<clipPath id="c"><rect clip-path="url(#c)"/></clipPath>', 2),
        (
            (
                '<symbol id="s"><rect clip-path="url(#c)"/></symbol>\n'
                '<clipPath id="c"><use href="#s"/></clipPath>'
            ),
            3,
        ),
        (
            (
                '<g id="a"><rect clip-path="url(#c)"/></g>\n'
                '<clipPath id="c"><use href="#a"/></clipPath>'
            ),
            3,
        ),
    ],
)
def test_reference_cycles(body, line):
    rejected(svg(body, OPEN_XLINK), "cycle", line)


def fan_out(levels: int, width: int = 10) -> str:
    symbols = ['<symbol id="s0"><rect/></symbol>']
    for level in range(1, levels + 1):
        uses = f'<use href="#s{level - 1}"/>' * width
        symbols.append(f'<symbol id="s{level}">{uses}</symbol>')
    return "\n".join(symbols) + f'\n<use href="#s{levels}"/>'


def chain(uses: int) -> str:
    symbols = ['<symbol id="s0"><rect/></symbol>']
    for level in range(1, uses):
        symbols.append(f'<symbol id="s{level}"><use href="#s{level - 1}"/></symbol>')
    return "\n".join(symbols) + f'\n<use href="#s{uses - 1}"/>'


def test_use_expansion_bound_stops_a_fan_out():
    found = issues(svg(fan_out(levels=30)))
    assert len(found) == 1
    assert str(MAX_USE_EXPANSION_NODES) in found[0].message
    parse_face(PATH, svg(fan_out(levels=3)))


def test_use_depth_bound():
    parse_face(PATH, svg(chain(MAX_USE_DEPTH)))
    found = issues(svg(chain(MAX_USE_DEPTH + 1)))
    assert len(found) == 1
    assert "nesting" in found[0].message


@pytest.mark.parametrize(
    "body, match, line",
    [
        ('<g id="grp"><rect id="con-a"/></g>\n<use href="#grp"/>', "con-a", 3),
        ('<rect id="con-a"/>\n<use href="#con-a"/>', "con-a", 3),
        (
            '<g id="grp"><rect id="bay-a" width="1" height="1"/></g>\n<use href="#grp"/>',
            "bay-a",
            3,
        ),
    ],
)
def test_use_must_not_copy_connectors_or_bays(body, match, line):
    rejected(svg(body), match, line)


# Connector and bay placement.


@pytest.mark.parametrize(
    "body, match, line",
    [
        ('<text><tspan id="con-a">1</tspan></text>', "<tspan>", 2),
        ('<linearGradient id="con-a"/>', "<linearGradient>", 2),
        ('<defs>\n<rect id="con-a"/></defs>', "<defs>", 3),
        ('<symbol>\n<rect id="con-a"/></symbol>', "<symbol>", 3),
        ('<clipPath>\n<rect id="con-a"/></clipPath>', "<clipPath>", 3),
        (
            '<linearGradient><g>\n<rect id="con-a"/></g></linearGradient>',
            "<linearGradient>",
            3,
        ),
        ('<g id="con-a">\n<rect id="con-b"/></g>', "con-a", 3),
        ('<circle id="bay-a"/>', "<rect>", 2),
        ('<rect id="bay-a" x="1mm" width="1" height="1"/>', "x", 2),
        ('<rect id="bay-a" y="10%" width="1" height="1"/>', "y", 2),
        ('<rect id="bay-a" width="1px" height="1"/>', "width", 2),
        ('<rect id="bay-a" width="0.5" height="1"/>', "width", 2),
        ('<rect id="bay-a" width="1"/>', "height", 2),
        ('<rect id="bay-a" height="1"/>', "width", 2),
        (
            '<rect id="bay-a" width="1" height="1" transform="scale(2)"/>',
            "transform",
            2,
        ),
        (
            '<g transform="translate(1)">\n<rect id="bay-a" width="1" height="1"/></g>',
            "ancestor",
            3,
        ),
        ('<defs>\n<rect id="bay-a" width="1" height="1"/></defs>', "<defs>", 3),
        ('<symbol>\n<rect id="bay-a" width="1" height="1"/></symbol>', "<symbol>", 3),
        (
            '<clipPath>\n<rect id="bay-a" width="1" height="1"/></clipPath>',
            "<clipPath>",
            3,
        ),
        ('<g id="con-a">\n<rect id="bay-a" width="1" height="1"/></g>', "con-a", 3),
        ('<text>\n<rect id="bay-a" width="1" height="1"/></text>', "ancestor", 3),
    ],
)
def test_connector_and_bay_rejections(body, match, line):
    rejected(svg(body), match, line)


REALISTIC_FACE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!-- 1U switch, front -->
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     viewBox="0 0 482.6 44.45" width="482.6mm" height="44.45mm">
  <title>Example switch front</title>
  <defs>
    <linearGradient id="panel-shade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3a3a3a"/>
      <stop offset="100%" style="stop-color: #1e1e1e; stop-opacity: 1"/>
    </linearGradient>
    <linearGradient id="panel-shade-dim" href="#panel-shade" spreadMethod="pad"/>
    <clipPath id="panel-clip">
      <rect x="0" y="0" width="482.6" height="44.45" rx="2"/>
    </clipPath>
    <symbol id="rj45">
      <path d="M0 0h12v9h-3v2h-6v-2h-3z" fill="#111" stroke="#888"
            stroke-width="0.3"/>
    </symbol>
  </defs>
  <g clip-path="url(#panel-clip)">
    <rect width="482.6" height="44.45" fill="url(#panel-shade)"/>
  </g>
  <g id="ports" transform="translate(20 10)">
    <use id="con-eth1" href="#rj45" x="0" y="0"/>
    <use id="con-eth2" xlink:href="#rj45" x="14" y="0"/>
    <g id="con-console">
      <rect x="40" y="0" width="12" height="9" fill="url(#panel-shade-dim)"/>
      <text x="46" y="14" font-family="'DejaVu Sans', sans-serif" font-size="3"
            text-anchor="middle">CON</text>
    </g>
  </g>
  <g>
    <rect id="bay-psu1" x="400" y="5" width="70" height="34.45"
          fill="none" stroke="#666" stroke-dasharray="2 1"/>
  </g>
  <rect id="bay-fan" width="10" height="10" fill="none"/>
  <circle id="con-power" cx="380" cy="22" r="4" fill="none" stroke="#ccc"/>
</svg>
"""


def test_realistic_face():
    assert parse_face(PATH, REALISTIC_FACE.encode()) == Face(
        view_box=(0.0, 0.0, 482.6, 44.45),
        connector_ids=frozenset({"con-eth1", "con-eth2", "con-console", "con-power"}),
        bay_ids=frozenset({"bay-psu1", "bay-fan"}),
        id_lines={
            "con-eth1": 24,
            "con-eth2": 25,
            "con-console": 26,
            "bay-psu1": 34,  # libxml2 gives the line where the start tag ends
            "bay-fan": 36,
            "con-power": 37,
        },
    )


def test_issues_are_collected_across_the_file():
    body = '<rect class="a"/>\n<script/>\n<use href="#missing"/>\n<g id="1"/>'
    assert [issue.line for issue in issues(svg(body))] == [2, 3, 5, 4]


# Review round 1: inputs that took super-linear time or crashed the validator.


@pytest.mark.parametrize("attribute", ["width", "x", "stroke-width", "opacity"])
def test_a_long_invalid_number_fails_in_linear_time(attribute):
    started = time.monotonic()
    rejected(svg(f'<rect {attribute}="{"1" * 50_000}x" height="1"/>'), attribute)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    "attribute",
    ['style="fill:', 'style="font-family:', 'fill="', 'font-family="'],
)
def test_deeply_nested_css_blocks_fail_with_an_issue(attribute):
    rejected(svg(f'<rect {attribute}{"(" * 5000}" width="1" height="1"/>'), "")


@pytest.mark.parametrize(
    "style",
    [
        "fill:" + "/*)*/f(" * 3000 + "0",
        "fill:'" + ")" * 50 + "'" + "f(" * 3000,
        "fill:" + "[(" * 3000,
    ],
)
def test_css_nesting_cannot_hide_behind_comments_strings_or_mixed_brackets(style):
    rejected(svg(f'<rect style="{style}" width="1" height="1"/>'), "")


def test_an_unlisted_marker_id_keeps_its_line():
    face = parse_face(
        PATH,
        svg('<rect width="1" height="1"/>\n<rect id="con-x" width="1" height="1"/>'),
    )
    assert face.id_lines["con-x"] == 3
