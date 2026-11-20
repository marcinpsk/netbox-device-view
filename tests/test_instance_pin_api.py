"""The REST API of instance pins writes through the same writer and checks as the plugin pages."""

from core.models import ObjectChange, ObjectType
from dcim.models import Module, ModuleBay, ModuleType
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view.models import DefinitionRevision, InstancePin

from .artwork_samples import DEVICE_FILES, MODULE_FILES
from .permissions import grant
from .test_api_artwork import ArtworkApiTestCase
from .test_instance_pin_rendering import import_revision

LIST = "plugins-api:netbox_device_view-api:instancepin-list"
DETAIL = "plugins-api:netbox_device_view-api:instancepin-detail"
REVISIONS = "plugins-api:netbox_device_view-api:definitionrevision-list"


class PinApiTestCase(ArtworkApiTestCase):
    def setUp(self):
        super().setUp()
        self.activate()
        admin = User.objects.get(username="admin")
        self.other = import_revision(admin, "ACME/SW-24", DEVICE_FILES)
        self.module_revision = import_revision(admin, "ACME/NM-2X", MODULE_FILES)
        self.sw2 = self.make_device("sw2")
        self.module = Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.create(device=self.device, name="Bay 1"),
            module_type=ModuleType.objects.create(
                manufacturer=self.manufacturer, model="NM-2X"
            ),
        )
        for model in (Module, DefinitionRevision, InstancePin):
            grant(self.user, model, "view")

    def post(self, data, status=201):
        response = self.api.post(reverse(LIST), data, format="json")
        self.assertEqual(response.status_code, status, response.content[:800])
        return response.json()

    def patch(self, pin, data, status=200):
        response = self.api.patch(reverse(DETAIL, args=[pin.pk]), data, format="json")
        self.assertEqual(response.status_code, status, response.content[:800])
        return response.json()

    def drawn(self, device=None):
        return self.member(device)["artwork"]["revision_id"]

    def stored(self, **scope):
        return InstancePin.objects.create(revision=self.other, **scope)

    def changes(self):
        return ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(InstancePin)
        )


