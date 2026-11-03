"""Activation and binding overrides: activation refuses a revision without an override's connector, and both take the type lock."""

import io
import shutil
import tempfile
import threading
import time

from core.models import ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view.artwork import overrides
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.type_lock import lock_types
from netbox_device_view.models import Association, BindingOverride

from .artwork_samples import bundle, definition
from .permissions import grant
from .test_artwork_activation import ACTIVATE_URL, ActivationTestCase
from .test_artwork_device_view import FILES, FRONT_SVG, MANIFEST
from .test_artwork_import_concurrency import waiting_advisory_locks

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

    def while_locked(self, change, waiter):
        """Run ``waiter`` in a thread while a transaction holds the type lock; ``change`` runs in that transaction."""
        result = {}

        def run():
            try:
                result["value"] = waiter()
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        with transaction.atomic():
            lock_types([self.device_type], "default")
            change()
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 10
            while waiting_advisory_locks() < 1:
                self.assertLess(time.monotonic(), deadline, "the writer did not wait")
                self.assertTrue(thread.is_alive(), result)
                time.sleep(0.05)
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
