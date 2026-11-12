"""Management command: check a layout library directory as the imports do, without a database."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from netbox_device_view.library_check import check_library


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


class Command(BaseCommand):
    help = (
        "Check the layouts below device-types/ and the artwork below artwork/ of a"
        " directory with the validation of the imports."
    )
    # The check reads only files, so it runs without a database.
    requires_system_checks = ()

    def add_arguments(self, parser):
        parser.add_argument("root", type=Path, help="The library directory.")
        parser.add_argument(
            "--device-types",
            type=Path,
            metavar="DIRECTORY",
            help=(
                "A devicetype-library checkout. Each artwork binding and bay must name"
                " a component or module bay of the community type at the same path."
            ),
        )

    def handle(self, *args, root, device_types, **options):
        if not root.is_dir():
            raise CommandError(f"{root} is not a directory.")
        if device_types is not None and not device_types.is_dir():
            raise CommandError(f"{device_types} is not a directory.")
        report = check_library(root, device_types)
        for issue in report.issues:
            self.stderr.write(str(issue))
        summary = (
            f"Checked {_count(report.layouts, 'layout')} and"
            f" {_count(report.definitions, 'artwork definition')}."
        )
        if report.issues:
            raise CommandError(
                f"{summary} {_count(len(report.issues), 'problem')} found."
            )
        self.stdout.write(self.style.SUCCESS(summary))
