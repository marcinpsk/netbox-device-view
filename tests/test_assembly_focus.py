"""The assembly navigation lists the drawn modules of a device; a focus view draws a module with artwork in its own coordinates."""

import copy
import re
from collections import Counter

import lxml.html
from dcim.models import Device, Interface, Module, ModuleBay, VirtualChassis
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from lxml import etree
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view.details import details_id

from .module_samples import Inventory
from .permissions import grant
from .test_api_artwork import API_URL
from .test_artwork_device_view import SVG_NS, faces, ids, links, references
from .test_module_composition import CompositionTestCase, art, clip_rect

G = f"{{{SVG_NS}}}g"
VIEWS = "//*[contains(concat(' ', @class, ' '), ' dv-art-view ')]"


def focus_faces(content):
    """Return (view, parsed face) of each focus view of a page, in page order."""
    return [
        (view, etree.fromstring(markup))
        for view, markup in re.findall(
            r'<div class="dv-art-viewport" data-dv-view="(f\d+)"[^>]*>(.*?)</div>',
            content,
            re.DOTALL,
        )
    ]


def focus_face(content, view):
    """Return the one focus face of ``view`` on a page with one device."""
    [face] = [face for name, face in focus_faces(content) if name == view]
    return face


def text(element):
    return " ".join(element.text_content().split())


def tree(items):
    """Return [(label, children)] of the items of an assembly list."""
    return [
        (
            text(item.xpath("./*[@data-dv-focus or self::a][1]")[0]),
            tree(item.xpath("./ul/li")),
        )
        for item in items
    ]


class AssemblyTestCase(CompositionTestCase):
    def page(self):
        return lxml.html.fromstring(self.tab())

    def nav(self, page):
        [nav] = page.xpath("//nav[contains(@class, 'dv-art-nav')]")
        return nav

    def overview(self):
        return f"dv-o-d{self.device.pk}"

    def view(self, n, *modules):
        return f"dv-f{n}-d{self.device.pk}" + "".join(f"-m{m.pk}" for m in modules)


class NavigationTests(AssemblyTestCase):
    def test_the_navigation_nests_the_drawn_modules_under_the_chassis(self):
        nav = self.nav(self.page())
        self.assertEqual(
            tree(nav.xpath("./ul/li")),
            [
                (
                    "ch1",
                    [
                        ("ACME FAN-1 in FAN", []),
                        ("ACME NM-2X in NM 1", [("ACME SFP-10G in Slot 1", [])]),
                        ("ACME NM-2X in NM 2", []),
                        ("ACME PSU-1 in PSU", []),
                    ],
                )
            ],
        )
        self.assertEqual(nav.get("aria-label"), "Assembly of ch1")

    def test_each_module_with_artwork_has_a_focus_control_and_each_module_a_record_link(
        self,
    ):
        inventory = self.inventory
        nav = self.nav(self.page())
        controls = nav.xpath(".//button[@data-dv-focus]")
        self.assertEqual(
            [(text(b), b.get("data-dv-focus")) for b in controls],
            [
                ("ch1", self.overview()),
                ("ACME NM-2X in NM 1", self.view(1, inventory.nm1)),
                ("ACME SFP-10G in Slot 1", self.view(2, inventory.nm1, inventory.sfp)),
                ("ACME NM-2X in NM 2", self.view(3, inventory.nm2)),
                ("ACME PSU-1 in PSU", self.view(4, inventory.psu)),
            ],
        )
        # The chassis is the current view; each control names the view it shows.
        self.assertEqual(
            [b.get("aria-current") for b in controls], ["true", *[None] * 4]
        )
        self.assertEqual(
            [b.get("aria-controls") for b in controls],
            [b.get("data-dv-focus") for b in controls],
        )
        records = {
            a.get("href"): a.get("aria-label") or text(a) for a in nav.xpath(".//a")
        }
        self.assertEqual(
            records,
            {
                inventory.fan.get_absolute_url(): "ACME FAN-1 in FAN",
                inventory.nm1.get_absolute_url(): "Open ACME NM-2X in NM 1",
                inventory.sfp.get_absolute_url(): "Open ACME SFP-10G in Slot 1",
                inventory.nm2.get_absolute_url(): "Open ACME NM-2X in NM 2",
                inventory.psu.get_absolute_url(): "Open ACME PSU-1 in PSU",
            },
        )

    def test_the_overview_shows_and_each_focus_view_is_hidden(self):
        page = self.page()
        views = page.xpath(VIEWS)
        self.assertEqual(
            [(v.get("id"), v.get("hidden")) for v in views],
            [
                (self.overview(), None),
                (self.view(1, self.inventory.nm1), ""),
                (self.view(2, self.inventory.nm1, self.inventory.sfp), ""),
                (self.view(3, self.inventory.nm2), ""),
                (self.view(4, self.inventory.psu), ""),
            ],
        )

    def test_a_module_that_the_user_cannot_view_has_no_item_and_no_focus_view(self):
        inventory = self.inventory
        user = User.objects.create_user(username="partial")
        for model in (Device, Interface, ModuleBay):
            grant(user, model, "view")
        grant(
            user,
            Module,
            "view",
            constraints={"id__in": [inventory.nm1.pk, inventory.psu.pk]},
        )
        self.client.force_login(user)
        page = self.page()
        self.assertEqual(
            tree(self.nav(page).xpath("./ul/li")),
            [("ch1", [("ACME NM-2X in NM 1", []), ("ACME PSU-1 in PSU", [])])],
        )
        self.assertEqual(
            [v.get("id") for v in page.xpath(VIEWS)],
            [
                self.overview(),
                self.view(1, inventory.nm1),
                self.view(2, inventory.psu),
            ],
        )

    def test_the_device_page_panel_has_the_navigation_and_the_focus_views(self):
        config = copy.deepcopy(settings.PLUGINS_CONFIG)
        config["netbox_device_view"]["show_on_device_tab"] = True
        with override_settings(PLUGINS_CONFIG=config):
            content = self.client.get(self.device.get_absolute_url()).content.decode()
        page = lxml.html.fromstring(content)
        self.assertEqual(len(tree(self.nav(page).xpath("./ul/li/ul/li"))), 4)
        self.assertEqual(len(focus_faces(content)), 4)

    def test_a_device_without_drawn_modules_has_no_navigation(self):
        Module.objects.filter(device=self.device).delete()
        page = self.page()
        self.assertEqual(page.xpath("//nav[contains(@class, 'dv-art-nav')]"), [])
        self.assertEqual(page.xpath(VIEWS), [])
        self.assertEqual(len(faces(self.tab())), 1)


