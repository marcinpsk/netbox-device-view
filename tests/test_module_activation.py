"""Activation of a module definition for a module type, through the plugin pages."""

from core.models import ObjectChange, ObjectType
from dcim.models import Module, ModuleBay, ModuleType
from django.contrib.messages import get_messages
from django.urls import reverse
from users.models import ObjectPermission

from netbox_device_view.models import Association, BindingOverride

from .artwork_samples import MODULE_FILES, MODULE_MANIFEST, MODULE_SVG
from .permissions import grant
from .test_artwork_activation import (
    ACTIVATE_URL,
    ASSOCIATION_URL,
    LIST_URL,
    REVISION_URL,
    ActivationTestCase,
)

P1 = """\
connectors:
  con-p1:
    face: installed
    type: sfp-plus
    bindings:
      - {component: interface, name: "TenGigabitEthernet1/{module}/1"}
"""
assert P1 in MODULE_MANIFEST
# A revision of the same module definition key without the connector con-p1.
WITHOUT_P1 = {
    "manifest.yaml": MODULE_MANIFEST.replace(P1, ""),
    "installed.svg": MODULE_SVG.replace('id="con-p1" ', ""),
}
NEW_MODULE_SVG = MODULE_SVG.replace("#444", "#555")


class ModuleActivationTestCase(ActivationTestCase):
    def setUp(self):
        super().setUp()
        self.module_revision = self.import_files(MODULE_FILES, "ACME/NM-2X")
        self.module_type = ModuleType.objects.create(
            manufacturer=self.manufacturer, model="NM-2X"
        )
        grant(self.user, ModuleType, "view")

    def activate_module(self, revision=None, module_type=None):
        return self.client.post(
            reverse(ACTIVATE_URL, args=[(revision or self.module_revision).pk]),
            {"module_type": (module_type or self.module_type).pk},
        )


class ModuleActivationTests(ModuleActivationTestCase):
    def test_a_module_revision_activates_for_a_module_type(self):
        grant(self.user, Association, "view", "add")
        url = reverse(ACTIVATE_URL, args=[self.module_revision.pk])
        page = self.client.get(reverse(REVISION_URL, args=[self.module_revision.pk]))
        self.assertContains(page, url)
        form = self.client.get(url)
        self.assertContains(form, 'name="module_type"')
        self.assertNotContains(form, 'name="device_type"')

        response = self.activate_module()

        association = Association.objects.get()
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            [f"ACME NM-2X now shows {self.module_revision}."],
        )
        self.assertRedirects(response, reverse(ASSOCIATION_URL, args=[association.pk]))
        self.assertEqual(
            (association.module_type, association.device_type, association.revision),
            (self.module_type, None, self.module_revision),
        )
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(Association)
        )
        self.assertEqual((change.action, change.user), ("create", self.user))

    def test_a_revision_activates_only_for_a_type_of_its_kind(self):
        grant(self.user, Association, "view", "add")
        form = self.client.get(reverse(ACTIVATE_URL, args=[self.revision.pk]))
        self.assertContains(form, 'name="device_type"')
        self.assertNotContains(form, 'name="module_type"')

        self.assertEqual(self.activate_module(self.revision).status_code, 400)
        self.assertEqual(self.post(self.module_revision).status_code, 400)

        self.assertFalse(Association.objects.exists())

    def test_a_later_activation_of_a_module_type_needs_change_permission(self):
        grant(self.user, Association, "view", "add")
        self.activate_module()
        newer = self.import_files(
            {**MODULE_FILES, "installed.svg": NEW_MODULE_SVG}, "ACME/NM-2X"
        )

        self.assertEqual(self.activate_module(newer).status_code, 403)
        grant(self.user, Association, "change")
        self.assertEqual(self.activate_module(newer).status_code, 302)

        self.assertEqual(Association.objects.get().revision, newer)

    def test_a_module_type_that_the_user_cannot_view_is_rejected(self):
        grant(self.user, Association, "view", "add")
        ObjectPermission.objects.filter(name__startswith="dcim.moduletype").update(
            constraints={"model": "Other"}
        )

        self.assertEqual(self.activate_module().status_code, 400)
        self.assertFalse(Association.objects.exists())

        ObjectPermission.objects.filter(name__startswith="dcim.moduletype").update(
            constraints=None
        )
        self.assertEqual(self.activate_module().status_code, 302)


class ModuleOverrideBlockTests(ModuleActivationTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, Association, "view", "add", "change")
        grant(self.user, BindingOverride, "view")
        grant(self.user, Module, "view")
        self.assertEqual(self.activate_module().status_code, 302)
        self.module = Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.create(device=self.device, name="Bay 1"),
            module_type=self.module_type,
        )
        self.candidate = self.import_files(WITHOUT_P1, "ACME/NM-2X")

    def add_override(self, **scope):
        return BindingOverride.objects.create(
            definition=self.module_revision.definition,
            connector_key="con-p1",
            bindings=[{"component": "interface", "name": "Te1/{module}/1"}],
            **scope,
        )

    def test_overrides_of_the_type_and_of_its_modules_block_a_candidate_without_the_connector(
        self,
    ):
        self.add_override(module_type=self.module_type)
        self.add_override(module=self.module)

        response = self.activate_module(self.candidate)

        self.assertContains(
            response,
            "These binding overrides name a connector that the revision does not have:"
            f" ACME NM-2X · con-p1, sw1 {self.module} · con-p1.",
            status_code=409,
        )
        self.assertEqual(Association.objects.get().revision, self.module_revision)

    def test_an_override_of_a_module_that_the_user_cannot_view_is_not_named(self):
        self.add_override(module=self.module)
        ObjectPermission.objects.filter(name__startswith="dcim.module ").update(
            constraints={"pk": 0}
        )

        response = self.activate_module(self.candidate)

        self.assertContains(
            response,
            "Your permissions do not cover every affected object.",
            status_code=409,
        )
        self.assertNotContains(response, "con-p1", status_code=409)

    def test_an_override_of_a_module_on_a_hidden_device_is_not_named(self):
        self.add_override(module=self.module)
        ObjectPermission.objects.filter(name__startswith="dcim.device ").update(
            constraints={"name": "other"}
        )

        response = self.activate_module(self.candidate)

        self.assertContains(
            response,
            "Your permissions do not cover every affected object.",
            status_code=409,
        )
        self.assertNotContains(response, "con-p1", status_code=409)


class AssociationListTests(ModuleActivationTestCase):
    def test_the_list_filters_the_associations_by_module_type(self):
        grant(self.user, Association, "view", "add")
        self.post()
        self.activate_module()
        url = reverse(LIST_URL)

        listing = self.client.get(url, {"module_type_id": self.module_type.pk})

        self.assertContains(listing, self.module_type.get_absolute_url())
        self.assertNotContains(listing, self.device_type.get_absolute_url())
        self.assertContains(self.client.get(url), 'name="module_type_id"')
