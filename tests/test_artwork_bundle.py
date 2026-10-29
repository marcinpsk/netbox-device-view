"""The offline bundle: zip archive checks and the definition directories inside it."""

import hashlib
import io
import os
import tracemalloc
import zipfile
import zlib

import pytest

from netbox_device_view.artwork.bundle import read_bundle, read_zip
from netbox_device_view.artwork.errors import ArtworkError
from netbox_device_view.artwork.limits import (
    MAX_BUNDLE_BYTES,
    MAX_DEFINITIONS,
    MAX_EXPANDED_BYTES,
    MAX_FILES_PER_DEFINITION,
    MAX_SVG_BYTES,
)

from .artwork_samples import (
    DEVICE_FILES,
    DEVICE_MANIFEST,
    FRONT_SVG,
    MODULE_FILES,
    bundle,
    definition,
)

DEVICE_DIR = "artwork/ACME/SW-24"


def messages(data):
    with pytest.raises(ArtworkError) as caught:
        read_bundle(io.BytesIO(data))
    return [str(issue) for issue in caught.value.issues]


def assert_rejected(data, text):
    found = messages(data)
    assert any(text in message for message in found), found


def lying_bundle(sizes):
    """Return a zip whose central directory declares the given member sizes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, _size in enumerate(sizes):
            archive.writestr(f"d/f{index}.svg", b"x")
        for info, size in zip(archive.filelist, sizes, strict=True):
            info.file_size = size
    return buffer.getvalue()


def test_a_valid_bundle_gives_one_definition_per_directory():
    entries = {
        **definition(DEVICE_DIR, DEVICE_FILES),
        **definition("artwork/ACME/NM-2X", MODULE_FILES),
    }
    definitions = read_bundle(io.BytesIO(bundle(entries)))
    assert [(d.directory, d.manifest["key"]) for d in definitions] == [
        ("artwork/ACME/NM-2X", "acme/nm-2x"),
        (DEVICE_DIR, "acme/sw-24"),
    ]
    device = definitions[1]
    assert set(device.files) == {"manifest.yaml", "front.svg", "rear.svg"}
    assert device.files["front.svg"] == FRONT_SVG.encode()


def test_the_digest_covers_relative_paths_and_file_digests_only():
    first = read_bundle(io.BytesIO(bundle()))[0]
    moved = read_bundle(io.BytesIO(bundle(definition("elsewhere", DEVICE_FILES))))[0]
    lines = "".join(
        f"{path}\0{hashlib.sha256(content.encode()).hexdigest()}\n"
        for path, content in sorted(DEVICE_FILES.items())
    )
    assert first.digest == moved.digest == hashlib.sha256(lines.encode()).hexdigest()
    changed = dict(DEVICE_FILES, **{"rear.svg": DEVICE_FILES["rear.svg"] + " "})
    other = read_bundle(io.BytesIO(bundle(definition(DEVICE_DIR, changed))))[0]
    assert other.digest != first.digest


def test_not_a_zip_and_an_oversized_upload_fail():
    assert_rejected(b"not a zip", "not a zip file")
    assert_rejected(b"\0" * (MAX_BUNDLE_BYTES + 1), "larger than")


@pytest.mark.parametrize(
    "name",
    [
        "../front.svg",
        "/abs/manifest.yaml",
        "a/../b/front.svg",
        "a\\b.svg",
        "C:/x/front.svg",
        "a//front.svg",
        "./a/front.svg",
        "a/\x7ffront.svg",
    ],
)
def test_unsafe_member_names_fail(name):
    assert_rejected(bundle({name: "x"}), "not a safe relative path")


def test_a_nul_byte_in_a_member_name_fails():
    # zipfile cuts a name at NUL on write, so the byte goes into the raw archive.
    data = bundle({"a/front.svg#../../etc": "x"}).replace(b"#../../etc", b"\0../../etc")
    assert_rejected(data, "not a safe relative path")


def test_duplicate_members_fail():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{DEVICE_DIR}/front.svg", FRONT_SVG)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(f"{DEVICE_DIR}/front.svg", FRONT_SVG)
    assert_rejected(buffer.getvalue(), "more than once")


def test_a_file_and_a_directory_with_one_name_fail():
    assert_rejected(
        bundle({"a/b": "x", "a/b/c.svg": "y"}), "both a file and a directory"
    )


def test_symlinks_and_special_files_fail():
    link = zipfile.ZipInfo(f"{DEVICE_DIR}/front.svg")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    assert_rejected(bundle({"x": link}), "not a regular file")


def test_encrypted_members_and_other_compression_fail():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{DEVICE_DIR}/front.svg", FRONT_SVG)
        # zipfile clears the flag on write, so set it in the central directory.
        archive.filelist[0].flag_bits |= 0x1
    assert_rejected(buffer.getvalue(), "encrypted")
    assert_rejected(bundle(compression=zipfile.ZIP_BZIP2), "compression method")


def test_an_oversized_member_fails_before_it_is_read():
    data = bundle({f"{DEVICE_DIR}/front.svg": os.urandom(MAX_SVG_BYTES + 1)})
    assert_rejected(data, f"larger than {MAX_SVG_BYTES} bytes")


def test_a_high_compression_ratio_fails():
    assert_rejected(bundle({f"{DEVICE_DIR}/front.svg": b"\0" * 500_000}), "ratio")


def test_the_expanded_total_is_bounded_by_the_declared_sizes():
    count = MAX_EXPANDED_BYTES // MAX_SVG_BYTES + 1
    assert_rejected(lying_bundle([MAX_SVG_BYTES] * count), "expands to more than")


def test_a_member_larger_than_its_declared_size_fails():
    assert_rejected(lying_bundle([0]), "damaged")


def test_too_many_definitions_fail_before_validation():
    entries = {
        f"d{index}/manifest.yaml": "not validated"
        for index in range(MAX_DEFINITIONS + 1)
    }
    found = messages(bundle(entries))
    assert found == [f"bundle: more than {MAX_DEFINITIONS} definitions."]


def test_too_many_files_in_one_definition_fail():
    entries = definition(DEVICE_DIR, DEVICE_FILES)
    entries.update(
        {f"{DEVICE_DIR}/extra{i}.svg": "x" for i in range(MAX_FILES_PER_DEFINITION)}
    )
    assert_rejected(bundle(entries), f"more than {MAX_FILES_PER_DEFINITION} files")


def test_an_empty_bundle_fails():
    assert_rejected(bundle({}), "no definition")


def test_files_outside_a_definition_directory_fail():
    entries = definition(DEVICE_DIR, DEVICE_FILES)
    entries["README.md"] = "hello"
    assert_rejected(bundle(entries), "README.md: not inside a definition directory")


def test_nested_definition_directories_fail():
    entries = definition(DEVICE_DIR, DEVICE_FILES)
    entries.update(definition(f"{DEVICE_DIR}/child", MODULE_FILES))
    assert_rejected(bundle(entries), "inside another definition")


def test_a_missing_face_file_fails():
    files = dict(DEVICE_FILES)
    del files["rear.svg"]
    assert_rejected(
        bundle(definition(DEVICE_DIR, files)), f"{DEVICE_DIR}/rear.svg: missing"
    )


def test_a_file_the_manifest_does_not_name_fails():
    files = dict(DEVICE_FILES, **{"spare.svg": FRONT_SVG, "sub/x.svg": FRONT_SVG})
    found = messages(bundle(definition(DEVICE_DIR, files)))
    assert f"{DEVICE_DIR}/spare.svg: the manifest does not name this file." in found
    assert f"{DEVICE_DIR}/sub/x.svg: the manifest does not name this file." in found


def test_one_key_in_two_directories_fails():
    entries = {
        **definition("a", DEVICE_FILES),
        **definition("b", DEVICE_FILES),
    }
    assert_rejected(bundle(entries), "the key acme/sw-24 is also in a/manifest.yaml")


def test_connector_and_bay_keys_must_match_the_svg_of_their_face():
    svg = FRONT_SVG.replace('id="con-eth1"', 'id="con-eth9"').replace(
        'id="bay-nm"', 'id="bay-xx"'
    )
    found = messages(
        bundle(definition(DEVICE_DIR, dict(DEVICE_FILES, **{"front.svg": svg})))
    )
    manifest = f"{DEVICE_DIR}/manifest.yaml"
    assert (
        f"{manifest}:20: connectors.con-eth1: no element has this id in front.svg."
        in (found)
    )
    assert f"{manifest}:32: bays.bay-nm: no element has this id in front.svg." in found
    assert (
        f"{DEVICE_DIR}/front.svg: con-eth9: the manifest does not list this connector."
        in found
    )
    assert (
        f"{DEVICE_DIR}/front.svg: bay-xx: the manifest does not list this bay." in found
    )


def test_a_connector_on_the_wrong_face_fails():
    manifest = DEVICE_MANIFEST.replace(
        "    face: front\n    type: rj45", "    face: rear\n    type: rj45"
    )
    found = messages(
        bundle(
            definition(DEVICE_DIR, dict(DEVICE_FILES, **{"manifest.yaml": manifest}))
        )
    )
    assert any("con-eth1: no element has this id in rear.svg" in m for m in found)


def test_unsafe_svg_names_the_bundle_path_and_line():
    svg = FRONT_SVG.replace("</svg>", "<script>alert(1)</script>\n</svg>")
    found = messages(
        bundle(definition(DEVICE_DIR, dict(DEVICE_FILES, **{"front.svg": svg})))
    )
    assert any(m.startswith(f"{DEVICE_DIR}/front.svg:5: ") for m in found), found


def test_cyclic_composition_fails():
    svg = FRONT_SVG.replace(
        "</svg>",
        '<defs><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g></defs>\n</svg>',
    )
    found = messages(
        bundle(definition(DEVICE_DIR, dict(DEVICE_FILES, **{"front.svg": svg})))
    )
    assert any(m.startswith(f"{DEVICE_DIR}/front.svg:") for m in found), found


def test_read_zip_returns_files_and_skips_directory_entries():
    directory = zipfile.ZipInfo("artwork/")
    entries = read_zip(
        io.BytesIO(bundle({"artwork/": directory, "artwork/a.svg": "x"}))
    )
    assert entries == [("artwork/a.svg", b"x")]


def test_a_member_that_inflates_past_its_declared_size_is_read_with_bounded_memory():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{DEVICE_DIR}/front.svg", b"A" * (16 * 1024 * 1024))
        archive.filelist[0].file_size = 1
        archive.filelist[0].CRC = zlib.crc32(b"A")
    tracemalloc.start()
    try:
        entries = read_zip(io.BytesIO(buffer.getvalue()))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert entries == [(f"{DEVICE_DIR}/front.svg", b"A")]
    assert peak < 4 * 1024 * 1024
