"""Check a layout library directory with the validation of the layout and artwork imports."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from django.core.exceptions import ValidationError

from .artwork.bundle import Definition, is_safe_path, read_definitions
from .artwork.errors import ArtworkError, Issue
from .artwork.limits import MAX_EXPANDED_BYTES, MAX_MEMBERS, MAX_SVG_BYTES
from .artwork.manifest import DEVICE_KIND, MODULE_KIND, SCHEMA
from .datasource_import import FORMAT_SUFFIXES, MAX_FILE_BYTES, validate_layout_file

LAYOUTS = "device-types"
ARTWORK = "artwork"
# The check does not follow links; the offline bundle import gives the same message.
NOT_REGULAR = "not a regular file or directory."
# The directory of each definition kind in the netbox-community devicetype-library.
COMMUNITY_DIRECTORIES = {DEVICE_KIND: "device-types", MODULE_KIND: "module-types"}
# The devicetype-library list of each component type that a binding can name.
COMMUNITY_LISTS = {
    "interface": "interfaces",
    "consoleport": "console-ports",
    "consoleserverport": "console-server-ports",
    "powerport": "power-ports",
    "poweroutlet": "power-outlets",
    "frontport": "front-ports",
    "rearport": "rear-ports",
}
COMMUNITY_MODULE_BAYS = "module-bays"
_BINDING = SCHEMA["properties"]["connectors"]["additionalProperties"]["properties"][
    "bindings"
]["items"]
if set(COMMUNITY_LISTS) != set(_BINDING["properties"]["component"]["enum"]):
    raise ImportError("The component types do not match manifest.schema.json.")


@dataclass(frozen=True)
class Report:
    layouts: int
    definitions: int
    issues: list[Issue]


def _files(root: Path, name: str) -> tuple[list[tuple[str, Path]], list[Issue]]:
    """Return (posix path below root/name, file) for each regular file, and an issue for each link."""
    directory = root / name
    if directory.is_symlink():
        return [], [Issue(name, None, NOT_REGULAR)]
    files, links = [], []
    # os.walk does not follow a link to a directory; it lists the link in the directory names.
    for parent, directories, names in os.walk(directory):
        for entry in [Path(parent, item) for item in directories + names]:
            if entry.is_symlink():
                links.append(entry)
            elif entry.is_file():
                files.append(entry)
    return (
        [(file.relative_to(directory).as_posix(), file) for file in sorted(files)],
        [
            Issue(f"{name}/{link.relative_to(directory).as_posix()}", None, NOT_REGULAR)
            for link in sorted(links)
        ],
    )


def _layout_issues(root: Path) -> tuple[int, list[Issue]]:
    files, issues = _files(root, LAYOUTS)
    layouts = [
        (f"{LAYOUTS}/{path}", file)
        for path, file in files
        if path.endswith(tuple(FORMAT_SUFFIXES))
    ]
    for path, file in layouts:
        # Read one byte more than the limit, so the import check reports a larger file.
        with file.open("rb") as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
        try:
            validate_layout_file(path, content)
        except ValidationError as exc:
            issues.extend(Issue(path, None, message) for message in exc.messages)
    return len(layouts), issues


def _source_issues(files: list[tuple[str, Path]]) -> list[Issue]:
    """Return the issues of the Data Source artwork import that come before it reads a file."""
    if len(files) > MAX_MEMBERS:
        return [
            Issue(ARTWORK, None, f"more than {MAX_MEMBERS} files below the prefix.")
        ]
    sizes = {path: file.stat().st_size for path, file in files}
    if sum(sizes.values()) > MAX_EXPANDED_BYTES:
        message = (
            f"the files below the prefix have more than {MAX_EXPANDED_BYTES} bytes."
        )
        return [Issue(ARTWORK, None, message)]
    issues = []
    for path, size in sizes.items():
        # The Data Source import checks the path below the prefix, with the artwork directory.
        if not is_safe_path(f"{ARTWORK}/{path}"):
            issues.append(Issue(f"{ARTWORK}/{path}", None, "not a safe relative path."))
        elif size > MAX_SVG_BYTES:
            message = f"larger than {MAX_SVG_BYTES} bytes."
            issues.append(Issue(f"{ARTWORK}/{path}", None, message))
    return issues


def _artwork(root: Path) -> tuple[list[Definition], list[Issue]]:
    files, links = _files(root, ARTWORK)
    if not files:
        return [], links
    if issues := _source_issues(files):
        return [], links + issues
    try:
        definitions = read_definitions(
            (path, file.read_bytes()) for path, file in files
        )
    except ArtworkError as exc:
        return [], links + [
            Issue(f"{ARTWORK}/{issue.path}", issue.line, issue.message)
            for issue in exc.issues
        ]
    return definitions, links


def _names(type_data: dict, key: str) -> set[str]:
    """Return the names of one devicetype-library list, for example its interfaces."""
    items = type_data.get(key, [])
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in items
    ):
        raise TypeError(f"{key} is not a list of items with a name.")
    return {item["name"] for item in items}


def _community_names(file: Path) -> tuple[dict[str, set[str]], set[str]]:
    """Return the component names by component type, and the module bay names, of a type file."""
    type_data = yaml.safe_load(file.read_bytes())
    if not isinstance(type_data, dict):
        raise TypeError("it is not a mapping.")
    names = {
        component: _names(type_data, key) for component, key in COMMUNITY_LISTS.items()
    }
    return names, _names(type_data, COMMUNITY_MODULE_BAYS)


def _binding_issues(definition: Definition, community: Path) -> list[Issue]:
    """Return an issue for each binding and bay that the community type does not have."""
    manifest = definition.manifest
    kind = manifest.data["kind"]
    path = f"{ARTWORK}/{definition.manifest_path}"
    type_path = f"{COMMUNITY_DIRECTORIES[kind]}/{definition.directory}.yaml"
    # The community root can be a link; a link below it is a problem, as in the library.
    parts = Path(type_path).parts
    if any(
        community.joinpath(*parts[:n]).is_symlink() for n in range(1, len(parts) + 1)
    ):
        return [Issue(path, None, f"The community type {type_path} is {NOT_REGULAR}")]
    try:
        names, bays = _community_names(community / type_path)
    except FileNotFoundError:
        return [Issue(path, None, f"The community {kind} type {type_path} is missing.")]
    except OSError as exc:
        message = f"The community type {type_path} cannot be read: {exc.strerror}."
        return [Issue(path, None, message)]
    except (yaml.YAMLError, TypeError) as exc:
        return [Issue(path, None, f"The community type {type_path} is invalid: {exc}")]
    issues = [
        Issue(
            path,
            manifest.line("connectors", key, "bindings", index, "name"),
            f"connectors.{key}: {type_path} has no {binding['component']}"
            f" named {binding['name']}.",
        )
        for key, connector in manifest.data["connectors"].items()
        for index, binding in enumerate(connector["bindings"])
        if binding["name"] not in names[binding["component"]]
    ]
    issues += [
        Issue(
            path,
            manifest.line("bays", key, "module_bay"),
            f"bays.{key}: {type_path} has no module bay named {bay['module_bay']}.",
        )
        for key, bay in manifest.data["bays"].items()
        if bay["module_bay"] not in bays
    ]
    return issues


def check_library(root: Path, community: Path | None = None) -> Report:
    """Check the layouts and the artwork below root; with community, check the names too."""
    if not (root / LAYOUTS).is_dir() and not (root / ARTWORK).is_dir():
        issue = Issue(".", None, f"no {LAYOUTS}/ or {ARTWORK}/ directory.")
        return Report(0, 0, [issue])
    layouts, issues = _layout_issues(root)
    definitions, artwork_issues = _artwork(root)
    issues += artwork_issues
    if community is not None:
        for definition in definitions:
            issues += _binding_issues(definition, community)
    return Report(layouts, len(definitions), issues)
