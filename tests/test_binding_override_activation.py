"""Activation and binding overrides: activation refuses a revision without an override's connector, and both take the type lock."""

import io
import shutil
import tempfile
import threading
import time

from core.models import ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import ObjectPermission, Token, User
from utilities.exceptions import AbortRequest

from netbox_device_view.artwork import overrides
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.type_lock import lock_bulk_writes, lock_types
from netbox_device_view.models import Association, BindingOverride

from .artwork_samples import bundle, definition
from .permissions import grant
from .test_artwork_activation import ACTIVATE_URL, ActivationTestCase
from .test_artwork_device_view import FILES, FRONT_SVG, MANIFEST
from .test_artwork_import_concurrency import waiting_advisory_locks
from .test_binding_override_api import DETAIL as API_DETAIL
from .test_binding_override_api import LIST as API_LIST

ETH1 = """\
  con-eth1:
    face: front
    type: rj45
    label: "1"
    bindings:
      - {component: interface, name: eth1}
"""
assert ETH1 in MANIFEST
# A revision of the same definition key without the connector con-eth1.
WITHOUT_ETH1 = {
    **FILES,
    "manifest.yaml": MANIFEST.replace(ETH1, ""),
    "front.svg": FRONT_SVG.replace('id="con-eth1" ', ""),
}
REPLACEMENT = [{"component": "interface", "name": "eth2"}]


def add_override(connector_key="con-eth1", **scope):
    association = Association.objects.get()
    return BindingOverride.objects.create(
        definition=association.revision.definition,
        connector_key=connector_key,
        bindings=REPLACEMENT,
        **scope,
    )


class ActivationBlockTests(ActivationTestCase):
    def setUp(self):
        super().setUp()
        Association.objects.create(device_type=self.device_type, revision=self.revision)
        grant(self.user, Association, "view", "change")
        grant(self.user, BindingOverride, "view")
        self.candidate = self.import_files(WITHOUT_ETH1)

    def assertBlocked(self, response, *names):
        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "These binding overrides name a connector that the revision does not"
            f" have: {', '.join(names)}.",
            status_code=409,
        )
        self.assertEqual(Association.objects.get().revision, self.revision)

    def test_a_type_override_of_a_connector_that_the_candidate_lacks_blocks_it(self):
        add_override(device_type=self.device_type)

        self.assertBlocked(self.post(self.candidate), "ACME SW-24 · con-eth1")

    def test_an_instance_override_of_the_type_blocks_it_too(self):
        add_override(device_type=self.device_type)
        add_override(device=self.device)

        self.assertBlocked(
            self.post(self.candidate), "ACME SW-24 · con-eth1", "sw1 · con-eth1"
        )

    def test_overrides_of_connectors_that_the_candidate_has_do_not_block(self):
        kept = add_override("con-psu", device_type=self.device_type)

        response = self.post(self.candidate)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Association.objects.get().revision, self.candidate)
        kept.refresh_from_db()
        self.assertEqual(
            (kept.connector_key, kept.bindings, kept.definition_id),
            ("con-psu", REPLACEMENT, self.revision.definition_id),
        )

    def test_an_override_of_another_type_does_not_block(self):
        other_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )
        add_override(device_type=other_type)
        add_override(device=self.make_device("sw48", other_type))

        self.assertEqual(self.post(self.candidate).status_code, 302)

    def test_a_first_activation_is_blocked_by_an_override_that_waits_for_artwork(
        self,
    ):
        add_override(device_type=self.device_type)
        Association.objects.all().delete()
        grant(self.user, Association, "add")

        response = self.post(self.candidate)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(Association.objects.exists())

    def test_a_blocking_override_that_the_user_cannot_view_is_not_named(self):
        add_override(device_type=self.device_type)
        add_override(device=self.device)
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.bindingoverride"
        ).update(constraints={"device__isnull": False})

        response = self.post(self.candidate)

        self.assertContains(
            response,
            "Binding overrides name a connector that the revision does not have."
            " Your permissions do not cover every affected object.",
            status_code=409,
        )
        self.assertNotContains(response, "· con-eth1", status_code=409)

    def test_a_blocking_override_of_a_device_that_the_user_cannot_view_is_not_named(
        self,
    ):
        add_override(device=self.device)
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "other"}
        )

        response = self.post(self.candidate)

        self.assertContains(
            response,
            "Your permissions do not cover every affected object.",
            status_code=409,
        )
        self.assertNotContains(response, "sw1 · con-eth1", status_code=409)


