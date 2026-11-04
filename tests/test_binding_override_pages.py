"""Operators add, change and remove binding overrides on the plugin pages."""

import io
import re

from core.models import ObjectChange, ObjectType
from dcim.models import (
    DeviceType,
    Interface,
    Module,
    ModuleBay,
    ModuleType,
)
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import Association, BindingOverride, VisualDefinition

from .artwork_samples import MODULE_FILES, bundle, definition
from .permissions import grant
from .test_artwork_device_view import ArtworkTestCase, faces, links

LIST_URL = "plugins:netbox_device_view:bindingoverride_list"
ADD_URL = "plugins:netbox_device_view:bindingoverride_add"
DETAIL_URL = "plugins:netbox_device_view:bindingoverride"
EDIT_URL = "plugins:netbox_device_view:bindingoverride_edit"
DELETE_URL = "plugins:netbox_device_view:bindingoverride_delete"
BULK_DELETE_URL = "plugins:netbox_device_view:bindingoverride_bulk_delete"
CHANGELOG_URL = "plugins:netbox_device_view:bindingoverride_changelog"
DEFINITION_URL = "plugins:netbox_device_view:visualdefinition"
ETH = "interface"


def rows(*bindings, initial=0):
    """Return the formset data of the binding rows: (component type, name) each."""
    data = {
        "binding-TOTAL_FORMS": len(bindings),
        "binding-INITIAL_FORMS": initial,
        "binding-MIN_NUM_FORMS": 0,
        "binding-MAX_NUM_FORMS": 16,
    }
    for index, (component, name) in enumerate(bindings):
        data[f"binding-{index}-component"] = component
        data[f"binding-{index}-name"] = name
    return data


def scope_data(scope):
    return {field: value.pk for field, value in scope.items()}


def form_errors(response):
    """Return the text of each error on a form page."""
    content = response.content.decode()
    return [
        " ".join(re.sub(r"<[^>]+>", " ", match).split())
        for match in re.findall(
            r'<(?:div|li|p)[^>]*class="[^"]*(?:invalid-feedback|alert-danger|errorlist|text-danger)[^"]*"[^>]*>(.*?)</(?:div|li|p|ul)>',
            content,
            re.DOTALL,
        )
    ]


class OverridePageTestCase(ArtworkTestCase):
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

    def add(self, *bindings, connector_key="con-eth1", **scope):
        return self.client.post(
            reverse(ADD_URL),
            {**scope_data(scope), "connector_key": connector_key, **rows(*bindings)},
        )

    def edit(self, override, *bindings, **extra):
        return self.client.post(
            reverse(EDIT_URL, args=[override.pk]),
            {**rows(*bindings, initial=len(override.bindings)), **extra},
        )

    def link(self, device=None, key="con-eth1"):
        device = device or self.device
        link = links(faces(self.tab(device))[0]).get(f"dv-o-d{device.pk}-front-{key}")
        return None if link is None else link.get("href")

    def changes(self):
        return ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(BindingOverride)
        ).order_by("time")


