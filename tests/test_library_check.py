"""The device_view_check command checks a library directory as the imports do, without a database."""

import contextlib
import errno
import io
import os
import shutil
import tempfile
import tracemalloc
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from netbox_device_view.artwork.limits import MAX_SVG_BYTES
from netbox_device_view.datasource_import import FILE_TOO_LARGE, MAX_FILE_BYTES
from netbox_device_view.library_check import NOT_REGULAR

from .artwork_samples import DEVICE_FILES, MODULE_FILES

LAYOUT = """\
version: 1
canvas: {columns: 4, rows: 2}
views:
  front:
    rows:
      - sequence: {kind: interface, prefix: eth, count: 4}
"""
SW_24 = {
    "manufacturer": "ACME",
    "model": "SW-24",
    "interfaces": [{"name": "eth1", "type": "1000base-t"}],
    "power-ports": [{"name": "PSU1", "type": "iec-60320-c14"}],
    "module-bays": [{"name": "Network Module"}],
}
NM_2X = {
    "manufacturer": "ACME",
    "model": "NM-2X",
    "interfaces": [
        {"name": "TenGigabitEthernet1/{module}/1", "type": "10gbase-x-sfpp"}
    ],
}


def write(root: Path, files: dict):
    for path, content in files.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            content = yaml.safe_dump(content)
        file.write_bytes(content if isinstance(content, bytes) else content.encode())


# The system calls of each kind of access; a directory without search permission denies "stat".
# "follow" denies only a stat that follows links, so the lstat of the listing still works.
CALLS = {
    "list": ("os.scandir",),
    "open": ("io.open",),
    "stat": ("os.stat", "os.lstat"),
    "follow": ("os.stat",),
}


@contextlib.contextmanager
def unreadable(*paths: Path, calls=("list", "open")):
    """Make the system calls fail with a permission error for paths; the tests run as root, which reads all."""
    blocked = {str(path) for path in paths}
    follow_only = "follow" in calls and "stat" not in calls

    def deny(call):
        def checked(path, *args, **kwargs):
            lstat = call is os.lstat or kwargs.get("follow_symlinks") is False
            if os.fspath(path) in blocked and not (follow_only and lstat):
                raise PermissionError(
                    errno.EACCES, os.strerror(errno.EACCES), os.fspath(path)
                )
            return call(path, *args, **kwargs)

        return checked

    targets = {target for kind in calls for target in CALLS[kind]}
    with contextlib.ExitStack() as stack:
        for target in sorted(targets):
            module, name = target.split(".")
            stack.enter_context(
                mock.patch(target, deny(getattr({"os": os, "io": io}[module], name)))
            )
        yield


def artwork(directory, files):
    return {f"artwork/{directory}/{name}": content for name, content in files.items()}


