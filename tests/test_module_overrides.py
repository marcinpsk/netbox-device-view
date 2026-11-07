"""Module type and installed-module binding overrides, added through the pages and the API, apply to the connectors of placed modules."""

import shutil
import tempfile

from dcim.models import (
    Cable,
    Device,
    DeviceType,
    Interface,
    Module,
    ModuleBay,
    ModuleType,
    PowerPort,
)
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view.models import Association, BindingOverride, DefinitionRevision

from .module_samples import Inventory, import_definitions
from .permissions import grant
from .test_api_artwork import API_URL
from .test_artwork_activation import activate_through_review
from .test_artwork_device_view import TAB_URL, faces, links
from .test_binding_override_api import LIST as API_LIST
from .test_binding_override_pages import ADD_URL, form_errors, rows

TYPE_NAME = "Te1/{module}/type"
MODULE_NAME = "Te1/{module}/instance"


class ModuleOverrideTestCase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        admin = User.objects.create_user(username="admin", is_superuser=True)
        self.inventory = Inventory(admin, activate=False)
        self.revisions = import_definitions(admin)
        for module in (self.inventory.nm1, self.inventory.nm2):
            for suffix in ("type", "instance"):
                Interface.objects.create(
                    device=self.inventory.device,
                    module=module,
                    name=f"Te1/{module.module_bay.position}/{suffix}",
                    type="10gbase-x-sfpp",
                )
        self.user = User.objects.create_user(username="operator")
        for model in (Device, Interface, PowerPort, Cable, Module, ModuleBay):
            grant(self.user, model, "view")
        for model in (DeviceType, ModuleType, DefinitionRevision):
            grant(self.user, model, "view")
        grant(self.user, Association, "view", "add")
        grant(self.user, BindingOverride, "view", "add")
        self.client.force_login(self.user)
        token = Token.objects.create(user=self.user)
        self.api = APIClient()
        self.api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        # The operator reviews and activates the chassis and the network module together.
        response = activate_through_review(
            self.client,
            (self.revisions["acme/ch-4"], self.inventory.device_type),
            (self.revisions["acme/nm-2x"], self.inventory.nm_type),
        )
        self.assertEqual(response.status_code, 302)

    def page_links(self):
        """Return {module: the link of its con-p1 on the tab}."""
        response = self.client.get(
            reverse(TAB_URL, kwargs={"pk": self.inventory.device.pk})
        )
        found = links(faces(response.content.decode())[0])
        prefix = f"dv-o-d{self.inventory.device.pk}"
        return {
            module: found[f"{prefix}-m{module.pk}-installed-con-p1"].get("href")
            for module in (self.inventory.nm1, self.inventory.nm2)
        }

    def api_names(self):
        """Return {module: the names that its con-p1 binds in the artwork API}."""
        response = self.api.get(
            reverse(API_URL, kwargs={"pk": self.inventory.device.pk})
        )
        [member] = response.json()["members"]
        modules = {entry["module_id"]: entry for entry in member["artwork"]["modules"]}
        return {
            module: [
                binding["name"]
                for connector in modules[module.pk]["connectors"]
                for binding in connector["bindings"]
            ]
            for module in (self.inventory.nm1, self.inventory.nm2)
        }

    def url(self, module, suffix):
        name = f"Te1/{module.module_bay.position}/{suffix}"
        return self.inventory.interface(module, name).get_absolute_url()


class PageTests(ModuleOverrideTestCase):
    def add(self, name, **scope):
        response = self.client.post(
            reverse(ADD_URL),
            {
                **{field: value.pk for field, value in scope.items()},
                "connector_key": "con-p1",
                **rows(("interface", name)),
            },
        )
        self.assertEqual(response.status_code, 302, form_errors(response))

    def test_a_module_type_override_applies_to_each_module_of_the_type(self):
        inventory = self.inventory
        self.add(TYPE_NAME, module_type=inventory.nm_type)

        self.assertEqual(
            self.page_links(),
            {
                inventory.nm1: self.url(inventory.nm1, "type"),
                inventory.nm2: self.url(inventory.nm2, "type"),
            },
        )

    def test_an_installed_module_override_replaces_the_type_override_on_that_module(
        self,
    ):
        inventory = self.inventory
        self.add(TYPE_NAME, module_type=inventory.nm_type)
        self.add(MODULE_NAME, module=inventory.nm2)

        self.assertEqual(
            self.page_links(),
            {
                inventory.nm1: self.url(inventory.nm1, "type"),
                inventory.nm2: self.url(inventory.nm2, "instance"),
            },
        )
        self.assertEqual(
            set(
                BindingOverride.objects.values_list(
                    "module_type", "module", "definition"
                )
            ),
            {
                (
                    inventory.nm_type.pk,
                    None,
                    self.revisions["acme/nm-2x"].definition_id,
                ),
                (None, inventory.nm2.pk, self.revisions["acme/nm-2x"].definition_id),
            },
        )


class ApiTests(ModuleOverrideTestCase):
    def post(self, data):
        response = self.api.post(reverse(API_LIST), data, format="json")
        self.assertEqual(response.status_code, 201, response.content[:800])

    def test_the_overrides_resolve_artwork_then_module_type_then_installed_module(self):
        inventory = self.inventory
        self.assertEqual(
            self.api_names(),
            {
                inventory.nm1: ["TenGigabitEthernet1/1/1"],
                inventory.nm2: ["TenGigabitEthernet1/2/1"],
            },
        )
        bindings = [{"component": "interface", "name": TYPE_NAME}]
        self.post(
            {
                "module_type": inventory.nm_type.pk,
                "connector_key": "con-p1",
                "bindings": bindings,
            }
        )
        self.post(
            {
                "module": inventory.nm1.pk,
                "connector_key": "con-p1",
                "bindings": [{"component": "interface", "name": MODULE_NAME}],
            }
        )

        self.assertEqual(
            self.api_names(),
            {inventory.nm1: ["Te1/1/instance"], inventory.nm2: ["Te1/2/type"]},
        )
        self.assertEqual(
            self.page_links(),
            {
                inventory.nm1: self.url(inventory.nm1, "instance"),
                inventory.nm2: self.url(inventory.nm2, "type"),
            },
        )
