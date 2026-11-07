"""Concurrent imports that share blobs take the digest locks in one order."""

import hashlib
import io
import shutil
import tempfile
import threading
import time

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from users.models import User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.storage import lock_digest
from netbox_device_view.models import DefinitionRevision

from .artwork_samples import DEVICE_MANIFEST, bundle, definition

FIRST = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44"><rect width="1" height="1"/></svg>'
SECOND = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44"><rect width="2" height="2"/></svg>'


def faces_bundle(key, front, rear):
    manifest = DEVICE_MANIFEST.split("connectors:")[0].replace("acme/sw-24", key)
    files = {"manifest.yaml": manifest, "front.svg": front, "rear.svg": rear}
    return bundle(definition(key, files))


def waiting_advisory_locks():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
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
