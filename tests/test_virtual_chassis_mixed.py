"""A virtual chassis shows each visible member in rack order: its artwork, its layout, or a generated schematic."""

import copy
import re
from collections import Counter
from decimal import Decimal

import lxml.html
from dcim.models import (
    Cable,
    Device,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Module,
    ModuleBay,
    ModuleType,
    PowerPort,
    Rack,
    VirtualChassis,
)
from django.apps import apps
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from lxml import etree
from users.models import ObjectPermission, User

from netbox_device_view.models import Association, DeviceView, RenderMode

from .activation import activate
from .module_samples import import_definitions
from .permissions import grant
from .test_api_artwork import ZERO_ROWS_YAML, ArtworkApiTestCase
from .test_api_rendered_layout import SVG_YAML
from .test_artwork_device_view import (
    GRID,
    SVG_NS,
    TAB_URL,
    faces,
    ids,
    links,
    references,
)
from .test_view_permissions import CARDS


def all_faces(content):
    """Return every parsed artwork face of a page: the overviews and the focus views."""
    return [
        etree.fromstring(markup)
        for markup in re.findall(
            r'<div class="dv-art-viewport" data-dv-view="[^"]+"[^>]*>(.*?)</div>',
            content,
            re.DOTALL,
        )
    ]


def members(content):
    """Return (label text, label link, member element) of each member of a page, in page order."""
    page = lxml.html.fromstring(content)
    return [
        (" ".join(label.text_content().split()), label.find("a"), label.getparent())
        for label in page.xpath(
            "//p[contains(concat(' ', @class, ' '), ' dv-member-label ')]"
        )
    ]


def classes(element):
    return element.get("class", "").split()


def by_id(face, ident):
    [element] = face.findall(f".//*[@id='{ident}']")
    return element


class MixedChassisTestCase(ArtworkApiTestCase):
    """Five members in rack order sw2, sw4, sw3, sw1, sw5: two of the artwork type, an SVG layout, a CSS layout and a schematic."""

    def setUp(self):
        super().setUp()
        for model in (Module, ModuleBay, DeviceView):
            grant(self.user, model, "view")
        self.chassis = VirtualChassis.objects.create(name="stack")
        self.rack = Rack.objects.create(site=self.site, name="R1")
        Device.objects.filter(pk=self.device.pk).update(
            virtual_chassis=self.chassis, vc_position=1, rack=self.rack, position=10
        )
        self.device.refresh_from_db()
        self.sw1 = self.device
        self.sw2 = self.add_member("sw2", self.device_type, 2, 40)
        self.sw2_eth1 = Interface.objects.create(
            device=self.sw2, name="eth1", type="1000base-t"
        )
        self.sw3 = self.add_member("sw3", self.layout_type("SW-SVG", SVG_YAML), 3, 20)
        DeviceView.objects.filter(device_type=self.sw3.device_type).update(
            render_mode=RenderMode.SVG
        )
        self.sw3_port = Interface.objects.create(
            device=self.sw3, name="GigabitEthernet1", type="1000base-t"
        )
        self.sw4 = self.add_member("sw4", self.layout_type("SW-CSS"), 4, 30)
        self.sw4_eth1 = Interface.objects.create(
            device=self.sw4, name="eth1", type="1000base-t"
        )
        bare = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="BARE-2", slug="bare-2", u_height=2
        )
        self.sw5 = self.add_member("sw5", bare, 5, 5)
        # Natural order puts ge-0/0/10 after ge-0/0/2.
        self.sw5_ports = [
            Interface.objects.create(device=self.sw5, name=name, type="1000base-t")
            for name in ("ge-0/0/1", "ge-0/0/2", "ge-0/0/10")
        ]
        self.sw5_psu = PowerPort.objects.create(device=self.sw5, name="PSU1")
        self.slot1 = ModuleBay.objects.create(device=self.sw5, name="Slot 1")
        self.slot2 = ModuleBay.objects.create(device=self.sw5, name="Slot 2")
        self.card = Module.objects.create(
            device=self.sw5,
            module_bay=self.slot1,
            module_type=ModuleType.objects.create(
                manufacturer=self.manufacturer, model="LC-1"
            ),
        )
        self.card_port = Interface.objects.create(
            device=self.sw5, module=self.card, name="xe-1/0/0", type="10gbase-x-sfpp"
        )
        # Only sw2 has a cable: two members of one type keep their own state.
        Cable(
            a_terminations=[self.sw2_eth1],
            b_terminations=[
                Interface.objects.create(
                    device=self.make_device("server1"), name="eth0", type="1000base-t"
                )
            ],
        ).save()
        self.activate()

    def add_member(self, name, device_type, position, rack_position):
        return self.make_device(
            name,
            device_type,
            virtual_chassis=self.chassis,
            vc_position=position,
            rack=self.rack,
            position=rack_position,
            face="front",
        )

    def layout_type(self, model, yaml_layout=""):
        device_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model=model, slug=model.lower()
        )
        DeviceView.objects.create(
            device_type=device_type, grid_template_area=GRID, yaml_layout=yaml_layout
        )
        return device_type

    def tab_url(self, device):
        return reverse(TAB_URL, kwargs={"pk": device.pk})

    def device_page_panel(self, device):
        config = copy.deepcopy(settings.PLUGINS_CONFIG)
        config["netbox_device_view"]["show_on_device_tab"] = True
        with override_settings(PLUGINS_CONFIG=config):
            content = self.client.get(device.get_absolute_url()).content.decode()
        found = [content.find(card) for card in CARDS if card in content]
        return content[found[0] :] if found else ""


