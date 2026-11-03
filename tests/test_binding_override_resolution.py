"""Binding overrides change the bindings of a connector: the artwork default, then the type override, then the instance override."""

import io

from dcim.models import Interface, Module, ModuleBay, ModuleType
from users.models import User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.overrides import connector_bindings
from netbox_device_view.models import BindingOverride, VisualDefinition

from .artwork_samples import MODULE_FILES, bundle, definition
from .test_api_artwork import ArtworkApiTestCase
from .test_artwork_device_view import faces, links, wrapper

ETH = "interface"


def bindings(*names, component=ETH):
    return [{"component": component, "name": name} for name in names]


class OverrideTestCase(ArtworkApiTestCase):
    def setUp(self):
        super().setUp()
        self.activate()
        self.definition = self.revision.definition

    def override(self, connector_key="con-eth1", replacement=(), **scope):
        return BindingOverride.objects.create(
            definition=self.definition,
            connector_key=connector_key,
            bindings=list(replacement),
            **scope,
        )

    def interface(self, name, device=None):
        return Interface.objects.create(
            device=device or self.device, name=name, type="1000base-t"
        )

    def page_link(self, device=None, key="con-eth1", face="front"):
        """Return the href of the connector on the page, or None when it has no link."""
        device = device or self.device
        page = faces(self.tab(device))[("front", "rear").index(face)]
        link = links(page).get(f"dv-o-d{device.pk}-{face}-{key}")
        return None if link is None else link.get("href")

    def api_bindings(self, device=None, key="con-eth1"):
        return self.connectors(self.member(device))[key]["bindings"]


class PrecedenceTests(OverrideTestCase):
    def setUp(self):
        super().setUp()
        self.sw2 = self.make_device("sw2")
        self.sw1_eth2 = self.interface("eth2")
        self.sw1_eth3 = self.interface("eth3")
        self.sw2_eth1 = self.interface("eth1", self.sw2)
        self.sw2_eth2 = self.interface("eth2", self.sw2)

    def test_without_an_override_the_artwork_bindings_apply(self):
        self.assertEqual(self.page_link(), self.eth1.get_absolute_url())
        self.assertEqual(self.page_link(self.sw2), self.sw2_eth1.get_absolute_url())

    def test_a_type_override_applies_to_every_device_of_the_type(self):
        self.override(device_type=self.device_type, replacement=bindings("eth2"))

        for device, component in (
            (self.device, self.sw1_eth2),
            (self.sw2, self.sw2_eth2),
        ):
            self.assertEqual(self.page_link(device), component.get_absolute_url())
            [binding] = self.api_bindings(device)
            self.assertEqual(
                (binding["status"], binding["object_id"]), ("found", component.pk)
            )

    def test_an_instance_override_replaces_the_type_override_on_its_device_only(self):
        self.override(device_type=self.device_type, replacement=bindings("eth2"))
        self.override(device=self.device, replacement=bindings("eth3"))

        self.assertEqual(self.page_link(), self.sw1_eth3.get_absolute_url())
        self.assertEqual(
            [b["object_id"] for b in self.api_bindings()], [self.sw1_eth3.pk]
        )
        self.assertEqual(self.page_link(self.sw2), self.sw2_eth2.get_absolute_url())
        self.assertEqual(
            [b["object_id"] for b in self.api_bindings(self.sw2)], [self.sw2_eth2.pk]
        )

    def test_an_instance_override_without_a_type_override_replaces_the_artwork(self):
        self.override(device=self.device, replacement=bindings("eth3"))

        self.assertEqual(self.page_link(), self.sw1_eth3.get_absolute_url())
        self.assertEqual(self.page_link(self.sw2), self.sw2_eth1.get_absolute_url())

    def test_a_replacement_list_replaces_the_list_below_it_and_does_not_merge(self):
        self.override(
            device_type=self.device_type, replacement=bindings("eth1", "eth2")
        )
        self.assertEqual(
            [b["object_id"] for b in self.api_bindings()],
            [self.eth1.pk, self.sw1_eth2.pk],
        )
        self.override(device=self.device, replacement=bindings("eth3"))

        self.assertEqual(
            [b["object_id"] for b in self.api_bindings()], [self.sw1_eth3.pk]
        )

    def test_an_override_changes_only_its_connector(self):
        self.override(device_type=self.device_type, replacement=bindings("eth2"))

        self.assertEqual(
            self.page_link(key="con-psu", face="rear"), self.psu.get_absolute_url()
        )
        [binding] = self.api_bindings(key="con-psu")
        self.assertEqual(binding["object_id"], self.psu.pk)

    def test_an_override_of_another_definition_key_does_not_apply(self):
        other = VisualDefinition.objects.create(key="acme/other", kind="device")
        BindingOverride.objects.create(
            definition=other,
            connector_key="con-eth1",
            bindings=bindings("eth2"),
            device_type=self.device_type,
        )

        self.assertEqual(self.page_link(), self.eth1.get_absolute_url())

    def test_an_override_of_a_connector_that_the_revision_does_not_have_is_ignored(
        self,
    ):
        self.override("con-gone", device_type=self.device_type)

        self.assertEqual(
            sorted(self.connectors(self.member())),
            ["con-eth1", "con-f1", "con-psu", "con-r1"],
        )