class WriterTests(ActivationTestCase):
    def test_the_writer_checks_the_type_of_a_device_that_changed_its_type(self):
        Association.objects.create(device_type=self.device_type, revision=self.revision)
        other_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )
        device = Device.objects.select_related("device_type").get(pk=self.device.pk)
        # A concurrent NetBox edit changes the type after the writer read the device.
        Device.objects.filter(pk=device.pk).update(device_type=other_type)

        with self.assertRaises(AbortRequest) as raised:
            overrides.save(
                self.admin,
                BindingOverride(
                    device=device, connector_key="con-eth1", bindings=REPLACEMENT
                ),
            )
        # NetBox marks the message of an AbortRequest safe, so it holds no name.
        self.assertEqual(
            raised.exception.message, "The type of the scope changed. Try again."
        )
        self.assertFalse(BindingOverride.objects.exists())


class ImportTests(ActivationTestCase):
    def test_a_new_revision_import_does_not_change_or_delete_an_override(self):
        Association.objects.create(device_type=self.device_type, revision=self.revision)
        override = add_override(device_type=self.device_type)
        stored = BindingOverride.objects.values().get(pk=override.pk)

        self.import_files(WITHOUT_ETH1)

        self.assertEqual(BindingOverride.objects.values().get(pk=override.pk), stored)
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(BindingOverride)
            ).exists()
        )


