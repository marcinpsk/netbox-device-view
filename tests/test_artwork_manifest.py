"""The manifest of a visual definition: YAML loading, schema 1 and the rules beyond it."""

import pytest

from netbox_device_view.artwork.errors import ArtworkError
from netbox_device_view.artwork.limits import MAX_MANIFEST_BYTES
from netbox_device_view.artwork.manifest import parse_manifest

from .artwork_samples import DEVICE_MANIFEST, MODULE_MANIFEST


def issues(text, path="acme/manifest.yaml"):
    data = text.encode() if isinstance(text, str) else text
    with pytest.raises(ArtworkError) as caught:
        parse_manifest(path, data)
    return caught.value.issues


def replace(old, new, text=DEVICE_MANIFEST):
    assert old in text
    return text.replace(old, new, 1)


def test_valid_device_manifest_gets_defaults():
    manifest = parse_manifest("m.yaml", DEVICE_MANIFEST.encode()).data
    assert manifest["key"] == "acme/sw-24"
    assert manifest["bays"]["bay-nm"]["rotate"] == 0
    assert manifest["connectors"]["con-eth1"]["bindings"] == [
        {"component": "interface", "name": "eth1"}
    ]


def test_valid_module_manifest_may_use_the_module_token():
    manifest = parse_manifest("m.yaml", MODULE_MANIFEST.encode()).data
    assert manifest["kind"] == "module"
    assert manifest["bays"] == {}


def test_connectors_and_bays_are_optional():
    text = DEVICE_MANIFEST.split("connectors:")[0]
    manifest = parse_manifest("m.yaml", text.encode()).data
    assert manifest["connectors"] == {} and manifest["bays"] == {}


@pytest.mark.parametrize(
    "text, line, message",
    [
        (replace("kind: device\n", "kind: device\nkind: module\n"), 1, "duplicate key"),
        (replace("authors: [Test Author]", "authors: &a [Test Author]"), 14, "anchors"),
        (replace("title: ACME SW-24\n", "title: !!binary aGk=\n"), 4, "tag"),
        (replace("title: ACME SW-24\n", "title: !custom x\n"), 4, "tag"),
        (replace("license: MIT", "license: 2026-10-29"), 13, "tag"),
        (DEVICE_MANIFEST + "---\nschema: 1\n", None, "single document"),
        ("x: " + "[" * 40 + "]" * 40, 1, "nested too deeply"),
        ("", 1, "empty"),
        ("key: [unclosed\n", 2, "Invalid YAML"),
    ],
)
def test_yaml_loading_rules(text, line, message):
    found = issues(text)
    assert message in found[0].message
    if line is not None:
        assert found[0].line == line
    assert found[0].path == "acme/manifest.yaml"


def test_non_utf8_and_oversized_manifests_fail():
    assert "UTF-8" in issues(b"title: \xff\n")[0].message
    big = DEVICE_MANIFEST + "#" * MAX_MANIFEST_BYTES
    assert "larger than" in issues(big)[0].message


@pytest.mark.parametrize(
    "text, where",
    [
        (replace("schema: 1", "schema: 2"), "schema"),
        (replace("schema: 1", "schema: true"), "schema"),
        (replace("key: acme/sw-24", "key: ACME/sw-24"), "key"),
        (replace("key: acme/sw-24", "key: acme"), "key"),
        (replace("kind: device", "kind: chassis"), "kind"),
        (replace("title: ACME SW-24", "title: " + "x" * 129), "title"),
        (replace("title: ACME SW-24", "title: 42"), "title"),
        (replace("sku: SW-24-AC", "sku: SW-24-AC\n  vid: V01"), "hardware"),
        (replace("authors: [Test Author]", "authors: []"), "authors"),
        (replace("front: {file: front.svg}", "front: {file: ../front.svg}"), "faces"),
        (replace("front: {file: front.svg}", "installed: {file: front.svg}"), "faces"),
        (replace("type: rj45", 'type: rj45" onclick="x'), "connectors"),
        (replace("con-eth1:", "con-<script>:"), "connectors"),
        (replace("con-eth1:", "eth1:"), "connectors"),
        (replace("component: interface", "component: inventoryitem"), "connectors"),
        (
            replace("module_bay: Network Module", "module_bay: NM\n    rotate: 45"),
            "bays",
        ),
        (replace("bay-nm:", "slot-nm:"), "bays"),
        (replace("license: MIT", "license: MIT\nextra: 1"), "extra"),
        (
            replace("url: https://example.com/sw-24.pdf", "url: http://example.com/a"),
            "hardware",
        ),
    ],
)
def test_schema_rules(text, where):
    found = issues(text)
    if where is not None:
        assert found[0].message.startswith(where), found[0].message
    assert found[0].line is not None


