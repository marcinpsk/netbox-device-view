"""The REST API of binding overrides writes through the same writer and checks as the plugin pages."""

from core.models import ObjectChange, ObjectType
from dcim.models import DeviceType, Interface
from django.urls import reverse
from users.models import ObjectPermission

from netbox_device_view.models import BindingOverride

from .permissions import grant
from .test_api_artwork import ArtworkApiTestCase

LIST = "plugins-api:netbox_device_view-api:bindingoverride-list"
DETAIL = "plugins-api:netbox_device_view-api:bindingoverride-detail"
ETH2 = [{"component": "interface", "name": "eth2"}]


class OverrideApiTestCase(ArtworkApiTestCase):
    def setUp(self):
        super().setUp()
        self.activate()
        self.eth2 = Interface.objects.create(
            device=self.device, name="eth2", type="1000base-t"
        )
        self.sw2 = self.make_device("sw2")
        self.sw2_eth1 = Interface.objects.create(
            device=self.sw2, name="eth1", type="1000base-t"
        )
        self.sw2_eth2 = Interface.objects.create(
            device=self.sw2, name="eth2", type="1000base-t"
        )
        grant(self.user, DeviceType, "view")
        grant(self.user, BindingOverride, "view")

    def post(self, data, status=201):
        response = self.api.post(reverse(LIST), data, format="json")
        self.assertEqual(response.status_code, status, response.content[:800])
        return response.json()

    def patch(self, override, data, status=200):
        response = self.api.patch(
            reverse(DETAIL, args=[override.pk]), data, format="json"
        )
        self.assertEqual(response.status_code, status, response.content[:800])
        return response.json()

    def found(self, device=None, key="con-eth1"):
        """Return the component ids of the bindings of a connector in the artwork API."""
        return [
            binding["object_id"]
            for binding in self.connectors(self.member(device))[key]["bindings"]
        ]

    def stored(self, **scope):
        return BindingOverride.objects.create(
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=ETH2,
            **scope,
        )


