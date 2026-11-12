"""The ACS6048 reference artwork imports offline and binds each connector on a device of the community type."""

import io
import shutil
import tempfile
from io import StringIO

from dcim.models import (
    Cable,
    ConsolePort,
    ConsoleServerPort,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    PowerPort,
    Site,
)
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import etree
from users.models import User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import Association

from .permissions import grant
from .reference_samples import (
    ARTWORK,
    COMMUNITY,
    LIBRARY,
    community_type,
    definition_bundle,
)
from .test_api_artwork import api_agreement, member_artwork, page_agreement
from .test_artwork_device_view import TAB_URL, faces, links

DEFINITION = ARTWORK / "Avocent" / "ACS6048"
COMMUNITY_TYPE = COMMUNITY / "device-types" / "Avocent" / "ACS6048.yaml"


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
            self.admin, "acs6048.zip", io.BytesIO(definition_bundle("Avocent/ACS6048"))
        )
        self.assertTrue(imported.created)
        manifest = imported.revision.manifest
        self.assertEqual(manifest["key"], "avocent/acs6048")
        self.assertEqual(manifest["hardware"]["sku"], "ACS6048DAC-G2")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(len(manifest["connectors"]), 54)

    def activate(self):
        """Return a device of the community type, and the revision that is its active artwork."""
        device_type = community_type(COMMUNITY_TYPE)
        device = Device.objects.create(
            name="acs1",
            device_type=device_type,
            site=Site.objects.create(name="Site", slug="site"),
            role=DeviceRole.objects.create(name="Role", slug="role"),
        )
        [imported] = import_bundle(
            self.admin, "acs6048.zip", io.BytesIO(definition_bundle("Avocent/ACS6048"))
        )
        Association.objects.create(device_type=device_type, revision=imported.revision)
        return device, imported.revision

    def tab(self, device):
        response = self.client.get(reverse(TAB_URL, kwargs={"pk": device.pk}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def tab_faces(self, device):
        return faces(self.tab(device))

    def cable_console(self, device):
        """Cable the first console-server port of ``device`` to a console port of another device; return the port."""
        port = device.consoleserverports.order_by("pk").first()
        peer = Device.objects.create(
            name="server1",
            device_type=DeviceType.objects.create(
                manufacturer=device.device_type.manufacturer,
                model="Server",
                slug="server",
            ),
            site=device.site,
            role=device.role,
        )
        Cable(
            a_terminations=[port],
            b_terminations=[ConsolePort.objects.create(device=peer, name="console")],
        ).save()
        return port

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

    def test_the_page_and_the_api_agree_for_a_superuser(self):
        device, _ = self.activate()
        port = self.cable_console(device)

        api = api_agreement(member_artwork(self, device, self.admin))

        self.assertEqual(page_agreement(self.tab(device)), api)
        self.assertEqual(len(api[1]), 54)
        self.assertEqual(
            [state for state in api[1].values() if state[1]],
            [(port.get_absolute_url(), {"dv-art-cable-connected"})],
        )

    def test_the_page_and_the_api_agree_for_a_user_who_cannot_view_one_component(
        self,
    ):
        device, _ = self.activate()
        port = self.cable_console(device)
        user = User.objects.create_user(username="viewer")
        for model in (Device, Interface, ConsolePort, PowerPort, Cable):
            grant(user, model, "view")
        visible = device.consoleserverports.exclude(pk=port.pk)
        grant(
            user,
            ConsoleServerPort,
            "view",
            constraints={"id__in": list(visible.values_list("pk", flat=True))},
        )
        self.client.force_login(user)

        artwork = member_artwork(self, device, user)

        self.assertEqual(page_agreement(self.tab(device)), api_agreement(artwork))
        statuses = {
            binding["name"]: binding["status"]
            for connector in artwork["connectors"]
            for binding in connector["bindings"]
            if binding["object_type"] == "dcim.consoleserverport"
        }
        self.assertEqual(statuses.pop(port.name), "missing")
        self.assertEqual(set(statuses.values()), {"found"})

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
            stdout.getvalue().strip(), "Checked 0 layouts and 4 artwork definitions."
        )
