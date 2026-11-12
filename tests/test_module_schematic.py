"""A module without drawable artwork shows as a schematic of its components; a bay without a recorded module shows that."""

import re

from dcim.models import (
    Cable,
    ConsolePort,
    Device,
    Interface,
    Module,
    ModuleBay,
    PowerPort,
)
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import etree
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view.artwork.svg import IDENTITY, multiply, transform_matrix
from netbox_device_view.details import details_id
from netbox_device_view.models import Association

from .module_samples import CHASSIS_SVG, Inventory
from .permissions import grant
from .test_api_artwork import API_URL
from .test_artwork_device_view import SVG_NS, faces, ids, links, references
from .test_module_composition import CompositionTestCase, art

G = f"{{{SVG_NS}}}g"
TEXT = f"{{{SVG_NS}}}text"
RECT = f"{{{SVG_NS}}}rect"


def by_id(face, ident):
    found = face.findall(f".//*[@id='{ident}']")
    return found[0] if found else None


def geometry(rect):
    return tuple(float(rect.get(name)) for name in ("x", "y", "width", "height"))


def to_face(element):
    """Return the matrix from the user space of ``element`` to the coordinates of its face."""
    matrix = IDENTITY
    for ancestor in reversed(list(element.iterancestors())):
        if ancestor.get("transform") is not None:
            matrix = multiply(matrix, transform_matrix(ancestor.get("transform")))
    return matrix


class SchematicTestCase(CompositionTestCase):
    def without_artwork(self, *module_types):
        Association.objects.filter(module_type__in=module_types).delete()

    def schematic_id(self, *modules):
        return f"{self.part_id(*modules)}-schematic-"


