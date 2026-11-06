"""Operators add, change and remove instance pins on the plugin pages."""

from core.models import ObjectChange, ObjectType
from dcim.models import Interface, Module, ModuleBay, ModuleType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from users.models import ObjectPermission

from netbox_device_view.artwork import pins
from netbox_device_view.models import (
    Association,
    BindingOverride,
    DefinitionRevision,
    InstancePin,
)

from .artwork_samples import DEVICE_FILES, DEVICE_MANIFEST, MODULE_FILES
from .permissions import grant
from .test_artwork_activation import ActivationTestCase
from .test_artwork_device_view import faces, ids, links
from .test_binding_override_pages import ADD_URL as OVERRIDE_ADD_URL
from .test_binding_override_pages import form_errors, rows

LIST_URL = "plugins:netbox_device_view:instancepin_list"
ADD_URL = "plugins:netbox_device_view:instancepin_add"
DETAIL_URL = "plugins:netbox_device_view:instancepin"
EDIT_URL = "plugins:netbox_device_view:instancepin_edit"
DELETE_URL = "plugins:netbox_device_view:instancepin_delete"
BULK_DELETE_URL = "plugins:netbox_device_view:instancepin_bulk_delete"
CHANGELOG_URL = "plugins:netbox_device_view:instancepin_changelog"
REVISION_DELETE_URL = "plugins:netbox_device_view:definitionrevision_delete"
ONE_INSTANCE = "Select one device or one installed module."


class PinPageTestCase(ActivationTestCase):
    def setUp(self):
        super().setUp()
        self.activate()
        # The sample revision has no connector con-r1; the revision of the test case has it.
        self.other = self.import_files(DEVICE_FILES)
        self.module_revision = self.import_files(MODULE_FILES, "ACME/NM-2X")
        self.sw2 = self.make_device("sw2")
        self.module = Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.create(device=self.device, name="Bay 1"),
            module_type=ModuleType.objects.create(
                manufacturer=self.manufacturer, model="NM-2X"
            ),
        )
        grant(self.user, Module, "view")
        grant(self.user, InstancePin, "view")

    def add(self, **data):
        return self.client.post(
            reverse(ADD_URL), {name: value.pk for name, value in data.items()}
        )

    def edit(self, pin, **data):
        return self.client.post(
            reverse(EDIT_URL, args=[pin.pk]),
            {name: value.pk for name, value in data.items()},
        )

    def pinned(self, **scope):
        return InstancePin.objects.create(**scope, revision=self.other)

    def has_r1(self, device=None):
        device = device or self.device
        return f"dv-o-d{device.pk}-front-con-r1" in ids(faces(self.tab(device))[0])

    def changes(self):
        return ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(InstancePin)
        ).order_by("time")


class AddTests(PinPageTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, InstancePin, "add")

    def test_a_device_pin_changes_the_view_of_its_device_only(self):
        response = self.add(device=self.device, device_revision=self.other)

        pin = InstancePin.objects.get()
        self.assertRedirects(response, reverse(DETAIL_URL, args=[pin.pk]))
        self.assertEqual(
            (pin.device, pin.module, pin.revision), (self.device, None, self.other)
        )
        self.assertFalse(self.has_r1())
        self.assertTrue(self.has_r1(self.sw2))
        [change] = self.changes()
        self.assertEqual((change.action, change.user), ("create", self.user))

    def test_an_installed_module_pin_gets_a_module_revision(self):
        response = self.add(module=self.module, module_revision=self.module_revision)

        self.assertEqual(response.status_code, 302, form_errors(response))
        pin = InstancePin.objects.get()
        self.assertEqual(
            (pin.module, pin.revision), (self.module, self.module_revision)
        )

    def test_the_add_page_has_a_tab_with_a_revision_of_each_kind(self):
        page = self.client.get(reverse(ADD_URL)).content.decode()

        for name in ("device", "device_revision", "module", "module_revision"):
            self.assertIn(f'name="{name}"', page)
        self.assertIn("kind", page)