class FocusViewTests(AssemblyTestCase):
    def test_a_focus_view_draws_the_module_and_its_parts_in_its_own_coordinates(self):
        inventory = self.inventory
        nm1 = focus_face(self.tab(), "f1")
        self.assertEqual(nm1.get("viewBox"), "0 0 100 30")
        self.assertIn(f"{self.view(1, inventory.nm1)}-installed-con-p1", ids(nm1))
        # The transceiver sits in the bay of the network module, not of the chassis.
        [sfp] = nm1.findall(
            f".//{G}[@id='{self.view(1, inventory.nm1, inventory.sfp)}']"
        )
        self.assertEqual(clip_rect(nm1, sfp), ("40", "10", "14", "10"))
        self.assertEqual(art(sfp).get("transform"), "matrix(1 0 0 1 40 10)")
        self.assertIn(
            f"{self.view(1, inventory.nm1, inventory.sfp)}-installed-con-port",
            ids(sfp),
        )

    def test_a_turned_module_shows_unturned_in_its_focus_view(self):
        overview_psu = self.group(self.front(), self.inventory.psu)
        self.assertNotEqual(art(overview_psu).get("transform"), "matrix(1 0 0 1 0 0)")
        psu = focus_face(self.tab(), "f4")
        self.assertEqual(psu.get("viewBox"), "0 0 40 30")
        self.assertIsNone(psu.get("transform"))
        self.assertEqual(psu.findall(f"{G}[@transform]"), [])

    def test_the_ids_of_the_page_are_unique_and_each_reference_stays_in_its_view(self):
        content = self.tab()
        drawn = [*faces(content), *(face for _, face in focus_faces(content))]
        counts = Counter(ident for face in drawn for ident in ids(face))
        self.assertEqual([i for i, n in counts.items() if n > 1], [])
        for face in drawn:
            self.assertLessEqual(set(references(face)), set(ids(face)))

    def test_connectors_and_modules_in_a_focus_view_open_their_records(self):
        inventory = self.inventory
        nm1 = focus_face(self.tab(), "f1")
        found = links(nm1)
        port = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        self.assertEqual(
            found[f"{self.view(1, inventory.nm1)}-installed-con-p1"].get("href"),
            port.get_absolute_url(),
        )
        bodies = [
            a.get("href")
            for a in nm1.iter(f"{{{SVG_NS}}}a")
            if "dv-art-module-body" in a.get("class", "")
        ]
        self.assertEqual(bodies, [inventory.sfp.get_absolute_url()])

    def test_a_focus_connector_has_its_own_details(self):
        inventory = self.inventory
        content = self.tab()
        nm1 = focus_face(content, "f1")
        prefix = f"{self.view(1, inventory.nm1)}-installed-"
        wrapper = links(nm1)[f"{prefix}con-p1"]
        ident = details_id(prefix, "con-p1")
        self.assertEqual(wrapper.get("data-dv-details"), ident)
        [template] = lxml.html.fromstring(content).xpath(f"//template[@id='{ident}']")
        self.assertEqual(
            template.get("data-title"), "Connector P1 · ACME NM-2X in NM 1"
        )

    def test_a_focus_view_has_a_breadcrumb_back_to_each_parent(self):
        inventory = self.inventory
        page = self.page()
        [sfp] = page.xpath(f"//*[@id='{self.view(2, inventory.nm1, inventory.sfp)}']")
        self.assertEqual(sfp.get("data-dv-parent"), self.view(1, inventory.nm1))
        [crumbs] = sfp.xpath(".//ol[contains(@class, 'breadcrumb')]")
        self.assertEqual(
            [
                (text(li), li.xpath("./button/@data-dv-focus"), li.get("aria-current"))
                for li in crumbs.xpath("./li")
            ],
            [
                ("ch1", [self.overview()], None),
                ("ACME NM-2X in NM 1", [self.view(1, inventory.nm1)], None),
                ("ACME SFP-10G in Slot 1", [], "page"),
            ],
        )
        # Escape returns to the parent, the last button of the breadcrumb.
        self.assertEqual(crumbs.xpath(".//button/@aria-keyshortcuts"), ["Escape"])
        [nm1] = page.xpath(f"//*[@id='{self.view(1, inventory.nm1)}']")
        self.assertEqual(nm1.get("data-dv-parent"), self.overview())

    def test_the_artwork_api_has_no_focus_views(self):
        token = Token.objects.create(user=self.user)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        response = api.get(reverse(API_URL, kwargs={"pk": self.device.pk}))
        self.assertEqual(response.status_code, 200)
        [member] = response.json()["members"]
        [face] = member["artwork"]["faces"]
        drawn = ids(etree.fromstring(face["svg"]))
        self.assertTrue(drawn)
        self.assertEqual(
            [i for i in drawn if not i.startswith(f"dv-a-d{self.device.pk}-")], []
        )


