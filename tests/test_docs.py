"""The documentation states limits and versions that the code defines; keep them equal."""

from pathlib import Path

from netbox_device_view import NetBoxDeviceViewConfig, datasource_import
from netbox_device_view.forms import DataSourceImportSourceForm
from netbox_device_view.layout.parser import MAX_CANVAS_CELLS, MAX_EXPANDED_ELEMENTS

ROOT = Path(__file__).resolve().parents[1]


def test_import_page_states_the_code_limits():
    page = (ROOT / "docs" / "import-from-data-source.md").read_text()
    prefix = DataSourceImportSourceForm.base_fields["prefix"].initial
    for statement in (
        f"The default prefix is `{prefix}`.",
        (
            f"at most {datasource_import.MAX_FILE_BYTES // 1024} KiB per file, "
            f"{datasource_import.MAX_BATCH_BYTES // 1024 // 1024} MiB and "
            f"{datasource_import.MAX_BATCH_ROWS} files per import, "
            f"{MAX_CANVAS_CELLS} cells per canvas, and "
            f"{MAX_EXPANDED_ELEMENTS} expanded elements per layout."
        ),
        f"a review is valid for {datasource_import.REVIEW_MAX_AGE_SECONDS // 60} minutes",
    ):
        assert statement in page


def test_requirements_state_the_minimum_netbox_version():
    major, minor, _ = NetBoxDeviceViewConfig.min_version.split(".")
    for path in ("README.md", "docs/installation.md"):
        assert f"- NetBox {major}.{minor}+ " in (ROOT / path).read_text(), path
