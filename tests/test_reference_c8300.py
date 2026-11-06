"""The C8300 reference artwork composes the transceivers and the service module of a device of the community types."""

import io
import shutil
import tempfile

from dcim.models import (
    ConsolePort,
    Device,
    DeviceRole,
    Interface,
    Module,
    ModuleBay,
    Site,
)
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import etree
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view.artwork.importing import import_bundle

from .activation import activate
from .reference_samples import (
    ARTWORK,
    COMMUNITY,
    UPSTREAM,
    community_type,
    definition_bundle,
)
from .test_api_artwork import API_URL
from .test_artwork_device_view import TAB_URL, faces, links

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

    def tab_faces(self, device):
        response = self.client.get(reverse(TAB_URL, kwargs={"pk": device.pk}))
        self.assertEqual(response.status_code, 200)
        return faces(response.content.decode())

    def artwork(self, device):
        token = Token.objects.create(user=self.admin)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        response = api.get(reverse(API_URL, kwargs={"pk": device.pk}))
        self.assertEqual(response.status_code, 200)
        [member] = response.json()["members"]
        return member["artwork"]


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


class NestedTests(C8300TestCase):
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

    def part(self, *modules):
        return f"dv-o-d{self.router.pk}" + "".join(f"-m{m.pk}" for m in modules)

    def interface(self, module, name):
        return Interface.objects.get(device=self.router, module=module, name=name)

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
