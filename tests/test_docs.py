"""The documentation states limits and versions that the code defines; keep them equal."""

from pathlib import Path

import pytest

from netbox_device_view import NetBoxDeviceViewConfig, datasource_import
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


def test_contributing_says_to_rebuild_the_container_for_the_browser_tests():
    assert "playwright install" in (ROOT / ".devcontainer" / "Dockerfile").read_text()
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    assert "`tests/browser.py`" in contributing
    assert "docker compose -f .devcontainer/docker-compose.yml build" in contributing
