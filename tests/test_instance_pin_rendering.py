"""A pin keeps one device or one installed module on its revision: the Device View tab and the artwork API draw that revision."""

import io
from unittest.mock import patch

from django.urls import reverse
from lxml import etree

from netbox_device_view.artwork import pins, render
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import BindingOverride, DefinitionRevision, InstancePin

from .activation import activate
from .artwork_samples import DEVICE_FILES, bundle, definition
from .module_samples import NM_MANIFEST, NM_SVG
from .test_api_artwork import ArtworkApiTestCase
from .test_api_module_composition import ModuleApiTestCase
from .test_artwork_device_view import SVG_NS, TAB_URL, faces, ids

# A revision of the key of the module type of nm1 and nm2, with another colour.
NM_VARIANT = {
    "manifest.yaml": NM_MANIFEST,
    "installed.svg": NM_SVG.replace('stop-color="#555"', 'stop-color="#0a0"'),
}


def import_revision(admin, directory, files):
    [imported] = import_bundle(
        admin, "pin.zip", io.BytesIO(bundle(definition(directory, files)))
    )
    return imported.revision


class DevicePinTests(ArtworkApiTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.user.__class__.objects.get(username="admin")
        # The sample revision has no connector con-r1; the revision of the test case has it.
        self.other = import_revision(self.admin, "ACME/SW-24", DEVICE_FILES)
        self.sw2 = self.make_device("sw2")

    def revision_id(self, device):
        return self.member(device)["artwork"]["revision_id"]

    def front_ids(self, device):
        return ids(faces(self.tab(device))[0])

    def test_a_device_pin_draws_its_revision_on_the_tab_and_in_the_api(self):
        self.activate()

        InstancePin.objects.create(device=self.device, revision=self.other)

        self.assertEqual(self.revision_id(self.device), self.other.pk)
        self.assertEqual(self.revision_id(self.sw2), self.revision.pk)
        self.assertNotIn(f"dv-o-d{self.device.pk}-front-con-r1", self.front_ids(None))
        self.assertIn(f"dv-o-d{self.sw2.pk}-front-con-r1", self.front_ids(self.sw2))

    def test_a_pin_shows_artwork_for_a_device_type_without_an_association(self):
        tab_url = reverse(TAB_URL, kwargs={"pk": self.device.pk})

        InstancePin.objects.create(device=self.device, revision=self.other)

        page = self.client.get(self.device.get_absolute_url()).content.decode()
        self.assertIn(tab_url, page)
        self.assertEqual(len(faces(self.tab())), 2)
        self.assertEqual(self.revision_id(self.device), self.other.pk)
        self.assertEqual(self.member(self.sw2)["representation"], "unavailable")

    def test_a_pin_keeps_its_revision_after_the_association_changes(self):
        activate(self.admin, (self.revision, self.device_type))
        pins.save(self.admin, InstancePin(device=self.device, revision=self.revision))

        activate(self.admin, (self.other, self.device_type))

        self.assertEqual(self.revision_id(self.device), self.revision.pk)
        self.assertEqual(self.revision_id(self.sw2), self.other.pk)

    def test_deleting_the_pin_returns_the_device_to_the_active_revision(self):
        self.activate()
        pin = InstancePin.objects.create(device=self.device, revision=self.other)

        pin.delete()

        self.assertEqual(self.revision_id(self.device), self.revision.pk)


class ModulePinTests(ModuleApiTestCase):
    def setUp(self):
        super().setUp()
        self.variant = import_revision(self.admin, "ACME/NM-2X", NM_VARIANT)

    def module_artwork(self, module):
        return self.modules()[module.pk]["artwork"]

    def drawn(self, module):
        return etree.tostring(self.group(self.front(), module)).decode()

    def test_a_module_pin_draws_its_revision_on_the_tab_and_in_the_api(self):
        inventory = self.inventory

        InstancePin.objects.create(module=inventory.nm1, revision=self.variant)

        self.assertEqual(
            self.module_artwork(inventory.nm1)["revision_id"], self.variant.pk
        )
        self.assertEqual(
            self.module_artwork(inventory.nm2)["revision_id"],
            inventory.revisions["acme/nm-2x"].pk,
        )
        self.assertIn("#0a0", self.drawn(inventory.nm1))
        self.assertNotIn("#0a0", self.drawn(inventory.nm2))

    def test_a_module_pin_keeps_its_revision_after_the_association_changes(self):
        inventory = self.inventory
        pins.save(
            self.admin,
            InstancePin(
                module=inventory.nm1, revision=inventory.revisions["acme/nm-2x"]
            ),
        )

        activate(self.admin, (self.variant, inventory.nm_type))

        self.assertEqual(
            self.module_artwork(inventory.nm1)["revision_id"],
            inventory.revisions["acme/nm-2x"].pk,
        )
        self.assertEqual(
            self.module_artwork(inventory.nm2)["revision_id"], self.variant.pk
        )

    def test_a_pin_revision_deleted_after_its_read_draws_the_active_revision(self):
        inventory = self.inventory
        pin = InstancePin.objects.create(module=inventory.nm1, revision=self.variant)
        compose = render._Composer.__init__

        def read_then_unpin(composer, *args, **kwargs):
            # The page read the pin, then a delete of the pin and of its revision ran.
            compose(composer, *args, **kwargs)
            InstancePin.objects.filter(pk=pin.pk).delete()
            for revision in DefinitionRevision.objects.filter(pk=self.variant.pk):
                revision.delete()

        with (
            patch.object(render._Composer, "__init__", read_then_unpin),
            self.assertNoLogs("netbox_device_view.artwork.render", "WARNING"),
        ):
            front = self.front()
        body = self.group(front, inventory.nm1).find(f"{{{SVG_NS}}}a")
        self.assertEqual(body.get("class"), "dv-art-module-body")
        self.assertNotIn(
            "#0a0", etree.tostring(self.group(front, inventory.nm1)).decode()
        )

    def test_a_pin_of_another_definition_binds_with_the_overrides_of_that_key(self):
        inventory = self.inventory
        psu = inventory.revisions["acme/psu-1"]
        # The fan type has no artwork; its pin selects the artwork of another key.
        InstancePin.objects.create(module=inventory.fan, revision=psu)
        BindingOverride.objects.create(
            module=inventory.fan,
            definition=psu.definition,
            connector_key="con-inlet",
            bindings=[{"component": "powerport", "name": "Fan power"}],
        )

        fan = self.modules()[inventory.fan.pk]

        self.assertEqual(fan["representation"], "artwork")
        self.assertEqual(fan["artwork"]["definition_key"], "acme/psu-1")
        [inlet] = fan["connectors"]
        self.assertEqual(
            [binding["name"] for binding in inlet["bindings"]], ["Fan power"]
        )
