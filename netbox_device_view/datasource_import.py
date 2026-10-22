"""
Import device view layouts from synchronized NetBox Data Sources.

This module owns discovery, validation of selected content, review tokens, and
transactional apply. Views call it and only handle forms and display.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from core.choices import DataSourceStatusChoices
from core.models import DataFile, DataSource
from dcim.models import DeviceType
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q

from .layout import LayoutParseError, parse_yaml, render_css
from .models import DeviceView, RenderMode

SIGNING_SALT = "netbox_device_view.datasource_import.review"
REVIEW_MAX_AGE_SECONDS = 15 * 60
YAML_SUFFIXES = (".yaml", ".yml")


class ImportRejected(Exception):
    """The whole import batch was rejected; nothing was written."""


class Action(models.TextChoices):
    CREATE = "create", "Create"


@dataclass(frozen=True)
class Selection:
    datafile_id: int
    device_type_id: int
    action: Action = Action.CREATE


@dataclass
class ReviewRow:
    datafile_id: int
    path: str
    device_type: DeviceType | None
    action: Action
    fields: dict[str, str] = field(default_factory=dict)
    sha256: str = ""
    error: str = ""

    @property
    def render_mode_label(self):
        return RenderMode(self.fields["render_mode"]).label if self.fields else ""


@dataclass
class Review:
    source: DataSource
    rows: list[ReviewRow]
    token: str = ""

    @property
    def is_valid(self):
        return bool(self.rows) and not any(row.error for row in self.rows)


def importable_sources(user):
    """Return the Data Sources the user can view that have synchronized successfully."""
    return DataSource.objects.restrict(user, "view").filter(
        enabled=True, status=DataSourceStatusChoices.COMPLETED
    )


def normalize_prefix(raw: str) -> str:
    """Return a relative POSIX directory prefix ending in "/", or "" for the source root."""
    if "\x00" in raw:
        raise ValidationError("The directory prefix must not contain NUL bytes.")
    path = PurePosixPath(raw.strip())
    if path.is_absolute():
        raise ValidationError("The directory prefix must be relative to the source.")
    if ".." in path.parts:
        raise ValidationError("The directory prefix must not contain '..'.")
    parts = [part for part in path.parts if part != "."]
    return "/".join(parts) + "/" if parts else ""


def catalog(source: DataSource, prefix: str):
    """Return the importable DataFiles below the prefix, without their content."""
    suffixes = Q()
    for suffix in YAML_SUFFIXES:
        suffixes |= Q(path__endswith=suffix)
    return (
        DataFile.objects.filter(source=source, path__startswith=prefix)
        .filter(suffixes)
        .only("pk", "path", "size", "last_updated")
        .order_by("path")
    )


def review(user, source: DataSource, selections: list[Selection]) -> Review:
    """Validate the selection and return a read-only review with a signed token."""
    rows = []
    for selection in selections:
        row = ReviewRow(
            datafile_id=selection.datafile_id,
            path="",
            device_type=None,
            action=selection.action,
        )
        rows.append(row)
        datafile = _read_datafile(source, selection.datafile_id)
        if datafile is None:
            row.error = "The file is not available in this Data Source."
            continue
        row.path, raw = datafile
        row.sha256 = hashlib.sha256(raw).hexdigest()
        row.device_type = _available_target(user, selection.device_type_id)
        if row.device_type is None:
            row.error = "The device type is not available as an import target."
            continue
        try:
            row.fields = _proposed_fields(raw)
            DeviceView(device_type=row.device_type, **row.fields).full_clean(
                validate_unique=False
            )
        except ValidationError as exc:
            row.error = " ".join(exc.messages)

    result = Review(source=source, rows=rows)
    if result.is_valid:
        result.token = signing.dumps(
            {
                "user": user.pk,
                "source": source.pk,
                "rows": [
                    {
                        "file": row.datafile_id,
                        "path": row.path,
                        "sha256": row.sha256,
                        "device_type": row.device_type.pk,
                        "action": row.action.value,
                        "render_mode": row.fields["render_mode"],
                    }
                    for row in rows
                ],
            },
            salt=SIGNING_SALT,
        )
    return result


def apply(user, token: str) -> list[DeviceView]:
    """Apply a reviewed import in one transaction and return the created records."""
    try:
        payload = signing.loads(
            token, salt=SIGNING_SALT, max_age=REVIEW_MAX_AGE_SECONDS
        )
    except signing.SignatureExpired as exc:
        raise ImportRejected(
            "The review has expired. Review the import again."
        ) from exc
    except signing.BadSignature as exc:
        raise ImportRejected("The review token is not valid.") from exc
    if not user.is_authenticated or payload["user"] != user.pk:
        raise ImportRejected("The review token belongs to a different user.")
    if not user.has_perm("netbox_device_view.add_deviceview"):
        raise ImportRejected("You do not have permission to add device views.")
    source = importable_sources(user).filter(pk=payload["source"]).first()
    if source is None:
        raise ImportRejected("The Data Source is not available for import.")

    created = []
    with transaction.atomic():
        for reviewed in payload["rows"]:
            created.append(_apply_row(user, source, reviewed))
    return created


def _apply_row(user, source, reviewed) -> DeviceView:
    datafile = _read_datafile(source, reviewed["file"])
    if datafile is None or datafile[0] != reviewed["path"]:
        raise ImportRejected(f"{reviewed['path']}: the file is no longer available.")
    path, raw = datafile
    if hashlib.sha256(raw).hexdigest() != reviewed["sha256"]:
        raise ImportRejected(f"{path}: the file changed after review.")
    device_type = _available_target(user, reviewed["device_type"])
    if device_type is None:
        raise ImportRejected(f"{path}: the device type is not available.")
    try:
        fields = _proposed_fields(raw)
        if fields["render_mode"] != reviewed["render_mode"]:
            raise ImportRejected(f"{path}: the render mode differs from the review.")
        device_view = DeviceView(device_type=device_type, **fields)
        device_view.full_clean()
    except ValidationError as exc:
        raise ImportRejected(f"{path}: {' '.join(exc.messages)}") from exc
    device_view.save()
    if not DeviceView.objects.restrict(user, "add").filter(pk=device_view.pk).exists():
        raise ImportRejected(f"{path}: the new device view violates your permissions.")
    return device_view


def _read_datafile(source, datafile_id) -> tuple[str, bytes] | None:
    row = (
        DataFile.objects.filter(source=source, pk=datafile_id)
        .values_list("path", "data")
        .first()
    )
    if row is None or not row[0].endswith(YAML_SUFFIXES):
        return None
    return row[0], bytes(row[1])


def _available_target(user, device_type_id) -> DeviceType | None:
    """Return a permitted device type without a device view, else None."""
    device_type = (
        DeviceType.objects.restrict(user, "view")
        .select_related("manufacturer")
        .filter(pk=device_type_id)
        .first()
    )
    # Hidden and visible existing device views both make the target unavailable.
    if (
        device_type is None
        or DeviceView.objects.filter(device_type=device_type).exists()
    ):
        return None
    return device_type


def _proposed_fields(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("The file is not valid UTF-8 text.") from exc
    if not text.strip():
        raise ValidationError("The file is empty.")
    try:
        css = render_css(parse_yaml(text))
    except LayoutParseError as exc:
        raise ValidationError(f"Invalid YAML layout: {exc}") from exc
    if not css.strip():
        raise ValidationError("The layout defines no grid areas.")
    return {
        "yaml_layout": text,
        "grid_template_area": css,
        "render_mode": RenderMode.CSS.value,
    }