class SchematicTests(SchematicTestCase):
    def test_a_module_without_artwork_shows_its_components_and_slots_in_the_chassis_artwork(
        self,
    ):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        front = self.front()
        # The chassis keeps its artwork and its own connector.
        self.assertIn(f"dv-o-d{self.device.pk}-front-con-eth1", links(front))
        group = self.group(front, inventory.nm1)
        [body] = group.findall(f"{{{SVG_NS}}}a")
        self.assertEqual(body.get("class"), "dv-art-module-body dv-art-schematic")
        self.assertEqual(body.get("href"), inventory.nm1.get_absolute_url())
        self.assertEqual(geometry(body.find(RECT)), (200, 7, 100, 30))
        self.assertEqual(body.find(TEXT).text, "NM-2X")
        port = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        key = f"{self.schematic_id(inventory.nm1)}interface-{port.pk}"
        self.assertEqual(links(front)[key].get("href"), port.get_absolute_url())
        self.assertEqual(by_id(front, key).find(TEXT).text, "TenGigabitEthernet1/1/1")
        slot_bay = ModuleBay.objects.get(module=inventory.nm1, name="Slot 1")
        slot = by_id(front, f"{self.schematic_id(inventory.nm1)}slot-{slot_bay.pk}")
        self.assertEqual(slot.get("class"), "dv-art-slot")
        self.assertEqual(slot.find(TEXT).text, "Slot 1")
        # The transceiver keeps its artwork, scaled to fit the slot and centred in it.
        sfp = slot.find(f"{G}[@id='{self.part_id(inventory.nm1, inventory.sfp)}']")
        a, b, c, d, e, f = transform_matrix(art(sfp).get("transform"))
        x, y, width, height = geometry(slot.find(RECT))
        strip = height / 4
        scale = min(width / 14, (height - strip) / 10)
        self.assertEqual(
            (round(a, 6), b, c, round(d, 6)), (round(scale, 6), 0, 0, round(scale, 6))
        )
        self.assertAlmostEqual(e, x + (width - 14 * scale) / 2)
        self.assertAlmostEqual(f, y + strip + (height - strip - 10 * scale) / 2)
        self.assertIn(
            f"{self.part_id(inventory.nm1, inventory.sfp)}-installed-con-port",
            links(front),
        )

    def test_a_schematic_shows_the_bindings_and_state_of_its_components(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        port = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        Interface.objects.filter(pk=port.pk).update(enabled=False)
        peer = Inventory(self.admin, name="ch2", activate=False)
        Cable(a_terminations=[port], b_terminations=[peer.eth1]).save()
        content = self.tab()
        [front] = faces(content)
        prefix = self.schematic_id(inventory.nm1)
        wrapper = links(front)[f"{prefix}interface-{port.pk}"]
        self.assertEqual(
            wrapper.get("class"),
            "dv-art-connector dv-art-cable-connected dv-art-disabled",
        )
        self.assertTrue(
            wrapper.find(f"{{{SVG_NS}}}title").text.startswith(
                "Interface TenGigabitEthernet1/1/1: disabled, cable connected"
            )
        )
        ident = details_id(prefix, f"interface-{port.pk}")
        self.assertEqual(wrapper.get("data-dv-details"), ident)
        self.assertIn(
            f'<template id="{ident}" data-title="Connector TenGigabitEthernet1/1/1'
            ' · ACME NM-2X in NM 1">',
            content,
        )

    def test_the_components_are_grouped_by_type_in_natural_name_order(self):
        inventory = self.inventory
        fan = inventory.fan
        for name in ("p10", "p2"):
            Interface.objects.create(
                device=self.device, module=fan, name=name, type="1000base-t"
            )
        ConsolePort.objects.create(device=self.device, module=fan, name="con")
        grant(self.user, ConsolePort, "view")
        PowerPort.objects.create(device=self.device, module=fan, name="in")
        group = self.group(self.front(), fan)
        prefix = self.schematic_id(fan)
        self.assertEqual(
            [
                element.find(TEXT).text
                for element in group.iter(G)
                if element.get("id", "").startswith(prefix)
            ],
            ["p2", "p10", "con", "in"],
        )

    def test_a_component_that_the_user_cannot_view_is_not_drawn(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        hidden = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        user = User.objects.create_user(username="partial")
        for model in (Device, Module, ModuleBay):
            grant(user, model, "view")
        grant(user, Interface, "view", constraints={"name": "eth1"})
        self.client.force_login(user)
        front = self.front()
        self.assertIsNone(
            by_id(front, f"{self.schematic_id(inventory.nm1)}interface-{hidden.pk}")
        )
        self.assertIsNotNone(self.group(front, inventory.nm1))

    def test_a_schematic_is_not_turned_with_its_bay_or_its_parents(self):
        inventory = self.inventory
        # The chassis turns the network module; the transceiver in it has no artwork.
        revision = inventory.revisions["acme/ch-4"]
        revision.manifest["bays"]["bay-nm1"]["rotate"] = 90
        type(revision).objects.filter(pk=revision.pk).update(manifest=revision.manifest)
        self.without_artwork(inventory.sfp_type, inventory.psu_type)
        front = self.front()
        nm1 = self.group(front, inventory.nm1)
        self.assertEqual(transform_matrix(art(nm1).get("transform"))[:4], (0, 1, -1, 0))
        for modules in ((inventory.nm1, inventory.sfp), (inventory.psu,)):
            body = self.group(front, *modules).find(f".//{{{SVG_NS}}}a")
            matrix = to_face(body.find(RECT))
            self.assertEqual(tuple(round(v, 9) for v in matrix[:4]), (1, 0, 0, 1))
        # The bay of the power supply turns its artwork; the schematic fills the bay as it is.
        psu = self.group(front, inventory.psu).find(f"{{{SVG_NS}}}a")
        self.assertEqual(geometry(psu.find(RECT)), (120, 2, 30, 40))

    def test_a_schematic_module_body_shows_a_focus_ring_over_its_parts(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type, inventory.sfp_type, inventory.psu_type)
        front = self.front()
        # A small body keeps the outline width of the chassis face, so its ring shows.
        chassis_width = max(map(float, front.get("viewBox").split()[2:])) / 300
        for modules in (
            (inventory.nm1,),
            (inventory.nm1, inventory.sfp),
            # A turned bay: the schematic and its ring draw upright.
            (inventory.psu,),
        ):
            body = self.group(front, *modules).find(f".//{{{SVG_NS}}}a")
            # The ring comes after the parts and slots, so they do not cover it.
            ring = body.getparent()[-1]
            self.assertEqual(ring.get("class"), "dv-art-focus-ring dv-art-focus-frame")
            if len(modules) == 1:
                self.assertAlmostEqual(
                    float(ring.get("stroke-width")), chassis_width, places=6
                )
            # The stroke is inside the frame of the body.
            half = float(ring.get("stroke-width")) / 2
            x, y, width, height = geometry(body.find(RECT))
            for value, expected in zip(
                geometry(ring),
                (x + half, y + half, width - 2 * half, height - 2 * half),
                strict=True,
            ):
                self.assertAlmostEqual(value, expected, places=6)

    def test_the_focus_ring_of_a_schematic_in_a_flat_bay_has_an_area(self):
        self.store_face(
            "acme/ch-4",
            "front.svg",
            CHASSIS_SVG.replace(
                'x="40" y="4" width="20" height="36"',
                'x="40" y="4" width="10" height="1"',
            ),
        )
        body = self.group(self.front(), self.inventory.fan).find(f".//{{{SVG_NS}}}a")
        ring = body.getparent()[-1]
        width, height, stroke = (
            float(ring.get(name)) for name in ("width", "height", "stroke-width")
        )
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        # The stroke stays inside the bay.
        self.assertAlmostEqual(height + stroke, 1, places=6)

    def test_a_schematic_module_has_a_focus_view_at_the_size_of_its_bay(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        content = self.tab()
        ident = f"dv-f2-d{self.device.pk}-m{inventory.nm1.pk}"
        self.assertIn(f'id="{ident}"', content)
        [face] = [
            etree.fromstring(markup)
            for markup in re.findall(
                r'<div class="dv-art-viewport" data-dv-view="f2"[^>]*>(.*?)</div>',
                content,
                re.DOTALL,
            )
        ]
        self.assertEqual(face.get("viewBox"), "0 0 100 30")
        port = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        self.assertIn(f"{ident}-schematic-interface-{port.pk}", links(face))
        self.assertIsNotNone(by_id(face, f"{ident}-m{inventory.sfp.pk}"))
        self.assertLessEqual(set(references(face)), set(ids(face)))


class NotRecordedTests(SchematicTestCase):
    def mark(self, face, key, *modules):
        face_name = "installed" if modules else "front"
        return by_id(face, f"{self.part_id(*modules)}-{face_name}--not-recorded-{key}")

    def test_a_bay_without_a_module_shows_that_its_inventory_is_not_recorded(self):
        inventory = self.inventory
        front = self.front()
        mark = self.mark(front, "bay-slot", inventory.nm2)
        self.assertEqual(mark.get("class"), "dv-art-not-recorded")
        self.assertEqual(mark.find(TEXT).text, "not recorded")
        self.assertEqual(geometry(mark.find(RECT)), (40, 10, 14, 10))
        self.assertIsNone(self.mark(front, "bay-slot", inventory.nm1))

    def test_a_module_or_a_bay_that_the_user_cannot_view_shows_as_not_recorded(self):
        inventory = self.inventory
        user = User.objects.create_user(username="partial")
        for model in (Device, Interface, PowerPort):
            grant(user, model, "view")
        grant(user, Module, "view", constraints={"id__in": [inventory.nm1.pk]})
        grant(user, ModuleBay, "view", constraints={"name__in": ["NM 1", "PSU"]})
        self.client.force_login(user)
        front = self.front()
        for key in ("bay-psu", "bay-nm2", "bay-fan", "bay-spare"):
            self.assertIsNotNone(self.mark(front, key), key)
        self.assertIsNone(self.mark(front, "bay-nm1"))
        # The slot of the network module is hidden too, so its transceiver does not show.
        self.assertIsNotNone(self.mark(front, "bay-slot", inventory.nm1))
        self.assertNotIn(f"-m{inventory.psu.pk}", " ".join(ids(front)))

    def test_a_cable_on_a_port_in_an_unrecorded_bay_does_not_draw_an_optic(self):
        inventory = self.inventory
        port = inventory.interface(inventory.nm2, "TenGigabitEthernet1/2/1")
        peer = Inventory(self.admin, name="ch2", activate=False)
        Cable(a_terminations=[port], b_terminations=[peer.eth1]).save()
        front = self.front()
        nm2 = self.group(front, inventory.nm2)
        self.assertIsNotNone(self.mark(front, "bay-slot", inventory.nm2))
        self.assertEqual(
            [
                g.get("id")
                for g in nm2.iter(G)
                if g.get("id", "").startswith(f"{self.part_id(inventory.nm2)}-m")
            ],
            [],
        )
        # The cable shows on the port of the network module only.
        self.assertIn(
            "dv-art-cable-connected",
            links(front)[f"{self.part_id(inventory.nm2)}-installed-con-p1"].get(
                "class"
            ),
        )

    def test_a_schematic_slot_without_a_module_shows_that_it_is_not_recorded(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        front = self.front()
        slot_bay = ModuleBay.objects.get(module=inventory.nm2, name="Slot 2")
        slot = by_id(front, f"{self.schematic_id(inventory.nm2)}slot-{slot_bay.pk}")
        self.assertEqual(slot.get("class"), "dv-art-slot dv-art-not-recorded")
        self.assertEqual(
            [text.text for text in slot.iter(TEXT)], ["Slot 2", "not recorded"]
        )


class BayApiTests(SchematicTestCase):
    def artwork(self):
        token = Token.objects.create(user=self.user)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        response = api.get(reverse(API_URL, kwargs={"pk": self.device.pk}))
        self.assertEqual(response.status_code, 200)
        [member] = response.json()["members"]
        return member["artwork"]

    def test_missing_artwork_and_missing_inventory_are_different_facts(self):
        inventory = self.inventory
        artwork = self.artwork()
        bay = {
            name: b.pk
            for name, b in (
                (b.name, b)
                for b in ModuleBay.objects.filter(device=self.device, module=None)
            )
        }
        nm1_slot = ModuleBay.objects.get(module=inventory.nm1)
        nm2_slot = ModuleBay.objects.get(module=inventory.nm2)
        device = f"dv-a-d{self.device.pk}"
        self.assertEqual(
            [
                (
                    entry["parent_module_id"],
                    entry["bay_key"],
                    entry["module_bay_id"],
                    entry["face"],
                    entry["state"],
                    entry["module_id"],
                    entry["element_id"],
                )
                for entry in artwork["bays"]
            ],
            [
                (
                    None,
                    "bay-fan",
                    bay["FAN"],
                    "front",
                    "installed",
                    inventory.fan.pk,
                    f"{device}-m{inventory.fan.pk}",
                ),
                (
                    None,
                    "bay-nm1",
                    bay["NM 1"],
                    "front",
                    "installed",
                    inventory.nm1.pk,
                    f"{device}-m{inventory.nm1.pk}",
                ),
                (
                    inventory.nm1.pk,
                    "bay-slot",
                    nm1_slot.pk,
                    "front",
                    "installed",
                    inventory.sfp.pk,
                    f"{device}-m{inventory.nm1.pk}-m{inventory.sfp.pk}",
                ),
                (
                    None,
                    "bay-nm2",
                    bay["NM 2"],
                    "front",
                    "installed",
                    inventory.nm2.pk,
                    f"{device}-m{inventory.nm2.pk}",
                ),
                (
                    inventory.nm2.pk,
                    "bay-slot",
                    nm2_slot.pk,
                    "front",
                    "not_recorded",
                    None,
                    f"{device}-m{inventory.nm2.pk}-installed--not-recorded-bay-slot",
                ),
                (
                    None,
                    "bay-psu",
                    bay["PSU"],
                    "front",
                    "installed",
                    inventory.psu.pk,
                    f"{device}-m{inventory.psu.pk}",
                ),
                (
                    None,
                    "bay-spare",
                    bay["Spare"],
                    "front",
                    "not_recorded",
                    None,
                    f"{device}-front--not-recorded-bay-spare",
                ),
            ],
        )
        # Missing artwork is the representation of an installed module.
        fan = next(m for m in artwork["modules"] if m["module_id"] == inventory.fan.pk)
        self.assertEqual((fan["representation"], fan["artwork"]), ("schematic", None))
        [face] = artwork["faces"]
        drawn = set(ids(etree.fromstring(face["svg"])))
        self.assertLessEqual({entry["element_id"] for entry in artwork["bays"]}, drawn)

    def test_a_schematic_module_lists_its_components_as_connectors(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        port = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        nm1 = next(
            m for m in self.artwork()["modules"] if m["module_id"] == inventory.nm1.pk
        )
        self.assertEqual(nm1["representation"], "schematic")
        self.assertEqual(nm1["placement"]["rotate"], 0)
        [connector] = nm1["connectors"]
        self.assertEqual(
            (
                connector["key"],
                connector["face"],
                connector["label"],
                connector["element_id"],
            ),
            (
                f"interface-{port.pk}",
                "schematic",
                "TenGigabitEthernet1/1/1",
                f"dv-a-d{self.device.pk}-m{inventory.nm1.pk}-schematic-interface-{port.pk}",
            ),
        )
        [binding] = connector["bindings"]
        self.assertEqual((binding["status"], binding["object_id"]), ("found", port.pk))
        slot = next(
            b
            for b in self.artwork()["bays"]
            if b["parent_module_id"] == inventory.nm1.pk
        )
        self.assertEqual((slot["bay_key"], slot["state"]), (None, "installed"))

    def test_a_bay_that_the_user_cannot_view_has_no_module_bay_id(self):
        user = User.objects.create_user(username="partial")
        for model in (Device, Interface, PowerPort, Module):
            grant(user, model, "view")
        grant(user, ModuleBay, "view", constraints={"name__in": ["NM 1", "NM 2"]})
        self.user = user
        spare = next(b for b in self.artwork()["bays"] if b["bay_key"] == "bay-spare")
        self.assertEqual(
            (spare["module_bay_id"], spare["state"]), (None, "not_recorded")
        )


class SlotTests(SchematicTestCase):
    def radius(self, face, ident):
        """Return the first radius of the mark filter of the connector ``ident``."""
        reference = links(face)[ident].get("filter")[5:-1]
        [found] = [
            f for f in face.iter(f"{{{SVG_NS}}}filter") if f.get("id") == reference
        ]
        return float(found.find(f"{{{SVG_NS}}}feMorphology").get("radius").split()[0])

    def test_a_connector_in_a_slot_has_the_mark_width_of_its_schematic(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        peer = Inventory(self.admin, name="ch2", activate=False)
        other = Interface.objects.create(
            device=peer.device, name="eth2", type="1000base-t"
        )
        port = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        Cable(a_terminations=[port], b_terminations=[peer.eth1]).save()
        sfp_port = Interface.objects.get(module=inventory.sfp)
        Cable(a_terminations=[sfp_port], b_terminations=[other]).save()
        front = self.front()
        sfp = self.group(front, inventory.nm1, inventory.sfp)
        scale = transform_matrix(art(sfp).get("transform"))[0]
        schematic = self.radius(
            front, f"{self.schematic_id(inventory.nm1)}interface-{port.pk}"
        )
        placed = self.radius(
            front, f"{self.part_id(inventory.nm1, inventory.sfp)}-installed-con-port"
        )
        # The transceiver face is scaled up to fit, so its own units are smaller.
        self.assertNotEqual(scale, 1)
        self.assertAlmostEqual(placed * scale, schematic, places=6)

    def test_a_module_in_a_slot_that_the_user_cannot_view_shows_as_not_recorded(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        user = User.objects.create_user(username="partial")
        for model in (Device, Interface, PowerPort, ModuleBay):
            grant(user, model, "view")
        grant(user, Module, "view", constraints={"id__in": [inventory.nm1.pk]})
        self.client.force_login(user)
        front = self.front()
        slot_bay = ModuleBay.objects.get(module=inventory.nm1, name="Slot 1")
        slot = by_id(front, f"{self.schematic_id(inventory.nm1)}slot-{slot_bay.pk}")
        self.assertEqual(slot.get("class"), "dv-art-slot dv-art-not-recorded")
        self.assertIsNone(self.group(front, inventory.nm1, inventory.sfp))

    def test_one_more_schematic_module_adds_no_query(self):
        inventory = self.inventory
        self.without_artwork(inventory.nm_type)
        self.tab()
        with CaptureQueriesContext(connection) as before:
            self.tab()
        inventory.install(
            ModuleBay.objects.get(device=self.device, name="Spare"), inventory.nm_type
        )
        with CaptureQueriesContext(connection) as after:
            front = faces(self.tab())[0]
        self.assertEqual(
            len(front.findall(".//*[@class='dv-art-module-body dv-art-schematic']")), 4
        )
        self.assertEqual(len(after), len(before))
