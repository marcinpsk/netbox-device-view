"""Let one Data Source sync run at a time and record it, so capture never reads a mix of two syncs."""

import hashlib

from core.exceptions import SyncError
from core.signals import post_sync, pre_sync
from django.db import connection
from django.dispatch import receiver
from django.utils import timezone

from ..models import SourceSyncRun

# The first key of each two-key session lock; pg_locks shows it as an oid, so it stays positive.
_LOCK_CLASS = (
    int.from_bytes(
        hashlib.sha256(b"netbox_device_view.source_sync").digest()[:4], "big"
    )
    & 0x7FFFFFFF
)
BUSY = "Another sync of this Data Source is running."


def _holds_lock(cursor, source_pk: int) -> bool:
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'"
        " AND pid = pg_backend_pid() AND granted AND classid = %s::oid"
        " AND objid = %s::oid AND objsubid = 2)",
        [_LOCK_CLASS, source_pk],
    )
    return cursor.fetchone()[0]


@receiver(pre_sync)
def start_guarded_sync(sender, instance, **kwargs):
    """Take the session lock of the source, or fail the sync before it changes anything."""
    with connection.cursor() as cursor:
        # Session locks are reentrant; take it once, so one unlock releases it.
        if not _holds_lock(cursor, instance.pk):
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [_LOCK_CLASS, instance.pk]
            )
            if not cursor.fetchone()[0]:
                raise SyncError(BUSY)
    SourceSyncRun.objects.update_or_create(
        source_id=instance.pk,
        defaults={"started": timezone.now(), "finished": None},
    )


@receiver(post_sync)
def finish_guarded_sync(sender, instance, **kwargs):
    SourceSyncRun.objects.filter(source_id=instance.pk).update(finished=timezone.now())
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [_LOCK_CLASS, instance.pk])
