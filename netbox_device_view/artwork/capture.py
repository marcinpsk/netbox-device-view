"""Capture the definition files below a prefix of a synchronized Data Source in one SQL statement."""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from core.choices import DataSourceStatusChoices
from core.models import DataFile, DataSource
from django.contrib.postgres.aggregates import BoolAnd
from django.db import router, transaction
from django.db.models import (
    BinaryField,
    BooleanField,
    Case,
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
    Window,
)

from ..models import SourceSyncRun
from ..stored_data import stored_sha256, stored_size
from .bundle import is_safe_path
from .errors import ArtworkError, Issue
from .limits import MAX_EXPANDED_BYTES, MAX_MEMBERS, MAX_SVG_BYTES

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
UNAVAILABLE_FILES = (
    "no files below {prefix} that you can import: the prefix has no files, or you"
    " cannot view every file below it."
)
CHANGED_FILE = (
    "the stored file does not match its recorded size and SHA-256: the file changed"
    " while a sync read it. Check for updates, then import again."
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
        data_size__lte=MAX_SVG_BYTES,
    )
    return (
        DataFile.objects.filter(source=source, path__startswith=prefix)
        # The size column can disagree with the data, so the limits use the stored bytes.
        .annotate(data_size=stored_size())
        .annotate(
            status=F("source__status"),
            synced=F("source__last_synced"),
            has_run=Exists(newest_run),
            run_finished=Subquery(newest_run.values("finished")[:1]),
            viewable=Exists(viewable),
        )
        # The window covers every file below the prefix, also those after the row limit.
        .annotate(
            all_viewable=Window(BoolAnd("viewable")),
            file_count=Window(Count("pk")),
            total_size=Window(Sum("data_size")),
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
            "data_size",
            "hash",
            "payload",
            "status",
            "synced",
            "has_run",
            "run_finished",
            "all_viewable",
            "file_count",
            "total_size",
        )[: MAX_MEMBERS + 1]
    )


def _state_issues(row) -> list[Issue]:
    if row["status"] != DataSourceStatusChoices.COMPLETED:
        return [Issue(SOURCE, None, NOT_SYNCED.format(status=row["status"]))]
    if not row["has_run"]:
        return [Issue(SOURCE, None, NO_GUARDED_SYNC)]
    if row["run_finished"] is None:
        return [Issue(SOURCE, None, SYNC_RUNNING)]
    return []


def _unavailable(prefix: str) -> ArtworkError:
    """Return one error for no files and for hidden files, so the reply does not show hidden paths."""
    return ArtworkError(
        [Issue(SOURCE, None, UNAVAILABLE_FILES.format(prefix=prefix or "/"))]
    )


def _limit_issues(row) -> list[Issue]:
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
    if row["data_size"] > MAX_SVG_BYTES:
        return [f"larger than {MAX_SVG_BYTES} bytes."]
    data = bytes(row["payload"])
    if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["hash"]:
        return [CHANGED_FILE]
    return []


def _read_again_at_next_sync(source: DataSource, paths: list[str]) -> None:
    """Clear the hash of the torn files, so the next sync reads them again."""
    consistent = Q(size=stored_size(), hash=stored_sha256(MAX_SVG_BYTES))
    # After a row lock wait, PostgreSQL checks this on the newest row: a repaired file keeps its hash.
    still_torn = Case(
        When(consistent, then=Value(False)),
        default=Value(True),
        output_field=BooleanField(),
    )
    # Durable: the change commits before the import fails, and nesting in a transaction is an error.
    with transaction.atomic(using=router.db_for_write(DataFile), durable=True):
        DataFile.objects.filter(still_torn, source=source, path__in=paths).update(
            hash=""
        )


def read_source_files(user, source: DataSource, prefix: str) -> Capture:
    """Return the files below the prefix, or raise ArtworkError when they are not one coherent sync."""
    rows = list(_files(user, source, prefix))
    if not rows:
        source.refresh_from_db(fields=["status"])
        run = SourceSyncRun.objects.filter(source=source).values("finished").first()
        state = {
            "status": source.status,
            "has_run": run is not None,
            "run_finished": run and run["finished"],
        }
        if issues := _state_issues(state):
            raise ArtworkError(issues)
        raise _unavailable(prefix)
    if issues := _state_issues(rows[0]):
        raise ArtworkError(issues)
    if not rows[0]["all_viewable"]:
        raise _unavailable(prefix)
    if issues := _limit_issues(rows[0]):
        raise ArtworkError(issues)
    issues = [
        Issue(row["path"], None, message)
        for row in rows
        for message in _file_issues(row)
    ]
    if torn := [issue.path for issue in issues if issue.message == CHANGED_FILE]:
        _read_again_at_next_sync(source, torn)
    if issues:
        raise ArtworkError(issues)
    entries = [(row["path"], bytes(row["payload"])) for row in rows]
    return Capture(source, rows[0]["synced"], entries)
