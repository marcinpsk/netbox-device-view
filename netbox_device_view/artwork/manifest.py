"""Load and validate the manifest.yaml of a visual definition (schema 1)."""

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jsonschema
import yaml
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from license_expression import ExpressionError, get_spdx_licensing

from ..layout.parser import DepthLimitedLoader, LayoutParseError
from .errors import ArtworkError, Issue
from .limits import MAX_MANIFEST_BYTES

SCHEMA = json.loads((Path(__file__).with_name("manifest.schema.json")).read_text())
_VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
_LICENSING = get_spdx_licensing()
# Physical faces in display order, from the schema.
FACES = tuple(SCHEMA["$defs"]["face_name"]["enum"])
_URL = URLValidator(schemes=["https"])
_CORE_TAGS = {
    f"tag:yaml.org,2002:{name}"
    for name in ("str", "int", "bool", "null", "float", "seq", "map")
}
# Unicode categories that no manifest string or bundle path may contain.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
MODULE_TOKEN = "{module}"


@dataclass(frozen=True)
class Manifest:
    """A validated manifest and the line of each value path in its YAML."""

    data: dict
    lines: dict

    def line(self, *at) -> int:
        return _line(self.lines, at)


class _LoadError(Exception):
    def __init__(self, message, line):
        super().__init__(message)
        self.line = line


class _ManifestLoader(DepthLimitedLoader):
    """The layout loader without anchors, aliases and explicit tags, with a line for each error."""

    def compose_node(self, parent, index):
        event = self.peek_event()
        line = event.start_mark.line + 1
        if isinstance(event, yaml.AliasEvent) or event.anchor is not None:
            raise _LoadError("YAML anchors and aliases are not allowed.", line)
        # An explicit tag can make the constructor raise; implicit tags resolve safely.
        if getattr(event, "tag", None) not in (None, "!"):
            raise _LoadError(f"The explicit YAML tag {event.tag} is not allowed.", line)
        try:
            node = super().compose_node(parent, index)
        except LayoutParseError as exc:
            raise _LoadError(str(exc), line) from exc
        if node.tag not in _CORE_TAGS:
            raise _LoadError(f"The YAML tag {node.tag} is not allowed.", line)
        return node


def _line_map(node, path=(), lines=None):
    """Map each value path of the document to the line where its node starts."""
    lines = {} if lines is None else lines
    lines[path] = node.start_mark.line + 1
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            _line_map(value_node, (*path, key_node.value), lines)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _line_map(item, (*path, index), lines)
    return lines


def _load(path: str, data: bytes) -> tuple[Any, dict]:
    if len(data) > MAX_MANIFEST_BYTES:
        raise ArtworkError(
            [
                Issue(
                    path,
                    None,
                    f"The manifest is larger than {MAX_MANIFEST_BYTES} bytes.",
                )
            ]
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtworkError([Issue(path, None, "The manifest is not UTF-8.")]) from exc
    try:
        # The reader rejects characters such as NUL when the loader is created.
        loader = _ManifestLoader(text)
        try:
            node = loader.get_single_node()
            if node is None:
                raise _LoadError("The manifest is empty.", 1)
            return loader.construct_document(node), _line_map(node)
        finally:
            loader.dispose()
    except _LoadError as exc:
        raise ArtworkError([Issue(path, exc.line, str(exc))]) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark else None
        raise ArtworkError([Issue(path, line, f"Invalid YAML: {exc}")]) from exc


def _strings(value, at=()):
    """Yield (path, string) for every string in the document, mapping keys included."""
    if isinstance(value, str):
        yield at, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield at, key
            yield from _strings(item, (*at, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*at, index))


def _where(at) -> str:
    return "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in at
    ).lstrip(".")


def _line(lines: dict, at) -> int | None:
    at = tuple(at)
    while at not in lines:
        at = at[:-1]
    return lines[at]


