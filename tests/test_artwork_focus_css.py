"""Each focusable element of the artwork shows its focus in every browser.

WebKit paints no outline on an SVG element, so a focusable SVG element also shows
a focus ring element. These are pure unit tests: no Django, no DB.
"""

import re
from pathlib import Path

import tinycss2

CSS = (
    Path(__file__).resolve().parents[1]
    / "netbox_device_view/static/netbox_device_view/css/device_view_artwork.css"
)
# The focusable SVG elements: each shows an outline and its .dv-art-focus-ring child or later sibling.
SVG_CLASSES = ("dv-art-connector", "dv-art-module-body")
# The focusable HTML elements: an outline shows in every browser.
HTML_CLASSES = ("dv-art-viewport",)
_FOCUSED = re.compile(r"\.([\w-]+):focus-visible")


def rules():
    """Return (selector, {property: value}) for each selector of each top-level rule."""
    found = []
    for rule in tinycss2.parse_stylesheet(
        CSS.read_text(), skip_comments=True, skip_whitespace=True
    ):
        if rule.type == "at-rule":
            # A focus rule in an at-rule would not be checked.
            assert ":focus-visible" not in tinycss2.serialize(rule.content or [])
            continue
        declarations = {
            declaration.lower_name: tinycss2.serialize(declaration.value).strip()
            for declaration in tinycss2.parse_declaration_list(
                rule.content, skip_comments=True, skip_whitespace=True
            )
            if declaration.type == "declaration"
        }
        for selector in tinycss2.serialize(rule.prelude).split(","):
            found.append((" ".join(selector.split()), declarations))
    return found


def test_every_focus_rule_names_a_listed_class():
    for selector, _ in rules():
        if ":focus-visible" in selector:
            classes = _FOCUSED.findall(selector)
            assert classes, selector
            assert set(classes) <= {*SVG_CLASSES, *HTML_CLASSES}, selector


def test_each_focusable_svg_element_shows_its_focus_ring():
    found = dict(rules())
    assert found[".dv-art-focus-ring"].get("display") == "none"
    for name in SVG_CLASSES:
        assert any(
            re.fullmatch(rf"\.{name}:focus-visible [>~] \.dv-art-focus-ring", selector)
            and declarations.get("display") == "inline"
            for selector, declarations in found.items()
        ), name


def test_each_focusable_svg_element_has_an_outline():
    # Blink and Gecko paint the outline; WebKit shows only the ring.
    found = dict(rules())
    for name in SVG_CLASSES:
        outline = found.get(f".{name}:focus-visible", {}).get("outline", "none")
        assert outline != "none", name
