"""The C8300 reference artwork composes the transceivers and the service module of a device of the community types."""

import io
import shutil
import tempfile
from collections import Counter

import lxml.html
from dcim.models import (
    Cable,
    ConsolePort,
    ConsoleServerPort,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Module,
    ModuleBay,
    PowerPort,
    Site,
)
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import etree
from users.models import User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.render import API, OVERVIEW

from .activation import activate
from .permissions import grant
from .reference_samples import (
    ARTWORK,
    COMMUNITY,
    UPSTREAM,
    community_type,
    definition_bundle,
)
from .test_api_artwork import (
    api_agreement,
    api_id,
    artwork_connectors,
    member_artwork,
    page_agreement,
)
from .test_artwork_device_view import SVG_NS, TAB_URL, faces, ids, links, references
from .test_assembly_focus import VIEWS, focus_faces, text

DIRECTORIES = ("Cisco/C8300-1N1S-6T", "Cisco/C-SM-16P4M2X", "Cisco/GLC-TE")
CHASSIS = "device-types/Cisco/C8300-1N1S-6T.yaml"
MODULES = {
    "sm": "C-SM-16P4M2X",
    "optic": "GLC-TE",
    "nim": "NIM-16A",
    "psu": "C8300-PWR-CC1-250WAC",
}


