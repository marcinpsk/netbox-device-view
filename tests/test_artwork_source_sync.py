"""The sync guard across database sessions, and the import from a Git Data Source end to end."""

import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from unittest import skipUnless

from core.choices import DataSourceStatusChoices, JobStatusChoices
from core.exceptions import SyncError
from core.jobs import SyncDataSourceJob
from core.models import DataFile, DataSource, Job
from core.signals import pre_sync
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rq import SimpleWorker
from users.models import User

from netbox_device_view.artwork import capture, importing, sync_guard
from netbox_device_view.artwork.errors import ArtworkError
from netbox_device_view.models import DefinitionRevision, SourceSyncRun

from .artwork_samples import DEVICE_FILES, FRONT_SVG, definition
from .test_artwork_import import DEVICE_DIR, expected_digest
from .test_artwork_source_import import (
    API_SOURCE_IMPORT,
    SOURCE_IMPORT_URL,
    private_sync_queue,
)

TIMEOUT = 30


class SourceSessionTestCase(TransactionTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for path, content in definition(DEVICE_DIR, DEVICE_FILES).items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(content)
        self.source = DataSource.objects.create(
            name="Artwork", type="local", source_url=f"file://{self.root}", enabled=True
        )

    def in_other_session(self, target):
        """Run the target in a thread, which has its own database connection."""

        def run():
            try:
                return target()
            finally:
                connection.close()

        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown)
        return executor.submit(run)


class SyncGuardTests(SourceSessionTestCase):

    def test_an_overlapping_sync_fails_before_it_changes_the_source(self):
        inside, release = threading.Event(), threading.Event()

        def hold(sender, instance, **kwargs):
            if threading.current_thread() is not threading.main_thread():
                inside.set()
                release.wait(TIMEOUT)

        # Connected after the guard, so the first sync holds the lock while it waits.
        pre_sync.connect(hold, weak=False)
        self.addCleanup(pre_sync.disconnect, hold)
        first = self.in_other_session(
            lambda: DataSource.objects.get(pk=self.source.pk).sync()
        )
        self.assertTrue(inside.wait(TIMEOUT))
        try:
            with self.assertRaisesMessage(SyncError, sync_guard.BUSY):
                DataSource.objects.get(pk=self.source.pk).sync()
            self.source.refresh_from_db()
            self.assertEqual(self.source.status, DataSourceStatusChoices.NEW)
            self.assertFalse(self.source.datafiles.exists())
            self.assertIsNone(SourceSyncRun.objects.get(source=self.source).finished)
        finally:
            release.set()
        first.result(TIMEOUT)

        self.source.refresh_from_db()
        self.assertEqual(self.source.status, DataSourceStatusChoices.COMPLETED)
        self.assertIsNotNone(SourceSyncRun.objects.get(source=self.source).finished)
        self.source.sync()

    def test_a_crashed_sync_releases_the_lock_and_blocks_capture_until_the_next_sync(
        self,
    ):
        self.source.sync()
        # pre_sync without post_sync: the worker stopped, and its connection closed.
        self.in_other_session(
            lambda: pre_sync.send(sender=DataSource, instance=self.source)
        ).result(TIMEOUT)
        user = User.objects.create_superuser(username="admin")
        with self.assertRaisesMessage(ArtworkError, capture.SYNC_RUNNING):
            capture.read_source_files(user, self.source, "")

        self.source.sync()

        self.assertEqual(
            len(capture.read_source_files(user, self.source, "").entries), 3
        )

    def test_a_later_sync_in_the_same_session_proceeds_and_releases_the_lock(self):
        # A failed sync in this session sent pre_sync and kept the lock.
        pre_sync.send(sender=DataSource, instance=self.source)

        self.source.sync()

        self.in_other_session(
            lambda: DataSource.objects.get(pk=self.source.pk).sync()
        ).result(TIMEOUT)

    def test_a_failed_sync_job_blocks_capture_until_a_retry_completes(self):
        queue = private_sync_queue(self)
        user = User.objects.create_superuser(username="admin")
        self.source.sync()
        # The backend raises when it reads the file, after the source is SYNCING.
        broken = self.root / DEVICE_DIR / "broken.svg"
        broken.symlink_to(self.root / "missing.svg")

        def run_sync_job():
            SyncDataSourceJob.enqueue(instance=self.source, user=user)
            SimpleWorker([queue], connection=queue.connection).work(burst=True)
            return Job.objects.filter(object_id=self.source.pk).latest("created")

        failed = run_sync_job()

        self.assertEqual(failed.status, JobStatusChoices.STATUS_ERRORED, failed.data)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, DataSourceStatusChoices.FAILED)
        self.assertIsNone(SourceSyncRun.objects.get(source=self.source).finished)
        with self.assertRaisesMessage(ArtworkError, "status is failed"):
            capture.read_source_files(user, self.source, "")

        broken.unlink()
        retried = run_sync_job()

        self.assertEqual(
            retried.status, JobStatusChoices.STATUS_COMPLETED, retried.data
        )
        self.assertIsNotNone(SourceSyncRun.objects.get(source=self.source).finished)
        self.assertEqual(
            len(capture.read_source_files(user, self.source, "").entries), 3
        )

    def test_a_failed_sync_job_releases_the_lock_for_another_worker(self):
        queue = private_sync_queue(self)
        user = User.objects.create_superuser(username="admin")
        (self.root / DEVICE_DIR / "broken.svg").symlink_to(self.root / "missing.svg")
        SyncDataSourceJob.enqueue(instance=self.source, user=user)
        SimpleWorker([queue], connection=queue.connection).work(burst=True)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, DataSourceStatusChoices.FAILED)
        (self.root / DEVICE_DIR / "broken.svg").unlink()

        self.in_other_session(
            lambda: DataSource.objects.get(pk=self.source.pk).sync()
        ).result(TIMEOUT)

        self.source.refresh_from_db()
        self.assertEqual(self.source.status, DataSourceStatusChoices.COMPLETED)