class ValidationTests(PinPageTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, InstancePin, "add")

    def assertRejected(self, response, message):
        self.assertContains(response, message)
        self.assertFalse(InstancePin.objects.exists())

    def test_a_revision_of_the_other_kind_is_not_a_choice(self):
        self.assertRejected(
            self.add(device=self.device, device_revision=self.module_revision),
            "Select a valid choice.",
        )
        self.assertRejected(
            self.add(module=self.module, module_revision=self.other),
            "Select a valid choice.",
        )

    def test_the_writer_rejects_a_revision_of_the_other_kind(self):
        for pin, message in (
            (
                InstancePin(device=self.device, revision=self.module_revision),
                (
                    "The revision is a module definition. The device needs a device"
                    " definition."
                ),
            ),
            (
                InstancePin(module=self.module, revision=self.other),
                (
                    "The revision is a device definition. The installed module needs a"
                    " module definition."
                ),
            ),
        ):
            with self.assertRaises(ValidationError) as caught:
                pins.save(self.admin, pin)
            self.assertEqual(caught.exception.message_dict, {"revision": [message]})
        self.assertFalse(InstancePin.objects.exists())

    def test_a_pin_needs_exactly_one_instance(self):
        self.assertRejected(self.add(device_revision=self.other), ONE_INSTANCE)
        self.assertRejected(
            self.add(
                device=self.device,
                device_revision=self.other,
                module=self.module,
                module_revision=self.module_revision,
            ),
            ONE_INSTANCE,
        )
        with self.assertRaisesMessage(ValidationError, ONE_INSTANCE):
            pins.save(self.admin, InstancePin(revision=self.other))

    def test_the_database_refuses_a_pin_without_one_instance(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InstancePin.objects.create(
                device=self.device, module=self.module, revision=self.other
            )

    def test_a_pin_needs_a_revision(self):
        self.assertRejected(self.add(device=self.device), "This field is required.")

    def test_an_instance_has_one_pin(self):
        self.pinned(device=self.device)

        response = self.add(device=self.device, device_revision=self.revision)

        self.assertIn("already exists", " ".join(form_errors(response)))
        self.assertEqual(InstancePin.objects.get().revision, self.other)

    def test_an_instance_that_the_user_cannot_view_is_rejected(self):
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw2"}
        )

        self.assertRejected(
            self.add(device=self.device, device_revision=self.other),
            "Select a valid choice.",
        )
        with self.assertRaisesMessage(
            ValidationError, "Select a device that you can view."
        ):
            pins.save(self.user, InstancePin(device=self.device, revision=self.other))
        self.assertFalse(InstancePin.objects.exists())

    def test_a_revision_that_the_user_cannot_view_is_rejected(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.definitionrevision"
        ).update(constraints={"pk": self.revision.pk})

        self.assertRejected(
            self.add(device=self.device, device_revision=self.other),
            "Select a valid choice.",
        )
        with self.assertRaisesMessage(
            ValidationError, "Select a definition revision that you can view."
        ):
            pins.save(self.user, InstancePin(device=self.device, revision=self.other))