class ResolutionTests(OverrideTestCase):
    def test_an_override_that_names_a_missing_component_is_a_missing_binding(self):
        self.override(device=self.device, replacement=bindings("eth9"))

        self.assertIsNone(self.page_link())
        page = faces(self.tab())[0]
        wrapped = wrapper(page, f"dv-o-d{self.device.pk}-front-con-eth1")
        self.assertEqual(wrapped.get("class"), "dv-art-connector dv-art-unresolved")
        [binding] = self.api_bindings()
        self.assertEqual(
            (binding["status"], binding["name"], binding["object_id"]),
            ("missing", "eth9", None),
        )

    def test_an_override_with_two_found_components_opens_the_chooser(self):
        self.override(
            device=self.device,
            replacement=[
                {"component": "interface", "name": "eth1"},
                {"component": "consoleport", "name": "eth1"},
            ],
        )

        page = faces(self.tab())[0]
        wrapped = wrapper(page, f"dv-o-d{self.device.pk}-front-con-eth1")
        self.assertEqual(wrapped.get("class"), "dv-art-connector dv-art-several")
        self.assertEqual(
            [b["object_id"] for b in self.api_bindings()],
            [self.eth1.pk, self.console.pk],
        )

    def test_an_empty_override_leaves_the_connector_without_a_binding(self):
        self.override(device_type=self.device_type, replacement=[])

        self.assertIsNone(self.page_link())
        page = faces(self.tab())[0]
        wrapped = wrapper(page, f"dv-o-d{self.device.pk}-front-con-eth1")
        self.assertEqual(wrapped.get("class"), "dv-art-connector dv-art-unresolved")
        self.assertEqual(self.api_bindings(), [])


class ModulePartTests(OverrideTestCase):
    def test_a_module_part_resolves_its_type_and_instance_overrides(self):
        [imported] = import_bundle(
            User.objects.get(username="admin"),
            "nm.zip",
            io.BytesIO(bundle(definition("ACME/NM-2X", MODULE_FILES))),
        )
        revision = imported.revision
        module_type = ModuleType.objects.create(
            manufacturer=self.manufacturer, model="NM-2X"
        )
        first, second = (
            Module.objects.create(
                device=self.device,
                module_bay=ModuleBay.objects.create(device=self.device, name=name),
                module_type=module_type,
            )
            for name in ("Bay 1", "Bay 2")
        )
        default = bindings("TenGigabitEthernet1/{module}/1")
        self.assertEqual(connector_bindings(revision, first), {"con-p1": default})

        for scope, name in (
            ({"module_type": module_type}, "Te1/{module}/type"),
            ({"module": first}, "Te1/{module}/instance"),
        ):
            BindingOverride.objects.create(
                definition=revision.definition,
                connector_key="con-p1",
                bindings=bindings(name),
                **scope,
            )

        self.assertEqual(
            connector_bindings(revision, first),
            {"con-p1": bindings("Te1/{module}/instance")},
        )
        self.assertEqual(
            connector_bindings(revision, second),
            {"con-p1": bindings("Te1/{module}/type")},
        )
