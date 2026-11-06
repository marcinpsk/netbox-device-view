"""A writer of a pin takes the type lock of its instance, as the writers of binding overrides do."""

import io
import shutil
import tempfile
import threading

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from users.models import User
from utilities.exceptions import AbortRequest

from netbox_device_view.artwork import pins
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import DefinitionRevision, InstancePin

from . import test_binding_override_activation as override_locks
from .artwork_samples import DEVICE_FILES, bundle, definition
from .permissions import grant


class PinLockTests(TransactionTestCase):
    # A module reference, so that pytest does not collect the override tests again here.
    while_locked = override_locks.TypeLockTests.while_locked

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="admin", is_superuser=True)
        [imported] = import_bundle(
            self.user,
            "sw24.zip",
            io.BytesIO(bundle(definition("ACME/SW-24", DEVICE_FILES))),
        )
        self.revision = imported.revision
        manufacturer = Manufacturer.objects.create(name="ACME", slug="acme")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="SW-24", slug="sw-24"
        )
        self.other_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="SW-48", slug="sw-48"
        )
        self.device = Device.objects.create(
            name="sw1",
            device_type=self.device_type,
            site=Site.objects.create(name="Site", slug="site"),
            role=DeviceRole.objects.create(name="Role", slug="role"),
        )

    def save(self, user=None):
        return lambda: pins.save(
            user or self.user, InstancePin(device=self.device, revision=self.revision)
        )

    def test_an_add_that_waits_for_the_type_lock_checks_the_instance_again(self):
        user = User.objects.create_user(username="limited")
        grant(user, InstancePin, "add", "view")
        grant(user, DefinitionRevision, "view")
        grant(user, Device, "view", constraints={"name": "sw1"})

        result = self.while_locked(
            lambda: Device.objects.filter(pk=self.device.pk).update(name="hidden"),
            self.save(user),
        )

        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("Select a device that you can view.", str(result["error"]))
        self.assertFalse(InstancePin.objects.exists())

    def test_a_writer_that_waits_while_the_device_changes_type_stops(self):
        result = self.while_locked(
            lambda: Device.objects.filter(pk=self.device.pk).update(
                device_type=self.other_type
            ),
            self.save(),
        )

        self.assertIsInstance(result.get("error"), AbortRequest, result)
        self.assertIn("The type of the scope changed.", result["error"].message)
        self.assertFalse(InstancePin.objects.exists())

    def test_deleting_a_pin_waits_for_the_type_lock(self):
        pin = InstancePin.objects.create(device=self.device, revision=self.revision)

        result = self.while_locked(lambda: None, pin.delete)

        self.assertNotIn("error", result)
        self.assertFalse(InstancePin.objects.exists())

    def test_a_writer_stops_at_once_when_the_device_is_locked(self):
        result = {}

        def run():
            try:
                self.save()()
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        with transaction.atomic():
            # A NetBox edit of the device holds its row.
            list(
                Device.objects.select_for_update()
                .filter(pk=self.device.pk)
                .values_list("pk")
            )
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(5)
            waited = thread.is_alive()
        thread.join(30)

        self.assertFalse(waited, "the writer waited for the device row")
        self.assertIsInstance(result.get("error"), AbortRequest)
        self.assertFalse(InstancePin.objects.exists())