class VirtualChassisTests(AssemblyTestCase):
    def setUp(self):
        super().setUp()
        chassis = VirtualChassis.objects.create(name="stack")
        self.other = Inventory(self.admin, name="ch2", activate=False)
        for position, device in enumerate((self.device, self.other.device), 1):
            Device.objects.filter(pk=device.pk).update(
                virtual_chassis=chassis, vc_position=position
            )
        grant(self.user, VirtualChassis, "view")

    def test_each_member_has_its_own_navigation_and_focus_views(self):
        content = self.tab()
        page = lxml.html.fromstring(content)
        navs = page.xpath("//nav[contains(@class, 'dv-art-nav')]")
        self.assertEqual(
            [nav.get("aria-label") for nav in navs],
            ["Assembly of ch1", "Assembly of ch2"],
        )
        for nav in navs:
            [part] = nav.xpath("ancestor::section[@data-dv-assembly]")
            # Each control shows a view of its own member.
            self.assertLessEqual(
                set(nav.xpath(".//button/@data-dv-focus")),
                set(part.xpath(".//*[@data-dv-parent or @class='dv-art-view']/@id")),
            )
        other = self.other.device.pk
        self.assertIn(
            f"dv-f1-d{other}-m{self.other.nm1.pk}",
            [v.get("id") for v in page.xpath(VIEWS)],
        )
        drawn = [*faces(content), *(face for _, face in focus_faces(content))]
        counts = Counter(ident for face in drawn for ident in ids(face))
        self.assertEqual([i for i, n in counts.items() if n > 1], [])