class TypeLockTests(TransactionTestCase):
    """A writer of an override and an activation wait for each other on the type lock."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="admin", is_superuser=True)
        self.client.force_login(self.user)
        self.revision, self.candidate = (
            import_bundle(
                self.user,
                "sw24.zip",
                io.BytesIO(bundle(definition("ACME/SW-24", files))),
            )[0].revision
            for files in (FILES, WITHOUT_ETH1)
        )
        self.device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        self.device = Device.objects.create(
            name="sw1",
            device_type=self.device_type,
            site=Site.objects.create(name="Site", slug="site"),
            role=DeviceRole.objects.create(name="Role", slug="role"),
        )
        Association.objects.create(device_type=self.device_type, revision=self.revision)

    def stored(self, connector_key="con-eth1"):
        return BindingOverride.objects.create(
            device=self.device,
            definition=self.revision.definition,
            connector_key=connector_key,
            bindings=REPLACEMENT,
        )

    def api(self):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        return client

    def while_locked(self, change, waiter, lock=None, while_waiting=None):
        """Run ``waiter`` in a thread while a transaction holds a lock, the type lock by default.

        ``change`` runs in that transaction before the thread starts, ``while_waiting`` while the thread waits.
        """
        result = {}

        def run():
            try:
                result["value"] = waiter()
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        with transaction.atomic():
            if lock is None:
                lock_types([self.device_type], "default")
            else:
                lock()
            change()
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 10
            while waiting_advisory_locks() < 1:
                self.assertLess(time.monotonic(), deadline, "the writer did not wait")
                self.assertTrue(thread.is_alive(), result)
                time.sleep(0.05)
            if while_waiting is not None:
                result["while_waiting"] = while_waiting()
        thread.join(30)
        return result

    def test_an_override_writer_that_waits_for_an_activation_checks_the_new_revision(
        self,
    ):
        def activate_candidate():
            Association.objects.filter(device_type=self.device_type).update(
                revision=self.candidate
            )

        result = self.while_locked(
            activate_candidate,
            lambda: overrides.save(
                self.user,
                BindingOverride(
                    device=self.device, connector_key="con-eth1", bindings=REPLACEMENT
                ),
            ),
        )

        self.assertIsInstance(result.get("error"), ValidationError)
        self.assertIn("has no connector con-eth1", str(result["error"]))
        self.assertFalse(BindingOverride.objects.exists())

    def test_an_activation_that_waits_for_an_override_writer_sees_the_override(self):
        def write_override():
            BindingOverride.objects.create(
                device_type=self.device_type,
                definition=self.revision.definition,
                connector_key="con-eth1",
                bindings=REPLACEMENT,
            )

        result = self.while_locked(
            write_override,
            lambda: self.client.post(
                reverse(ACTIVATE_URL, args=[self.candidate.pk]),
                {"device_type": self.device_type.pk},
            ),
        )

        self.assertContains(result["value"], "ACME SW-24 · con-eth1", status_code=409)
        self.assertEqual(Association.objects.get().revision, self.revision)

    def test_deleting_an_override_waits_for_the_type_lock(self):
        override = BindingOverride.objects.create(
            device_type=self.device_type,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=REPLACEMENT,
        )

        result = self.while_locked(lambda: None, override.delete)

        self.assertNotIn("error", result)
        self.assertFalse(BindingOverride.objects.exists())

    def test_a_bulk_api_write_waits_for_another_writer_of_several_overrides(self):
        first, second = self.stored(), self.stored("con-psu")
        api = self.api()

        result = self.while_locked(
            lambda: None,
            lambda: api.patch(
                reverse(API_LIST),
                [{"id": first.pk, "bindings": []}, {"id": second.pk, "bindings": []}],
                format="json",
            ),
            lock=lambda: lock_bulk_writes("default"),
        )

        self.assertEqual(result["value"].status_code, 200, result)
        self.assertEqual(
            list(BindingOverride.objects.values_list("bindings", flat=True)), [[], []]
        )

    def test_an_api_change_takes_the_type_lock_before_the_row_lock(self):
        override = self.stored()
        api = self.api()
        etag = api.get(reverse(API_DETAIL, args=[override.pk])).get("ETag")
        headers = {"HTTP_IF_MATCH": etag} if etag else {}

        def row_is_free():
            try:
                with transaction.atomic():
                    list(
                        BindingOverride.objects.select_for_update(nowait=True)
                        .filter(pk=override.pk)
                        .values_list("pk")
                    )
            except DatabaseError:
                return False
            return True

        result = self.while_locked(
            lambda: None,
            lambda: api.patch(
                reverse(API_DETAIL, args=[override.pk]),
                {"bindings": []},
                format="json",
                **headers,
            ),
            while_waiting=row_is_free,
        )

        self.assertTrue(result["while_waiting"])
        self.assertEqual(result["value"].status_code, 200, result)

    def test_a_writer_stops_at_once_when_the_device_is_locked(self):
        result = {}
        override = self.stored()
        with transaction.atomic():
            # A NetBox edit of the device holds its row.
            list(
                Device.objects.select_for_update()
                .filter(pk=self.device.pk)
                .values_list("pk")
            )
            thread = threading.Thread(target=lambda: self._delete(override, result))
            thread.start()
            thread.join(5)
            waited = thread.is_alive()
        thread.join(30)

        self.assertFalse(waited, "the writer waited for the device row")
        self.assertIsInstance(result.get("error"), AbortRequest)
        self.assertTrue(BindingOverride.objects.filter(pk=override.pk).exists())

    @staticmethod
    def _delete(override, result):
        try:
            override.delete()
        except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
            result["error"] = exc
        finally:
            connection.close()

    def test_an_add_that_waits_for_the_type_lock_checks_the_scope_again(self):
        user = User.objects.create_user(username="limited")
        grant(user, BindingOverride, "add", "view")
        grant(user, Device, "view", constraints={"name": "sw1"})

        result = self.while_locked(
            lambda: Device.objects.filter(pk=self.device.pk).update(name="hidden"),
            lambda: overrides.save(
                user,
                BindingOverride(
                    device=self.device, connector_key="con-eth1", bindings=REPLACEMENT
                ),
            ),
        )

        self.assertIsInstance(result.get("error"), ValidationError, result)
        self.assertIn("Select a device that you can view.", str(result["error"]))
        self.assertFalse(BindingOverride.objects.exists())