class LibraryCheckTests(SimpleTestCase):
    """SimpleTestCase fails any database query, so each test also shows that the check needs none."""

    def setUp(self):
        self.library = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.community = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write(
            self.library,
            {
                "README.md": "The library.",
                "device-types/ACME/SW-24.yaml": LAYOUT,
                **artwork("ACME/SW-24", DEVICE_FILES),
                **artwork("ACME/NM-2X", MODULE_FILES),
            },
        )
        write(
            self.community,
            {
                "device-types/ACME/SW-24.yaml": SW_24,
                "module-types/ACME/NM-2X.yaml": NM_2X,
            },
        )

    def check(self, *args):
        stdout, stderr = StringIO(), StringIO()
        try:
            call_command("device_view_check", *args, stdout=stdout, stderr=stderr)
        except CommandError as exc:
            return str(exc), stderr.getvalue().splitlines()
        return stdout.getvalue().strip(), stderr.getvalue().splitlines()

    def check_names(self):
        return self.check(str(self.library), "--device-types", str(self.community))

    def test_a_valid_library_passes(self):
        summary, problems = self.check_names()
        self.assertEqual(problems, [])
        self.assertEqual(summary, "Checked 1 layout and 2 artwork definitions.")

    def test_a_layout_that_the_import_rejects_fails_with_the_import_message(self):
        write(self.library, {"device-types/ACME/Broken.yml": "version: 1\nviews: [x"})
        summary, problems = self.check(str(self.library))
        # A YAML error message has several lines.
        self.assertTrue(
            problems[0].startswith(
                "device-types/ACME/Broken.yml: Invalid YAML layout:"
            ),
            problems,
        )
        self.assertIn("1 problem found.", summary)

    def test_a_layout_larger_than_the_import_limit_fails(self):
        write(
            self.library,
            {"device-types/ACME/Big.yaml": LAYOUT + "#" * MAX_FILE_BYTES},
        )
        _, problems = self.check(str(self.library))
        self.assertEqual(problems, [f"device-types/ACME/Big.yaml: {FILE_TOO_LARGE}"])

    def test_a_large_layout_is_read_only_up_to_the_import_limit(self):
        with (self.library / "device-types/ACME/Huge.yaml").open("wb") as file:
            file.truncate(64 * MAX_FILE_BYTES)
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        _, problems = self.check(str(self.library))
        _, peak = tracemalloc.get_traced_memory()
        self.assertEqual(problems, [f"device-types/ACME/Huge.yaml: {FILE_TOO_LARGE}"])
        self.assertLess(peak, 8 * MAX_FILE_BYTES)

    def test_a_symbolic_link_is_a_problem_and_its_target_is_not_read(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write(
            outside,
            {
                "target.yaml": "version: 1\nviews: [x",
                **artwork("ACME/Other", DEVICE_FILES),
            },
        )
        (outside / "target.svg").write_text("<svg/>")
        (self.library / "device-types/ACME/Link.yaml").symlink_to(
            outside / "target.yaml"
        )
        (self.library / "artwork/ACME/SW-24/extra.svg").symlink_to(
            outside / "target.svg"
        )
        (self.library / "artwork/Linked").symlink_to(outside / "artwork")
        _, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            [
                f"device-types/ACME/Link.yaml: {NOT_REGULAR}",
                f"artwork/ACME/SW-24/extra.svg: {NOT_REGULAR}",
                f"artwork/Linked: {NOT_REGULAR}",
            ],
        )

    def test_a_fifo_is_a_problem_and_is_not_read(self):
        (self.library / "device-types/ACME/SW-24.yaml").unlink()
        os.mkfifo(self.library / "device-types/ACME/SW-24.yaml")
        os.mkfifo(self.library / "artwork/ACME/SW-24/extra.svg")
        summary, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            [
                f"device-types/ACME/SW-24.yaml: {NOT_REGULAR}",
                f"artwork/ACME/SW-24/extra.svg: {NOT_REGULAR}",
            ],
        )
        self.assertIn("Checked 0 layouts", summary)

    def test_a_linked_layout_or_artwork_directory_is_a_problem(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.move(self.library / "device-types", outside / "device-types")
        (self.library / "device-types").symlink_to(outside / "device-types")
        summary, problems = self.check(str(self.library))
        self.assertEqual(problems, [f"device-types: {NOT_REGULAR}"])
        self.assertIn("Checked 0 layouts", summary)

    def test_artwork_that_the_import_rejects_fails_with_its_issues(self):
        write(
            self.library,
            {
                "artwork/ACME/SW-24/notes.txt": "draft",
                "artwork/stray.svg": "<svg/>",
            },
        )
        _, problems = self.check(str(self.library))
        self.assertEqual(
            problems, ["artwork/stray.svg: not inside a definition directory."]
        )
        (self.library / "artwork/stray.svg").unlink()
        _, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            ["artwork/ACME/SW-24/notes.txt: the manifest does not name this file."],
        )

    def test_a_binding_that_the_community_type_does_not_have_fails_at_its_line(self):
        write(
            self.library,
            artwork(
                "ACME/SW-24",
                {
                    "manifest.yaml": DEVICE_FILES["manifest.yaml"].replace(
                        "name: eth1", "name: Eth1"
                    )
                },
            ),
        )
        _, problems = self.check_names()
        manifest = DEVICE_FILES["manifest.yaml"].splitlines()
        line = next(i for i, text in enumerate(manifest, 1) if "name: eth1" in text)
        self.assertEqual(
            problems,
            [
                (
                    f"artwork/ACME/SW-24/manifest.yaml:{line}: connectors.con-eth1:"
                    " device-types/ACME/SW-24.yaml has no interface named Eth1."
                )
            ],
        )

    def test_a_bay_that_the_community_type_does_not_have_fails(self):
        write(
            self.community,
            {"device-types/ACME/SW-24.yaml": {**SW_24, "module-bays": []}},
        )
        _, problems = self.check_names()
        self.assertEqual(len(problems), 1)
        self.assertIn(
            "bays.bay-nm: device-types/ACME/SW-24.yaml has no module bay named"
            " Network Module.",
            problems[0],
        )

    def test_a_module_definition_is_checked_against_the_module_type(self):
        write(
            self.community,
            {"module-types/ACME/NM-2X.yaml": {**NM_2X, "interfaces": []}},
        )
        _, problems = self.check_names()
        self.assertEqual(len(problems), 1)
        self.assertIn(
            "module-types/ACME/NM-2X.yaml has no interface named"
            " TenGigabitEthernet1/{module}/1.",
            problems[0],
        )

    def test_a_definition_without_a_community_type_at_its_path_fails(self):
        (self.community / "device-types/ACME/SW-24.yaml").unlink()
        _, problems = self.check_names()
        self.assertEqual(
            problems,
            [
                (
                    "artwork/ACME/SW-24/manifest.yaml: The community device type"
                    " device-types/ACME/SW-24.yaml is missing."
                )
            ],
        )

    def test_a_community_type_that_cannot_be_read_is_a_problem(self):
        type_file = self.community / "device-types/ACME/SW-24.yaml"
        type_file.unlink()
        type_file.mkdir()
        _, problems = self.check_names()
        self.assertEqual(
            problems,
            [
                (
                    "artwork/ACME/SW-24/manifest.yaml: The community type"
                    " device-types/ACME/SW-24.yaml cannot be read: Is a directory."
                )
            ],
        )

    def test_a_layout_that_cannot_be_read_is_a_problem(self):
        with unreadable(self.library / "device-types/ACME/SW-24.yaml"):
            summary, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            ["device-types/ACME/SW-24.yaml: cannot be read: Permission denied."],
        )
        self.assertIn("1 problem found", summary)

    def test_an_artwork_file_that_cannot_be_read_is_a_problem(self):
        with unreadable(self.library / "artwork/ACME/SW-24/manifest.yaml"):
            summary, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            ["artwork/ACME/SW-24/manifest.yaml: cannot be read: Permission denied."],
        )
        self.assertIn("0 artwork definitions", summary)

    def test_a_directory_that_cannot_be_listed_is_a_problem(self):
        with unreadable(self.library / "artwork/ACME/SW-24"):
            summary, problems = self.check(str(self.library))
        self.assertEqual(
            problems, ["artwork/ACME/SW-24: cannot be read: Permission denied."]
        )
        self.assertIn("1 problem found", summary)

    def test_a_file_that_cannot_be_inspected_is_a_problem(self):
        manifest = self.library / "artwork/ACME/SW-24/manifest.yaml"
        with unreadable(manifest, calls=("stat", "open")):
            summary, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            ["artwork/ACME/SW-24/manifest.yaml: cannot be read: Permission denied."],
        )
        self.assertIn("0 artwork definitions", summary)

    def test_a_community_type_that_cannot_be_inspected_is_a_problem(self):
        type_file = self.community / "device-types/ACME/SW-24.yaml"
        with unreadable(type_file, calls=("stat", "open")):
            _, problems = self.check_names()
        self.assertEqual(
            problems,
            [
                (
                    "artwork/ACME/SW-24/manifest.yaml: The community type"
                    " device-types/ACME/SW-24.yaml cannot be read: Permission denied."
                )
            ],
        )

    def test_a_file_that_becomes_unreadable_after_the_listing_is_a_problem(self):
        manifest = self.library / "artwork/ACME/SW-24/manifest.yaml"
        with unreadable(manifest, calls=("follow", "open")):
            _, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            ["artwork/ACME/SW-24/manifest.yaml: cannot be read: Permission denied."],
        )

    def test_a_library_directory_without_search_permission_is_a_problem(self):
        with unreadable(
            self.library / "device-types", self.library / "artwork", calls=("stat",)
        ):
            summary, problems = self.check(str(self.library))
        self.assertEqual(problems, [".: cannot be read: Permission denied."])
        self.assertIn("1 problem found", summary)

    def test_without_device_types_the_names_are_not_checked(self):
        (self.community / "device-types/ACME/SW-24.yaml").unlink()
        summary, problems = self.check(str(self.library))
        self.assertEqual(problems, [])
        self.assertIn("2 artwork definitions", summary)

    def test_a_missing_directory_is_an_error(self):
        summary, _ = self.check(str(self.library / "missing"))
        self.assertIn("is not a directory", summary)

    def test_a_library_without_artwork_passes(self):
        shutil.rmtree(self.library / "artwork")
        summary, problems = self.check(str(self.library))
        self.assertEqual(problems, [])
        self.assertEqual(summary, "Checked 1 layout and 0 artwork definitions.")

    def test_a_file_that_the_data_source_import_rejects_fails_before_it_is_read(self):
        write(
            self.library,
            {
                "artwork/ACME/SW-24/a:b.svg": "<svg/>",
                "artwork/ACME/NM-2X/installed.svg": " " * (MAX_SVG_BYTES + 1),
            },
        )
        _, problems = self.check(str(self.library))
        self.assertEqual(
            problems,
            [
                f"artwork/ACME/NM-2X/installed.svg: larger than {MAX_SVG_BYTES} bytes.",
                "artwork/ACME/SW-24/a:b.svg: not a safe relative path.",
            ],
        )

    def test_the_path_length_limit_counts_the_artwork_directory(self):
        path = f"ACME/SW-24/{'d' * 200}/{'x' * 34}.svg"
        self.assertEqual(len(path), 250)
        write(self.library, {f"artwork/{path}": "<svg/>"})
        _, problems = self.check(str(self.library))
        self.assertEqual(problems, [f"artwork/{path}: not a safe relative path."])

    def test_a_community_type_that_is_not_a_type_file_is_an_error(self):
        write(
            self.community,
            {"device-types/ACME/SW-24.yaml": {**SW_24, "interfaces": "eth1"}},
        )
        _, problems = self.check_names()
        self.assertEqual(
            problems,
            [
                (
                    "artwork/ACME/SW-24/manifest.yaml: The community type"
                    " device-types/ACME/SW-24.yaml is invalid:"
                    " interfaces is not a list of items with a name."
                )
            ],
        )

    def test_a_link_on_the_path_of_a_community_type_is_a_problem(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The type behind each link lacks eth1, so a followed link gives a name problem.
        write(outside, {"ACME/SW-24.yaml": {**SW_24, "interfaces": []}})
        problem = (
            "artwork/ACME/SW-24/manifest.yaml: The community type"
            f" device-types/ACME/SW-24.yaml is {NOT_REGULAR}"
        )
        vendor = self.community / "device-types/ACME"
        (vendor / "SW-24.yaml").unlink()
        (vendor / "SW-24.yaml").symlink_to(outside / "ACME/SW-24.yaml")
        self.assertEqual(self.check_names()[1], [problem])
        shutil.rmtree(vendor)
        vendor.symlink_to(outside / "ACME")
        self.assertEqual(self.check_names()[1], [problem])

    def test_a_linked_community_directory_is_followed(self):
        linked = Path(self.enterContext(tempfile.TemporaryDirectory())) / "community"
        linked.symlink_to(self.community)
        _, problems = self.check(str(self.library), "--device-types", str(linked))
        self.assertEqual(problems, [])

    def test_a_directory_without_layouts_or_artwork_is_an_error(self):
        summary, problems = self.check(str(self.library / "device-types"))
        self.assertEqual(problems, [".: no device-types/ or artwork/ directory."])
        self.assertIn("1 problem found.", summary)
