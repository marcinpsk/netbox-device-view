"""Concurrent imports and deletions of artwork revisions."""

import hashlib
import io
import shutil
import tempfile
import threading
import time

from django.core.files.storage import FileSystemStorage
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from users.models import User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.storage import lock_digest
from netbox_device_view.models import DefinitionRevision, RevisionImport

from .artwork_samples import DEVICE_MANIFEST, bundle, definition

FIRST = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44"><rect width="1" height="1"/></svg>'
SECOND = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44"><rect width="2" height="2"/></svg>'


def faces_bundle(key, front, rear):
    manifest = DEVICE_MANIFEST.split("connectors:")[0].replace("acme/sw-24", key)
    files = {"manifest.yaml": manifest, "front.svg": front, "rear.svg": rear}
    return bundle(definition(key, files))


def waiting_advisory_locks():
    """Return the number of advisory lock requests in this database that wait."""
    with connection.cursor() as cursor:
        # pg_locks shows the whole server, so a wait in another database must not count.
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
        return cursor.fetchone()[0]


class ImportLockOrderTests(TransactionTestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="admin", is_superuser=True)

    def wait_for_waiters(self, count):
        deadline = time.monotonic() + 10
        while waiting_advisory_locks() < count:
            self.assertLess(time.monotonic(), deadline, "imports did not wait")
            time.sleep(0.05)

    def test_imports_with_blobs_in_opposite_path_order_do_not_deadlock(self):
        bundles = [
            faces_bundle("acme/one", FIRST, SECOND),
            faces_bundle("acme/two", SECOND, FIRST),
        ]
        errors = []
        holding, release = threading.Event(), threading.Event()

        def hold_first_blob():
            try:
                with transaction.atomic():
                    lock_digest(hashlib.sha256(FIRST.encode()).hexdigest())
                    holding.set()
                    release.wait(20)
            finally:
                connection.close()

        def run_import(data):
            try:
                import_bundle(self.user, "concurrent.zip", io.BytesIO(data))
            except Exception as exc:  # noqa: BLE001  (the test reports any failure)
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_first_blob)
        holder.start()
        self.assertTrue(holding.wait(10))
        imports = [
            threading.Thread(target=run_import, args=(data,)) for data in bundles
        ]
        for count, thread in enumerate(imports, start=1):
            thread.start()
            self.wait_for_waiters(count)
        release.set()
        for thread in (holder, *imports):
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(DefinitionRevision.objects.count(), 2)


class PausingStorage(FileSystemStorage):
    """Media storage that holds the first read of a stored file until the test releases it."""

    reading = threading.Event()
    release = threading.Event()

    def _open(self, name, mode="rb"):
        if not self.reading.is_set():
            self.reading.set()
            self.release.wait(20)
        return super()._open(name, mode)


def waiting_row_locks():
    """Return the number of lock requests on rows or transactions in this database that wait."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype IN ('tuple', 'transactionid')"
            " AND NOT granted"
        )
        return cursor.fetchone()[0]


class DeleteDuringReimportTests(TransactionTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media)
        self.user = User.objects.create_user(username="admin", is_superuser=True)
        with override_settings(MEDIA_ROOT=self.media):
            import_bundle(self.user, "first.zip", io.BytesIO(bundle()))
        PausingStorage.reading.clear()
        PausingStorage.release.clear()

    def test_a_delete_waits_for_a_reimport_and_removes_its_import_record(self):
        storages = {
            "default": {
                "BACKEND": f"{__name__}.PausingStorage",
                "OPTIONS": {"location": self.media},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        }
        revision = DefinitionRevision.objects.get()
        errors, responses = [], []

        def reimport():
            try:
                import_bundle(self.user, "again.zip", io.BytesIO(bundle()))
            except Exception as exc:  # noqa: BLE001  (the test reports any failure)
                errors.append(exc)
            finally:
                connection.close()

        def delete():
            try:
                client = APIClient()
                client.force_authenticate(self.user)
                url = reverse(
                    "plugins-api:netbox_device_view-api:definitionrevision-detail",
                    args=[revision.pk],
                )
                responses.append(client.delete(url).status_code)
            except Exception as exc:  # noqa: BLE001  (the test reports any failure)
                errors.append(exc)
            finally:
                connection.close()

        with override_settings(MEDIA_ROOT=self.media, STORAGES=storages):
            importer = threading.Thread(target=reimport)
            importer.start()
            # The reimport has found the revision and now reads its stored files.
            self.assertTrue(PausingStorage.reading.wait(10))
            deleter = threading.Thread(target=delete)
            deleter.start()
            deadline = time.monotonic() + 3
            while waiting_row_locks() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            PausingStorage.release.set()
            for thread in (importer, deleter):
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(responses, [204])
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(RevisionImport.objects.exists())

    def test_a_reimport_after_a_delete_creates_the_revision_again(self):
        revision = DefinitionRevision.objects.get()
        errors = []
        deleted, release = threading.Event(), threading.Event()

        def delete():
            try:
                with transaction.atomic():
                    revision.delete()
                    deleted.set()
                    release.wait(20)
            finally:
                connection.close()

        def reimport():
            try:
                import_bundle(self.user, "again.zip", io.BytesIO(bundle()))
            except Exception as exc:  # noqa: BLE001  (the test reports any failure)
                errors.append(exc)
            finally:
                connection.close()

        with override_settings(MEDIA_ROOT=self.media):
            deleter = threading.Thread(target=delete)
            deleter.start()
            self.assertTrue(deleted.wait(10))
            importer = threading.Thread(target=reimport)
            importer.start()
            deadline = time.monotonic() + 3
            while waiting_row_locks() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            release.set()
            for thread in (deleter, importer):
                thread.join(30)
        self.assertEqual(errors, [])
        again = DefinitionRevision.objects.get()
        self.assertNotEqual(again.pk, revision.pk)
        self.assertEqual(again.digest, revision.digest)
        self.assertEqual(again.files.count(), 3)
        self.assertEqual(
            list(again.imports.values_list("source_name", flat=True)), ["again.zip"]
        )
