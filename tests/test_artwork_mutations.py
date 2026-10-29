"""The manifest and SVG parsers raise only ArtworkError, for mutated valid input too."""

import random

import pytest

from netbox_device_view.artwork.errors import ArtworkError
from netbox_device_view.artwork.manifest import parse_manifest
from netbox_device_view.artwork.svg import parse_face

from .artwork_samples import DEVICE_MANIFEST, FRONT_SVG, MODULE_MANIFEST

# Fragments that earlier reviews found to raise other exceptions.
FRAGMENTS = [
    "\\U00110000",
    "0x_",
    "1:" * 20,
    "!!int ",
    "%YAML 1.1\n",
    "&a ",
    "*a",
    "/*)*/f(",
    "(" * 50,
    "\0",
    "1e400",
    "<!DOCTYPE x>",
    "&amp;",
    "]]>",
]


def mutations(text, seed, count=1000):
    rng = random.Random(seed)
    for _ in range(count):
        chars = list(text)
        for _ in range(rng.randint(1, 4)):
            at = rng.randrange(len(chars))
            choice = rng.random()
            if choice < 0.4:
                chars[at] = chr(rng.randrange(32, 127))
            elif choice < 0.7:
                del chars[at]
            else:
                chars.insert(at, rng.choice(FRAGMENTS))
        yield "".join(chars)


@pytest.mark.parametrize("source", [DEVICE_MANIFEST, MODULE_MANIFEST])
def test_mutated_manifests_raise_only_artwork_errors(source):
    for text in mutations(source, seed=39):
        try:
            parse_manifest("m.yaml", text.encode())
        except ArtworkError:
            pass


def test_mutated_faces_raise_only_artwork_errors():
    for text in mutations(FRONT_SVG, seed=39):
        try:
            parse_face("f.svg", text.encode())
        except ArtworkError:
            pass
