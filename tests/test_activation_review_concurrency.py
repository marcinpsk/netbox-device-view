"""Activation and concurrent writers: each thread has its own PostgreSQL connection and transaction.

A writer that runs first makes the activation wait for its locks and then ask
for a new review. A writer that comes during the activation waits for the
locks of the activation and applies after it. No activation applies a stale
review.
"""

import io
import shutil
import tempfile
import threading
import time

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    ModuleBay,
    PowerPort,
    RearPort,
    Site,
)
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.db.models.signals import post_save
from django.test import Client, TransactionTestCase, override_settings
from users.models import User

from netbox_device_view.artwork import overrides, pins
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import Association, BindingOverride, InstancePin

from .artwork_samples import bundle, definition
from .test_artwork_activation import NEWER, REVIEW_AGAIN, review_url, token_of
from .test_artwork_device_view import FILES, FRONT_SVG, MANIFEST

WAIT_SECONDS = 20
R1 = """\
  con-r1:
    face: front
    type: lc
    bindings:
      - {component: rearport, name: R1}
"""
assert R1 in MANIFEST
WITHOUT_R1 = {
    **FILES,
    "manifest.yaml": MANIFEST.replace(R1, ""),
    "front.svg": FRONT_SVG.replace('id="con-r1" ', ""),
}