class TornFileTests(SourceSessionTestCase):
    """An import that finds a torn file commits the cleared hash, although the import fails."""

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        self.enterContext(override_settings(MEDIA_ROOT=media))
        self.user = User.objects.create_superuser(username="admin")
        self.source.sync()
        self.front = DataFile.objects.filter(path=f"{DEVICE_DIR}/front.svg")
        self.front.update(data=FRONT_SVG.replace("#333", "#222").encode())

    def api_import(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client.post(
            reverse(API_SOURCE_IMPORT), {"data_source": self.source.pk}, format="json"
        )

    def test_the_next_sync_reads_the_torn_file_that_a_failed_api_import_found(self):
        failed = self.api_import()

        self.assertEqual(failed.status_code, 400, failed.content)
        self.assertEqual(
            failed.json()["errors"],
            [
                {
                    "path": self.front.get().path,
                    "line": None,
                    "message": capture.CHANGED_FILE,
                }
            ],
        )
        stored = self.in_other_session(lambda: self.front.get().hash).result(TIMEOUT)
        self.assertEqual(stored, "")

        self.source.sync()

        self.assertEqual(bytes(self.front.get().data), FRONT_SVG.encode())
        imported = self.api_import()
        self.assertEqual(imported.status_code, 201, imported.content)
        self.assertEqual(
            DefinitionRevision.objects.get().digest, expected_digest(DEVICE_FILES)
        )

    def test_a_sync_that_repairs_the_file_after_capture_keeps_its_hash(self):
        recorded = self.front.get().hash
        holding, release = threading.Event(), threading.Event()

        def repair():
            with transaction.atomic():
                self.front.update(data=FRONT_SVG.encode())
                holding.set()
                release.wait(TIMEOUT)

        repaired = self.in_other_session(repair)
        self.assertTrue(holding.wait(TIMEOUT))
        try:
            failed = self.in_other_session(self.api_import)
            # The import captured the torn row and now waits for the row lock of the repair.
            for _ in range(TIMEOUT * 10):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                        " AND query LIKE 'UPDATE%%')"
                    )
                    if cursor.fetchone()[0]:
                        break
                threading.Event().wait(0.1)
            else:
                self.fail("The import did not wait for the row lock.")
        finally:
            release.set()
        repaired.result(TIMEOUT)

        self.assertContains(
            failed.result(TIMEOUT), capture.CHANGED_FILE, status_code=400
        )
        self.assertEqual(self.front.get().hash, recorded)
        self.assertEqual(bytes(self.front.get().data), FRONT_SVG.encode())

    def test_an_import_inside_a_transaction_fails_before_it_clears_a_hash(self):
        recorded = self.front.get().hash

        with (
            self.assertRaisesMessage(RuntimeError, "durable"),
            transaction.atomic(),
        ):
            importing.import_from_source(self.user, self.source, "")

        self.assertEqual(self.front.get().hash, recorded)


@skipUnless(
    find_spec("dulwich"),
    "NetBox's Git data backend needs the optional dulwich package, which is not installed.",
)
class GitArtworkImportTests(TransactionTestCase):
    def setUp(self):
        self.queue = private_sync_queue(self)
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media)
        self.enterContext(override_settings(MEDIA_ROOT=self.media))
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.client.force_login(User.objects.create_superuser(username="admin"))

    def commit(self, files):
        from dulwich import porcelain

        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        porcelain.init(str(self.repo))
        porcelain.add(str(self.repo))
        identity = b"Test <test@example.com>"
        porcelain.commit(
            str(self.repo), message=b"Add artwork", author=identity, committer=identity
        )

    def test_check_for_updates_then_import_from_a_git_source(self):
        self.commit({**definition(DEVICE_DIR, DEVICE_FILES), "README.md": "Art\n"})
        created = self.client.post(
            reverse("core:datasource_add"),
            {
                "name": "Artwork",
                "type": "git",
                "source_url": f"file://{self.repo}",
                "enabled": "on",
            },
        )
        self.assertEqual(created.status_code, 302)
        source = DataSource.objects.get(name="Artwork")

        page = self.client.get(reverse(SOURCE_IMPORT_URL), {"data_source": source.pk})
        self.assertContains(page, "Check for updates")
        self.assertEqual(self.client.post(page.context["sync_url"]).status_code, 302)
        worked = SimpleWorker([self.queue], connection=self.queue.connection).work(
            burst=True
        )
        self.assertTrue(worked)
        job = Job.objects.get(object_id=source.pk, name="Synchronization")
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.data)

        response = self.client.post(
            reverse(SOURCE_IMPORT_URL), {"data_source": source.pk, "prefix": "artwork"}
        )

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.digest, expected_digest(DEVICE_FILES))
        event = revision.imports.get()
        source.refresh_from_db()
        self.assertEqual(
            (event.data_source, event.path, event.last_synced),
            (source, DEVICE_DIR, source.last_synced),
        )
        self.assertNotContains(self.client.get(revision.get_absolute_url()), "ommit")