class BrowserTests(MixedChassisTestCase):
    def test_the_members_show_in_rack_order_with_their_labels_and_links(self):
        for page, content in (
            ("tab", self.tab()),
            ("device page panel", self.device_page_panel(self.sw1)),
        ):
            with self.subTest(page):
                self.assertEqual(
                    [(text, link.get("href")) for text, link, _ in members(content)],
                    [
                        (f"{device.name} · position {device.vc_position}", url)
                        for device in (self.sw2, self.sw4, self.sw3, self.sw1, self.sw5)
                        for url in [self.tab_url(device)]
                    ],
                )

    def test_only_the_current_member_is_marked(self):
        for device in (self.sw1, self.sw5):
            with self.subTest(device.name):
                content = self.tab(device)
                [current] = [
                    (text, link)
                    for text, link, element in members(content)
                    if "active-device" in classes(element)
                ]
                self.assertEqual(
                    current[0], f"{device.name} · position {device.vc_position}"
                )
                self.assertEqual(current[1].get("aria-current"), "page")
                self.assertEqual(content.count("active-device"), 1)
                self.assertEqual(
                    [link.get("aria-current") for _, link, _ in members(content)].count(
                        "page"
                    ),
                    1,
                )

    def test_each_member_shows_its_own_kind_on_one_page(self):
        content = self.tab()
        kinds = {}
        for text, _link, element in members(content):
            names = element.xpath(
                ".//span[@class='dv-art-face-name']"
                "[not(ancestor::div[contains(@class, 'dv-art-focus')])]/text()"
            )
            if names:
                kinds[text.split()[0]] = ("part", names)
            elif element.xpath(".//div[@id='dv-svg-wrapper-3']"):
                kinds[text.split()[0]] = ("svg", [])
            else:
                [grid] = element.xpath(".//div[contains(@class, 'deviceview')]")
                kinds[text.split()[0]] = (grid.get("class"), [])
        self.assertEqual(
            kinds,
            {
                "sw2": ("part", ["Front", "Rear"]),
                "sw4": ("deviceview area d4", []),
                "sw3": ("svg", []),
                "sw1": ("part", ["Front", "Rear"]),
                "sw5": ("part", ["Schematic"]),
            },
        )
        # Each member brings its assets, and each CSS grid has the height of its own type.
        for asset in (
            "device_view_artwork.css",
            "device_view_artwork.js",
            "device_view_svg.css",
        ):
            self.assertIn(asset, content)
        self.assertIn(".area.d4", content)
        self.assertRegex(content, r"\.deviceview\.d4 \{ height:42(\.0)?px; \}")
        self.assertEqual(content.count('class="dv-art-legend'), 1)
        self.assertEqual(content.count('<dialog class="dv-details"'), 1)
        self.assertIn(self.sw3_port.get_absolute_url(), content)
        self.assertIn(self.sw4_eth1.get_absolute_url(), content)

    def test_the_schematic_member_shows_its_components_and_slots(self):
        [face] = [
            face
            for face in faces(self.tab())
            if face.get("viewBox") == "0 0 444.5 88.9"
        ]
        prefix = f"dv-o-d{self.sw5.pk}-schematic-"
        linked = links(face)
        cells = [
            ident for ident in ids(face) if ident.startswith(prefix) and ident in linked
        ]
        components = [*self.sw5_ports, self.sw5_psu]
        self.assertEqual(
            cells,
            [
                f"{prefix}{component._meta.model_name}-{component.pk}"
                for component in components
            ],
        )
        for ident, component in zip(cells, components, strict=True):
            self.assertEqual(linked[ident].get("href"), component.get_absolute_url())
        # The body opens the device record and names the device type.
        [body] = [
            link
            for link in face.iter(f"{{{SVG_NS}}}a")
            if "dv-art-device-body" in classes(link)
        ]
        self.assertEqual(body.get("href"), self.sw5.get_absolute_url())
        self.assertIn("BARE-2", "".join(body.itertext()))
        # Slot 1 draws its module as a schematic; Slot 2 has no recorded module.
        card = by_id(face, f"dv-o-d{self.sw5.pk}-m{self.card.pk}")
        self.assertEqual(card.tag, f"{{{SVG_NS}}}g")
        port_id = f"dv-o-d{self.sw5.pk}-m{self.card.pk}-schematic-interface-{self.card_port.pk}"
        self.assertEqual(linked[port_id].get("href"), self.card_port.get_absolute_url())
        empty = by_id(face, f"{prefix}slot-{self.slot2.pk}")
        self.assertIn("dv-art-not-recorded", classes(empty))
        self.assertEqual(
            [text.text for text in empty.iter(f"{{{SVG_NS}}}text")],
            ["Slot 2", "not recorded"],
        )
        texts = [text.text for text in face.iter(f"{{{SVG_NS}}}text")]
        self.assertIn("Slot 1", texts)

    def test_the_schematic_member_body_shows_a_focus_ring_after_its_parts(self):
        [face] = [
            face
            for face in faces(self.tab())
            if face.get("viewBox") == "0 0 444.5 88.9"
        ]
        [body] = [
            link
            for link in face.iter(f"{{{SVG_NS}}}a")
            if "dv-art-device-body" in classes(link)
        ]
        siblings = list(body.getparent())
        # The page CSS shows the last sibling of the body when the body has the focus.
        self.assertEqual(
            classes(siblings[-1]), ["dv-art-focus-ring", "dv-art-focus-frame"]
        )
        self.assertLess(siblings.index(body), len(siblings) - 1)

    def test_a_slot_of_the_schematic_member_draws_a_module_with_its_artwork(self):
        admin = User.objects.get(username="admin")
        revision = import_definitions(admin)["acme/sfp-10g"]
        sfp_type = ModuleType.objects.create(
            manufacturer=self.manufacturer, model="SFP-10G"
        )
        InterfaceTemplate.objects.create(
            module_type=sfp_type, name="Port {module}", type="10gbase-x-sfpp"
        )
        activate(admin, (revision, sfp_type))
        ModuleBay.objects.filter(pk=self.slot2.pk).update(position="2")
        self.slot2.refresh_from_db()
        sfp = Module.objects.create(
            device=self.sw5, module_bay=self.slot2, module_type=sfp_type
        )
        port = Interface.objects.get(device=self.sw5, module=sfp, name="Port 2")
        [face] = [
            face
            for face in faces(self.tab())
            if face.get("viewBox") == "0 0 444.5 88.9"
        ]
        part_id = f"dv-o-d{self.sw5.pk}-m{sfp.pk}"
        slot = by_id(face, f"dv-o-d{self.sw5.pk}-schematic-slot-{self.slot2.pk}")
        self.assertNotIn("dv-art-not-recorded", classes(slot))
        group = by_id(slot, part_id)
        self.assertTrue(
            [
                g
                for g in group.iter(f"{{{SVG_NS}}}g")
                if "dv-art-module-art" in classes(g)
            ]
        )
        self.assertEqual(
            links(face)[f"{part_id}-installed-con-port"].get("href"),
            port.get_absolute_url(),
        )
        [member] = [
            m for m in self.get(self.sw5)["members"] if m["device_id"] == self.sw5.pk
        ]
        modules = {m["module_id"]: m for m in member["artwork"]["modules"]}
        self.assertEqual(modules[sfp.pk]["representation"], "artwork")
        self.assertEqual(modules[sfp.pk]["artwork"]["definition_key"], "acme/sfp-10g")

    def test_the_schematic_member_has_assembly_navigation_and_a_focus_view(self):
        page = lxml.html.fromstring(self.tab())
        [nav] = page.xpath(f"//nav[@aria-label='Assembly of {self.sw5.name}']")
        self.assertEqual(
            nav.xpath(".//button/@data-dv-focus"),
            [
                f"dv-o-d{self.sw5.pk}",
                f"dv-f1-d{self.sw5.pk}-m{self.card.pk}",
            ],
        )

    def test_no_id_repeats_and_same_type_members_keep_their_own_links_and_state(self):
        content = self.tab()
        counts = Counter(i for face in all_faces(content) for i in ids(face))
        self.assertEqual([i for i, n in counts.items() if n > 1], [])
        overview = {
            device: [
                f for f in faces(content) if ids(f)[0].startswith(f"dv-o-d{device.pk}-")
            ]
            for device in (self.sw1, self.sw2)
        }
        for device, eth1, cabled in (
            (self.sw1, self.eth1, False),
            (self.sw2, self.sw2_eth1, True),
        ):
            with self.subTest(device.name):
                front = overview[device][0]
                link = links(front)[f"dv-o-d{device.pk}-front-con-eth1"]
                self.assertEqual(link.get("href"), eth1.get_absolute_url())
                self.assertEqual("dv-art-cable-connected" in classes(link), cabled)

    def test_the_schematic_member_has_the_tab_and_the_device_page_panel(self):
        device_page = self.client.get(self.sw5.get_absolute_url()).content.decode()
        self.assertIn(self.tab_url(self.sw5), device_page)
        panel = self.device_page_panel(self.sw5)
        self.assertIn(f"dv-o-d{self.sw5.pk}-schematic-", panel)
        self.assertEqual([text for text, *_ in members(panel)][-1], "sw5 · position 5")

    def test_a_layout_that_does_not_parse_shows_its_error_only_for_its_member(self):
        DeviceView.objects.filter(device_type=self.sw3.device_type).update(
            yaml_layout=ZERO_ROWS_YAML
        )
        shown = {
            text.split()[0]: element.text_content()
            for text, _link, element in members(self.tab())
        }
        self.assertEqual(list(shown), ["sw2", "sw4", "sw3", "sw1", "sw5"])
        self.assertIn("canvas.rows", shown["sw3"])
        self.assertEqual(
            [name for name, text in shown.items() if "does not parse" in text], ["sw3"]
        )
        self.assertEqual(len(faces(self.tab())), 5)

    def test_a_schematic_is_at_least_one_rack_unit_high(self):
        for u_height, view_box in (
            (0, "0 0 444.5 44.45"),
            (Decimal("0.5"), "0 0 444.5 44.45"),
            (Decimal("1.5"), "0 0 444.5 66.675"),
        ):
            with self.subTest(u_height):
                DeviceType.objects.filter(pk=self.sw5.device_type_id).update(
                    u_height=u_height
                )
                [face] = self.get(self.sw5)["members"][-1]["artwork"]["faces"]
                self.assertEqual(etree.fromstring(face["svg"]).get("viewBox"), view_box)


