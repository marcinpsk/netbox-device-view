"""
Tests for the strict parser rules that the Data Source import applies.

These are pure unit tests: no Django, no DB, no NetBox required.
"""

import copy
import time
from pathlib import Path

import pytest
import yaml

from netbox_device_view.layout.parser import (
    MAX_CANVAS_CELLS,
    MAX_EXPANDED_ELEMENTS,
    LayoutParseError,
    parse,
)

EXAMPLES = sorted(
    (Path(__file__).resolve().parents[1] / "examples" / "yaml").glob("*/*.yaml")
)

BASE = {
    "version": 1,
    "meta": {"description": "Test", "background": "#d9d9d9"},
    "canvas": {"columns": 8, "rows": 2, "cell_size": 20},
    "views": {
        "front": {
            "rows": [
                {"blank": 1},
                {"sequence": {"kind": "interface", "prefix": "eth", "count": 4}},
            ],
        }
    },
    "variants": {
        "NM-2X": {
            "match": "module",
            "view": "front",
            "elements": [
                {
                    "kind": "port",
                    "key": "te1",
                    "at": {"row": 1, "col": 8},
                    "span": {"rows": 2, "cols": 1},
                    "label": "TE 1",
                    "css_classes": ["uplink", "_x-1"],
                    "face": "front",
                }
            ],
        }
    },
}


def strict(data):
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    return parse(text, strict=True)


def layout(change):
    data = copy.deepcopy(BASE)
    change(data)
    return data


def front(data):
    return data["views"]["front"]


def variant_element(data):
    return data["variants"]["NM-2X"]["elements"][0]


def separate_faces(data):
    front(data)["elements"] = [{"kind": "port", "key": "p1"}]
    del front(data)["rows"]
    data["views"]["rear"] = {"copy_from": "front"}


def test_base_layout_passes():
    parsed = strict(BASE)
    assert [el.key for el in parsed.front.elements_for_variant("NM-2X")][-1] == "te1"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.stem)
def test_bundled_examples_pass_unchanged(example):
    strict(example.read_text())


def test_lenient_parse_is_unchanged():
    assert parse("views: {}").views == {}
    with pytest.raises(LayoutParseError, match="grid area"):
        strict("views: {}")