def test_a_reference_figure_is_optional():
    manifest = parse_manifest(
        "m.yaml", replace('      figure: "1.2"\n', "").encode()
    ).data
    assert "figure" not in manifest["hardware"]["references"][0]


def test_an_unknown_key_names_its_line():
    found = issues(replace("    type: rj45\n", "    type: rj45\n    colour: red\n"))
    assert found[0].line == 22
    assert "colour" in found[0].message


@pytest.mark.parametrize(
    "value",
    ["eth1\u202e", "eth1\x00", "eth1\n", "eth1\u200b", "\ue000"],
)
def test_text_rejects_control_and_format_characters(value):
    text = replace(
        "name: eth1}", f'name: "{value.encode("unicode_escape").decode()}"}}'
    )
    found = issues(text)
    assert "control or format" in found[0].message
    assert found[0].message.startswith("connectors.con-eth1.bindings[0].name")


def test_a_trailing_newline_does_not_pass_an_anchored_pattern():
    found = issues(replace("key: acme/sw-24", 'key: "acme/sw-24\\n"'))
    assert "control or format" in found[0].message


@pytest.mark.parametrize(
    "text, message",
    [
        (replace("license: MIT", "license: Foo-1.0"), "Unknown license key"),
        (replace("license: MIT", "license: MIT AND"), "licence expression"),
        (replace("license: MIT", "license: (MIT"), "licence expression"),
        (
            replace("https://example.com/sw-24.pdf", "https://user:pw@example.com/a"),
            "user information",
        ),
        (
            replace("https://example.com/sw-24.pdf", "https://exa mple.com/"),
            "https URL",
        ),
        (
            replace(
                "face: installed\n    type", "face: front\n    type", MODULE_MANIFEST
            ),
            "not in faces",
        ),
        (
            replace("    module_bay: Network Module\n", "    module_bay: NM\n")
            + "  bay-two:\n    face: front\n    module_bay: NM\n",
            "also mapped by bay-nm",
        ),
        (replace("name: eth1", 'name: "eth{module}"'), "Only a module definition"),
    ],
)
def test_rules_beyond_the_schema(text, message):
    found = issues(text)
    assert any(message in issue.message for issue in found), found


def test_a_face_that_the_manifest_does_not_declare():
    found = issues(replace("  rear: {file: rear.svg}\n", ""))
    assert found[0].message == "connectors.con-psu.face: The face rear is not in faces."


@pytest.mark.parametrize(
    "text, line",
    [
        (replace("title: ACME SW-24", "title: ACME\x00"), None),
        (replace("schema: 1", "schema: !!int x"), 1),
        (replace("schema: 1", "schema: !!bool garbage"), 1),
        (replace("schema: 1", "schema: !!int 1"), 1),
    ],
)
def test_malformed_scalars_and_explicit_tags_fail_with_an_issue(text, line):
    found = issues(text)
    if line is not None:
        assert found[0].line == line


@pytest.mark.parametrize("value", ['" "', '""'])
def test_a_blank_licence_fails(value):
    found = issues(replace("license: MIT", f"license: {value}"))
    assert found[0].line == 13


def test_two_faces_cannot_name_one_file():
    found = issues(replace("rear: {file: rear.svg}", "rear: {file: front.svg}"))
    assert "faces.rear.file" in found[0].message
    assert found[0].line == 17


@pytest.mark.parametrize(
    "digits",
    ["1" * 4301, "0x" + "f" * 5000, "0x_", "0b_", "1:" * 174 + "0.0", "1.5", ".inf"],
)
def test_implicit_numbers_that_cannot_be_built_fail_with_an_issue(digits):
    found = issues(replace("schema: 1", f"schema: {digits}"))
    assert found[0].line == 1