class AddTests(OverridePageTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, BindingOverride, "add")

    def test_a_type_override_changes_the_view_of_every_device_of_the_type(self):
        response = self.add((ETH, "eth2"), device_type=self.device_type)

        override = BindingOverride.objects.get()
        self.assertRedirects(response, reverse(DETAIL_URL, args=[override.pk]))
        self.assertEqual(
            (override.definition, override.connector_key, override.bindings),
            (
                self.revision.definition,
                "con-eth1",
                [{"component": ETH, "name": "eth2"}],
            ),
        )
        self.assertEqual(self.link(), self.eth2.get_absolute_url())
        self.assertEqual(self.link(self.sw2), self.sw2_eth2.get_absolute_url())
        [change] = self.changes()
        self.assertEqual((change.action, change.user), ("create", self.user))

    def test_an_htmx_add_answers_with_the_location_of_the_override(self):
        response = self.client.post(
            reverse(ADD_URL),
            {
                "device": self.device.pk,
                "connector_key": "con-eth1",
                **rows((ETH, "eth2")),
            },
            HTTP_HX_REQUEST="true",
        )

        override = BindingOverride.objects.get()
        self.assertEqual(response["HX-Location"], override.get_absolute_url())

    def test_an_instance_override_changes_the_view_of_its_device_only(self):
        self.add((ETH, "eth2"), device=self.device)

        self.assertEqual(self.link(), self.eth2.get_absolute_url())
        self.assertEqual(self.link(self.sw2), self.sw2_eth1.get_absolute_url())

    def test_an_override_with_no_rows_leaves_the_connector_without_a_binding(self):
        self.add(device_type=self.device_type)

        self.assertEqual(BindingOverride.objects.get().bindings, [])
        self.assertIsNone(self.link())

    def test_an_empty_row_is_left_out(self):
        self.add((ETH, "eth2"), ("", ""), device=self.device)

        self.assertEqual(
            BindingOverride.objects.get().bindings, [{"component": ETH, "name": "eth2"}]
        )

    def test_the_add_page_offers_each_component_type_of_the_manifest(self):
        page = self.client.get(reverse(ADD_URL)).content.decode()

        self.assertEqual(
            re.findall(r'<option value="(\w+)"', page.split('id="binding-rows"')[1]),
            [
                "interface",
                "consoleport",
                "consoleserverport",
                "powerport",
                "poweroutlet",
                "frontport",
                "rearport",
            ]
            * 2,
        )
        self.assertIn('name="binding-0-name"', page)
        self.assertIn('maxlength="64"', page)

    def test_each_input_of_a_binding_row_has_a_name(self):
        page = self.client.get(reverse(ADD_URL)).content.decode()
        inputs = re.findall(r"<(?:select|input)[^>]*binding-[^>]*>", page)
        # The first row, and the empty row that the script copies.
        self.assertEqual(len([i for i in inputs if 'type="hidden"' not in i]), 4)
        for tag in inputs:
            if 'type="hidden"' in tag:
                continue
            label = "Component type" if "-component" in tag else "Name"
            self.assertIn(f'aria-label="{label}"', tag)

    def test_a_module_type_and_an_installed_module_can_have_overrides(self):
        [imported] = import_bundle(
            User.objects.get(username="admin"),
            "nm.zip",
            io.BytesIO(bundle(definition("ACME/NM-2X", MODULE_FILES))),
        )
        module_type = ModuleType.objects.create(
            manufacturer=self.manufacturer, model="NM-2X"
        )
        Association.objects.create(module_type=module_type, revision=imported.revision)
        module = Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.create(device=self.device, name="Bay 1"),
            module_type=module_type,
        )
        grant(self.user, ModuleType, "view")
        grant(self.user, Module, "view")

        for scope in ({"module_type": module_type}, {"module": module}):
            response = self.add(
                (ETH, "Te1/{module}/1"), connector_key="con-p1", **scope
            )
            self.assertEqual(response.status_code, 302, form_errors(response))

        self.assertEqual(
            set(
                BindingOverride.objects.values_list(
                    "module_type", "module", "definition"
                )
            ),
            {
                (module_type.pk, None, imported.revision.definition_id),
                (None, module.pk, imported.revision.definition_id),
            },
        )


class ValidationTests(OverridePageTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, BindingOverride, "add")

    def assertRejected(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertFalse(BindingOverride.objects.exists())

    def test_a_type_without_active_artwork_is_rejected(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )

        self.assertRejected(
            self.add((ETH, "eth2"), device_type=other),
            "ACME SW-48 has no active artwork. Activate a revision for it first.",
        )

    def test_a_device_whose_type_has_no_active_artwork_is_rejected(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )

        self.assertRejected(
            self.add((ETH, "eth2"), device=self.make_device("sw48", other)),
            "ACME SW-48 has no active artwork.",
        )

    def test_a_connector_that_the_active_revision_does_not_have_is_rejected(self):
        self.assertRejected(
            self.add((ETH, "eth2"), connector_key="con-eth9", device=self.device),
            "The active revision of acme/sw-24 has no connector con-eth9.",
        )

    def test_a_second_override_of_a_connector_in_one_scope_is_rejected(self):
        self.add((ETH, "eth2"), device=self.device)

        response = self.add((ETH, "eth1"), device=self.device)

        self.assertContains(
            response, "This scope already has an override of this connector."
        )
        self.assertEqual(BindingOverride.objects.count(), 1)

    def test_an_override_needs_exactly_one_scope(self):
        message = "Select one scope: a device type, a module type, a device or an installed module."
        self.assertRejected(self.add((ETH, "eth2")), message)
        self.assertRejected(
            self.add((ETH, "eth2"), device_type=self.device_type, device=self.device),
            message,
        )

    def test_a_row_needs_both_the_component_type_and_the_name(self):
        self.assertRejected(
            self.add((ETH, ""), device=self.device),
            "Give the component type and the name, or clear both.",
        )

    def test_a_device_scope_cannot_use_the_module_token(self):
        self.assertRejected(
            self.add((ETH, "eth{module}"), device=self.device),
            "Only a module definition can use {module}.",
        )

    def test_a_name_with_a_control_character_is_rejected(self):
        self.assertRejected(
            self.add((ETH, "eth\u200b2"), device=self.device),
            "Text must not contain control or format characters.",
        )

    def test_more_rows_than_a_manifest_connector_allows_are_rejected(self):
        response = self.add(*[(ETH, f"eth{n}") for n in range(17)], device=self.device)

        self.assertContains(response, "16")
        self.assertFalse(BindingOverride.objects.exists())

    def test_a_scope_that_the_user_cannot_view_is_rejected(self):
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw2"}
        )

        response = self.add((ETH, "eth2"), device=self.device)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BindingOverride.objects.exists())


