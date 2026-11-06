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
    Module,
    ModuleBay,
    ModuleType,
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
from netbox_device_view.artwork.type_lock import lock_bulk_writes, lock_key, lock_types
from netbox_device_view.models import Association, BindingOverride, InstancePin

from .artwork_samples import MODULE_FILES, bundle, definition
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
        # In a transaction PostgreSQL keeps its first pg_stat_activity snapshot, without later backends.
        cursor.execute("SELECT pg_stat_clear_snapshot()")
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

    def import_files(self, files, directory="ACME/SW-24"):
        [imported] = import_bundle(
            self.admin, "more.zip", io.BytesIO(bundle(definition(directory, files)))
        )
        return imported.revision

    def module_association(self):
        """Install a module in the device, with an association of its type: a type of the dependency set."""
        module_type = ModuleType.objects.create(
            manufacturer=self.device_type.manufacturer, model="NM-2X"
        )
        Module.objects.create(
            device=self.device, module_bay=self.bay, module_type=module_type
        )
        return Association.objects.create(
            module_type=module_type,
            revision=self.import_files(MODULE_FILES, "ACME/NM-2X"),
        )

    def pinned_device(self):
        device = self.make_device("sw2")
        InstancePin.objects.create(device=device, revision=self.revision)
        return device

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

    def test_an_association_delete_of_a_dependency_type(self):
        association = self.module_association()

        self.activation_after(association.delete)

        self.assertFalse(Association.objects.filter(pk=association.pk).exists())

    def test_a_rename_of_a_pinned_device(self):
        device = self.pinned_device()

        self.activation_after(
            lambda: Device.objects.filter(pk=device.pk).update(name="sw2-renamed")
        )

    def test_a_delete_of_the_candidate_revision(self):
        self.activation_after(self.newer.delete)

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

    def test_an_association_delete_of_a_dependency_type_waits(self):
        association = self.module_association()

        self.assertIsNone(self.writer_during_activation(association.delete))

        self.assertFalse(Association.objects.filter(pk=association.pk).exists())

    def test_a_rename_of_a_pinned_device_waits(self):
        device = self.pinned_device()

        self.assertIsNone(
            self.writer_during_activation(
                lambda: Device.objects.filter(pk=device.pk).update(name="sw2-renamed")
            )
        )

    def test_a_rename_of_the_bay_of_a_pinned_module_waits(self):
        module_type = ModuleType.objects.create(
            manufacturer=self.device_type.manufacturer, model="NM-2X"
        )
        module_revision = self.import_files(MODULE_FILES, "ACME/NM-2X")
        # The review names the pinned module of a device that the selection does not affect.
        sw9 = Device.objects.create(
            name="sw9",
            device_type=DeviceType.objects.create(
                manufacturer=self.device_type.manufacturer, model="SW-9", slug="sw-9"
            ),
            site=self.site,
            role=self.role,
        )
        bay = ModuleBay.objects.create(device=sw9, name="visible-slot", position="1")
        InstancePin.objects.create(
            module=Module.objects.create(
                device=sw9, module_bay=bay, module_type=module_type
            ),
            revision=module_revision,
        )
        self.url = review_url(
            (self.newer, self.device_type), (module_revision, module_type)
        )

        self.assertIsNone(
            self.writer_during_activation(
                lambda: ModuleBay.objects.filter(pk=bay.pk).update(name="hidden-slot")
            )
        )

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
    def test_a_pin_delete_while_the_activation_waits_leaves_the_device_locked(self):
        # The active revision binds con-r1 to the rear port R1, which does not exist; the candidate drops con-r1.
        self.newer = self.import_files(WITHOUT_R1)
        self.url = review_url((self.newer, self.device_type))
        token = self.token()
        pin = InstancePin.objects.create(device=self.device, revision=self.revision)
        self.pause_activation()
        with transaction.atomic():
            # A bulk writer deletes the pin: the device is affected again when the activation runs.
            lock_bulk_writes("default")
            pin.delete()
            activation = self.activate_in_thread(token)
            self.wait_for_a_waiter(activation[0])
        self.assertTrue(self.paused.wait(WAIT_SECONDS))
        # A rename after the inspection would make con-r1 resolve, so its removal would block.
        rename = self.start(
            "writer",
            lambda: RearPort.objects.filter(pk=self.spare.pk).update(name="R1"),
        )
        self.wait_for_a_waiter(rename[0])
        self.release.set()

        self.assertEqual(self.finish(*activation).status_code, 302)
        self.finish(*rename)
        self.assertActivated()

    def test_an_activation_queues_behind_a_bulk_writer_without_a_deadlock(self):
        module_type = self.module_association().module_type
        # The dependency set of the activation holds both types.
        first, later = sorted((self.device_type, module_type), key=lock_key)
        token = self.token()
        with transaction.atomic():
            # A bulk writer of pins or overrides holds the bulk lock and the type lock of its first row.
            lock_bulk_writes("default")
            lock_types([later], "default")
            activation = self.activate_in_thread(token)
            self.wait_for_a_waiter(activation[0])
            # Its next row needs the other type lock, which the activation must not hold.
            lock_types([first], "default")

        self.assertEqual(self.finish(*activation).status_code, 302)
        self.assertActivated()

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