class TestLimits:
    def test_canvas_cell_limit(self):
        strict(layout(lambda d: d["canvas"].update(columns=MAX_CANVAS_CELLS // 2)))
        with pytest.raises(LayoutParseError, match="4096 cells"):
            strict(
                layout(lambda d: d["canvas"].update(columns=MAX_CANVAS_CELLS // 2 + 1))
            )

    def test_view_canvas_cell_limit(self):
        with pytest.raises(LayoutParseError, match="4096 cells"):
            strict(
                layout(lambda d: front(d).update(canvas={"columns": 4096, "rows": 2}))
            )

    @pytest.mark.parametrize(
        "row",
        [
            {"sequence": {"prefix": "p", "count": 10**12}},
            {"blank": 10**12},
            {"spacer": 10**12},
            {"group": {"spacer": 10**12, "sections": [{"blank": 1}, {"blank": 1}]}},
        ],
    )
    def test_expansion_is_checked_before_allocation(self, row):
        started = time.monotonic()
        with pytest.raises(LayoutParseError, match="10000 elements"):
            strict(layout(lambda d: front(d)["rows"].append(row)))
        assert time.monotonic() - started < 1

    def test_expansion_budget_spans_views_and_variants(self):
        # Each part fits a canvas; together they exceed the budget.
        part = MAX_EXPANDED_ELEMENTS // 4 + 1
        canvas = {"columns": part, "rows": 1, "cell_size": 0}
        sequence = {"sequence": {"prefix": "p", "count": part, "pattern": "all"}}

        def change(d):
            d["canvas"] = canvas
            d["views"] = {"front": {"rows": [sequence]}, "rear": {"rows": [sequence]}}
            d["variants"] = {
                name: {"match": "module", "view": view, "rows": [sequence]}
                for name, view in (("A", "front"), ("B", "rear"))
            }

        with pytest.raises(LayoutParseError, match="10000 elements"):
            strict(layout(change))


class TestCycles:
    def test_recursive_alias(self):
        with pytest.raises(LayoutParseError, match="Recursive YAML alias"):
            strict("meta: &m {description: x, background: *m}\nviews: {}")

    def test_shared_aliases_are_walked_once(self):
        levels = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
        levels += [
            f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 12)
        ]
        text = "meta:\n  description:\n    " + "\n    ".join(levels) + "\nviews: {}"
        started = time.monotonic()
        with pytest.raises(LayoutParseError, match="meta.description"):
            strict(text)
        assert time.monotonic() - started < 1

    def test_deep_nesting(self):
        with pytest.raises(LayoutParseError, match="nested too deeply"):
            strict("meta: " + "[" * 100000)

    @pytest.mark.parametrize(
        "views",
        [
            {"front": {"copy_from": "front"}},
            {"front": {"copy_from": "rear"}, "rear": {"copy_from": "front"}},
        ],
    )
    def test_cyclic_or_chained_copy_from(self, views):
        with pytest.raises(LayoutParseError, match="copy_from"):
            strict(layout(lambda d: d.update(views=views)))


INJECTION = 'x" onmouseover="alert(1)'


class TestCssClasses:
    @pytest.mark.parametrize(
        "value",
        [
            "uplink",
            "a b",
            [""],
            ["a b"],
            [INJECTION],
            ["x'"],
            ["a\\62"],
            [1],
            [None],
            ["café"],
            ["1a"],
            ["-a"],
            ["a\t"],
        ],
    )
    def test_rejected_in_variant(self, value):
        with pytest.raises(LayoutParseError, match="css_classes"):
            strict(layout(lambda d: variant_element(d).update(css_classes=value)))

    def test_rejected_in_base_element(self):
        def change(d):
            front(d)["elements"] = [{"key": "p1", "css_classes": [INJECTION]}]
            del front(d)["rows"]

        with pytest.raises(LayoutParseError, match="css_classes"):
            strict(layout(change))

    def test_rejected_in_a_variant_of_a_separate_face_layout(self):
        # The CSS renderer does not emit variants for front/rear layouts.
        def change(d):
            separate_faces(d)
            variant_element(d)["css_classes"] = [INJECTION]

        with pytest.raises(LayoutParseError, match="css_classes"):
            strict(layout(change))


class TestFields:
    @pytest.mark.parametrize(
        ("change", "message"),
        [
            (lambda d: variant_element(d).update(key="te 1"), "key"),
            (lambda d: variant_element(d).update(key='te"1'), "key"),
            (lambda d: variant_element(d).update(key="1"), "key"),
            (lambda d: variant_element(d).update(key=7), "key"),
            (lambda d: front(d)["rows"][1]["sequence"].update(prefix="eth "), "key"),
            (lambda d: front(d)["rows"][1]["sequence"].update(prefix=""), "key"),
            (lambda d: front(d)["rows"].append({"elements": ["a}b"]}), "key"),
            (lambda d: d["variants"].update({"bad name": {"elements": []}}), "variant"),
            (lambda d: d["variants"].update({'x"}': {"elements": []}}), "variant"),
            (lambda d: variant_element(d).update(label=5), "label"),
            (lambda d: variant_element(d).update(label="a\nb"), "label"),
            (lambda d: variant_element(d).update(label="x" * 65), "label"),
            (lambda d: d["meta"].update(background="red; } body {"), "background"),
            (lambda d: d["meta"].update(background="#12"), "background"),
            (lambda d: d["canvas"].update(background=5), "background"),
            (lambda d: front(d).update(background="url(x)"), "background"),
            (lambda d: d["meta"].update(description=["x"]), "description"),
            (lambda d: d["canvas"].update(columns=0), "columns"),
            (lambda d: d["canvas"].update(columns=True), "columns"),
            (lambda d: d["canvas"].update(columns="8"), "columns"),
            (lambda d: d["canvas"].update(rows=-1), "rows"),
            (lambda d: d["canvas"].update(cell_size=-1), "cell_size"),
            (lambda d: d["canvas"].update(cell_size=101), "cell_size"),
            (lambda d: d.update(version="1"), "version"),
            (lambda d: d.update(version=2), "version"),
            (lambda d: variant_element(d).update(kind="flux"), "kind"),
            (lambda d: variant_element(d).update(face="side"), "face"),
            (lambda d: d["views"].update(side=d["views"].pop("front")), "side"),
            (lambda d: d["variants"]["NM-2X"].update(match="slot"), "match"),
            (lambda d: d["variants"]["NM-2X"].update(view="side"), "view"),
            (
                lambda d: front(d)["rows"][1]["sequence"].update(pattern="zig"),
                "pattern",
            ),
            (lambda d: front(d)["rows"][1]["sequence"].update(start="a"), "start"),
            (lambda d: front(d)["rows"][1]["sequence"].update(count=-1), "count"),
            (lambda d: front(d)["rows"][1]["sequence"].update(step=0), "step"),
            (lambda d: front(d)["rows"][1]["sequence"].update(row=3), "row"),
            (lambda d: front(d)["rows"].append({"spacer": -1}), "spacer"),
            (lambda d: front(d)["rows"].append({"blank": "2"}), "blank"),
            (lambda d: variant_element(d).update(at={"row": 0, "col": 1}), "row"),
            (lambda d: variant_element(d).update(span={"rows": 1, "cols": 0}), "cols"),
            (lambda d: front(d).update(rows="blank"), "rows"),
            (
                lambda d: front(d)["rows"].append({"spacer": 1, "blank": 1}),
                "exactly one",
            ),
            (lambda d: front(d)["rows"].append({"extra": 1}), "exactly one"),
            (
                lambda d: front(d)["rows"].append({"group": {"sections": [{"x": 1}]}}),
                "exactly one",
            ),
            (lambda d: variant_element(d).update(css_classes=None), "css_classes"),
        ],
    )
    def test_rejected(self, change, message):
        with pytest.raises(LayoutParseError, match=message):
            strict(layout(change))

    def test_named_and_hex_backgrounds(self):
        for background in ("black", "#abc", "#abcd", "#aabbcc", "#aabbccdd"):
            strict(
                layout(lambda d, value=background: d["meta"].update(background=value))
            )

    def test_view_background_is_applied(self):
        parsed = parse(
            yaml.safe_dump(layout(lambda d: front(d).update(background="#000")))
        )
        assert parsed.front.canvas.background == "#000"


class TestUnknownKeys:
    @pytest.mark.parametrize(
        "change",
        [
            lambda d: d.update(extra=1),
            lambda d: d["meta"].update(extra=1),
            lambda d: d["canvas"].update(extra=1),
            lambda d: front(d).update(extra=1),
            lambda d: d["variants"]["NM-2X"].update(extra=1),
            lambda d: front(d)["rows"][1]["sequence"].update(pattren="all"),
            lambda d: front(d)["rows"].append({"group": {"sections": [], "extra": 1}}),
            lambda d: variant_element(d).update(css_class=["a"]),
            lambda d: variant_element(d).update(at={"row": 1, "col": 1, "x": 1}),
            lambda d: variant_element(d).update(span={"rows": 1, "colz": 1}),
        ],
    )
    def test_rejected(self, change):
        with pytest.raises(LayoutParseError, match="unknown key"):
            strict(layout(change))


class TestGridAreas:
    @pytest.mark.parametrize(
        "change",
        [
            lambda d: d.update(views={}),
            lambda d: d.update(views={"front": {}}),
            lambda d: d.pop("views"),
        ],
    )
    def test_at_least_one_element_is_required(self, change):
        with pytest.raises(LayoutParseError, match="grid area"):
            strict(layout(change))

    def test_elements_outside_the_canvas_are_accepted(self):
        # Three bundled examples place elements past the canvas edge.
        strict(layout(lambda d: variant_element(d).update(at={"row": 1, "col": 9})))

    def test_port_names_need_not_match_components(self):
        strict(layout(lambda d: variant_element(d).update(key="does-not-exist")))
