"""The Device View tab draws each installed module in the bay of its parent, bound to the components of that module."""

import copy
import io
import re
import shutil
import tempfile

from dcim.models import (
    Cable,
    Device,
    Interface,
    Module,
    ModuleBay,
    PowerPort,
)
from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import etree
from users.models import ObjectPermission, User

from netbox_device_view.artwork import activation
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.storage import store_blob
from netbox_device_view.details import details_id

from .artwork_samples import bundle, definition
from .module_samples import CHASSIS_SVG, NM_MANIFEST, NM_SVG, Inventory
from .permissions import grant
from .test_artwork_device_view import SVG_NS, TAB_URL, faces, ids, links, references

G = f"{{{SVG_NS}}}g"


class CompositionTestCase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_user(username="admin", is_superuser=True)
        self.inventory = Inventory(self.admin)
        self.device = self.inventory.device
        self.user = User.objects.create_user(username="viewer")
        for model in (Device, Interface, PowerPort, Cable, Module, ModuleBay):
            grant(self.user, model, "view")
        self.client.force_login(self.user)

    def tab(self):
        response = self.client.get(reverse(TAB_URL, kwargs={"pk": self.device.pk}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def front(self):
        [front] = faces(self.tab())
        return front

    def part_id(self, *modules):
        return f"dv-o-d{self.device.pk}" + "".join(f"-m{m.pk}" for m in modules)

    def group(self, face, *modules):
        """Return the group of the module occurrence at the end of the placement path ``modules``."""
        found = face.findall(f".//{G}[@id='{self.part_id(*modules)}']")
        return found[0] if found else None

    def store_face(self, key, path, svg):
        """Store ``svg`` for ``path`` of the revision of ``key`` without the import validator."""
        self.inventory.revisions[key].files.filter(path=path).update(
            blob=store_blob(svg.encode(), "svg")
        )


def clip_rect(face, group):
    """Return the x, y, width and height of the clip rect of a module group."""
    clip_id = re.fullmatch(r"url\(#(.+)\)", group.get("clip-path"))[1]
    [rect] = face.find(f".//*[@id='{clip_id}']")
    return tuple(rect.get(name) for name in ("x", "y", "width", "height"))


def art(group):
    """Return the group that holds the placed child face."""
    return group.find(f"{G}[@transform]")


def warnings(content):
    """Return the text of each item of the warning lists of a page."""
    return [
        " ".join(re.sub(r"<[^>]+>", " ", item).split())
        for listing in re.findall(
            r'<ul class="dv-art-warnings[^"]*"[^>]*>(.*?)</ul>', content, re.DOTALL
        )
        for item in re.findall(r"<li>(.*?)</li>", listing, re.DOTALL)
    ]


class PlacementTests(CompositionTestCase):
    def test_an_installed_module_shows_at_the_position_of_its_bay(self):
        front = self.front()
        group = self.group(front, self.inventory.nm1)
        self.assertEqual(clip_rect(front, group), ("200", "7", "100", "30"))
        self.assertEqual(art(group).get("transform"), "matrix(1 0 0 1 200 7)")
        # The module draws over the bay rect of the chassis.
        self.assertEqual(
            group.getprevious().get("id"), f"dv-o-d{self.device.pk}-front-bay-nm1"
        )
        self.assertIn(
            f"{self.part_id(self.inventory.nm1)}-installed-con-p1", ids(group)
        )

    def test_a_module_face_keeps_its_own_viewport(self):
        # Percentage lengths resolve against the nearest viewport: the module face, not the chassis.
        self.store_face(
            "acme/nm-2x",
            "installed.svg",
            NM_SVG.replace('viewBox="0 0 100 30"', 'viewBox="5 7 100 30"').replace(
                '<rect id="con-p1" x="10"', '<rect id="con-p1" x="10%"'
            ),
        )
        group = self.group(self.front(), self.inventory.nm1)
        # The viewport maps the viewBox origin (5, 7) to the bay origin.
        self.assertEqual(art(group).get("transform"), "matrix(1 0 0 1 200 7)")
        [connector] = group.findall(
            f".//*[@id='{self.part_id(self.inventory.nm1)}-installed-con-p1']"
        )
        viewport = next(connector.iterancestors(f"{{{SVG_NS}}}svg"))
        self.assertEqual(
            {
                name: viewport.get(name)
                for name in ("viewBox", "x", "y", "width", "height")
            },
            {
                "viewBox": "5 7 100 30",
                "x": "0",
                "y": "0",
                "width": "100",
                "height": "30",
            },
        )
        self.assertEqual(viewport.getparent(), art(group))
        self.assertEqual(connector.get("x"), "10%")

    def test_a_rotated_bay_turns_the_module(self):
        group = self.group(self.front(), self.inventory.psu)
        # The 40 x 30 face turns by 90 degrees into the 30 x 40 bay at (120, 2).
        self.assertEqual(art(group).get("transform"), "matrix(0 1 -1 0 150 2)")

    def test_a_transceiver_in_a_bay_of_a_module_shows_at_that_bay_inside_the_module(
        self,
    ):
        front = self.front()
        nm1 = self.group(front, self.inventory.nm1)
        sfp = self.group(front, self.inventory.nm1, self.inventory.sfp)
        self.assertIn(sfp, art(nm1).iter())
        # The bay is in the coordinates of the module face.
        self.assertEqual(clip_rect(front, sfp), ("40", "10", "14", "10"))
        self.assertEqual(art(sfp).get("transform"), "matrix(1 0 0 1 40 10)")
        self.assertIsNone(self.group(front, self.inventory.nm2, self.inventory.sfp))

    def test_an_empty_bay_draws_nothing_more(self):
        front = self.front()
        spare = front.find(f".//*[@id='dv-o-d{self.device.pk}-front-bay-spare']")
        # The spare bay is the last element of the face, and nothing follows it.
        self.assertIsNone(spare.getnext())
        modules = [
            g
            for g in front.iter(G)
            if re.fullmatch(r"dv-o-d\d+(-m\d+)+", g.get("id", ""))
        ]
        self.assertEqual(len(modules), 5)


class IdentityTests(CompositionTestCase):
    def test_the_connectors_of_a_module_bind_to_the_components_of_that_module(self):
        inventory = self.inventory
        found = links(self.front())
        for path, name in (
            ((inventory.nm1,), "TenGigabitEthernet1/1/1"),
            ((inventory.nm2,), "TenGigabitEthernet1/2/1"),
            ((inventory.nm1, inventory.sfp), "Port 7"),
        ):
            module = path[-1]
            key = "con-port" if module == inventory.sfp else "con-p1"
            self.assertEqual(
                found[f"{self.part_id(*path)}-installed-{key}"].get("href"),
                inventory.interface(module, name).get_absolute_url(),
            )
        self.assertEqual(
            found[f"{self.part_id(inventory.psu)}-installed-con-inlet"].get("href"),
            PowerPort.objects.get(module=inventory.psu).get_absolute_url(),
        )

    def test_two_bays_with_the_same_module_type_share_no_id_state_or_link(self):
        inventory = self.inventory
        peer = Inventory(self.admin, name="ch2", activate=False)
        Cable(
            a_terminations=[
                inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
            ],
            b_terminations=[peer.eth1],
        ).save()
        front = self.front()
        every_id = ids(front)
        self.assertEqual(len(every_id), len(set(every_id)))
        self.assertLessEqual(set(references(front)), set(every_id))
        wrappers = {
            path[-1]: self.group(front, *path).find(
                f".//*[@data-dv-details='{details_id(self.part_id(*path) + '-installed-', 'con-p1')}']"
            )
            for path in ((inventory.nm1,), (inventory.nm2,))
        }
        self.assertIn("dv-art-cable-connected", wrappers[inventory.nm1].get("class"))
        self.assertNotIn("dv-art-cable", wrappers[inventory.nm2].get("class"))
        self.assertNotEqual(
            wrappers[inventory.nm1].get("href"), wrappers[inventory.nm2].get("href")
        )


class LinkTests(CompositionTestCase):
    def test_a_click_on_the_module_body_opens_the_module(self):
        front = self.front()
        for path in (
            (self.inventory.nm1,),
            (self.inventory.nm2,),
            (self.inventory.nm1, self.inventory.sfp),
        ):
            group = self.group(front, *path)
            [body] = group.findall(f"{{{SVG_NS}}}a[@class='dv-art-module-body']")
            self.assertEqual(body.get("href"), path[-1].get_absolute_url())
            # The artwork lets a click through to the body, except on a connector.
            self.assertEqual(art(group).get("class"), "dv-art-module-art")

    def test_a_module_without_artwork_is_an_outline_with_its_name_and_link(self):
        group = self.group(self.front(), self.inventory.fan)
        [body] = group.findall(f"{{{SVG_NS}}}a")
        self.assertEqual(body.get("class"), "dv-art-module-body dv-art-schematic")
        self.assertEqual(body.get("href"), self.inventory.fan.get_absolute_url())
        rect = body.find(f"{{{SVG_NS}}}rect")
        self.assertEqual(
            tuple(rect.get(name) for name in ("x", "y", "width", "height")),
            ("40", "4", "20", "36"),
        )
        self.assertEqual(body.find(f"{{{SVG_NS}}}text").text, "FAN-1")
        self.assertEqual(
            body.find(f"{{{SVG_NS}}}title").text, "Module ACME FAN-1 in FAN"
        )

    def test_the_details_of_a_module_connector_show_its_component_and_module(self):
        content = self.tab()
        ident = details_id(f"{self.part_id(self.inventory.nm2)}-installed-", "con-p1")
        template = re.search(
            rf'<template id="{ident}" data-title="([^"]*)">(.*?)</template>',
            content,
            re.DOTALL,
        )
        self.assertEqual(template[1], "Connector P1 · ACME NM-2X in NM 2")
        interface = self.inventory.interface(
            self.inventory.nm2, "TenGigabitEthernet1/2/1"
        )
        self.assertIn(f'href="{interface.get_absolute_url()}"', template[2])
        # The entry names the component that NetBox gives the module, not the template.
        self.assertIn("Interface TenGigabitEthernet1/2/1</a>", template[2])
        self.assertIn(f'href="{self.inventory.nm2.get_absolute_url()}"', template[2])


class VisibilityTests(CompositionTestCase):
    def test_a_module_that_the_user_cannot_view_is_not_drawn(self):
        ObjectPermission.objects.filter(name__startswith="dcim.module ").update(
            constraints={"module_type__model": "NM-2X"}
        )
        front = self.front()
        self.assertIsNotNone(self.group(front, self.inventory.nm1))
        self.assertIsNone(self.group(front, self.inventory.psu))

    def test_a_module_in_a_bay_that_the_user_cannot_view_is_not_drawn(self):
        ObjectPermission.objects.filter(name__startswith="dcim.modulebay").update(
            constraints={"name": "NM 2"}
        )
        front = self.front()
        self.assertIsNotNone(self.group(front, self.inventory.nm2))
        self.assertIsNone(self.group(front, self.inventory.nm1))
        self.assertIsNone(self.group(front, self.inventory.psu))


class WarningTests(CompositionTestCase):
    def test_a_size_mismatch_is_a_warning_and_the_module_shows(self):
        self.store_face(
            "acme/ch-4",
            "front.svg",
            CHASSIS_SVG.replace(
                'x="320" y="7" width="100"', 'x="320" y="7" width="110"'
            ),
        )
        content = self.tab()
        self.assertEqual(
            warnings(content),
            [
                (
                    "ACME NM-2X in NM 2: the artwork is 100 × 30 mm, but the bay"
                    " bay-nm2 is 110 × 30 mm."
                )
            ],
        )
        self.assertIsNotNone(art(self.group(faces(content)[0], self.inventory.nm2)))

    def test_a_placement_that_is_not_finite_is_a_warning_and_draws_nothing(self):
        self.store_face(
            "acme/ch-4", "front.svg", CHASSIS_SVG.replace('x="320"', 'x="1e999"')
        )
        content = self.tab()
        self.assertEqual(
            warnings(content),
            [
                (
                    "ACME NM-2X in NM 2: the bay bay-nm2 has a position or size that"
                    " is not a finite number, so the module is not drawn."
                )
            ],
        )
        front = faces(content)[0]
        self.assertIsNone(self.group(front, self.inventory.nm2))
        self.assertIsNotNone(self.group(front, self.inventory.nm1))
        self.assertNotIn("inf", etree.tostring(front, encoding="unicode"))

    def test_damaged_module_artwork_is_an_outline_with_a_warning(self):
        revision = self.inventory.revisions["acme/nm-2x"]
        blob = revision.files.get(path="installed.svg").blob
        path = settings.MEDIA_ROOT + "/" + blob.name
        with open(path, "w") as file:
            file.write(NM_SVG.replace("#555", "#666"))
        with self.assertLogs("netbox_device_view.artwork.render", "WARNING"):
            content = self.tab()
        front = faces(content)[0]
        for module in (self.inventory.nm1, self.inventory.nm2):
            body = self.group(front, module).find(f"{{{SVG_NS}}}a")
            self.assertEqual(body.get("class"), "dv-art-module-body dv-art-schematic")
        self.assertEqual(
            [warning.split(":")[0] for warning in warnings(content)],
            ["ACME NM-2X in NM 1", "ACME NM-2X in NM 2"],
        )
        self.assertIn("a stored file", warnings(content)[0])


class DevicePagePanelTests(CompositionTestCase):
    def test_the_device_page_panel_shows_the_modules(self):
        config = copy.deepcopy(settings.PLUGINS_CONFIG)
        config["netbox_device_view"]["show_on_device_tab"] = True
        with override_settings(PLUGINS_CONFIG=config):
            content = self.client.get(self.device.get_absolute_url()).content.decode()
        self.assertIsNotNone(self.group(faces(content)[0], self.inventory.nm1))


class QueryTests(CompositionTestCase):
    def test_one_more_module_of_a_drawn_type_adds_no_query(self):
        self.tab()
        with CaptureQueriesContext(connection) as before:
            self.tab()
        self.inventory.install(
            ModuleBay.objects.get(device=self.device, name="Spare"),
            self.inventory.nm_type,
        )
        with CaptureQueriesContext(connection) as after:
            content = self.tab()
        self.assertEqual(content.count('class="dv-art-module-art"'), 5)
        self.assertEqual(len(after), len(before))


def activate_alias_revision(test):
    """Activate a network module revision whose bays bay-alias and bay-slot both name Slot 1 in NM 1."""
    # The import compares the names before {module} resolves, so it cannot see this.
    bay = '  bay-slot: {face: installed, module_bay: "Slot {module}"}'
    files = {
        "manifest.yaml": NM_MANIFEST.replace(
            bay, f'  bay-alias: {{face: installed, module_bay: "Slot 1"}}\n{bay}'
        ),
        "installed.svg": NM_SVG.replace(
            '<rect id="bay-slot"',
            '<rect id="bay-alias" x="60" y="10" width="14" height="10"/>\n'
            '  <rect id="bay-slot"',
        ),
    }
    [imported] = import_bundle(
        test.admin, "alias.zip", io.BytesIO(bundle(definition("NM", files)))
    )
    activation.activate(test.admin, imported.revision, test.inventory.nm_type)


class DuplicateBayTests(CompositionTestCase):
    def test_two_bays_that_resolve_to_one_module_bay_draw_the_module_once(self):
        activate_alias_revision(self)
        content = self.tab()
        front = faces(content)[0]
        every_id = ids(front)
        self.assertEqual(len(every_id), len(set(every_id)))
        path = (self.inventory.nm1, self.inventory.sfp)
        [sfp] = front.findall(f".//{G}[@id='{self.part_id(*path)}']")
        self.assertEqual(clip_rect(front, sfp), ("60", "10", "14", "10"))
        self.assertIn(
            "ACME SFP-10G in Slot 1: the bays bay-alias and bay-slot map to the module"
            " bay Slot 1, so the module shows only in bay-alias.",
            warnings(content),
        )