class TwoSvgLayoutTests(MixedChassisTestCase):
    """sw3 and sw6 have one SVG layout type, so one page shows two SVG layout panels."""

    def setUp(self):
        super().setUp()
        self.sw6 = self.add_member("sw6", self.sw3.device_type, 6, 1)
        Interface.objects.create(
            device=self.sw6, name="GigabitEthernet1", type="1000base-t"
        )

    def test_each_svg_layout_panel_has_its_own_ids(self):
        for page, content in (
            ("tab", self.tab()),
            ("device page panel", self.device_page_panel(self.sw1)),
        ):
            with self.subTest(page):
                document = lxml.html.fromstring(content)
                # NetBox repeats an id of its own page; the plugin ids start with "dv-".
                counts = Counter(document.xpath("//@id[starts-with(., 'dv-')]"))
                self.assertEqual([i for i, n in counts.items() if n > 1], [])
                panels = document.xpath(
                    "//div[contains(concat(' ', @class, ' '), ' dv-svg-wrapper ')]"
                )
                self.assertEqual(len(panels), 2)
                for panel in panels:
                    found = references(panel)
                    self.assertTrue(found)
                    self.assertLessEqual(set(found), set(panel.xpath(".//@id")))


class HiddenMemberTests(MixedChassisTestCase):
    def setUp(self):
        super().setUp()
        ObjectPermission.objects.filter(name__startswith="dcim.device view").update(
            constraints={"name__in": ["sw1", "sw2", "sw3", "sw5", "server1"]}
        )

    def test_a_hidden_member_has_no_label_and_no_panel(self):
        for page, content in (
            ("tab", self.tab()),
            ("device page panel", self.device_page_panel(self.sw1)),
        ):
            with self.subTest(page):
                self.assertEqual(
                    [text for text, *_ in members(content)],
                    [
                        "sw2 · position 2",
                        "sw3 · position 3",
                        "sw1 · position 1",
                        "sw5 · position 5",
                    ],
                )
                for leak in ("sw4", "position 4", "area d4", self.tab_url(self.sw4)):
                    self.assertNotIn(leak, content)

    def test_the_api_leaves_out_the_hidden_member(self):
        self.assertEqual(
            [m["device_id"] for m in self.get()["members"]],
            [self.sw2.pk, self.sw3.pk, self.sw1.pk, self.sw5.pk],
        )

    def test_a_hidden_member_does_not_give_the_others_a_view(self):
        # sw1 and sw2 have the only artwork type; without them no visible member has a view.
        DeviceView.objects.all().delete()
        ObjectPermission.objects.filter(name__startswith="dcim.device view").update(
            constraints={"name__in": ["sw5", "server1"]}
        )
        self.assertNotIn(
            self.tab_url(self.sw5),
            self.client.get(self.sw5.get_absolute_url()).content.decode(),
        )
        self.assertEqual(self.device_page_panel(self.sw5), "")
        self.assertEqual(members(self.tab(self.sw5)), [])
        [member] = self.get(self.sw5)["members"]
        self.assertEqual(
            (member["representation"], member["reason"]), ("unavailable", "no_layout")
        )


