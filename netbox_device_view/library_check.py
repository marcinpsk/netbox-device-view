"""Check a layout library directory with the validation of the layout and artwork imports."""

import os
import stat
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


def _unreadable(path: str, error: OSError) -> Issue:
    return Issue(path, None, f"cannot be read: {error.strerror}.")


def _is_directory(path: Path) -> bool:
    """Return whether path is a directory; unlike Path.is_dir, raise each OSError except a missing path."""
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _files(
    root: Path, name: str
) -> tuple[list[tuple[str, Path, int]], list[Issue], list[Issue]]:
    """Return (posix path below root/name, file, size) of each regular file, the NOT_REGULAR issues and the read issues."""
    directory = root / name
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        return [], [], []
    except OSError as exc:
        return [], [], [_unreadable(name, exc)]
    if stat.S_ISLNK(mode):
        return [], [Issue(name, None, NOT_REGULAR)], []
    files, irregular, errors = [], [], []

    def unreadable(error: OSError):
        errors.append(
            _unreadable(Path(error.filename).relative_to(root).as_posix(), error)
        )

    # os.walk does not follow a link to a directory; it lists the link in the directory names.
    for parent, directories, names in os.walk(directory, onerror=unreadable):
        for entry in [Path(parent, item) for item in directories + names]:
            # A directory without search permission lists its entries, but lstat fails.
            try:
                status = entry.lstat()
            except OSError as exc:
                errors.append(_unreadable(entry.relative_to(root).as_posix(), exc))
                continue
            if stat.S_ISREG(status.st_mode):
                files.append((entry, status.st_size))
            elif not stat.S_ISDIR(status.st_mode):
                irregular.append(entry)
    return (
        [
            (file.relative_to(directory).as_posix(), file, size)
            for file, size in sorted(files)
        ],
        [
            Issue(f"{name}/{item.relative_to(directory).as_posix()}", None, NOT_REGULAR)
            for item in sorted(irregular)
        ],
        errors,
    )


def _layout_issues(root: Path) -> tuple[int, list[Issue]]:
    files, links, errors = _files(root, LAYOUTS)
    issues = links + errors
    layouts = [
        (f"{LAYOUTS}/{path}", file)
        for path, file, _ in files
        if path.endswith(tuple(FORMAT_SUFFIXES))
    ]
    for path, file in layouts:
        try:
            # Read one byte more than the limit, so the import check reports a larger file.
            with file.open("rb") as handle:
                content = handle.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            issues.append(_unreadable(path, exc))
            continue
        try:
            validate_layout_file(path, content)
        except ValidationError as exc:
            issues.extend(Issue(path, None, message) for message in exc.messages)
    return len(layouts), issues


def _source_issues(files: list[tuple[str, Path, int]]) -> list[Issue]:
    """Return the issues of the Data Source artwork import that come before it reads a file."""
    if len(files) > MAX_MEMBERS:
        return [
            Issue(ARTWORK, None, f"more than {MAX_MEMBERS} files below the prefix.")
        ]
    sizes = {path: size for path, _, size in files}
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
    files, links, errors = _files(root, ARTWORK)
    # A definition with a file that the check cannot list or read is not complete.
    if errors:
        return [], links + errors
    if not files:
        return [], links
    if issues := _source_issues(files):
        return [], links + issues
    contents = []
    for path, file, _ in files:
        try:
            contents.append((path, file.read_bytes()))
        except OSError as exc:
            issues.append(_unreadable(f"{ARTWORK}/{path}", exc))
    if issues:
        return [], links + issues
    try:
        definitions = read_definitions(contents)
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
    try:
        if any(
            community.joinpath(*parts[:n]).is_symlink()
            for n in range(1, len(parts) + 1)
        ):
            message = f"The community type {type_path} is {NOT_REGULAR}"
            return [Issue(path, None, message)]
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
    try:
        found = _is_directory(root / LAYOUTS) or _is_directory(root / ARTWORK)
    except OSError as exc:
        return Report(0, 0, [_unreadable(".", exc)])
    if not found:
        issue = Issue(".", None, f"no {LAYOUTS}/ or {ARTWORK}/ directory.")
        return Report(0, 0, [issue])
    layouts, issues = _layout_issues(root)
    definitions, artwork_issues = _artwork(root)
    issues += artwork_issues
    if community is not None:
        for definition in definitions:
            issues += _binding_issues(definition, community)
    return Report(layouts, len(definitions), issues)