class WriteTests(PinApiTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, InstancePin, "add", "change", "delete")

    def test_a_post_pins_a_device_that_the_artwork_api_then_draws(self):
        body = self.post(
            {
                "device": {"name": "sw1"},
                "revision": self.other.pk,
                "changelog_message": "Keep the old front",
            }
        )

        self.assertEqual(body["device"]["id"], self.device.pk)
        self.assertEqual(body["module"], None)
        self.assertEqual(body["revision"]["digest"], self.other.digest)
        self.assertEqual(self.drawn(), self.other.pk)
        self.assertEqual(self.drawn(self.sw2), self.revision.pk)
        change = self.changes().get()
        self.assertEqual(
            (change.action, change.user, change.message),
            ("create", self.user, "Keep the old front"),
        )

    def test_a_post_pins_an_installed_module(self):
        body = self.post(
            {"module": self.module.pk, "revision": self.module_revision.pk}
        )

        self.assertEqual(InstancePin.objects.get(pk=body["id"]).module, self.module)

    def test_a_patch_changes_the_revision(self):
        pin = self.stored(device=self.device)

        self.patch(pin, {"revision": self.revision.pk})

        self.assertEqual(self.drawn(), self.revision.pk)
        change = self.changes().get()
        self.assertEqual(change.prechange_data["revision"], self.other.pk)

    def test_a_patch_cannot_change_the_instance(self):
        pin = self.stored(device=self.device)

        for data in (
            {"device": self.sw2.pk},
            {"device": None, "module": self.module.pk},
        ):
            body = self.patch(pin, data, status=400)
            self.assertIn("cannot change", str(body))
        pin.refresh_from_db()
        self.assertEqual((pin.device, pin.module), (self.device, None))

    def test_a_delete_removes_the_pin(self):
        pin = self.stored(device=self.device)

        response = self.api.delete(reverse(DETAIL, args=[pin.pk]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(InstancePin.objects.exists())
        self.assertEqual(self.drawn(), self.revision.pk)

    def test_bulk_patch_and_bulk_delete_follow_the_same_rules(self):
        first = self.stored(device=self.device)
        second = self.stored(device=self.sw2)

        response = self.api.patch(
            reverse(LIST),
            [
                {"id": first.pk, "revision": self.revision.pk},
                {"id": second.pk, "revision": self.revision.pk},
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        response = self.api.patch(
            reverse(LIST),
            [
                {"id": first.pk, "revision": self.other.pk},
                {"id": second.pk, "revision": self.module_revision.pk},
            ],
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            set(InstancePin.objects.values_list("revision", flat=True)),
            {self.revision.pk},
        )

        response = self.api.delete(
            reverse(LIST), [{"id": first.pk}, {"id": second.pk}], format="json"
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(InstancePin.objects.exists())


class ValidationTests(PinApiTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, InstancePin, "add", "change")

    def errors(self, **data):
        return self.post(data, status=400)

    def test_the_revision_must_have_the_kind_of_the_instance(self):
        self.assertEqual(
            self.errors(device=self.device.pk, revision=self.module_revision.pk),
            {
                "revision": [
                    (
                        "The revision is a module definition. The device needs a"
                        " device definition."
                    )
                ]
            },
        )
        self.assertEqual(
            self.errors(module=self.module.pk, revision=self.other.pk),
            {
                "revision": [
                    (
                        "The revision is a device definition. The installed module"
                        " needs a module definition."
                    )
                ]
            },
        )

    def test_a_pin_needs_exactly_one_instance(self):
        message = "Select one device or one installed module."
        self.assertIn(message, str(self.errors(revision=self.other.pk)))
        self.assertIn(
            message,
            str(
                self.errors(
                    device=self.device.pk,
                    module=self.module.pk,
                    revision=self.other.pk,
                )
            ),
        )

    def test_an_instance_has_one_pin(self):
        self.stored(device=self.device)

        self.assertIn(
            "already exists",
            str(self.errors(device=self.device.pk, revision=self.revision.pk)),
        )

    def test_an_instance_that_the_user_cannot_view_is_rejected(self):
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw2"}
        )

        self.assertEqual(
            self.errors(device=self.device.pk, revision=self.module_revision.pk),
            {"device": ["Select a device that you can view."]},
        )
        self.assertFalse(InstancePin.objects.exists())

    def test_a_revision_that_the_user_cannot_view_is_rejected(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.definitionrevision"
        ).update(constraints={"pk": self.revision.pk})

        self.assertEqual(
            self.errors(device=self.device.pk, revision=self.module_revision.pk),
            {"revision": ["Select a definition revision that you can view."]},
        )

    def test_a_pin_of_a_hidden_instance_cannot_change(self):
        pin = self.stored(device=self.device)
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw2"}
        )

        body = self.patch(pin, {"revision": self.revision.pk}, status=400)

        self.assertEqual(body, {"device": ["Select a device that you can view."]})
        pin.refresh_from_db()
        self.assertEqual(pin.revision, self.other)


class PermissionTests(PinApiTestCase):
    def test_pin_management_needs_its_own_permissions(self):
        pin = self.stored(device=self.device)

        self.assertEqual(
            self.api.post(
                reverse(LIST),
                {"device": self.sw2.pk, "revision": self.other.pk},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.api.patch(
                reverse(DETAIL, args=[pin.pk]),
                {"revision": self.revision.pk},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.api.delete(reverse(DETAIL, args=[pin.pk])).status_code, 403
        )
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.instancepin"
        ).delete()
        self.assertEqual(self.api.get(reverse(LIST)).status_code, 403)
        self.assertEqual(InstancePin.objects.get().revision, self.other)

    def test_constraints_apply_to_the_saved_pin(self):
        grant(self.user, InstancePin, "add", constraints={"device__name": "sw2"})

        response = self.api.post(
            reverse(LIST),
            {"device": self.device.pk, "revision": self.other.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(InstancePin.objects.exists())
        self.assertFalse(self.changes().exists())
        self.post({"device": self.sw2.pk, "revision": self.other.pk})


class ReadTests(PinApiTestCase):
    def setUp(self):
        super().setUp()
        self.device_pin = self.stored(device=self.sw2)
        self.module_pin = InstancePin.objects.create(
            module=self.module, revision=self.module_revision
        )

    def ids(self, url=LIST, **query):
        response = self.api.get(reverse(url), query)
        self.assertEqual(response.status_code, 200)
        return [item["id"] for item in response.json()["results"]]

    def test_the_list_filters_by_instance_revision_definition_and_search(self):
        both = [self.device_pin.pk, self.module_pin.pk]
        self.assertEqual(self.ids(), both)
        self.assertEqual(self.ids(device_id=self.sw2.pk), [self.device_pin.pk])
        self.assertEqual(self.ids(module_id=self.module.pk), [self.module_pin.pk])
        self.assertEqual(self.ids(revision_id=self.other.pk), [self.device_pin.pk])
        self.assertEqual(self.ids(definition="acme/nm-2x"), [self.module_pin.pk])
        self.assertEqual(self.ids(q="sw2"), [self.device_pin.pk])

    def test_the_brief_form_has_the_id_the_display_and_the_revision(self):
        response = self.api.get(reverse(LIST), {"brief": 1})

        item = response.json()["results"][0]
        self.assertEqual(
            {key: item[key] for key in ("id", "display")},
            {"id": self.device_pin.pk, "display": f"sw2 → {self.other}"},
        )
        self.assertEqual(item["revision"]["id"], self.other.pk)

    def test_the_list_shows_only_the_pins_that_the_user_can_view(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.instancepin"
        ).update(constraints={"module__isnull": False})

        self.assertEqual(self.ids(), [self.module_pin.pk])

    def test_the_revision_list_filters_by_kind(self):
        self.assertEqual(self.ids(REVISIONS, kind="module"), [self.module_revision.pk])