class NoViewTests(MixedChassisTestCase):
    def setUp(self):
        super().setUp()
        DeviceView.objects.all().delete()
        Association.objects.all().delete()

    def test_a_chassis_without_artwork_or_a_layout_has_no_device_view(self):
        device_page = self.client.get(self.sw5.get_absolute_url()).content.decode()
        self.assertNotIn(self.tab_url(self.sw5), device_page)
        self.assertEqual(self.device_page_panel(self.sw5), "")
        content = self.tab(self.sw5)
        self.assertEqual(members(content), [])
        self.assertNotIn("dv-art-part", content)
        self.assertEqual(
            {(m["representation"], m["reason"]) for m in self.get(self.sw5)["members"]},
            {("unavailable", "no_layout")},
        )


class ApiTests(MixedChassisTestCase):
    def test_each_member_has_its_own_representation(self):
        body = self.get()
        self.assertEqual(
            [
                (
                    m["device_id"],
                    m["position"],
                    m["current"],
                    m["representation"],
                    m["reason"],
                )
                for m in body["members"]
            ],
            [
                (self.sw2.pk, 2, False, "artwork", None),
                (self.sw4.pk, 4, False, "unavailable", "svg_layout_required"),
                (self.sw3.pk, 3, False, "layout", None),
                (self.sw1.pk, 1, True, "artwork", None),
                (self.sw5.pk, 5, False, "schematic", None),
            ],
        )

    def test_an_unreadable_module_artwork_file_makes_only_its_schematic_member_unavailable(
        self,
    ):
        admin = User.objects.get(username="admin")
        revision = import_definitions(admin)["acme/sfp-10g"]
        sfp_type = ModuleType.objects.create(
            manufacturer=self.manufacturer, model="SFP-10G"
        )
        activate(admin, (revision, sfp_type))
        ModuleBay.objects.filter(pk=self.slot2.pk).update(position="2")
        Module.objects.create(
            device=self.sw5, module_bay=self.slot2, module_type=sfp_type
        )
        # Storage cannot read a directory, as it cannot read a file without permission.
        for file in revision.files.select_related("blob"):
            stored = self.media / file.blob.name
            stored.unlink()
            stored.mkdir()
        with self.assertLogs("netbox_device_view.api.device_artwork", "ERROR"):
            members = self.get()["members"]
        self.assertEqual(
            [(m["device_id"], m["representation"], m["reason"]) for m in members],
            [
                (self.sw2.pk, "artwork", None),
                (self.sw4.pk, "unavailable", "svg_layout_required"),
                (self.sw3.pk, "layout", None),
                (self.sw1.pk, "artwork", None),
                (self.sw5.pk, "unavailable", "artwork_error"),
            ],
        )

    def test_the_schematic_member_gives_its_artwork_without_a_revision(self):
        [member] = [m for m in self.get()["members"] if m["device_id"] == self.sw5.pk]
        self.assertEqual(member["panels"], [])
        artwork = member["artwork"]
        self.assertEqual(
            (artwork["definition_key"], artwork["revision_id"], artwork["digest"]),
            (None, None, None),
        )
        self.assertEqual(
            [(face["name"], face["label"]) for face in artwork["faces"]],
            [("schematic", "Schematic")],
        )
        prefix = f"dv-a-d{self.sw5.pk}-schematic-"
        self.assertEqual(
            [
                (
                    c["key"],
                    c["face"],
                    c["label"],
                    c["element_id"],
                    c["bindings"][0]["object_id"],
                )
                for c in artwork["connectors"]
            ],
            [
                (key, "schematic", component.name, f"{prefix}{key}", component.pk)
                for component in (*self.sw5_ports, self.sw5_psu)
                for key in [f"{component._meta.model_name}-{component.pk}"]
            ],
        )
        [module] = artwork["modules"]
        self.assertEqual(
            {
                k: module[k]
                for k in (
                    "module_id",
                    "module_bay_id",
                    "parent_module_id",
                    "bay_key",
                    "face",
                    "representation",
                    "element_id",
                )
            },
            {
                "module_id": self.card.pk,
                "module_bay_id": self.slot1.pk,
                "parent_module_id": None,
                "bay_key": None,
                "face": "schematic",
                "representation": "schematic",
                "element_id": f"dv-a-d{self.sw5.pk}-m{self.card.pk}",
            },
        )
        self.assertEqual(
            [
                (b["module_bay_id"], b["state"], b["module_id"], b["element_id"])
                for b in artwork["bays"]
            ],
            [
                (
                    self.slot1.pk,
                    "installed",
                    self.card.pk,
                    f"dv-a-d{self.sw5.pk}-m{self.card.pk}",
                ),
                (self.slot2.pk, "not_recorded", None, f"{prefix}slot-{self.slot2.pk}"),
            ],
        )

    def test_the_api_and_the_page_draw_the_same_connectors(self):
        content = self.tab()
        page = {}
        page_ids = []
        for face in faces(content):
            page.update(
                {ident: link.get("href") for ident, link in links(face).items()}
            )
            # The page adds the mark and the focus ring filters of its connectors.
            page_ids.append(
                [i for i in ids(face) if "-marks-" not in i and "-focus-" not in i]
            )
        api = {}
        api_ids = []
        for member in self.get()["members"]:
            if member["artwork"] is None:
                continue
            for face in member["artwork"]["faces"]:
                api_ids.append(
                    [
                        i.replace("dv-a-", "dv-o-", 1)
                        for i in ids(etree.fromstring(face["svg"]))
                    ]
                )
            connectors = [
                *member["artwork"]["connectors"],
                *(c for m in member["artwork"]["modules"] for c in m["connectors"]),
            ]
            for connector in connectors:
                found = [b for b in connector["bindings"] if b["status"] == "found"]
                if (
                    connector["element_id"]
                    and len(found) == len(connector["bindings"]) == 1
                ):
                    [binding] = found
                    api[connector["element_id"].replace("dv-a-", "dv-o-", 1)] = (
                        apps.get_model(binding["object_type"])
                        .objects.get(pk=binding["object_id"])
                        .get_absolute_url()
                    )
        self.assertEqual(page, api)
        self.assertEqual(sorted(page_ids), sorted(api_ids))
        self.assertTrue(any(f"-d{self.sw5.pk}-schematic-" in i for i in api))