class C8300TestCase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_user(username="admin", is_superuser=True)
        self.client.force_login(self.admin)
        self.revisions = {
            imported.revision.definition.key: imported.revision
            for imported in import_bundle(
                self.admin, "c8300.zip", io.BytesIO(definition_bundle(*DIRECTORIES))
            )
        }

    def device(self, community, name):
        device_type = community_type(community / CHASSIS)
        activate(self.admin, (self.revisions["cisco/c8300-1n1s-6t"], device_type))
        return Device.objects.create(
            name=name,
            device_type=device_type,
            site=Site.objects.get_or_create(name="Site", slug="site")[0],
            role=DeviceRole.objects.get_or_create(name="Role", slug="role")[0],
        )

    def install(self, device, bay_name, module_type, parent=None):
        bay = ModuleBay.objects.get(device=device, module=parent, name=bay_name)
        return Module.objects.create(
            device=device, module_bay=bay, module_type=module_type
        )

    def tab(self, device):
        response = self.client.get(reverse(TAB_URL, kwargs={"pk": device.pk}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def tab_faces(self, device):
        return faces(self.tab(device))

    def artwork(self, device, user=None):
        return member_artwork(self, device, user or self.admin)


class DefinitionTests(C8300TestCase):
    def test_the_definitions_pass_the_offline_bundle_import(self):
        self.assertEqual(
            {
                key: (
                    revision.manifest["kind"],
                    revision.manifest["hardware"]["sku"],
                    revision.manifest["license"],
                    sorted(revision.manifest["faces"]),
                )
                for key, revision in self.revisions.items()
            },
            {
                "cisco/c8300-1n1s-6t": (
                    "device",
                    "C8300-1N1S-6T",
                    "MIT",
                    ["front", "rear"],
                ),
                "cisco/c-sm-16p4m2x": ("module", "C-SM-16P4M2X", "MIT", ["installed"]),
                "cisco/glc-te": ("module", "GLC-TE", "MIT", ["installed"]),
            },
        )

    def test_each_face_has_its_real_size_and_each_bay_fits_its_module(self):
        def size(directory, face):
            root = etree.fromstring((ARTWORK / directory / f"{face}.svg").read_bytes())
            width, height = root.get("viewBox").split()[2:]
            # A viewer outside the plugin shows the face at its size in millimetres.
            self.assertEqual(
                (root.get("width"), root.get("height")), (f"{width}mm", f"{height}mm")
            )
            return float(width), float(height)

        def bay(directory, face, key):
            root = etree.fromstring((ARTWORK / directory / f"{face}.svg").read_bytes())
            [rect] = root.xpath("//*[@id=$key]", key=key)
            return float(rect.get("width")), float(rect.get("height"))

        chassis, sm, optic = DIRECTORIES
        # 1.73 x 17.50 in, from the data sheet.
        self.assertEqual(size(chassis, "front"), (444.5, 43.9))
        self.assertEqual(size(chassis, "rear"), (444.5, 43.9))
        self.assertEqual(bay(chassis, "front", "bay-sm1"), size(sm, "installed"))
        for directory, key in (
            (chassis, "bay-sfp4"),
            (chassis, "bay-sfp5"),
            (sm, "bay-te20"),
            (sm, "bay-te21"),
        ):
            face = "front" if directory == chassis else "installed"
            self.assertEqual(bay(directory, face, key), size(optic, "installed"))


class NestedTestCase(C8300TestCase):
    """The modules of the lab device: transceivers in the chassis and in the service module."""

    def setUp(self):
        super().setUp()
        types = {
            role: community_type(COMMUNITY / "module-types" / "Cisco" / f"{model}.yaml")
            for role, model in MODULES.items()
        }
        activate(
            self.admin,
            (self.revisions["cisco/c-sm-16p4m2x"], types["sm"]),
            (self.revisions["cisco/glc-te"], types["optic"]),
        )
        self.router = self.device(COMMUNITY, "rtr1")
        self.optic4 = self.install(self.router, "Transceiver 0/0/4", types["optic"])
        self.optic5 = self.install(self.router, "Transceiver 0/0/5", types["optic"])
        self.nim = self.install(self.router, "NIM 1", types["nim"])
        self.sm = self.install(self.router, "SM 1", types["sm"])
        self.sm_optic = self.install(
            self.router, "Transceiver 1/0/20", types["optic"], parent=self.sm
        )
        self.psu0 = self.install(self.router, "PSU0", types["psu"])
        self.psu1 = self.install(self.router, "PSU1", types["psu"])

    def part(self, *modules, view=OVERVIEW):
        return f"dv-{view}-d{self.router.pk}" + "".join(f"-m{m.pk}" for m in modules)

    def placement(self, module):
        """Return the modules on the placement path of ``module``, outermost first."""
        parent = module.module_bay.module
        return (module,) if parent is None else (*self.placement(parent), module)

    def interface(self, module, name):
        return Interface.objects.get(device=self.router, module=module, name=name)

    def cable(self, interface):
        peer_type, _ = DeviceType.objects.get_or_create(
            manufacturer=self.router.device_type.manufacturer, model="Peer", slug="peer"
        )
        peer, _ = Device.objects.get_or_create(
            name="peer",
            device_type=peer_type,
            site=self.router.site,
            role=self.router.role,
        )
        name = f"eth{peer.interfaces.count()}"
        Cable(
            a_terminations=[interface],
            b_terminations=[
                Interface.objects.create(device=peer, name=name, type="1000base-t")
            ],
        ).save()


class NestedTests(NestedTestCase):
    def test_the_composed_view_binds_each_connector_to_the_component_of_its_part(self):
        expected = {
            f"{self.part()}-front-con-con0": ConsolePort.objects.get(
                device=self.router, name="con0"
            ),
            **{
                f"{self.part()}-front-con-gi0-0-{n}": self.interface(
                    None, f"GigabitEthernet0/0/{n}"
                )
                for n in range(4)
            },
            # The transceiver owns the interface; the position of its bay gives the name.
            f"{self.part(self.optic4)}-installed-con-rj45": self.interface(
                self.optic4, "GigabitEthernet0/0/4"
            ),
            f"{self.part(self.optic5)}-installed-con-rj45": self.interface(
                self.optic5, "GigabitEthernet0/0/5"
            ),
            **{
                f"{self.part(self.sm)}-installed-con-gi{n}": self.interface(
                    self.sm, f"GigabitEthernet1/0/{n}"
                )
                for n in range(16)
            },
            **{
                f"{self.part(self.sm)}-installed-con-mgig{n}": self.interface(
                    self.sm, f"TwoGigabitEthernet1/0/{n}"
                )
                for n in range(16, 20)
            },
            f"{self.part(self.sm, self.sm_optic)}-installed-con-rj45": self.interface(
                self.sm_optic, "TenGigabitEthernet1/0/20"
            ),
        }

        front, _rear = self.tab_faces(self.router)

        found = {key: link.get("href") for key, link in links(front).items()}
        # The console-server ports of the NIM-16A schematic link too; they are not artwork.
        self.assertEqual(
            {key: href for key, href in found.items() if "-schematic-" not in key},
            {key: c.get_absolute_url() for key, c in expected.items()},
        )
        self.assertEqual(
            len([key for key in found if "-schematic-consoleserverport-" in key]), 16
        )

    def test_each_bay_shows_its_module_or_not_recorded(self):
        artwork = self.artwork(self.router)
        self.assertEqual(
            {
                (entry["bay_key"], entry["state"], entry["module_id"])
                for entry in artwork["bays"]
            },
            {
                ("bay-sfp4", "installed", self.optic4.pk),
                ("bay-sfp5", "installed", self.optic5.pk),
                ("bay-nim1", "installed", self.nim.pk),
                ("bay-sm1", "installed", self.sm.pk),
                ("bay-te20", "installed", self.sm_optic.pk),
                ("bay-te21", "not_recorded", None),
                ("bay-psu0", "installed", self.psu0.pk),
                ("bay-psu1", "installed", self.psu1.pk),
                ("bay-pim1", "not_recorded", None),
            },
        )
        self.assertEqual(
            {
                module["module_id"]: (module["representation"], module["warnings"])
                for module in artwork["modules"]
            },
            {
                self.optic4.pk: ("artwork", []),
                self.optic5.pk: ("artwork", []),
                self.nim.pk: ("schematic", []),
                self.sm.pk: ("artwork", []),
                self.sm_optic.pk: ("artwork", []),
                self.psu0.pk: ("schematic", []),
                self.psu1.pk: ("schematic", []),
            },
        )


class AgreementTests(NestedTestCase):
    """The page and the artwork API give the same ids, links, binding states, bays and modules."""

    def setUp(self):
        super().setUp()
        self.port4 = self.interface(self.optic4, "GigabitEthernet0/0/4")
        self.cable(self.port4)
        # A disabled interface with a cable gets the cable mark and the disabled mark.
        self.gi0 = self.interface(None, "GigabitEthernet0/0/0")
        self.gi0.enabled = False
        self.gi0.save()
        self.cable(self.gi0)

    def element(self, content, element_id):
        """Return the element ``element_id`` of the overview."""
        [element] = [
            e
            for face in faces(content)
            for e in face.iter()
            if e.get("id") == element_id
        ]
        return element

    def bindings(self, artwork, element_id):
        [connector] = [
            c for c in artwork_connectors(artwork) if c["element_id"] == element_id
        ]
        return connector["bindings"]

    def binding(self, component, peer, **facts):
        """Return the API binding of a found interface that has a cable to the interface ``peer`` of the peer."""
        return {
            "status": "found",
            "object_type": "dcim.interface",
            "object_id": component.pk,
            "name": component.name,
            "cable_color": "",
            "marked_connected": False,
            "path_complete": True,
            "peer_labels": [f"peer | {peer}"],
            "trace_supported": True,
            **facts,
        }

    def expected_modules(self, *modules, view=API):
        artwork = {self.optic4, self.optic5, self.sm, self.sm_optic}
        return {
            self.part(*self.placement(m), view=view): (
                m.get_absolute_url(),
                "artwork" if m in artwork else "schematic",
            )
            for m in modules
        }

    def test_the_page_and_the_api_agree_for_a_superuser(self):
        page = page_agreement(self.tab(self.router))
        api = api_agreement(self.artwork(self.router))
        self.assertEqual(page, api)
        rj45 = f"dv-a-d{self.router.pk}-m{self.optic4.pk}-installed-con-rj45"
        self.assertEqual(api[1][rj45][1], {"dv-art-cable-connected"})

    def test_each_drawn_bay_and_module_has_one_state_on_the_page_and_in_the_api(self):
        _faces, _connectors, bays, modules = page_agreement(self.tab(self.router))
        installed = (
            self.optic4,
            self.optic5,
            self.nim,
            self.sm,
            self.sm_optic,
            self.psu0,
            self.psu1,
        )
        self.assertEqual(
            bays,
            {
                **{
                    self.part(*self.placement(m), view=API): (
                        "installed",
                        m.get_absolute_url(),
                    )
                    for m in installed
                },
                f"{self.part(view=API)}-rear--not-recorded-bay-pim1": (
                    "not_recorded",
                    None,
                ),
                f"{self.part(self.sm, view=API)}-installed--not-recorded-bay-te21": (
                    "not_recorded",
                    None,
                ),
            },
        )
        self.assertEqual(modules, self.expected_modules(*installed))
        self.assertEqual(api_agreement(self.artwork(self.router))[2:], (bays, modules))

    def test_named_connectors_and_bays_have_their_known_page_marks_and_api_fields(
        self,
    ):
        content = self.tab(self.router)
        artwork = self.artwork(self.router)
        gi0 = f"{self.part()}-front-con-gi0-0-0"
        rj45 = f"{self.part(self.optic4)}-installed-con-rj45"
        te21 = f"{self.part(self.sm)}-installed--not-recorded-bay-te21"

        for element_id, component, peer, classes, enabled in (
            (gi0, self.gi0, "eth1", "dv-art-cable-connected dv-art-disabled", False),
            (rj45, self.port4, "eth0", "dv-art-cable-connected", True),
        ):
            with self.subTest(component.name):
                link = self.element(content, element_id).getparent()
                self.assertEqual(
                    (link.tag, link.get("href"), link.get("class")),
                    (
                        f"{{{SVG_NS}}}a",
                        component.get_absolute_url(),
                        f"dv-art-connector {classes}",
                    ),
                )
                self.assertEqual(
                    self.bindings(artwork, api_id(element_id)),
                    [
                        self.binding(
                            component, peer, enabled=enabled, cable_status="connected"
                        )
                    ],
                )
        # The empty transceiver bay of the module has no optic, so no port and no interface.
        mark = self.element(content, te21)
        self.assertEqual(
            (mark.get("class"), list(links(mark)), "".join(mark.itertext())),
            ("dv-art-not-recorded", [], "not recorded"),
        )
        self.assertIn(
            {
                "parent_module_id": self.sm.pk,
                "bay_key": "bay-te21",
                "module_bay_id": ModuleBay.objects.get(
                    module=self.sm, name="Transceiver 1/0/21"
                ).pk,
                # The face of the device that shows the bay.
                "face": "front",
                "state": "not_recorded",
                "module_id": None,
                "element_id": api_id(te21),
            },
            artwork["bays"],
        )
        self.assertFalse(
            Interface.objects.filter(
                device=self.router, name="TenGigabitEthernet1/0/21"
            ).exists()
        )

    def test_the_page_and_the_api_agree_for_a_user_who_cannot_view_a_module_or_a_component(
        self,
    ):
        user = User.objects.create_user(username="viewer")
        for model in (
            Device,
            ConsolePort,
            ConsoleServerPort,
            PowerPort,
            Cable,
            ModuleBay,
        ):
            grant(user, model, "view")
        hidden = self.interface(None, "GigabitEthernet0/0/1")
        grant(
            user,
            Interface,
            "view",
            constraints={
                "id__in": list(
                    Interface.objects.exclude(pk=hidden.pk).values_list("pk", flat=True)
                )
            },
        )
        grant(
            user,
            Module,
            "view",
            constraints={
                "id__in": list(
                    Module.objects.exclude(pk=self.optic5.pk).values_list(
                        "pk", flat=True
                    )
                )
            },
        )
        self.client.force_login(user)

        content = self.tab(self.router)
        artwork = self.artwork(self.router, user)

        page = page_agreement(content)
        self.assertEqual(page, api_agreement(artwork))
        _faces, _connectors, bays, modules = page
        self.assertNotIn(self.optic5.pk, [m["module_id"] for m in artwork["modules"]])
        self.assertIn(
            ("bay-sfp5", "not_recorded", None),
            [(b["bay_key"], b["state"], b["module_id"]) for b in artwork["bays"]],
        )
        sfp5 = f"{self.part()}-front--not-recorded-bay-sfp5"
        self.assertEqual(bays[api_id(sfp5)], ("not_recorded", None))
        self.assertEqual(
            modules,
            self.expected_modules(
                self.optic4, self.nim, self.sm, self.sm_optic, self.psu0, self.psu1
            ),
        )
        # The hidden optic draws no port, and the API binds no connector to its interface.
        self.assertNotRegex(content, rf"-m{self.optic5.pk}(?!\d)")
        port5 = self.interface(self.optic5, "GigabitEthernet0/0/5")
        self.assertNotIn(
            port5.pk,
            [
                b["object_id"]
                for c in artwork_connectors(artwork)
                for b in c["bindings"]
            ],
        )
        # The connector of the hidden interface has no component: a button, not a link.
        gi1 = f"{self.part()}-front-con-gi0-0-1"
        button = self.element(content, gi1).getparent()
        self.assertEqual(
            (button.tag, button.get("href"), button.get("class"), button.get("role")),
            (f"{{{SVG_NS}}}g", None, "dv-art-connector dv-art-unresolved", "button"),
        )
        self.assertEqual(
            self.bindings(artwork, api_id(gi1)),
            [
                {
                    "status": "missing",
                    "object_type": "dcim.interface",
                    "object_id": None,
                    "name": "GigabitEthernet0/0/1",
                    "enabled": None,
                    "cable_status": None,
                    "cable_color": None,
                    "marked_connected": None,
                    "path_complete": None,
                    "peer_labels": [],
                    "trace_supported": False,
                }
            ],
        )


class FocusTests(NestedTestCase):
    """The service module and its transceiver have focus views of their own."""

    def view(self, *modules):
        """Return the focus view of the module at the end of the placement path ``modules``."""
        page = lxml.html.fromstring(self.tab(self.router))
        path = f"-d{self.router.pk}" + "".join(f"-m{m.pk}" for m in modules)
        [view] = [v for v in page.xpath(VIEWS) if v.get("id").endswith(path)]
        return view

    def test_the_ids_of_the_page_are_unique_and_each_reference_stays_in_its_view(self):
        content = self.tab(self.router)
        drawn = [*faces(content), *(face for _, face in focus_faces(content))]
        # The overview and a view for each of the 7 modules.
        self.assertEqual(len(drawn), 2 + 7)
        counts = Counter(ident for face in drawn for ident in ids(face))
        self.assertEqual([i for i, n in counts.items() if n > 1], [])
        for face in drawn:
            self.assertLessEqual(set(references(face)), set(ids(face)))

    def test_the_transceiver_of_the_service_module_has_a_breadcrumb_to_the_module_and_the_chassis(
        self,
    ):
        sm = self.view(self.sm)
        optic = self.view(self.sm, self.sm_optic)
        self.assertEqual(optic.get("data-dv-parent"), sm.get("id"))
        [crumbs] = optic.xpath(".//ol[contains(@class, 'breadcrumb')]")
        self.assertEqual(
            [
                (text(li), li.xpath("./button/@data-dv-focus"), li.get("aria-current"))
                for li in crumbs.xpath("./li")
            ],
            [
                ("rtr1", [self.part()], None),
                ("Cisco C-SM-16P4M2X in SM 1", [sm.get("id")], None),
                ("Cisco GLC-TE in Transceiver 1/0/20", [], "page"),
            ],
        )

    def test_two_transceivers_of_one_type_keep_their_own_cable_state(self):
        port4 = self.interface(self.optic4, "GigabitEthernet0/0/4")
        port5 = self.interface(self.optic5, "GigabitEthernet0/0/5")
        self.cable(port4)

        content = self.tab(self.router)
        drawn = [*faces(content), *(face for _, face in focus_faces(content))]
        found = {key: link for face in drawn for key, link in links(face).items()}

        for optic, port, classes in (
            (self.optic4, port4, "dv-art-connector dv-art-cable-connected"),
            (self.optic5, port5, "dv-art-connector"),
        ):
            with self.subTest(optic.module_bay.name):
                rj45 = [
                    link
                    for key, link in found.items()
                    if key.endswith(f"-m{optic.pk}-installed-con-rj45")
                ]
                # The overview and the focus view of the transceiver.
                self.assertEqual(len(rj45), 2)
                for link in rj45:
                    self.assertEqual(
                        (link.get("href"), link.get("class")),
                        (port.get_absolute_url(), classes),
                    )
        modules = {m["module_id"]: m for m in self.artwork(self.router)["modules"]}
        self.assertEqual(
            [
                [
                    b["cable_status"]
                    for b in modules[optic.pk]["connectors"][0]["bindings"]
                ]
                for optic in (self.optic4, self.optic5)
            ],
            [["connected"], [None]],
        )


class UpstreamTypeTests(C8300TestCase):
    """The upstream type has the SFP interfaces on the chassis and no transceiver bays."""

    def test_no_transceiver_is_invented_for_an_sfp_interface_of_the_chassis(self):
        router = self.device(UPSTREAM, "rtr2")
        sfp_ports = Interface.objects.filter(
            device=router, name__in=("GigabitEthernet0/0/4", "GigabitEthernet0/0/5")
        )
        self.assertEqual(sfp_ports.count(), 2)

        artwork = self.artwork(router)
        front, _rear = self.tab_faces(router)

        self.assertEqual(artwork["modules"], [])
        self.assertEqual(
            {
                (entry["bay_key"], entry["state"], entry["module_bay_id"])
                for entry in artwork["bays"]
                if entry["bay_key"].startswith("bay-sfp")
            },
            {("bay-sfp4", "not_recorded", None), ("bay-sfp5", "not_recorded", None)},
        )
        self.assertEqual(
            [
                element.get("id")
                for element in front.iter()
                if "--not-recorded-bay-sfp" in (element.get("id") or "")
            ],
            [
                f"dv-o-d{router.pk}-front--not-recorded-bay-sfp4",
                f"dv-o-d{router.pk}-front--not-recorded-bay-sfp5",
            ],
        )
        hrefs = {link.get("href") for link in links(front).values()}
        self.assertFalse(hrefs & {port.get_absolute_url() for port in sfp_ports})