class ChangeAndRemoveTests(PinPageTestCase):
    def setUp(self):
        super().setUp()
        self.pin = self.pinned(device=self.device)

    def test_an_operator_changes_the_revision_of_a_pin(self):
        grant(self.user, InstancePin, "change")
        page = self.client.get(reverse(EDIT_URL, args=[self.pin.pk]))
        self.assertContains(page, f'value="{self.other.pk}"')

        response = self.edit(self.pin, device_revision=self.revision)

        self.assertRedirects(response, reverse(DETAIL_URL, args=[self.pin.pk]))
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.revision, self.revision)
        self.assertTrue(self.has_r1())
        change = self.changes().last()
        self.assertEqual(change.action, "update")
        self.assertEqual(
            (change.prechange_data["revision"], change.postchange_data["revision"]),
            (self.other.pk, self.revision.pk),
        )

    def test_the_instance_of_a_pin_does_not_change(self):
        grant(self.user, InstancePin, "change")

        self.edit(self.pin, device=self.sw2, device_revision=self.revision)

        self.pin.refresh_from_db()
        self.assertEqual(
            (self.pin.device, self.pin.revision), (self.device, self.revision)
        )
        self.pin.device = self.sw2
        with self.assertRaisesMessage(
            ValidationError,
            "The device or the installed module of a pin cannot change.",
        ):
            pins.save(self.admin, self.pin)

    def test_an_operator_removes_a_pin(self):
        grant(self.user, InstancePin, "delete")

        response = self.client.post(
            reverse(DELETE_URL, args=[self.pin.pk]), {"confirm": True}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(InstancePin.objects.exists())
        self.assertTrue(self.has_r1())
        self.assertEqual(self.changes().last().action, "delete")

    def test_bulk_delete_removes_the_selected_pins(self):
        grant(self.user, InstancePin, "delete")
        module_pin = InstancePin.objects.create(
            module=self.module, revision=self.module_revision
        )
        kept = self.pinned(device=self.sw2)

        response = self.client.post(
            reverse(BULK_DELETE_URL),
            {"pk": [self.pin.pk, module_pin.pk], "_confirm": True, "confirm": True},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(InstancePin.objects.all()), [kept])


class PermissionTests(PinPageTestCase):
    def setUp(self):
        super().setUp()
        self.pin = self.pinned(device=self.device)

    def test_viewing_a_device_grants_no_pin_permission(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.instancepin"
        ).delete()
        for url in (
            reverse(LIST_URL),
            reverse(ADD_URL),
            reverse(DETAIL_URL, args=[self.pin.pk]),
            reverse(EDIT_URL, args=[self.pin.pk]),
            reverse(DELETE_URL, args=[self.pin.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(
            self.add(device=self.sw2, device_revision=self.other).status_code, 403
        )
        self.assertEqual(
            self.edit(self.pin, device_revision=self.revision).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse(DELETE_URL, args=[self.pin.pk]), {"confirm": True}
            ).status_code,
            403,
        )
        self.assertEqual(list(InstancePin.objects.all()), [self.pin])
        self.assertEqual(InstancePin.objects.get().revision, self.other)

    def test_add_constraints_apply_to_the_saved_pin(self):
        grant(self.user, InstancePin, "add", constraints={"device__name": "sw1"})

        response = self.add(device=self.sw2, device_revision=self.other)

        self.assertContains(response, "permissions violation")
        self.assertEqual(list(InstancePin.objects.all()), [self.pin])
        self.assertFalse(self.changes().exists())

    def test_change_constraints_apply_to_the_saved_pin(self):
        # The user may change the pin only while it keeps the other revision.
        grant(self.user, InstancePin, "change", constraints={"revision": self.other.pk})

        response = self.edit(self.pin, device_revision=self.revision)

        self.assertContains(response, "permissions violation")
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.revision, self.other)

    def test_a_pin_of_an_instance_that_the_user_cannot_view_cannot_be_edited(self):
        grant(self.user, InstancePin, "change")
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw2"}
        )

        self.assertEqual(
            self.client.get(reverse(EDIT_URL, args=[self.pin.pk])).status_code, 404
        )
        self.assertEqual(
            self.edit(self.pin, device_revision=self.revision).status_code, 404
        )
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.revision, self.other)


