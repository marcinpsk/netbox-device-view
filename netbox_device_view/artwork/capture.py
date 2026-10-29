"""Capture the definition files below a prefix of a synchronized Data Source in one SQL statement."""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from core.choices import DataSourceStatusChoices
from core.models import DataFile, DataSource
from django.db.models import (
    BinaryField,
    Case,
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    When,
    Window,
)

from ..models import SourceSyncRun
from .bundle import MAX_MEMBERS, is_safe_path
from .errors import ArtworkError, Issue
from .limits import MAX_EXPANDED_BYTES, MAX_SVG_BYTES

SOURCE = "data source"
NOT_SYNCED = (
    "the Data Source status is {status}. Check for updates and import after the"
    " sync completes."
)
NO_GUARDED_SYNC = (
    "the Data Source has no sync since the plugin was installed. Check for updates"
    " once, then import."
)
SYNC_RUNNING = "a sync of the Data Source has not finished. Import after it completes."
HIDDEN_FILES = "you cannot view every file below this prefix."
CHANGED_FILE = (
    "the stored file does not match its recorded size and SHA-256. Check for"
    " updates, then import again."
)


@dataclass(frozen=True)
class Capture:
    source: DataSource
    last_synced: datetime
    entries: list[tuple[str, bytes]]


def _files(user, source: DataSource, prefix: str):
    """Return one row per file; the row also holds the source state of the same snapshot."""
    newest_run = SourceSyncRun.objects.filter(source=OuterRef("source"))
    viewable = DataFile.objects.restrict(user, "view").filter(pk=OuterRef("pk"))
    within_limits = Q(
        file_count__lte=MAX_MEMBERS,
        total_size__lte=MAX_EXPANDED_BYTES,
        size__lte=MAX_SVG_BYTES,
    )
    return (
        DataFile.objects.filter(source=source, path__startswith=prefix)
        .annotate(
            status=F("source__status"),
            synced=F("source__last_synced"),
            has_run=Exists(newest_run),
            run_finished=Subquery(newest_run.values("finished")[:1]),
            viewable=Exists(viewable),
            file_count=Window(Count("pk")),
            total_size=Window(Sum("size")),
        )
        # Only input within the limits reaches Python with its bytes.
        .annotate(
            payload=Case(
                When(within_limits, then=F("data")),
                default=None,
                output_field=BinaryField(),
            )
        )
        .order_by("path")
        .values(
            "path",
            "size",
            "hash",
            "payload",
            "status",
            "synced",
            "has_run",
            "run_finished",
            "viewable",
            "file_count",
            "total_size",
        )[: MAX_MEMBERS + 1]
    )


def _source_issues(row) -> list[Issue]:
    if row["status"] != DataSourceStatusChoices.COMPLETED:
        return [Issue(SOURCE, None, NOT_SYNCED.format(status=row["status"]))]
    if not row["has_run"]:
        return [Issue(SOURCE, None, NO_GUARDED_SYNC)]
    if row["run_finished"] is None:
        return [Issue(SOURCE, None, SYNC_RUNNING)]
    if row["file_count"] > MAX_MEMBERS:
        return [Issue(SOURCE, None, f"more than {MAX_MEMBERS} files below the prefix.")]
    if row["total_size"] > MAX_EXPANDED_BYTES:
        message = (
            f"the files below the prefix have more than {MAX_EXPANDED_BYTES} bytes."
        )
        return [Issue(SOURCE, None, message)]
    return []


def _file_issues(row) -> list[str]:
    if not is_safe_path(row["path"]):
        return ["not a safe relative path."]
    if row["size"] > MAX_SVG_BYTES:
        return [f"larger than {MAX_SVG_BYTES} bytes."]
    data = bytes(row["payload"])
    if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["hash"]:
        return [CHANGED_FILE]
    return []


def capture(user, source: DataSource, prefix: str) -> Capture:
    """Return the files below the prefix, or raise ArtworkError when they are not one coherent sync."""
    rows = list(_files(user, source, prefix))
    if not rows:
        source.refresh_from_db(fields=["status"])
        if source.status != DataSourceStatusChoices.COMPLETED:
            message = NOT_SYNCED.format(status=source.status)
            raise ArtworkError([Issue(SOURCE, None, message)])
        raise ArtworkError([Issue(SOURCE, None, f"no files below {prefix or '/'}.")])
    if issues := _source_issues(rows[0]):
        raise ArtworkError(issues)
    if not all(row["viewable"] for row in rows):
        raise ArtworkError([Issue(SOURCE, None, HIDDEN_FILES)])
    issues = [
        Issue(row["path"], None, message)
        for row in rows
        for message in _file_issues(row)
    ]
    if issues:
        raise ArtworkError(issues)
    entries = [(row["path"], bytes(row["payload"])) for row in rows]
    return Capture(source, rows[0]["synced"], entries)
