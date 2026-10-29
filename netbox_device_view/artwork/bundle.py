"""Read an offline bundle (.zip) and the definition directories of any (path, bytes) input."""

import hashlib
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from posixpath import dirname

from .errors import ArtworkError, Issue
from .limits import (
    MAX_BUNDLE_BYTES,
    MAX_COMPRESSION_RATIO,
    MAX_DEFINITIONS,
    MAX_EXPANDED_BYTES,
    MAX_FILES_PER_DEFINITION,
    MAX_SVG_BYTES,
)
from .manifest import UNSAFE_CATEGORIES, Manifest, parse_manifest
from .svg import Face, parse_face

MANIFEST_NAME = "manifest.yaml"
BUNDLE = "bundle"
MAX_MEMBERS = MAX_DEFINITIONS * MAX_FILES_PER_DEFINITION
MAX_PATH_LENGTH = 255
_ALLOWED_TYPES = {0, stat.S_IFREG, stat.S_IFDIR}
# The zip end record, zip64 end locator and central directory record, as zipfile reads them.
_END = struct.Struct("<4s4H2LH")
_END_SIGNATURE = b"PK\x05\x06"
_END64_LOCATOR_SIZE = 20
_END64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_RECORD = struct.Struct("<4s4B4HL2L5H2L")
_RECORD_SIGNATURE = b"PK\x01\x02"


def revision_digest(file_digests: dict[str, str]) -> str:
    """Return the revision identity: SHA-256 over the sorted (path, file SHA-256) lines."""
    lines = "".join(f"{path}\0{file_digests[path]}\n" for path in sorted(file_digests))
    return hashlib.sha256(lines.encode()).hexdigest()


@dataclass(frozen=True)
class Definition:
    """One validated definition directory, with paths relative to the directory."""

    directory: str
    manifest: Manifest
    files: dict[str, bytes]

    @property
    def manifest_path(self) -> str:
        return _join(self.directory, MANIFEST_NAME)

    @cached_property
    def file_digests(self) -> dict[str, str]:
        return {
            path: hashlib.sha256(data).hexdigest() for path, data in self.files.items()
        }

    @cached_property
    def digest(self) -> str:
        return revision_digest(self.file_digests)


def is_safe_path(name: str) -> bool:
    parts = name.removesuffix("/").split("/")
    return (
        0 < len(name) <= MAX_PATH_LENGTH
        and all(part not in ("", ".", "..") for part in parts)
        and "\\" not in name
        and ":" not in name
        and not any(unicodedata.category(char) in UNSAFE_CATEGORIES for char in name)
    )


def _member_issues(info: zipfile.ZipInfo) -> list[str]:
    found = []
    if (info.external_attr >> 16) & 0o170000 not in _ALLOWED_TYPES:
        found.append("not a regular file or directory.")
    if info.flag_bits & 0x1:
        found.append("encrypted members are not supported.")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        found.append("unsupported compression method; use stored or deflated.")
    if info.file_size > MAX_SVG_BYTES:
        found.append(f"larger than {MAX_SVG_BYTES} bytes.")
    elif info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1):
        found.append(f"compression ratio above {MAX_COMPRESSION_RATIO}:1.")
    return found


def _archive_issues(infos: list[zipfile.ZipInfo]) -> list[Issue]:
    if len(infos) > MAX_MEMBERS:
        return [Issue(BUNDLE, None, f"more than {MAX_MEMBERS} archive members.")]
    issues = []
    if sum(info.file_size for info in infos) > MAX_EXPANDED_BYTES:
        issues.append(
            Issue(BUNDLE, None, f"expands to more than {MAX_EXPANDED_BYTES} bytes.")
        )
    seen, files = set(), set()
    for info in infos:
        if info.orig_filename != info.filename or not is_safe_path(info.filename):
            message = f"{info.orig_filename!a} is not a safe relative path."
            issues.append(Issue(BUNDLE, None, message))
            continue
        found = _member_issues(info)
        name = info.filename.removesuffix("/")
        if name in seen:
            found.append("appears more than once.")
        seen.add(name)
        if not info.is_dir():
            files.add(name)
        issues.extend(Issue(info.filename, None, message) for message in found)
    # Safe names are relative, so dirname() reaches "" and the loop ends.
    for name in sorted(files):
        parent = dirname(name)
        while parent:
            if parent in files:
                issues.append(Issue(parent, None, "both a file and a directory."))
                break
            parent = dirname(parent)
    return issues