class ListAndDetailTests(PinPageTestCase):
    def setUp(self):
        super().setUp()
        self.device_pin = self.pinned(device=self.sw2)
        self.module_pin = InstancePin.objects.create(
            module=self.module, revision=self.module_revision
        )

    def records(self, **query):
        response = self.client.get(reverse(LIST_URL), query)
        self.assertEqual(response.status_code, 200)
        return [row.record for row in response.context["table"].rows]

    def test_the_list_shows_each_pin(self):
        content = self.client.get(reverse(LIST_URL)).content.decode()

        self.assertIn(f'<a href="{self.sw2.get_absolute_url()}">sw2</a>', content)
        self.assertIn(self.module.get_absolute_url(), content)
        self.assertIn(self.other.digest[:10], content)
        self.assertIn(self.module_revision.get_absolute_url(), content)
        self.assertIn('id="filters-form"', content)

    def test_the_list_filters_by_instance_revision_definition_and_search(self):
        for query, expected in (
            ({"device_id": self.sw2.pk}, [self.device_pin]),
            ({"module_id": self.module.pk}, [self.module_pin]),
            ({"revision_id": self.other.pk}, [self.device_pin]),
            ({"definition": "acme/nm-2x"}, [self.module_pin]),
            (
                {"definition_id": self.module_revision.definition_id},
                [self.module_pin],
            ),
            ({"q": "sw2"}, [self.device_pin]),
            ({"q": "NM-2X"}, [self.module_pin]),
        ):
            self.assertEqual(self.records(**query), expected, query)

    def test_the_detail_page_shows_the_instance_the_key_and_the_revision(self):
        content = self.client.get(
            reverse(DETAIL_URL, args=[self.module_pin.pk])
        ).content.decode()

        self.assertIn("Installed module", content)
        self.assertIn(self.module.get_absolute_url(), content)
        self.assertIn(self.module_revision.definition.get_absolute_url(), content)
        self.assertIn(self.module_revision.digest, content)

    def test_the_changelog_page_lists_the_changes(self):
        self.assertEqual(
            self.client.get(
                reverse(CHANGELOG_URL, args=[self.device_pin.pk])
            ).status_code,
            200,
        )

    def test_the_menu_links_the_pins_for_a_user_who_can_view_them(self):
        page = self.client.get(self.device.get_absolute_url())

        self.assertContains(page, reverse(LIST_URL))


class RevisionDeleteTests(PinPageTestCase):
    def test_a_pinned_revision_cannot_be_deleted(self):
        grant(self.user, DefinitionRevision, "delete")
        self.pinned(device=self.sw2)

        response = self.client.post(
            reverse(REVISION_DELETE_URL, args=[self.other.pk]),
            {"confirm": True},
            follow=True,
        )

        self.assertContains(response, "Unable to delete")
        self.assertContains(response, "sw2 →")
        self.assertTrue(DefinitionRevision.objects.filter(pk=self.other.pk).exists())

    def test_a_revision_without_a_pin_or_an_association_can_be_deleted(self):
        grant(self.user, DefinitionRevision, "delete")

        self.client.post(
            reverse(REVISION_DELETE_URL, args=[self.other.pk]), {"confirm": True}
        )

        self.assertFalse(DefinitionRevision.objects.filter(pk=self.other.pk).exists())


class PinnedInstanceOverrideTests(PinPageTestCase):
    """An instance override takes its definition from the pin of its instance, as rendering does."""

    def setUp(self):
        super().setUp()
        grant(self.user, BindingOverride, "view", "add")
        self.eth2 = Interface.objects.create(
            device=self.device, name="eth2", type="1000base-t"
        )
        # A key that has con-eth1, but not con-r1 of the revision of the type.
        self.other_key = self.import_files(
            {
                **DEVICE_FILES,
                "manifest.yaml": DEVICE_MANIFEST.replace(
                    "key: acme/sw-24", "key: acme/other"
                ),
            },
            "ACME/OTHER",
        )
        InstancePin.objects.create(device=self.device, revision=self.other_key)

    def add_override(self, connector_key):
        return self.client.post(
            reverse(OVERRIDE_ADD_URL),
            {
                "device": self.device.pk,
                "connector_key": connector_key,
                **rows(("interface", "eth2")),
            },
        )

    def test_an_override_of_a_pinned_device_takes_the_definition_of_the_pin(self):
        response = self.add_override("con-eth1")

        self.assertEqual(response.status_code, 302, form_errors(response))
        override = BindingOverride.objects.get()
        self.assertEqual(override.definition, self.other_key.definition)
        link = links(faces(self.tab())[0])[f"dv-o-d{self.device.pk}-front-con-eth1"]
        self.assertEqual(link.get("href"), self.eth2.get_absolute_url())

    def test_a_connector_that_only_the_revision_of_the_type_has_is_rejected(self):
        response = self.add_override("con-r1")

        self.assertContains(
            response, "The active revision of acme/other has no connector con-r1."
        )
        self.assertFalse(BindingOverride.objects.exists())

    def test_a_pinned_device_of_a_type_without_an_association_gets_an_override(self):
        Association.objects.all().delete()

        response = self.add_override("con-eth1")

        self.assertEqual(response.status_code, 302, form_errors(response))
        self.assertEqual(
            BindingOverride.objects.get().definition, self.other_key.definition
        )
