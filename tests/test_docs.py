"""The documentation states limits and versions that the code defines; keep them equal."""

from pathlib import Path

import pytest

from netbox_device_view import NetBoxDeviceViewConfig, datasource_import
from netbox_device_view.artwork import limits
from netbox_device_view.forms import DataSourceImportSourceForm
from netbox_device_view.layout.parser import MAX_CANVAS_CELLS, MAX_EXPANDED_ELEMENTS

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "statement",
    [
        f"default prefix is `{DataSourceImportSourceForm.base_fields['prefix'].initial}`",
        f"{datasource_import.MAX_FILE_BYTES // 1024} KiB per file",
        f"{datasource_import.MAX_BATCH_BYTES // 1024 // 1024} MiB",
        f"{datasource_import.MAX_BATCH_ROWS} files per import",
        f"more than {datasource_import.MAX_DIFF_LINES} lines",
        f"more than {datasource_import.MAX_DIFF_CHARS} characters",
        f"{MAX_CANVAS_CELLS} cells per canvas",
        f"{MAX_EXPANDED_ELEMENTS} expanded elements per layout",
        f"valid for {datasource_import.REVIEW_MAX_AGE_SECONDS // 60} minutes",
    ],
)
def test_import_page_states_the_code_limits(statement):
    assert statement in (ROOT / "docs" / "import-from-data-source.md").read_text()


def test_requirements_state_the_minimum_netbox_version():
    major, minor, _ = NetBoxDeviceViewConfig.min_version.split(".")
    for path in ("README.md", "docs/installation.md"):
        assert f"- NetBox {major}.{minor}+ " in (ROOT / path).read_text(), path


MIB = 1024 * 1024


@pytest.mark.parametrize(
    "statement",
    [
        f"| Bundle file | {limits.MAX_BUNDLE_BYTES // MIB} MiB compressed |",
        f"| Bundle content | {limits.MAX_EXPANDED_BYTES // MIB} MiB expanded |",
        f"| Compression ratio | {limits.MAX_COMPRESSION_RATIO}:1 per member |",
        f"| Definitions | {limits.MAX_DEFINITIONS} per bundle |",
        f"| Files | {limits.MAX_FILES_PER_DEFINITION} per definition |",
        f"| Manifest | {limits.MAX_MANIFEST_BYTES // 1024} KiB |",
        f"| SVG file | {limits.MAX_SVG_BYTES // MIB} MiB |",
        f"| SVG elements | {limits.MAX_SVG_ELEMENTS} per file |",
        f"| SVG nesting | {limits.MAX_SVG_DEPTH} levels |",
        f"| `use` expansion | {limits.MAX_USE_EXPANSION_NODES} nodes, {limits.MAX_USE_DEPTH} levels |",
        f"| `viewBox` size | {limits.MIN_VIEWBOX_SIZE} to {limits.MAX_VIEWBOX_SIZE} mm |",
        f"The import shows at most {limits.MAX_ISSUES} errors.",
    ],
)
def test_artwork_page_states_the_code_limits(statement):
    assert statement in (ROOT / "docs" / "artwork-bundles.md").read_text()