def _raise(issues: list[Issue]):
    raise ArtworkError(issues)


def _directory_issues(fileobj, size: int) -> list[Issue]:
    """Count the central directory records before zipfile builds one object for each of them."""
    tail_size = min(size, _END.size + 0xFFFF)
    fileobj.seek(size - tail_size)
    tail = fileobj.read(tail_size)
    # As zipfile: an end record without a comment first, then the last signature.
    found = tail_size - _END.size
    if found < 0 or not (
        tail.startswith(_END_SIGNATURE, found) and tail.endswith(b"\0\0")
    ):
        found = tail.rfind(_END_SIGNATURE)
    if found < 0 or tail_size - found < _END.size:
        return [Issue(BUNDLE, None, "not a zip file.")]
    location = size - tail_size + found
    if location >= _END64_LOCATOR_SIZE:
        fileobj.seek(location - _END64_LOCATOR_SIZE)
        if fileobj.read(4) == _END64_LOCATOR_SIGNATURE:
            return [Issue(BUNDLE, None, "a zip64 archive; use the classic zip format.")]
    directory_size = _END.unpack_from(tail, found)[5]
    start = location - directory_size
    if start < 0:
        return [Issue(BUNDLE, None, "not a zip file.")]
    total = records = 0
    while total < directory_size:
        records += 1
        if records > MAX_MEMBERS:
            return [Issue(BUNDLE, None, f"more than {MAX_MEMBERS} archive members.")]
        fileobj.seek(start + total)
        header = fileobj.read(_RECORD.size)
        if len(header) != _RECORD.size or header[:4] != _RECORD_SIGNATURE:
            return [Issue(BUNDLE, None, "not a zip file.")]
        fields = _RECORD.unpack(header)
        total += _RECORD.size + fields[12] + fields[13] + fields[14]
    return []


def read_zip(fileobj) -> list[tuple[str, bytes]]:
    """Return the (path, bytes) files of a zip bundle after the archive checks."""
    size = fileobj.seek(0, 2)
    fileobj.seek(0)
    if size > MAX_BUNDLE_BYTES:
        _raise([Issue(BUNDLE, None, f"larger than {MAX_BUNDLE_BYTES} bytes.")])
    if issues := _directory_issues(fileobj, size):
        _raise(issues)
    fileobj.seek(0)
    try:
        archive = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile:
        _raise([Issue(BUNDLE, None, "not a zip file.")])
    except NotImplementedError as exc:
        _raise([Issue(BUNDLE, None, f"unsupported zip feature ({exc}).")])
    with archive:
        infos = archive.infolist()
        if issues := _archive_issues(infos):
            _raise(issues)
        entries = []
        for info in infos:
            if info.is_dir():
                continue
            try:
                # read(n) inflates at most n bytes; read() inflates the whole stream first.
                with archive.open(info) as member:
                    data = member.read(info.file_size)
                    # The read at the end of the member makes zipfile check its CRC.
                    if member.read(1):
                        raise zipfile.BadZipFile("more data than the declared size")
                entries.append((info.filename, data))
            except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
                _raise([Issue(info.filename, None, f"damaged archive member ({exc}).")])
            except NotImplementedError as exc:
                _raise(
                    [Issue(info.filename, None, f"unsupported zip feature ({exc}).")]
                )
    return entries


def _definition_directories(paths: list[str]) -> tuple[list[str], list[Issue]]:
    directories = sorted(
        dirname(path) for path in paths if path.rsplit("/", 1)[-1] == MANIFEST_NAME
    )
    if len(directories) > MAX_DEFINITIONS:
        _raise([Issue(BUNDLE, None, f"more than {MAX_DEFINITIONS} definitions.")])
    if not directories:
        _raise([Issue(BUNDLE, None, f"no definition directory (no {MANIFEST_NAME}).")])
    issues = [
        Issue(
            f"{inner}/{MANIFEST_NAME}",
            None,
            f"inside another definition directory, {outer or '.'}.",
        )
        for outer in directories
        for inner in directories
        if inner != outer and _is_inside(inner, outer)
    ]
    return directories, issues


