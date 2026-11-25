"""A delete of a binding override reads the override again after the type lock and the scope lock."""

import io
import shutil
import tempfile
import threading
import time

from core.models import ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.messages import get_messages
from django.db import connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.type_lock import lock_types
from netbox_device_view.models import SCOPE_TYPE_CHANGED, Association, BindingOverride

from .artwork_samples import bundle, definition
from .permissions import grant
from .test_artwork_device_view import FILES
from .test_artwork_import_concurrency import backend_pid, blocking_pids
from .test_binding_override_api import DETAIL as API_DETAIL
from .test_binding_override_api import LIST as API_LIST
from .test_binding_override_pages import BULK_DELETE_URL, DELETE_URL

REPLACEMENT = [{"component": "interface", "name": "eth2"}]


class DeleteAfterLockTests(TransactionTestCase):
    """The delete constraint of the user is on the device status; a writer changes the device while the delete waits."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        admin = User.objects.create_user(username="admin", is_superuser=True)
        revision = import_bundle(
            admin, "sw24.zip", io.BytesIO(bundle(definition("ACME/SW-24", FILES)))
        )[0].revision
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
            status="active",
        )
        Association.objects.create(device_type=self.device_type, revision=revision)
        self.override = BindingOverride.objects.create(
            device=self.device,
            definition=revision.definition,
            connector_key="con-eth1",
            bindings=REPLACEMENT,
        )
        self.user = User.objects.create_user(username="operator")
        grant(
            self.user,
            BindingOverride,
            "view",
            "delete",
            constraints={"device__status": "active"},
        )
        self.pids = {}

    def browser(self):
        client = Client()
        client.force_login(self.user)
        return client

    def api(self):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        return client

    def wait_until_blocked(self, waiter, holder):
        """Wait until the waiter's backend waits for a lock of the holder's backend."""
        deadline = time.monotonic() + 10
        while waiter not in self.pids or self.pids[holder] not in blocking_pids(
            self.pids[waiter]
        ):
            self.assertLess(
                time.monotonic(),
                deadline,
                f"the {waiter} did not wait for the {holder}",
            )
            time.sleep(0.05)

    def delete_while_locked(self, request, change):
        """Send ``request`` while a writer holds the type lock; ``change`` commits with that writer."""
        result = {}

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '20s'")
                self.pids["delete"] = backend_pid()
                result["response"] = request()
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        thread = threading.Thread(target=run)
        self.addCleanup(thread.join, 30)
        with transaction.atomic():
            lock_types([self.device_type], "default")
            self.pids["writer"] = backend_pid()
            thread.start()
            self.wait_until_blocked("delete", "writer")
            change()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "the delete did not end")
        self.assertNotIn("error", result)
        return result["response"]

    def stop_the_device(self):
        Device.objects.filter(pk=self.device.pk).update(status="offline")

    def change_the_bindings(self):
        BindingOverride.objects.filter(pk=self.override.pk).update(bindings=[])

    def change_the_device_type(self):
        Device.objects.filter(pk=self.device.pk).update(device_type=self.other_type)

    def recorded_bindings(self):
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(BindingOverride),
            action="delete",
        )
        return change.prechange_data["bindings"]

    def page_delete(self):
        client = self.browser()
        return lambda: client.post(
            reverse(DELETE_URL, args=[self.override.pk]), {"confirm": True}
        )

    def page_bulk_delete(self):
        client = self.browser()
        return lambda: client.post(
            reverse(BULK_DELETE_URL),
            {"pk": [self.override.pk], "_confirm": True, "confirm": True},
        )

    def api_delete(self):
        client = self.api()
        return lambda: client.delete(reverse(API_DETAIL, args=[self.override.pk]))

    def api_bulk_delete(self):
        client = self.api()
        return lambda: client.delete(
            reverse(API_LIST), [{"id": self.override.pk}], format="json"
        )

    def test_a_page_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(self.page_delete(), self.stop_the_device)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_page_bulk_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(
            self.page_bulk_delete(), self.stop_the_device
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_an_api_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(self.api_delete(), self.stop_the_device)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_an_api_bulk_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(
            self.api_bulk_delete(), self.stop_the_device
        )

        # NetBox 4.7 reports an id that the constraints exclude; earlier versions skip it.
        self.assertIn(response.status_code, (204, 400))
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_page_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(
            self.page_delete(), self.change_the_bindings
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recorded_bindings(), [])

    def test_a_page_bulk_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(
            self.page_bulk_delete(), self.change_the_bindings
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recorded_bindings(), [])

    def test_an_api_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(self.api_delete(), self.change_the_bindings)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.recorded_bindings(), [])

    def test_an_api_bulk_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(
            self.api_bulk_delete(), self.change_the_bindings
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.recorded_bindings(), [])

    def test_a_page_delete_stops_when_the_device_type_changes(self):
        response = self.delete_while_locked(
            self.page_delete(), self.change_the_device_type
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [str(m) for m in get_messages(response.wsgi_request)],
            [str(SCOPE_TYPE_CHANGED)],
        )
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_an_api_delete_stops_when_the_device_type_changes(self):
        response = self.delete_while_locked(
            self.api_delete(), self.change_the_device_type
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": str(SCOPE_TYPE_CHANGED)})
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def etag(self, client):
        etag = client.get(reverse(API_DETAIL, args=[self.override.pk])).get("ETag")
        if etag is None:
            self.skipTest("This NetBox version has no ETag.")
        return etag

    def test_an_api_delete_with_the_current_etag_removes_the_override(self):
        client = self.api()
        etag = self.etag(client)

        response = client.delete(
            reverse(API_DETAIL, args=[self.override.pk]), HTTP_IF_MATCH=etag
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(BindingOverride.objects.exists())

    def test_an_api_delete_checks_the_etag_on_the_current_override(self):
        client = self.api()
        etag = self.etag(client)

        def change():
            BindingOverride.objects.filter(pk=self.override.pk).update(
                bindings=[], last_updated=timezone.now()
            )

        response = self.delete_while_locked(
            lambda: client.delete(
                reverse(API_DETAIL, args=[self.override.pk]), HTTP_IF_MATCH=etag
            ),
            change,
        )

        self.assertEqual(response.status_code, 412)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())
