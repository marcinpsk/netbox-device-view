"""The system job that removes artwork blobs that no revision uses."""

import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from core.choices import JobIntervalChoices, JobStatusChoices
from core.models import Job
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from netbox.registry import registry

from netbox_device_view.artwork.storage import cleanup, lock_digest, store_blob
from netbox_device_view.jobs import ArtworkCleanupJob
from netbox_device_view.models import (
    ArtworkBlob,
    DefinitionRevision,
    RevisionFile,
    VisualDefinition,
)

OLD = timezone.now() - timedelta(days=2)


class MediaMixin:
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media)
        media_override = override_settings(MEDIA_ROOT=self.media)
        media_override.enable()
        self.addCleanup(media_override.disable)

    def path(self, blob):
        return Path(self.media, blob.name)

    def age(self, blob):
        ArtworkBlob.objects.filter(digest=blob.digest).update(created=OLD)
        os.utime(self.path(blob), (OLD.timestamp(), OLD.timestamp()))

    def use(self, blob):
        definition, _ = VisualDefinition.objects.get_or_create(
            key="acme/x", kind="device"
        )
        revision = DefinitionRevision.objects.create(
            definition=definition, digest=uuid.uuid4().hex * 2, manifest={}
        )
        RevisionFile.objects.create(revision=revision, path="front.svg", blob=blob)


class CleanupTests(MediaMixin, TestCase):
    def test_an_old_unused_blob_is_removed_with_its_file(self):
        blob = store_blob(b"<svg/>", "svg")
        self.age(blob)
        self.assertEqual(cleanup(), 1)
        self.assertFalse(ArtworkBlob.objects.exists())
        self.assertFalse(self.path(blob).exists())

    def test_used_and_recent_blobs_stay(self):
        used = store_blob(b"<svg>used</svg>", "svg")
        self.age(used)
        self.use(used)
        recent = store_blob(b"<svg>recent</svg>", "svg")
        self.assertEqual(cleanup(), 0)
        self.assertEqual(ArtworkBlob.objects.count(), 2)
        self.assertTrue(self.path(used).exists() and self.path(recent).exists())

    def test_an_old_file_without_a_blob_row_is_removed(self):
        blob = store_blob(b"<svg>aborted</svg>", "svg")
        self.age(blob)
        ArtworkBlob.objects.filter(digest=blob.digest).delete()
        foreign = Path(self.media, "artwork", "notes.txt")
        foreign.write_text("not a blob")
        os.utime(foreign, (OLD.timestamp(), OLD.timestamp()))
        self.assertEqual(cleanup(), 1)
        self.assertFalse(self.path(blob).exists())
        self.assertTrue(foreign.exists())

    def test_the_system_job_runs_the_cleanup_daily(self):
        self.assertEqual(
            registry["system_jobs"][ArtworkCleanupJob]["interval"],
            JobIntervalChoices.INTERVAL_DAILY,
        )
        blob = store_blob(b"<svg/>", "svg")
        self.age(blob)
        job = Job.objects.create(name=ArtworkCleanupJob.name, job_id=uuid.uuid4())
        ArtworkCleanupJob.handle(job)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertFalse(ArtworkBlob.objects.exists())


class CleanupLockTests(MediaMixin, TransactionTestCase):
    @staticmethod
    def wait_for_a_lock_wait(timeout=10):
        """Return True when a session waits for an advisory lock, as cleanup does at lock_digest()."""
        deadline = time.monotonic() + timeout
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute(
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                )
                if cursor.fetchone():
                    return True
                time.sleep(0.05)
        return False

    def test_cleanup_waits_for_an_import_that_holds_the_digest_lock(self):
        blob = store_blob(b"<svg>reused</svg>", "svg")
        self.age(blob)
        importing, finished = threading.Event(), threading.Event()
        release = threading.Event()
        results = {}

        def import_reusing_the_blob():
            try:
                with transaction.atomic():
                    lock_digest(blob.digest)
                    importing.set()
                    release.wait(10)
                    self.use(blob)
            finally:
                connection.close()

        def run_cleanup():
            try:
                results["removed"] = cleanup()
            finally:
                finished.set()
                connection.close()

        importer = threading.Thread(target=import_reusing_the_blob)
        importer.start()
        self.assertTrue(importing.wait(10))
        cleaner = threading.Thread(target=run_cleanup)
        cleaner.start()
        self.assertTrue(
            self.wait_for_a_lock_wait(), "cleanup did not wait for the lock"
        )
        self.assertFalse(finished.is_set())
        release.set()
        importer.join(10)
        cleaner.join(10)
        self.assertEqual(results["removed"], 0)
        self.assertTrue(ArtworkBlob.objects.filter(digest=blob.digest).exists())
        self.assertTrue(self.path(blob).exists())


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class OtherStorageCleanupTests(TestCase):
    def test_cleanup_works_without_a_filesystem_and_without_an_artwork_root(self):
        self.assertEqual(cleanup(), 0)
        blob = store_blob(b"<svg>memory</svg>", "svg")
        ArtworkBlob.objects.filter(digest=blob.digest).delete()
        self.assertEqual(cleanup(now=timezone.now() + timedelta(days=2)), 1)
        self.assertFalse(default_storage.exists(blob.name))
