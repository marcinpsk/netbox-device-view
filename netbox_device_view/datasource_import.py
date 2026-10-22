"""
Import device view layouts from synchronized NetBox Data Sources.

This module owns discovery, validation of selected content, review tokens, and
transactional apply. Views call it and only handle forms and display.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from core.choices import DataSourceStatusChoices
from core.models import DataFile, DataSource
from core.signals import clear_events
from dcim.models import DeviceType
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
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


@dataclass(frozen=True)
class ReviewedRow:
    """One reviewed row, as the signed review token stores it."""

    datafile_id: int
    path: str
    sha256: str
    device_type_id: int
    action: Action
    render_mode: str

    def to_payload(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_payload(cls, payload: dict) -> ReviewedRow:
        return cls(**{**payload, "action": Action(payload["action"])})


@dataclass
class ReviewRow:
    """One row of the review page."""

    datafile_id: int
    action: Action
    path: str = ""
    device_type: DeviceType | None = None
    proposed_fields: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def render_mode_label(self):
        if not self.proposed_fields:
            return ""
        return RenderMode(self.proposed_fields["render_mode"]).label


@dataclass
class Review:
    source: DataSource
    rows: list[ReviewRow]
    token: str = ""

    @property
    def is_valid(self):
        return bool(self.rows) and not any(row.error for row in self.rows)


@dataclass(frozen=True)
class _SourceFile:
    path: str
    content: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class _Proposal:
    device_type: DeviceType
    fields: dict[str, str]


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


def catalog(user, source: DataSource, prefix: str):
    """Return the viewable importable DataFiles below the prefix, without their content."""
    suffixes = Q()
    for suffix in YAML_SUFFIXES:
        suffixes |= Q(path__endswith=suffix)
    return (
        DataFile.objects.restrict(user, "view")
        .filter(source=source, path__startswith=prefix)
        .filter(suffixes)
        .only("pk", "path", "size", "last_updated")
        .order_by("path")
    )


def review(user, source: DataSource, selections: list[Selection]) -> Review:
    """Validate the selection and return a read-only review with a signed token."""
    source = _importable_source(user, source.pk)
    rows = []
    reviewed = []
    claimed_device_types = set()
    for selection in selections:
        row = ReviewRow(datafile_id=selection.datafile_id, action=selection.action)
        rows.append(row)
        source_file = _read_source_file(user, source, selection.datafile_id)
        if source_file is None:
            row.error = "The file is not available in this Data Source."
            continue
        row.path = source_file.path
        try:
            proposal = _propose(user, source_file, selection.device_type_id)
        except ValidationError as exc:
            row.error = " ".join(exc.messages)
            continue
        row.device_type = proposal.device_type
        if proposal.device_type.pk in claimed_device_types:
            row.error = "Another selected file already targets this device type."
            continue
        claimed_device_types.add(proposal.device_type.pk)
        row.proposed_fields = proposal.fields
        reviewed.append(
            ReviewedRow(
                datafile_id=selection.datafile_id,
                path=source_file.path,
                sha256=source_file.sha256,
                device_type_id=proposal.device_type.pk,
                action=selection.action,
                render_mode=proposal.fields["render_mode"],
            )
        )

    result = Review(source=source, rows=rows)
    if result.is_valid:
        result.token = signing.dumps(
            {
                "user": user.pk,
                "source": source.pk,
                "rows": [row.to_payload() for row in reviewed],
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
    source = _importable_source(user, payload["source"])
    rows = [ReviewedRow.from_payload(row) for row in payload["rows"]]

    try:
        with transaction.atomic():
            return [_apply_row(user, source, row) for row in rows]
    except ImportRejected:
        # Drop events queued for the rolled-back records.
        clear_events.send(sender=DeviceView)
        raise


def _apply_row(user, source, reviewed: ReviewedRow) -> DeviceView:
    source_file = _read_source_file(user, source, reviewed.datafile_id)
    if source_file is None or source_file.path != reviewed.path:
        raise ImportRejected(f"{reviewed.path}: the file is no longer available.")
    if source_file.sha256 != reviewed.sha256:
        raise ImportRejected(f"{reviewed.path}: the file changed after review.")
    try:
        proposal = _propose(user, source_file, reviewed.device_type_id)
    except ValidationError as exc:
        raise ImportRejected(f"{reviewed.path}: {' '.join(exc.messages)}") from exc
    if proposal.fields["render_mode"] != reviewed.render_mode:
        raise ImportRejected(
            f"{reviewed.path}: the render mode differs from the review."
        )
    device_view = DeviceView(device_type=proposal.device_type, **proposal.fields)
    try:
        device_view.save()
    except IntegrityError as exc:
        raise ImportRejected(
            f"{reviewed.path}: a device view already exists for this device type."
        ) from exc
    if not DeviceView.objects.restrict(user, "add").filter(pk=device_view.pk).exists():
        raise ImportRejected(
            f"{reviewed.path}: the new device view violates your permissions."
        )
    return device_view


def _importable_source(user, source_id) -> DataSource:
    source = importable_sources(user).filter(pk=source_id).first()
    if source is None:
        raise ImportRejected("The Data Source is not available for import.")
    return source


def _read_source_file(user, source, datafile_id) -> _SourceFile | None:
    datafile = (
        DataFile.objects.restrict(user, "view")
        .filter(source=source, pk=datafile_id)
        .only("path", "data")
        .first()
    )
    if datafile is None or not datafile.path.endswith(YAML_SUFFIXES):
        return None
    return _SourceFile(path=datafile.path, content=bytes(datafile.data))


def _propose(user, source_file: _SourceFile, device_type_id) -> _Proposal:
    """Validate one file for one target; raise ValidationError when it cannot be imported."""
    device_type = _available_target(user, device_type_id)
    if device_type is None:
        raise ValidationError("The device type is not available as an import target.")
    fields = _proposed_fields(source_file.content)
    DeviceView(device_type=device_type, **fields).full_clean(validate_unique=False)
    return _Proposal(device_type=device_type, fields=fields)


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