class ChangeAndRemoveTests(OverridePageTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, BindingOverride, "add")
        self.add((ETH, "eth2"), device_type=self.device_type)
        self.override = BindingOverride.objects.get()

    def test_an_operator_changes_the_bindings_of_an_override(self):
        grant(self.user, BindingOverride, "change")
        page = self.client.get(reverse(EDIT_URL, args=[self.override.pk]))
        self.assertContains(page, 'value="eth2"')

        response = self.edit(self.override, (ETH, "eth1"), ("consoleport", "eth1"))

        self.assertRedirects(response, reverse(DETAIL_URL, args=[self.override.pk]))
        self.override.refresh_from_db()
        self.assertEqual(
            self.override.bindings,
            [
                {"component": ETH, "name": "eth1"},
                {"component": "consoleport", "name": "eth1"},
            ],
        )
        self.assertIsNone(self.link())
        change = self.changes().last()
        self.assertEqual(change.action, "update")
        self.assertEqual(
            change.prechange_data["bindings"], [{"component": ETH, "name": "eth2"}]
        )

    def test_an_operator_clears_every_row_of_an_override(self):
        grant(self.user, BindingOverride, "change")

        self.edit(self.override, ("", ""))

        self.override.refresh_from_db()
        self.assertEqual(self.override.bindings, [])
        self.assertIsNone(self.link())

    def test_the_scope_and_the_connector_of_an_override_do_not_change(self):
        grant(self.user, BindingOverride, "change")

        self.edit(
            self.override,
            (ETH, "eth1"),
            device=self.device.pk,
            connector_key="con-psu",
        )

        self.override.refresh_from_db()
        self.assertEqual(
            (
                self.override.device_type,
                self.override.device,
                self.override.connector_key,
            ),
            (self.device_type, None, "con-eth1"),
        )

    def test_an_override_of_a_definition_that_is_no_longer_active_cannot_change(self):
        grant(self.user, BindingOverride, "change")
        other = VisualDefinition.objects.create(key="acme/other", kind="device")
        BindingOverride.objects.filter(pk=self.override.pk).update(definition=other)

        response = self.edit(self.override, (ETH, "eth1"))

        self.assertContains(
            response, "The active artwork of ACME SW-24 is acme/sw-24, not acme/other."
        )

    def test_an_operator_removes_an_override(self):
        grant(self.user, BindingOverride, "delete")

        response = self.client.post(
            reverse(DELETE_URL, args=[self.override.pk]), {"confirm": True}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(BindingOverride.objects.exists())
        self.assertEqual(self.link(), self.eth1.get_absolute_url())
        self.assertEqual(self.changes().last().action, "delete")

    def test_bulk_delete_removes_the_selected_overrides(self):
        grant(self.user, BindingOverride, "delete")
        self.add((ETH, "eth1"), device=self.device)
        kept = BindingOverride.objects.create(
            device=self.sw2,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[],
        )
        selected = BindingOverride.objects.exclude(pk=kept.pk)

        response = self.client.post(
            reverse(BULK_DELETE_URL),
            {"pk": [o.pk for o in selected], "_confirm": True, "confirm": True},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(BindingOverride.objects.all()), [kept])


class PermissionTests(OverridePageTestCase):
    def setUp(self):
        super().setUp()
        self.override = BindingOverride.objects.create(
            device_type=self.device_type,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[{"component": ETH, "name": "eth2"}],
        )

    def test_viewing_a_device_grants_no_override_permission(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.bindingoverride"
        ).delete()
        for url in (
            reverse(LIST_URL),
            reverse(ADD_URL),
            reverse(DETAIL_URL, args=[self.override.pk]),
            reverse(EDIT_URL, args=[self.override.pk]),
            reverse(DELETE_URL, args=[self.override.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.add((ETH, "eth1"), device=self.device).status_code, 403)
        self.assertEqual(self.edit(self.override, (ETH, "eth1")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse(DELETE_URL, args=[self.override.pk]), {"confirm": True}
            ).status_code,
            403,
        )
        self.override.refresh_from_db()
        self.assertEqual(self.override.bindings, [{"component": ETH, "name": "eth2"}])

    def test_add_constraints_apply_to_the_saved_override(self):
        grant(
            self.user,
            BindingOverride,
            "add",
            constraints={"connector_key": "con-psu"},
        )

        response = self.add((ETH, "eth1"), device=self.device)

        self.assertContains(response, "permissions violation")
        self.assertEqual(list(BindingOverride.objects.all()), [self.override])
        self.assertFalse(self.changes().exists())

    def test_change_constraints_apply_to_the_saved_override(self):
        # The user may change the override only while it keeps a binding to eth2.
        grant(
            self.user,
            BindingOverride,
            "change",
            constraints={"bindings__0__name": "eth2"},
        )

        response = self.edit(self.override, (ETH, "eth1"))

        self.assertContains(response, "permissions violation")
        self.override.refresh_from_db()
        self.assertEqual(self.override.bindings, [{"component": ETH, "name": "eth2"}])

    def test_an_override_of_a_scope_that_the_user_cannot_view_cannot_be_edited(self):
        grant(self.user, BindingOverride, "change")
        override = BindingOverride.objects.create(
            device=self.sw2,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[],
        )
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "sw1"}
        )

        self.assertEqual(
            self.client.get(reverse(EDIT_URL, args=[override.pk])).status_code, 404
        )
        self.assertEqual(self.edit(override, (ETH, "eth1")).status_code, 404)
        override.refresh_from_db()
        self.assertEqual(override.bindings, [])

    def test_an_override_outside_the_change_constraints_cannot_be_edited(self):
        grant(
            self.user, BindingOverride, "change", constraints={"device__isnull": False}
        )

        self.assertEqual(self.edit(self.override, (ETH, "eth1")).status_code, 404)


class ListAndDetailTests(OverridePageTestCase):
    def setUp(self):
        super().setUp()
        self.type_override = BindingOverride.objects.create(
            device_type=self.device_type,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[{"component": ETH, "name": "eth2"}],
        )
        self.device_override = BindingOverride.objects.create(
            device=self.sw2,
            definition=self.revision.definition,
            connector_key="con-psu",
            bindings=[],
        )

    def test_the_list_shows_each_override(self):
        content = self.client.get(reverse(LIST_URL)).content.decode()

        self.assertIn(
            f'<a href="{self.device_type.get_absolute_url()}">ACME SW-24</a>', content
        )
        self.assertIn(f'<a href="{self.sw2.get_absolute_url()}">sw2</a>', content)
        self.assertIn("Interface eth2", content)
        self.assertIn("No binding", content)

    def test_the_list_filters_by_scope_definition_and_search(self):
        for query, expected in (
            ({"device_type_id": self.device_type.pk}, [self.type_override]),
            ({"device_id": self.sw2.pk}, [self.device_override]),
            ({"connector_key": "con-psu"}, [self.device_override]),
            ({"q": "sw2"}, [self.device_override]),
            (
                {"definition_id": self.revision.definition_id},
                [self.type_override, self.device_override],
            ),
        ):
            response = self.client.get(reverse(LIST_URL), query)
            self.assertEqual(
                [row.record for row in response.context["table"].rows], expected, query
            )

    def test_the_list_has_a_filters_tab(self):
        self.assertContains(self.client.get(reverse(LIST_URL)), 'id="filters-form"')

    def test_the_detail_page_shows_the_scope_the_key_the_connector_and_the_bindings(
        self,
    ):
        content = self.client.get(
            reverse(DETAIL_URL, args=[self.type_override.pk])
        ).content.decode()

        self.assertIn(self.device_type.get_absolute_url(), content)
        self.assertIn(self.revision.definition.get_absolute_url(), content)
        self.assertIn("con-eth1", content)
        self.assertIn("Interface eth2", content)

    def test_the_changelog_page_lists_the_changes(self):
        self.assertEqual(
            self.client.get(
                reverse(CHANGELOG_URL, args=[self.type_override.pk])
            ).status_code,
            200,
        )

    def test_the_definition_page_lists_the_overrides_that_the_user_can_view(self):
        grant(self.user, VisualDefinition, "view")
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.bindingoverride"
        ).update(constraints={"device__isnull": True})

        content = self.client.get(
            reverse(DEFINITION_URL, args=[self.revision.definition_id])
        ).content.decode()

        self.assertIn(self.type_override.get_absolute_url(), content)
        self.assertNotIn(self.device_override.get_absolute_url(), content)

    def test_the_menu_links_the_overrides_for_a_user_who_can_view_them(self):
        page = self.client.get(self.device.get_absolute_url())

        self.assertContains(page, reverse(LIST_URL))

    def test_the_revision_page_lists_the_connectors_with_their_keys(self):
        grant(self.user, self.revision.__class__, "view")

        content = self.client.get(self.revision.get_absolute_url()).content.decode()

        for key in ("con-eth1", "con-psu", "con-r1", "con-f1"):
            self.assertIn(f">{key}<", content)
        self.assertIn("Interface eth1", content)