def _is_inside(path: str, directory: str) -> bool:
    return directory == "" or path.startswith(f"{directory}/")


def _owner(path: str, directories) -> str | None:
    """Return the nearest definition directory that contains the path."""
    parent = path
    while parent:
        parent = dirname(parent)
        if parent in directories:
            return parent
    return None


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _key_issues(
    directory: str, manifest: Manifest, faces: dict[str, Face]
) -> list[Issue]:
    """Check that the manifest keys and the con-/bay- ids of each face are equal."""
    issues = []
    manifest_path = _join(directory, MANIFEST_NAME)
    for kind, attribute in (("connectors", "connector_ids"), ("bays", "bay_ids")):
        listed = manifest.data[kind]
        for key, item in listed.items():
            face = faces.get(item["face"])
            if face is not None and key not in getattr(face, attribute):
                file = manifest.data["faces"][item["face"]]["file"]
                issues.append(
                    Issue(
                        manifest_path,
                        manifest.line(kind, key),
                        f"{kind}.{key}: no element has this id in {file}.",
                    )
                )
        noun = kind.removesuffix("s")
        for name, face in faces.items():
            file = manifest.data["faces"][name]["file"]
            issues.extend(
                Issue(
                    _join(directory, file),
                    face.id_lines[key],
                    f"{key}: the manifest does not list this {noun}.",
                )
                for key in sorted(getattr(face, attribute))
                if listed.get(key, {}).get("face") != name
            )
    return issues


def _read_definition(directory: str, files: dict[str, bytes]) -> Definition:
    if len(files) > MAX_FILES_PER_DEFINITION:
        _raise(
            [
                Issue(
                    directory or ".",
                    None,
                    f"more than {MAX_FILES_PER_DEFINITION} files.",
                )
            ]
        )
    manifest = parse_manifest(_join(directory, MANIFEST_NAME), files[MANIFEST_NAME])
    named = {face["file"]: name for name, face in manifest.data["faces"].items()}
    issues = [
        Issue(_join(directory, path), None, "the manifest does not name this file.")
        for path in sorted(files)
        if path != MANIFEST_NAME and path not in named
    ]
    faces = {}
    for file, name in sorted(named.items()):
        if file not in files:
            issues.append(
                Issue(
                    _join(directory, file),
                    None,
                    "missing: the manifest names this file.",
                )
            )
            continue
        try:
            faces[name] = parse_face(_join(directory, file), files[file])
        except ArtworkError as exc:
            issues.extend(exc.issues)
    if not issues:
        issues = _key_issues(directory, manifest, faces)
    if issues:
        _raise(issues)
    return Definition(directory, manifest, files)


def read_definitions(entries: Iterable[tuple[str, bytes]]) -> list[Definition]:
    """Validate every definition directory in the entries; raise one ArtworkError for all."""
    entries = list(entries)
    directories, issues = _definition_directories([path for path, _ in entries])
    grouped = {directory: {} for directory in directories}
    for path, data in entries:
        directory = _owner(path, grouped)
        if directory is None:
            issues.append(Issue(path, None, "not inside a definition directory."))
            continue
        grouped[directory][path.removeprefix(f"{directory}/")] = data
    if issues:
        _raise(issues)
    definitions, owners = [], {}
    for directory, files in grouped.items():
        try:
            definition = _read_definition(directory, files)
        except ArtworkError as exc:
            issues.extend(exc.issues)
            continue
        key = definition.manifest.data["key"]
        if key in owners:
            issues.append(
                Issue(
                    _join(directory, MANIFEST_NAME),
                    definition.manifest.line("key"),
                    f"the key {key} is also in {_join(owners[key], MANIFEST_NAME)}.",
                )
            )
        owners.setdefault(key, directory)
        definitions.append(definition)
    if issues:
        _raise(issues)
    return definitions


def read_bundle(fileobj) -> list[Definition]:
    """Return the validated definitions of an offline bundle."""
    return read_definitions(read_zip(fileobj))
