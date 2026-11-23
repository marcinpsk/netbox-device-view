"""The ACS6048 reference artwork imports offline and binds each connector on a device of the community type."""

import io
import shutil
import tempfile
import zipfile
from io import StringIO
from pathlib import Path

import yaml
from dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    Device,
    DeviceRole,
    DeviceType,
    InterfaceTemplate,
    Manufacturer,
    PowerPortTemplate,
    Site,
)
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import etree
from users.models import User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import Association

from .test_artwork_device_view import TAB_URL, faces, links

# A copy of the definition in netbox-device-view-library, and of the community type that it binds.
FIXTURES = Path(__file__).parent / "fixtures"
LIBRARY = FIXTURES / "library"
DEFINITION = LIBRARY / "artwork" / "Avocent" / "ACS6048"
COMMUNITY = FIXTURES / "devicetype-library"
COMMUNITY_TYPE = COMMUNITY / "device-types" / "Avocent" / "ACS6048.yaml"
TEMPLATES = {
    "interfaces": InterfaceTemplate,
    "console-ports": ConsolePortTemplate,
    "console-server-ports": ConsoleServerPortTemplate,
    "power-ports": PowerPortTemplate,
}


def definition_bundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(DEFINITION.iterdir()):
            archive.writestr(f"artwork/Avocent/ACS6048/{file.name}", file.read_bytes())
    return buffer.getvalue()


def community_device_type() -> DeviceType:
    """Create the community device type with its component templates, as an import would."""
    data = yaml.safe_load(COMMUNITY_TYPE.read_text())
    manufacturer = Manufacturer.objects.create(
        name=data["manufacturer"], slug=data["manufacturer"].lower()
    )
    device_type = DeviceType.objects.create(
        manufacturer=manufacturer,
        model=data["model"],
        slug=data["slug"],
        part_number=data["part_number"],
        u_height=data["u_height"],
    )
    for key, template in TEMPLATES.items():
        template.objects.bulk_create(
            template(device_type=device_type, name=item["name"], type=item["type"])
            for item in data[key]
        )
    return device_type


class ReferenceArtworkTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_user(username="admin", is_superuser=True)
        self.client.force_login(self.admin)

    def test_the_definition_passes_the_offline_bundle_import(self):
        [imported] = import_bundle(
            self.admin, "acs6048.zip", io.BytesIO(definition_bundle())
        )
        self.assertTrue(imported.created)
        manifest = imported.revision.manifest
        self.assertEqual(manifest["key"], "avocent/acs6048")
        self.assertEqual(manifest["hardware"]["sku"], "ACS6048DAC-G2")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(len(manifest["connectors"]), 54)

    def activate(self):
        """Return a device of the community type, and the revision that is its active artwork."""
        device_type = community_device_type()
        device = Device.objects.create(
            name="acs1",
            device_type=device_type,
            site=Site.objects.create(name="Site", slug="site"),
            role=DeviceRole.objects.create(name="Role", slug="role"),
        )
        [imported] = import_bundle(
            self.admin, "acs6048.zip", io.BytesIO(definition_bundle())
        )
        Association.objects.create(device_type=device_type, revision=imported.revision)
        return device, imported.revision

    def tab_faces(self, device):
        content = self.client.get(reverse(TAB_URL, kwargs={"pk": device.pk}))
        return faces(content.content.decode())

    def test_each_face_has_its_real_size_and_the_page_scales_it(self):
        for name in ("front", "rear"):
            root = etree.fromstring((DEFINITION / f"{name}.svg").read_bytes())
            self.assertEqual(root.get("viewBox"), "0 0 438.15 43.56")
            # A viewer outside the plugin shows the face at its size on the unit.
            self.assertEqual(
                (root.get("width"), root.get("height")), ("438.15mm", "43.56mm")
            )
        device, _ = self.activate()
        for face in self.tab_faces(device):
            self.assertEqual((face.get("width"), face.get("height")), (None, None))

    def test_each_bound_connector_links_to_its_component(self):
        device, revision = self.activate()
        components = {
            "interface": device.interfaces,
            "consoleport": device.consoleports,
            "consoleserverport": device.consoleserverports,
            "powerport": device.powerports,
        }
        expected = {
            f"dv-o-d{device.pk}-rear-{key}": components[binding["component"]]
            .get(name=binding["name"])
            .get_absolute_url()
            for key, connector in revision.manifest["connectors"].items()
            for binding in connector["bindings"]
        }

        front, rear = self.tab_faces(device)

        self.assertEqual(len(expected), 54)
        self.assertEqual(
            {key: link.get("href") for key, link in links(rear).items()}, expected
        )
        # The USB port and the LEDs are decorative.
        self.assertEqual(links(front), {})

    def test_the_library_copy_passes_the_check_against_the_community_type(self):
        stdout = StringIO()
        call_command(
            "device_view_check",
            str(LIBRARY),
            "--device-types",
            str(COMMUNITY),
            stdout=stdout,
        )
        self.assertEqual(
            stdout.getvalue().strip(), "Checked 0 layouts and 1 artwork definition."
        )