class WriteTests(OverrideApiTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, BindingOverride, "add", "change", "delete")

    def test_a_post_adds_a_type_override_that_the_artwork_api_applies(self):
        body = self.post(
            {
                "device_type": self.device_type.pk,
                "connector_key": "con-eth1",
                "bindings": ETH2,
                "changelog_message": "Ports are numbered from 2",
            }
        )

        self.assertEqual(body["definition"]["key"], "acme/sw-24")
        self.assertEqual(body["device_type"]["id"], self.device_type.pk)
        self.assertEqual((body["device"], body["module"]), (None, None))
        self.assertEqual(self.found(), [self.eth2.pk])
        self.assertEqual(self.found(self.sw2), [self.sw2_eth2.pk])
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(BindingOverride)
        )
        self.assertEqual(
            (change.action, change.user, change.message),
            ("create", self.user, "Ports are numbered from 2"),
        )

    def test_a_post_adds_an_instance_override_for_one_device(self):
        self.post(
            {
                "device": {"name": "sw1"},
                "connector_key": "con-eth1",
                "bindings": ETH2,
            }
        )

        self.assertEqual(self.found(), [self.eth2.pk])
        self.assertEqual(self.found(self.sw2), [self.sw2_eth1.pk])

    def test_a_patch_changes_the_bindings(self):
        override = self.stored(device=self.device)

        body = self.patch(override, {"bindings": []})

        self.assertEqual(body["bindings"], [])
        self.assertEqual(self.found(), [])
        change = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(BindingOverride)
        ).get()
        self.assertEqual(change.prechange_data["bindings"], ETH2)

    def test_a_patch_cannot_change_the_scope_or_the_connector(self):
        override = self.stored(device_type=self.device_type)

        for data in (
            {"device_type": None, "device": self.device.pk},
            {"connector_key": "con-psu"},
        ):
            body = self.patch(override, data, status=400)
            self.assertIn("cannot change", str(body))
        override.refresh_from_db()
        self.assertEqual(
            (override.device_type, override.connector_key),
            (self.device_type, "con-eth1"),
        )

    def test_the_definition_is_read_only(self):
        body = self.post(
            {
                "device_type": self.device_type.pk,
                "connector_key": "con-eth1",
                "bindings": ETH2,
                "definition": 999999,
            }
        )

        self.assertEqual(body["definition"]["id"], self.revision.definition_id)

    def test_a_delete_removes_the_override(self):
        override = self.stored(device=self.device)

        response = self.api.delete(reverse(DETAIL, args=[override.pk]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(BindingOverride.objects.exists())
        self.assertEqual(self.found(), [self.eth1.pk])

    def test_bulk_patch_and_bulk_delete_follow_the_same_rules(self):
        first = self.stored(device=self.device)
        second = self.stored(device=self.sw2)

        response = self.api.patch(
            reverse(LIST),
            [{"id": first.pk, "bindings": []}, {"id": second.pk, "bindings": []}],
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(
            list(BindingOverride.objects.values_list("bindings", flat=True)), [[], []]
        )
        response = self.api.patch(
            reverse(LIST),
            [{"id": first.pk, "bindings": [{"component": "fan", "name": "x"}]}],
            format="json",
        )
        self.assertEqual(response.status_code, 400)

        response = self.api.delete(
            reverse(LIST), [{"id": first.pk}, {"id": second.pk}], format="json"
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(BindingOverride.objects.exists())


class ValidationTests(OverrideApiTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, BindingOverride, "add")

    def errors(self, **data):
        return self.post(
            {"connector_key": "con-eth1", "bindings": ETH2, **data}, status=400
        )

    def test_the_type_needs_active_artwork(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )

        self.assertEqual(
            self.errors(device_type=other.pk),
            {
                "device_type": [
                    "ACME SW-48 has no active artwork. Activate a revision for it first."
                ]
            },
        )

    def test_the_connector_must_be_in_the_active_revision(self):
        self.assertEqual(
            self.errors(device=self.device.pk, connector_key="con-eth9"),
            {
                "connector_key": [
                    "The active revision of acme/sw-24 has no connector con-eth9."
                ]
            },
        )

    def test_the_bindings_follow_the_manifest_schema(self):
        for bindings, message in (
            ([{"component": "fan", "name": "x"}], "is not one of"),
            ([{"component": "interface"}], "'name' is a required property"),
            ([{"component": "interface", "name": "x" * 65}], "is too long"),
            ([{"component": "interface", "name": "eth1"}] * 17, "is too long"),
            ({"component": "interface"}, "is not of type 'array'"),
            ([{"component": "interface", "name": "eth{module}"}], "{module}"),
        ):
            body = self.errors(device=self.device.pk, bindings=bindings)
            self.assertEqual(list(body), ["bindings"], bindings)
            self.assertIn(message, " ".join(body["bindings"]), bindings)
        self.assertFalse(BindingOverride.objects.exists())

    def test_an_override_needs_exactly_one_scope(self):
        message = "Select one scope: a device type, a module type, a device or an installed module."
        self.assertIn(message, str(self.errors()))
        self.assertIn(
            message,
            str(self.errors(device_type=self.device_type.pk, device=self.device.pk)),
        )

    def test_a_second_override_of_a_connector_in_one_scope_is_rejected(self):
        self.post(
            {"device": self.device.pk, "connector_key": "con-eth1", "bindings": []}
        )

        self.assertIn(
            "This scope already has an override of this connector.",
            str(self.errors(device=self.device.pk)),
        )

    def test_a_scope_that_the_user_cannot_view_is_rejected(self):
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw2"}
        )

        body = self.errors(device=self.device.pk)

        self.assertEqual(list(body), ["device"])
        self.assertFalse(BindingOverride.objects.exists())


class PermissionTests(OverrideApiTestCase):
    def test_override_management_needs_its_own_permissions(self):
        override = self.stored(device=self.device)
        data = {"device": self.sw2.pk, "connector_key": "con-eth1", "bindings": ETH2}

        self.assertEqual(
            self.api.post(reverse(LIST), data, format="json").status_code, 403
        )
        self.assertEqual(
            self.api.patch(
                reverse(DETAIL, args=[override.pk]), {"bindings": []}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.api.delete(reverse(DETAIL, args=[override.pk])).status_code, 403
        )
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.bindingoverride"
        ).delete()
        self.assertEqual(self.api.get(reverse(LIST)).status_code, 403)
        self.assertEqual(BindingOverride.objects.get().bindings, ETH2)

    def test_constraints_apply_to_the_saved_override(self):
        grant(self.user, BindingOverride, "add", constraints={"device__name": "sw2"})

        response = self.api.post(
            reverse(LIST),
            {"device": self.device.pk, "connector_key": "con-eth1", "bindings": ETH2},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(BindingOverride.objects.exists())
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(BindingOverride)
            ).exists()
        )
        self.post(
            {"device": self.sw2.pk, "connector_key": "con-eth1", "bindings": ETH2}
        )


class ReadTests(OverrideApiTestCase):
    def setUp(self):
        super().setUp()
        self.type_override = self.stored(device_type=self.device_type)
        self.device_override = self.stored(device=self.sw2)

    def ids(self, **query):
        response = self.api.get(reverse(LIST), query)
        self.assertEqual(response.status_code, 200)
        return [item["id"] for item in response.json()["results"]]

    def test_the_list_filters_by_scope_definition_and_connector(self):
        both = [self.type_override.pk, self.device_override.pk]
        self.assertEqual(self.ids(), both)
        self.assertEqual(
            self.ids(device_type_id=self.device_type.pk), [self.type_override.pk]
        )
        self.assertEqual(self.ids(device_id=self.sw2.pk), [self.device_override.pk])
        self.assertEqual(self.ids(definition_id=self.revision.definition_id), both)
        self.assertEqual(self.ids(connector_key="con-psu"), [])
        self.assertEqual(self.ids(q="sw2"), [self.device_override.pk])

    def test_the_brief_form_has_the_id_the_display_and_the_connector(self):
        response = self.api.get(reverse(LIST), {"brief": 1})

        self.assertEqual(
            response.json()["results"][0],
            {
                "id": self.type_override.pk,
                "url": f"http://testserver{reverse(DETAIL, args=[self.type_override.pk])}",
                "display": "ACME SW-24 · con-eth1",
                "connector_key": "con-eth1",
            },
        )

    def test_the_list_shows_only_the_overrides_that_the_user_can_view(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.bindingoverride"
        ).update(constraints={"device__isnull": False})

        self.assertEqual(self.ids(), [self.device_override.pk])