def _licence_and_url_issues(manifest: dict) -> list[tuple[tuple, str]]:
    issues = []
    try:
        if _LICENSING.parse(manifest["license"], validate=True, strict=True) is None:
            issues.append((("license",), "The licence expression is empty."))
    except ExpressionError as exc:
        issues.append((("license",), f"Not a valid SPDX licence expression: {exc}"))
    for index, reference in enumerate(manifest["hardware"]["references"]):
        at = ("hardware", "references", index, "url")
        try:
            _URL(reference["url"])
        except ValidationError:
            issues.append((at, "Not a valid https URL."))
            continue
        if "@" in urlsplit(reference["url"]).netloc:
            issues.append((at, "The URL must not contain user information."))
    return issues


def _face_issues(manifest: dict) -> list[tuple[tuple, str]]:
    files = {}
    for name, face in manifest["faces"].items():
        files.setdefault(face["file"], []).append(name)
    return [
        (("faces", name, "file"), f"The faces {' and '.join(names)} name one file.")
        for names in files.values()
        if len(names) > 1
        for name in names[1:]
    ] + [
        ((kind, key, "face"), f"The face {item['face']} is not in faces.")
        for kind in ("connectors", "bays")
        for key, item in manifest.get(kind, {}).items()
        if item["face"] not in manifest["faces"]
    ]


def _name_issues(manifest: dict) -> list[tuple[tuple, str]]:
    issues = []
    names = [
        (("connectors", key, "bindings", index, "name"), binding["name"])
        for key, connector in manifest.get("connectors", {}).items()
        for index, binding in enumerate(connector["bindings"])
    ]
    seen_bays = {}
    for key, bay in manifest.get("bays", {}).items():
        at = ("bays", key, "module_bay")
        names.append((at, bay["module_bay"]))
        if bay["module_bay"] in seen_bays:
            issues.append(
                (
                    at,
                    (
                        f"The module bay {bay['module_bay']} is also mapped by "
                        f"{seen_bays[bay['module_bay']]}."
                    ),
                )
            )
        seen_bays[bay["module_bay"]] = key
    if manifest["kind"] != "module":
        issues.extend(
            (at, f"Only a module definition can use {MODULE_TOKEN}.")
            for at, name in names
            if MODULE_TOKEN in name
        )
    return issues


def _error_path(error) -> tuple:
    """Return the path of a schema error; an unknown key points at that key."""
    at = tuple(error.absolute_path)
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        known = error.schema.get("properties", {})
        unknown = [key for key in error.instance if key not in known]
        if unknown:
            return (*at, unknown[0])
    return at


def _semantic_issues(manifest: dict) -> list[tuple[tuple, str]]:
    """Return the rules that JSON Schema cannot state; run only on a schema-valid manifest."""
    return (
        _licence_and_url_issues(manifest)
        + _face_issues(manifest)
        + _name_issues(manifest)
    )


def _with_defaults(manifest: dict) -> dict:
    manifest.setdefault("connectors", {})
    manifest.setdefault("bays", {})
    for bay in manifest["bays"].values():
        bay.setdefault("rotate", 0)
    return manifest


def parse_manifest(path: str, data: bytes) -> Manifest:
    """Return the validated manifest with defaults filled in; raise ArtworkError otherwise."""
    manifest, lines = _load(path, data)
    found = [
        (at, "Text must not contain control or format characters.")
        for at, value in _strings(manifest)
        if any(unicodedata.category(char) in UNSAFE_CATEGORIES for char in value)
    ]
    if not found:
        found = [
            (_error_path(error), error.message)
            for error in sorted(
                _VALIDATOR.iter_errors(manifest), key=lambda e: list(map(str, e.path))
            )
        ]
    if not found:
        found = _semantic_issues(manifest)
    if found:
        raise ArtworkError(
            [
                Issue(path, _line(lines, at), f"{_where(at) or 'manifest'}: {message}")
                for at, message in found
            ]
        )
    return Manifest(_with_defaults(manifest), lines)