def waiting_locks():
    """Return the number of lock requests in this database that wait."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
            " WHERE NOT l.granted AND a.datname = current_database()"
        )
        return cursor.fetchone()[0]


class ConcurrentActivationTestCase(TransactionTestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.admin = User.objects.create_user(username="admin", is_superuser=True)
        self.revision, self.newer = map(self.import_files, (FILES, NEWER))
        self.site = Site.objects.create(name="Site", slug="site")
        self.role = DeviceRole.objects.create(name="Role", slug="role")
        self.device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        self.device = self.make_device("sw1")
        self.eth1 = Interface.objects.create(
            device=self.device, name="eth1", type="1000base-t"
        )
        self.spare = RearPort.objects.create(
            device=self.device, name="spare", type="lc"
        )
        PowerPort.objects.create(device=self.device, name="PSU1")
        self.bay = ModuleBay.objects.create(
            device=self.device, name="Network Module", position="1"
        )
        self.association = Association.objects.create(
            device_type=self.device_type, revision=self.revision
        )
        self.url = review_url((self.newer, self.device_type))
        self.clients = [Client(), Client()]
        for client in self.clients:
            client.force_login(self.admin)
        self.paused = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def import_files(self, files):
        [imported] = import_bundle(
            self.admin, "sw24.zip", io.BytesIO(bundle(definition("ACME/SW-24", files)))
        )
        return imported.revision

    def make_device(self, name):
        return Device.objects.create(
            name=name, device_type=self.device_type, site=self.site, role=self.role
        )

    def token(self):
        token = token_of(self.clients[0].get(self.url))
        self.assertTrue(token)
        return token

    def start(self, name, target):
        results = {}

        def run():
            try:
                results["value"] = target()
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                results["error"] = exc
            finally:
                connections.close_all()

        thread = threading.Thread(target=run, name=name)
        thread.start()
        self.addCleanup(thread.join, WAIT_SECONDS)
        return thread, results

    def activate_in_thread(self, token, client=None, name="activate"):
        client = client or self.clients[0]
        return self.start(name, lambda: client.post(self.url, {"token": token}))

    def wait_for_a_waiter(self, thread):
        deadline = time.monotonic() + WAIT_SECONDS
        while waiting_locks() < 1:
            self.assertTrue(thread.is_alive(), "the thread did not wait")
            self.assertLess(time.monotonic(), deadline, "no transaction waited")
            time.sleep(0.05)

    def finish(self, thread, results):
        thread.join(WAIT_SECONDS)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", results)
        return results["value"]

    def pause_activation(self, name="activate"):
        """Hold the transaction of the named thread open after it writes the association."""

        def pause(sender, **kwargs):
            if threading.current_thread().name == name:
                self.paused.set()
                self.release.wait(WAIT_SECONDS)

        post_save.connect(pause, sender=Association, weak=False)
        self.addCleanup(post_save.disconnect, pause, sender=Association)

    def assertUnchanged(self):
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)

    def assertActivated(self):
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.newer)


class WriterFirstTests(ConcurrentActivationTestCase):
    """The writer holds its transaction open; the activation waits, then asks for a new review."""

    def activation_after(self, change):
        token = self.token()
        with transaction.atomic():
            change()
            activation = self.activate_in_thread(token)
            self.wait_for_a_waiter(activation[0])
        response = self.finish(*activation)
        self.assertContains(response, REVIEW_AGAIN, status_code=409)
        self.assertUnchanged()

    def test_a_new_device_of_the_type(self):
        self.activation_after(lambda: self.make_device("sw9"))

        self.assertTrue(Device.objects.filter(name="sw9").exists())

    def test_a_component_rename(self):
        self.activation_after(
            lambda: Interface.objects.filter(pk=self.eth1.pk).update(name="eth5")
        )

    def test_a_rename_of_an_unbound_component_into_a_binding_lookup(self):
        self.activation_after(
            lambda: RearPort.objects.filter(pk=self.spare.pk).update(name="R1")
        )

    def test_a_module_bay_position_change(self):
        self.activation_after(
            lambda: ModuleBay.objects.filter(pk=self.bay.pk).update(position="2")
        )

    def test_a_pin_change(self):
        self.activation_after(
            lambda: pins.save(
                self.admin, InstancePin(device=self.device, revision=self.revision)
            )
        )

        self.assertTrue(InstancePin.objects.exists())

    def test_an_override_change(self):
        self.activation_after(
            lambda: overrides.save(
                self.admin,
                BindingOverride(
                    device_type=self.device_type, connector_key="con-psu", bindings=[]
                ),
            )
        )


class ActivationFirstTests(ConcurrentActivationTestCase):
    """The activation holds its locks; the writer waits and applies after the activation."""

    def writer_during_activation(self, change):
        """Run ``change`` while the activation holds its locks; return what the writer raised, or None."""
        self.pause_activation()
        activation = self.activate_in_thread(self.token())
        self.assertTrue(self.paused.wait(WAIT_SECONDS))

        def write():
            with transaction.atomic():
                change()

        thread, results = self.start("writer", write)
        self.wait_for_a_waiter(thread)
        # The writer waits while the activation holds its locks.
        self.assertTrue(thread.is_alive())
        self.release.set()

        self.assertEqual(self.finish(*activation).status_code, 302)
        thread.join(WAIT_SECONDS)
        self.assertFalse(thread.is_alive())
        self.assertActivated()
        return results.get("error")

    def test_a_new_device_of_the_type_waits(self):
        self.assertIsNone(
            self.writer_during_activation(lambda: self.make_device("sw9"))
        )

        self.assertTrue(Device.objects.filter(name="sw9").exists())

    def test_a_rename_of_an_unbound_component_into_a_binding_lookup_waits(self):
        # con-r1 of the candidate looks up the rear port R1, which does not exist yet.
        self.assertIsNone(
            self.writer_during_activation(
                lambda: RearPort.objects.filter(pk=self.spare.pk).update(name="R1")
            )
        )

        self.assertEqual(RearPort.objects.get(pk=self.spare.pk).name, "R1")

    def test_a_module_bay_position_change_waits(self):
        self.assertIsNone(
            self.writer_during_activation(
                lambda: ModuleBay.objects.filter(pk=self.bay.pk).update(position="2")
            )
        )

    def test_a_pin_change_waits(self):
        self.assertIsNone(
            self.writer_during_activation(
                lambda: pins.save(
                    self.admin, InstancePin(device=self.device, revision=self.revision)
                )
            )
        )

        self.assertTrue(InstancePin.objects.filter(device=self.device).exists())

    def test_an_override_writer_waits_and_checks_the_new_revision(self):
        # The candidate has no con-r1; its binding finds no component, so nothing blocks.
        self.newer = self.import_files(WITHOUT_R1)
        self.url = review_url((self.newer, self.device_type))

        error = self.writer_during_activation(
            lambda: overrides.save(
                self.admin,
                BindingOverride(
                    device_type=self.device_type, connector_key="con-r1", bindings=[]
                ),
            )
        )

        self.assertIsInstance(error, ValidationError)
        self.assertIn("has no connector con-r1", str(error))
        self.assertFalse(BindingOverride.objects.exists())


class ConcurrentActivationTests(ConcurrentActivationTestCase):
    def test_two_activations_of_one_association_cannot_both_apply(self):
        first_token, second_token = self.token(), self.token()
        self.pause_activation("first")
        first = self.activate_in_thread(first_token, name="first")
        self.assertTrue(self.paused.wait(WAIT_SECONDS))
        second = self.activate_in_thread(second_token, self.clients[1], "second")
        self.wait_for_a_waiter(second[0])
        self.release.set()

        self.assertEqual(self.finish(*first).status_code, 302)
        self.assertContains(self.finish(*second), REVIEW_AGAIN, status_code=409)
        self.assertActivated()
