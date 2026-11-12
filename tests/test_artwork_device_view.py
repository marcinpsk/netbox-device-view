"""The Device View tab and the device page panel draw the active artwork, bound to the components of each device."""

import copy
import io
import re
import shutil
import tempfile
from pathlib import Path

import pytest
from dcim.models import (
    Cable,
    ConsolePort,
    Device,
    DeviceRole,
    DeviceType,
    FrontPort,
    Interface,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleType,
    PowerOutlet,
    PowerPort,
    RearPort,
    Site,
    VirtualChassis,
)
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import etree
from users.models import ObjectPermission, User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.storage import store_blob
from netbox_device_view.artwork.svg import transform_matrix
from netbox_device_view.models import Association, DeviceView

from .artwork_samples import DEVICE_MANIFEST, bundle, definition
from .permissions import grant

TAB_URL = "dcim:device_deviceview"
SVG_NS = "http://www.w3.org/2000/svg"
GRID = '.deviceview.area{grid-template-areas:"eth-1";}'

# A front face with every kind of same-file reference that the profile allows.
FRONT_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     viewBox="0 0 440 44" width="440mm" height="44mm">
  <!-- editor comment -->
  <defs>
    <linearGradient id="metal">
      <stop offset="0" stop-color="#555"/><stop offset="1" stop-color="#222"/>
    </linearGradient>
    <clipPath id="edge"><rect x="0" y="0" width="440" height="44"/></clipPath>
    <symbol id="led"><circle cx="2" cy="2" r="2" fill="#0f0"/></symbol>
  </defs>
  <rect id="chassis" x="0" y="0" width="440" height="44" fill="url(#metal)" clip-path="url(#edge)"/>
  <use xlink:href="#led" x="5" y="5"/>
  <g transform="translate(10 10)">
    <rect id="con-eth1" width="12" height="10" style="fill:url(#metal);stroke:#000"/>
  </g>
  <text x="30" y="20"><tspan>Port</tspan> <tspan>&lt;1&gt;</tspan></text>
  <rect id="con-r1" x="200" y="10" width="12" height="10" fill="#111"/>
  <rect id="bay-nm" x="300" y="5" width="100" height="30" fill="none" stroke="#999"/>
</svg>
"""
# Connectors in transformed groups: negative local coordinates, and a scale.
REAR_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44">
  <rect x="0" y="0" width="440" height="44" fill="#333"/>
  <g transform="translate(400 0)">
    <rect id="con-psu" x="-20" y="10" width="30" height="20" fill="#111"/>
  </g>
  <g transform="scale(0.25 0.5)">
    <rect id="con-f1" x="40" y="20" width="40" height="20" fill="#111"/>
  </g>
</svg>
"""
# A physical face is independent of the front-port and rear-port types.
MANIFEST = DEVICE_MANIFEST.replace(
    "bays:\n",
    """\
  con-r1:
    face: front
    type: lc
    bindings:
      - {component: rearport, name: R1}
  con-f1:
    face: rear
    type: lc
    bindings:
      - {component: frontport, name: F1}
bays:
""",
)
FILES = {
    "manifest.yaml": MANIFEST,
    "front.svg": FRONT_SVG,
    "rear.svg": REAR_SVG,
}


def faces(content):
    """Return the parsed artwork faces of a page, in page order."""
    return [
        etree.fromstring(markup)
        for markup in re.findall(
            r'<svg[^>]*class="dv-art-face".*?</svg>', content, re.DOTALL
        )
    ]


def ids(face):
    return [element.get("id") for element in face.iter() if element.get("id")]


def references(face):
    """Return every id that the face refers to through href or url(#id)."""
    found = []
    for element in face.iter():
        for name, value in element.attrib.items():
            if name == "href" and value.startswith("#"):
                found.append(value[1:])
            found.extend(re.findall(r"url\(#([^)]*)\)", value))
    return found


def links(face):
    """Return {connector id: link element} for each connector that a link wraps."""
    return {
        child.get("id"): link
        for link in face.iter(f"{{{SVG_NS}}}a")
        for child in link
        if child.get("id")
    }


class ArtworkTestCase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        self.media = Path(media)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)

        self.manufacturer = Manufacturer.objects.create(name="ACME", slug="acme")
        self.site = Site.objects.create(name="Site", slug="site")
        self.role = DeviceRole.objects.create(name="Role", slug="role")
        self.device_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-24", slug="sw-24"
        )
        self.device = self.make_device("sw1")
        self.eth1 = Interface.objects.create(
            device=self.device, name="eth1", type="1000base-t"
        )
        self.psu = PowerPort.objects.create(device=self.device, name="PSU1")
        # The same name as eth1 in another component type.
        self.console = ConsolePort.objects.create(device=self.device, name="eth1")
        self.rear_port = RearPort.objects.create(
            device=self.device, name="R1", type="lc"
        )
        self.front_port = FrontPort.objects.create(
            device=self.device, name="F1", type="lc"
        )

        admin = User.objects.create_user(username="admin", is_superuser=True)
        [imported] = import_bundle(
            admin, "sw24.zip", io.BytesIO(bundle(definition("ACME/SW-24", FILES)))
        )
        self.revision = imported.revision

        self.user = User.objects.create_user(username="viewer")
        for model in (
            Device,
            Interface,
            PowerPort,
            ConsolePort,
            FrontPort,
            RearPort,
            Cable,
        ):
            grant(self.user, model, "view")
        self.client.force_login(self.user)

    def make_device(self, name, device_type=None, **kwargs):
        return Device.objects.create(
            name=name,
            device_type=device_type or self.device_type,
            site=self.site,
            role=self.role,
            **kwargs,
        )

    def store_face(self, path, svg):
        """Store ``svg`` for ``path`` of the revision without the import validator."""
        self.revision.files.filter(path=path).update(
            blob=store_blob(svg.encode(), "svg")
        )

    def activate(self, device_type=None):
        return Association.objects.create(
            device_type=device_type or self.device_type, revision=self.revision
        )

    def tab(self, device=None, status=200):
        response = self.client.get(
            reverse(TAB_URL, kwargs={"pk": (device or self.device).pk})
        )
        self.assertEqual(response.status_code, status)
        return response.content.decode()


class ArtworkTabTests(ArtworkTestCase):
    def test_import_alone_changes_no_view(self):
        DeviceView.objects.create(device_type=self.device_type, grid_template_area=GRID)
        grant(self.user, DeviceView, "view")
        content = self.tab()
        self.assertEqual(faces(content), [])
        self.assertIn('class="deviceview area', content)

    def test_active_artwork_shows_the_front_and_the_rear_face_together(self):
        DeviceView.objects.create(device_type=self.device_type, grid_template_area=GRID)
        self.activate()
        content = self.tab()
        front, rear = faces(content)
        self.assertNotIn('class="deviceview area', content)
        self.assertEqual(
            re.findall(r'class="dv-art-face-name">(\w+)<', content), ["Front", "Rear"]
        )
        self.assertIn(f"dv-o-d{self.device.pk}-front-con-eth1", ids(front))
        self.assertIn(f"dv-o-d{self.device.pk}-rear-con-psu", ids(rear))
        # The faces stack at every width: each panel has a full-width column.
        self.assertEqual(
            re.findall(r'<div class="([^"]*)">\s*<div class="dv-art-panel">', content),
            ["col-12", "col-12"],
        )
        # Only a virtual chassis marks the current device.
        self.assertNotIn("active-device", content)

    def test_the_tab_shows_artwork_without_a_device_view(self):
        device_page = self.client.get(self.device.get_absolute_url()).content.decode()
        self.assertNotIn(reverse(TAB_URL, kwargs={"pk": self.device.pk}), device_page)
        self.activate()
        device_page = self.client.get(self.device.get_absolute_url()).content.decode()
        self.assertIn(reverse(TAB_URL, kwargs={"pk": self.device.pk}), device_page)
        self.assertEqual(len(faces(self.tab())), 2)

    def test_every_id_and_reference_gets_the_prefix_of_its_face(self):
        self.activate()
        for face, name in zip(faces(self.tab()), ("front", "rear"), strict=True):
            prefix = f"dv-o-d{self.device.pk}-{name}-"
            self.assertTrue(all(i.startswith(prefix) for i in ids(face)), ids(face))
            self.assertLessEqual(set(references(face)), set(ids(face)))
        front = faces(self.tab())[0]
        # Each focus ring refers to its filter.
        locals_ = ("edge", "led", "metal", "-focus-con-eth1", "-focus-con-r1")
        self.assertEqual(
            sorted(set(references(front))),
            sorted(f"dv-o-d{self.device.pk}-front-{local}" for local in locals_),
        )

    def test_a_face_with_a_prefixed_svg_namespace_renders_as_svg(self):
        prefixed = re.sub(r"<(/?)(\w)", r"<\1s:\2", FRONT_SVG).replace(
            'xmlns="http://www.w3.org/2000/svg"',
            'xmlns:s="http://www.w3.org/2000/svg"',
        )
        admin = User.objects.get(username="admin")
        [imported] = import_bundle(
            admin,
            "prefixed.zip",
            io.BytesIO(
                bundle(definition("ACME/SW-24", {**FILES, "front.svg": prefixed}))
            ),
        )
        Association.objects.create(
            device_type=self.device_type, revision=imported.revision
        )
        content = self.tab()
        markup = re.findall(
            r'<svg[^>]*class="dv-art-face".*?</svg>', content, re.DOTALL
        )
        # The HTML parser reads s:rect as an unknown element, not as an SVG rect.
        self.assertEqual(len(markup), 2)
        self.assertNotRegex(markup[0], r"<\w+:\w+[ >/]")
        self.assertIn(f'<rect id="dv-o-d{self.device.pk}-front-chassis"', markup[0])

    def test_title_and_desc_keep_only_their_text(self):
        # In HTML, title and desc are integration points: an element in them is parsed as HTML.
        # The import rejects this content now; an earlier import could store it.
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                "<defs>", '<desc>Front <g style="display:none"/>panel</desc>\n  <defs>'
            ),
        )
        self.activate()
        front = faces(self.tab())[0]
        [desc] = front.iter(f"{{{SVG_NS}}}desc")
        self.assertEqual((len(desc), desc.text), (0, "Front panel"))
        for title in front.iter(f"{{{SVG_NS}}}title"):
            self.assertEqual(len(title), 0)

    def test_a_connector_or_a_gradient_in_desc_is_not_drawn(self):
        # A browser draws no content of desc; the face renders without it.
        hidden = (
            FRONT_SVG.replace(
                '<rect id="con-eth1" width="12" height="10" style="fill:url(#metal);stroke:#000"/>',
                '<desc><rect id="con-eth1" width="12" height="10"/></desc>',
            )
            .replace(
                '<linearGradient id="metal">',
                '</defs><desc><linearGradient id="metal">',
            )
            .replace("</linearGradient>", "</linearGradient></desc><defs>")
        )
        self.store_face("front.svg", hidden)
        self.activate()
        front = faces(self.tab())[0]
        prefix = f"dv-o-d{self.device.pk}-front-"
        self.assertNotIn(f"{prefix}con-eth1", ids(front))
        self.assertNotIn(f"{prefix}con-eth1", links(front))
        self.assertIn(f"{prefix}con-r1", links(front))

    def test_the_face_is_serialized_from_the_parsed_tree(self):
        self.activate()
        content = self.tab()
        front = re.search(r'<svg[^>]*class="dv-art-face".*?</svg>', content, re.DOTALL)[
            0
        ]
        self.assertNotIn("editor comment", front)
        self.assertNotIn("xlink", front)
        # The text keeps its spaces and stays escaped.
        self.assertIn("<tspan>Port</tspan> <tspan>&lt;1&gt;</tspan>", front)
        root = faces(content)[0]
        self.assertEqual(root.get("viewBox"), "0 0 440 44")
        self.assertIsNone(root.get("width"))

    def test_a_connector_links_to_the_component_of_its_binding(self):
        self.activate()
        front, rear = faces(self.tab())
        prefix = f"dv-o-d{self.device.pk}"
        self.assertEqual(
            links(front)[f"{prefix}-front-con-eth1"].get("href"),
            self.eth1.get_absolute_url(),
        )
        self.assertEqual(
            links(rear)[f"{prefix}-rear-con-psu"].get("href"),
            self.psu.get_absolute_url(),
        )
        # The console port with the same name does not collide with the interface binding.
        self.assertNotIn(self.console.get_absolute_url(), self.tab())

    def test_a_title_in_the_connector_keeps_its_text_and_names_the_component(self):
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                'style="fill:url(#metal);stroke:#000"/>',
                'style="fill:url(#metal);stroke:#000"><title>Port 1</title></rect>',
            ),
        )
        self.activate()
        link = links(faces(self.tab())[0])[f"dv-o-d{self.device.pk}-front-con-eth1"]
        [own] = link[1].iter(f"{{{SVG_NS}}}title")
        self.assertEqual(
            link[0].text, "Interface eth1: enabled, no cable, path incomplete"
        )
        self.assertEqual(
            own.text, "Port 1 · Interface eth1: enabled, no cable, path incomplete"
        )

    def test_a_name_with_characters_that_xml_does_not_allow_renders(self):
        # NetBox accepts these characters in a component name; lxml refuses them.
        name = "eth\ufffe\uffff\x011"
        Interface.objects.filter(pk=self.eth1.pk).update(name=name)
        manifest = copy.deepcopy(self.revision.manifest)
        manifest["connectors"]["con-eth1"]["bindings"][0]["name"] = name
        type(self.revision).objects.filter(pk=self.revision.pk).update(
            manifest=manifest
        )
        self.activate()
        link = links(faces(self.tab())[0])[f"dv-o-d{self.device.pk}-front-con-eth1"]
        self.assertEqual(link.get("href"), self.eth1.get_absolute_url())
        self.assertEqual(
            link[0].text, "Interface eth1: enabled, no cable, path incomplete"
        )

    def test_a_connector_link_shows_a_focus_ring_around_the_connector(self):
        self.activate()
        front, rear = faces(self.tab())
        prefix = f"dv-o-d{self.device.pk}"
        filters = {
            element.get("id"): element
            for face in (front, rear)
            for element in face.iter(f"{{{SVG_NS}}}filter")
        }
        rings = {}
        for face in (front, rear):
            for ident, link in links(face).items():
                [ring] = link.findall(f"{{{SVG_NS}}}g[@class='dv-art-focus-ring']")
                connector = next(child for child in link if child.get("id") == ident)
                # The ring draws a copy of the connector.
                self.assertEqual(
                    (ring[0].tag, ring[0].get("width")),
                    (connector.tag, connector.get("width")),
                )
                self.assertEqual(ring.get("aria-hidden"), "true")
                rings[ident] = filters[ring.get("filter")[5:-1]]
        self.assertEqual(
            set(rings),
            {f"{prefix}-front-con-eth1", f"{prefix}-front-con-r1"}
            | {f"{prefix}-rear-con-psu", f"{prefix}-rear-con-f1"},
        )
        ring = rings[f"{prefix}-rear-con-psu"]
        # The ring is in the user space of con-psu, inside translate(400 0).
        self.assertEqual(
            [float(ring.get(name)) for name in ("x", "y", "width", "height")],
            [-400, 0, 440, 44],
        )
        self.assertEqual(
            ring.find(f"{{{SVG_NS}}}feFlood").get("class"), "dv-art-flood-focus"
        )
        # The ring draws only the outline; the link draws the connector.
        self.assertNotIn("SourceGraphic", etree.tostring(ring, encoding="unicode"))

    def test_the_focus_ring_draws_a_connector_without_paint(self):
        # WebKit paints no outline on an SVG element, so the ring must not depend on the paint.
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-r1" x="200" y="10" width="12" height="10" fill="#111"/>',
                '<g id="con-r1" opacity="0" style="visibility:hidden;font-size:4px">'
                '<title>R1</title><rect id="con-r1-area" x="200" y="10" width="12"'
                ' height="10" fill="none" fill-opacity="0" stroke-opacity="0"'
                ' style="stroke:none;stroke-width:2;fill:url(#metal)"/></g>',
            ),
        )
        self.activate()
        prefix = f"dv-o-d{self.device.pk}-front-"
        link = links(faces(self.tab())[0])[f"{prefix}con-r1"]
        [ring] = link.findall(f"{{{SVG_NS}}}g[@class='dv-art-focus-ring']")
        self.assertEqual(
            dict(ring.attrib),
            {
                "class": "dv-art-focus-ring",
                "filter": f"url(#{prefix}-focus-con-r1)",
                "fill": "#000",
                "stroke": "#000",
                "fill-opacity": "1",
                "stroke-opacity": "1",
                "visibility": "visible",
                "aria-hidden": "true",
            },
        )
        # The copy keeps the shape and drops the paint, the ids and the titles.
        self.assertEqual(
            etree.tostring(ring[0], encoding="unicode"),
            f'<g xmlns="{SVG_NS}" style="font-size:4px">'
            '<rect x="200" y="10" width="12" height="10" style="stroke-width:2"/></g>',
        )

    def test_the_focus_ring_of_a_use_connector_refers_to_its_target_without_paint(self):
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-r1" x="200" y="10" width="12" height="10" fill="#111"/>',
                '<defs><rect id="hit" x="200" y="10" width="12" height="10"'
                ' fill="none" stroke="none"/></defs>'
                '<use id="con-r1" x="2" y="1mm" transform="scale(1)" font-size="4"'
                ' href="#hit"/>',
            ),
        )
        self.activate()
        page = faces(self.tab())[0]
        prefix = f"dv-o-d{self.device.pk}-front-"
        [ring] = links(page)[f"{prefix}con-r1"].findall(
            f"{{{SVG_NS}}}g[@class='dv-art-focus-ring']"
        )
        # A use draws with the paint of its target, so the copy keeps the use and refers to a target without paint.
        self.assertEqual(
            etree.tostring(ring[0], encoding="unicode"),
            f'<use xmlns="{SVG_NS}" x="2" y="1mm" transform="scale(1)" font-size="4"'
            f' href="#{prefix}-shape-hit"/>',
        )
        [target] = page.iterfind(f".//*[@id='{prefix}-shape-hit']")
        self.assertEqual(
            etree.tostring(target, encoding="unicode", with_tail=False),
            f'<rect xmlns="{SVG_NS}" x="200" y="10" width="12" height="10"'
            f' id="{prefix}-shape-hit"/>',
        )

    def test_the_focus_ring_paints_under_an_ancestor_without_paint(self):
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-r1" x="200" y="10" width="12" height="10" fill="#111"/>',
                '<g fill-opacity="0" stroke-opacity="0" visibility="hidden">'
                '<rect id="con-r1" x="200" y="10" width="12" height="10"'
                ' fill-opacity="1" visibility="visible"/></g>',
            ),
        )
        self.activate()
        prefix = f"dv-o-d{self.device.pk}-front-"
        [ring] = links(faces(self.tab())[0])[f"{prefix}con-r1"].findall(
            f"{{{SVG_NS}}}g[@class='dv-art-focus-ring']"
        )
        # The ring sets each inherited paint property, so no ancestor value hides it.
        self.assertEqual(
            {
                name: ring.get(name)
                for name in ("fill-opacity", "stroke-opacity", "visibility")
            },
            {"fill-opacity": "1", "stroke-opacity": "1", "visibility": "visible"},
        )

    def test_the_focus_ring_keeps_the_text_after_a_title(self):
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-r1" x="200" y="10" width="12" height="10" fill="#111"/>',
                '<text id="con-r1" x="200" y="20"><title>R1</title>PORT<desc>d</desc>'
                " 1</text>",
            ),
        )
        self.activate()
        prefix = f"dv-o-d{self.device.pk}-front-"
        [ring] = links(faces(self.tab())[0])[f"{prefix}con-r1"].findall(
            f"{{{SVG_NS}}}g[@class='dv-art-focus-ring']"
        )
        self.assertEqual(
            etree.tostring(ring[0], encoding="unicode"),
            f'<text xmlns="{SVG_NS}" x="200" y="20">PORT 1</text>',
        )

    def test_the_focus_ring_keeps_a_use_without_a_target(self):
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-r1" x="200" y="10" width="12" height="10" fill="#111"/>',
                '<g id="con-r1"><rect x="200" y="10" width="12" height="10"/><use/></g>',
            ),
        )
        self.activate()
        prefix = f"dv-o-d{self.device.pk}-front-"
        [ring] = links(faces(self.tab())[0])[f"{prefix}con-r1"].findall(
            f"{{{SVG_NS}}}g[@class='dv-art-focus-ring']"
        )
        self.assertEqual(
            etree.tostring(ring[0], encoding="unicode"),
            f'<g xmlns="{SVG_NS}"><rect x="200" y="10" width="12" height="10"/><use/></g>',
        )

    def test_each_face_scrolls_with_the_keyboard(self):
        self.activate()
        viewports = re.findall(r'<div class="dv-art-viewport"[^>]*>', self.tab())
        self.assertEqual(len(viewports), 2)
        for viewport, face in zip(viewports, ("Front", "Rear")):
            self.assertIn('tabindex="0"', viewport)
            self.assertIn(f'aria-label="Artwork of the {face} face"', viewport)

    def test_a_face_binds_front_and_rear_ports_of_either_type(self):
        self.activate()
        front, rear = faces(self.tab())
        prefix = f"dv-o-d{self.device.pk}"
        self.assertEqual(
            links(front)[f"{prefix}-front-con-r1"].get("href"),
            self.rear_port.get_absolute_url(),
        )
        self.assertEqual(
            links(rear)[f"{prefix}-rear-con-f1"].get("href"),
            self.front_port.get_absolute_url(),
        )

    def test_decorative_artwork_has_no_link(self):
        self.activate()
        front = faces(self.tab())[0]
        prefix = f"dv-o-d{self.device.pk}-front-"
        self.assertEqual(set(links(front)), {f"{prefix}con-eth1", f"{prefix}con-r1"})
        self.assertEqual(len(list(front.iter(f"{{{SVG_NS}}}a"))), 2)

    def test_a_connector_without_its_component_has_no_link(self):
        self.psu.delete()
        self.activate()
        rear = faces(self.tab())[1]
        self.assertNotIn(f"dv-o-d{self.device.pk}-rear-con-psu", links(rear))
        self.assertIn(f"dv-o-d{self.device.pk}-rear-con-psu", ids(rear))

    def test_a_component_that_the_user_cannot_view_is_a_missing_binding(self):
        ObjectPermission.objects.filter(name__startswith="dcim.powerport").delete()
        self.activate()
        rear = faces(self.tab())[1]
        self.assertNotIn(f"dv-o-d{self.device.pk}-rear-con-psu", links(rear))

    def test_a_module_component_does_not_bind_in_the_device_scope(self):
        self.eth1.delete()
        module = Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.create(device=self.device, name="Bay 1"),
            module_type=ModuleType.objects.create(
                manufacturer=self.manufacturer, model="NM-2X"
            ),
        )
        Interface.objects.create(
            device=self.device, module=module, name="eth1", type="1000base-t"
        )
        grant(self.user, Module, "view")
        self.activate()
        front = faces(self.tab())[0]
        self.assertNotIn(f"dv-o-d{self.device.pk}-front-con-eth1", links(front))


class CableOccupancyTests(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        peer = self.make_device(
            "server1",
            DeviceType.objects.create(
                manufacturer=self.manufacturer, model="Server", slug="server"
            ),
        )
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[
                Interface.objects.create(device=peer, name="eth0", type="1000base-t")
            ],
        ).save()
        self.activate()

    def link(self):
        front = faces(self.tab())[0]
        return front, links(front)[f"dv-o-d{self.device.pk}-front-con-eth1"]

    def test_a_cabled_connector_shows_its_occupancy(self):
        front, link = self.link()
        self.assertEqual(link.get("class"), "dv-art-connector dv-art-cable-connected")
        self.assertIn(link.get("filter")[5:-1], ids(front))
        self.assertIn("cable connected", link.find(f"{{{SVG_NS}}}title").text)

    def test_a_connector_without_a_cable_shows_no_occupancy(self):
        Cable.objects.all().delete()
        front, link = self.link()
        self.assertEqual(link.get("class"), "dv-art-connector")
        self.assertIsNone(link.get("filter"))
        rings = {
            ring.get("filter")
            for ring in front.iterfind(f".//{{{SVG_NS}}}g[@class='dv-art-focus-ring']")
        }
        for element in front.iter(f"{{{SVG_NS}}}filter"):
            self.assertIn(f"url(#{element.get('id')})", rings)

    def test_a_cable_that_the_user_cannot_view_is_no_occupancy(self):
        ObjectPermission.objects.filter(name__startswith="dcim.cable").delete()
        self.assertEqual(self.link()[1].get("class"), "dv-art-connector")

    def outline(self, face, name, key):
        """Return (x, y, width, height, x radius, y radius) of the outline filter of a connector."""
        link = links(face)[f"dv-o-d{self.device.pk}-{name}-{key}"]
        [found] = [
            element
            for element in face.iter(f"{{{SVG_NS}}}filter")
            if element.get("id") == link.get("filter")[5:-1]
        ]
        radius = found.find(f"{{{SVG_NS}}}feMorphology").get("radius").split()
        return (
            *(float(found.get(name)) for name in ("x", "y", "width", "height")),
            *map(float, radius),
        )

    def test_the_outline_of_a_connector_in_a_transformed_group_is_not_clipped(self):
        peer = Device.objects.get(name="server1")
        Cable(
            a_terminations=[self.psu],
            b_terminations=[PowerOutlet.objects.create(device=peer, name="out1")],
        ).save()
        Cable(
            a_terminations=[self.front_port],
            b_terminations=[
                Interface.objects.create(device=peer, name="eth9", type="1000base-t")
            ],
        ).save()
        front, rear = faces(self.tab())
        radius = self.outline(front, "front", "con-eth1")[4]
        # con-psu is at x -20..10 inside translate(400 0): the region is the face in local units.
        self.assertEqual(
            self.outline(rear, "rear", "con-psu"),
            (-400, 0, 440, 44, radius, radius),
        )
        # con-f1 is inside scale(0.25 0.5): each axis keeps the outline width of the face.
        *region, x_radius, y_radius = self.outline(rear, "rear", "con-f1")
        self.assertEqual(region, [0, 0, 1760, 88])
        self.assertAlmostEqual(x_radius, radius * 4, places=3)
        self.assertAlmostEqual(y_radius, radius * 2, places=3)


def wrapper(face, ident):
    """Return the element that wraps the connector ``ident``: its link, or its unresolved group."""
    [element] = [element for element in face.iter() if element.get("id") == ident]
    return element.getparent()


def mark_filter(face, wrapped):
    """Return the filter that marks a wrapped connector, or None."""
    reference = wrapped.get("filter")
    if reference is None:
        return None
    [found] = [
        element
        for element in face.iter(f"{{{SVG_NS}}}filter")
        if element.get("id") == reference[5:-1]
    ]
    return found


def shape(found):
    """Return the primitives of a mark filter without their colour."""
    return [
        (
            etree.QName(element).localname,
            {
                name: value
                for name, value in element.attrib.items()
                if name not in ("class", "flood-color")
            },
        )
        for element in found.iter()
    ]


class ConnectivityFactsTests(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        self.peer = self.make_device(
            "server1",
            DeviceType.objects.create(
                manufacturer=self.manufacturer, model="Server", slug="server"
            ),
        )
        self.peer_eth0 = Interface.objects.create(
            device=self.peer, name="eth0", type="1000base-t"
        )

    def cable(self, status="connected", peer=None):
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[peer or self.peer_eth0],
            status=status,
        ).save()

    def connector(self, key="con-eth1", face=0):
        """Return (class, filter, title) of a connector on the page."""
        page = faces(self.tab())[face]
        wrapped = wrapper(page, f"dv-o-d{self.device.pk}-{page_face(face)}-{key}")
        return (
            wrapped.get("class"),
            mark_filter(page, wrapped),
            wrapped.find(f"{{{SVG_NS}}}title").text,
        )

    def test_a_cabled_disabled_interface_shows_the_cable_and_the_disabled_state(self):
        self.activate()
        Interface.objects.filter(pk=self.eth1.pk).update(enabled=False)
        self.cable()
        classes, found, title = self.connector()
        self.assertEqual(
            classes, "dv-art-connector dv-art-cable-connected dv-art-disabled"
        )
        self.assertEqual(
            title,
            "Interface eth1: disabled, cable connected, path complete, "
            "peer server1 | eth0",
        )
        operators = [
            element.get("operator")
            for element in found.iter(f"{{{SVG_NS}}}feMorphology")
        ]
        # The cable ring grows outside the connector; the disabled ring is inside it.
        self.assertIn("dilate", operators)
        self.assertIn("erode", operators)

    def test_a_planned_cable_and_a_marked_connected_port_are_their_own_facts(self):
        self.activate()
        self.cable("planned")
        PowerPort.objects.filter(pk=self.psu.pk).update(mark_connected=True)
        classes, _, title = self.connector()
        self.assertEqual(classes, "dv-art-connector dv-art-cable-planned")
        self.assertIn("cable planned", title)
        classes, found, title = self.connector("con-psu", face=1)
        self.assertEqual(classes, "dv-art-connector dv-art-marked")
        self.assertEqual(
            title, "Power port PSU1: no cable, marked connected, path incomplete"
        )
        self.assertIsNotNone(found)

    def test_a_cable_status_that_netbox_configuration_adds_shows_a_cable(self):
        # NetBox can add cable statuses (FIELD_CHOICES).
        self.activate()
        self.cable()
        Cable.objects.update(status="inspection")
        classes, found, title = self.connector()
        self.assertEqual(classes, "dv-art-connector dv-art-cable-other")
        self.assertIsNotNone(found)
        self.assertIn("cable inspection", title)

    def test_path_completeness_is_separate_from_the_cable(self):
        self.activate()
        self.cable(
            peer=FrontPort.objects.create(device=self.peer, name="F1", type="lc")
        )
        classes, _, title = self.connector()
        self.assertEqual(classes, "dv-art-connector dv-art-cable-connected")
        self.assertIn("cable connected, path incomplete", title)

    def test_each_mark_has_its_own_pattern_not_only_its_colour(self):
        self.activate()
        cases = {
            "cable connected": lambda: self.cable("connected"),
            "cable planned": lambda: self.cable("planned"),
            "cable decommissioning": lambda: self.cable("decommissioning"),
            "cable inspection": lambda: self.cable("inspection"),
            "marked connected": lambda: Interface.objects.filter(
                pk=self.eth1.pk
            ).update(mark_connected=True),
            "disabled": lambda: Interface.objects.filter(pk=self.eth1.pk).update(
                enabled=False
            ),
        }
        shapes = {}
        for name, setup in cases.items():
            Cable.objects.all().delete()
            Interface.objects.filter(pk=self.eth1.pk).update(
                enabled=True, mark_connected=False
            )
            self.eth1.refresh_from_db()
            setup()
            _, found, title = self.connector()
            self.assertIn(name, title)
            shapes[name] = repr(shape(found))
        # The filter id and region are per connector, so the unresolved case uses the same connector.
        Cable.objects.all().delete()
        self.eth1.delete()
        _, found, _ = self.connector()
        shapes["unresolved"] = repr(shape(found))
        self.assertEqual(len(set(shapes.values())), len(shapes), shapes)

    def test_a_missing_binding_does_not_look_like_a_port_without_a_cable(self):
        self.activate()
        classes, found, title = self.connector("con-psu", face=1)
        self.assertEqual(
            (classes, found, title),
            ("dv-art-connector", None, "Power port PSU1: no cable, path incomplete"),
        )
        self.psu.delete()
        page = faces(self.tab())[1]
        wrapped = wrapper(page, f"dv-o-d{self.device.pk}-rear-con-psu")
        self.assertEqual(etree.QName(wrapped).localname, "g")
        self.assertEqual(wrapped.get("class"), "dv-art-unresolved")
        self.assertIsNotNone(mark_filter(page, wrapped))
        self.assertEqual(
            wrapped.find(f"{{{SVG_NS}}}title").text, "No component: Power port PSU1"
        )

    def test_two_found_bindings_are_unresolved(self):
        files = {
            **FILES,
            "manifest.yaml": MANIFEST.replace(
                "      - {component: interface, name: eth1}\n",
                "      - {component: interface, name: eth1}\n"
                "      - {component: consoleport, name: eth1}\n",
            ),
        }
        admin = User.objects.get(username="admin")
        [imported] = import_bundle(
            admin, "two.zip", io.BytesIO(bundle(definition("ACME/SW-24", files)))
        )
        Association.objects.create(
            device_type=self.device_type, revision=imported.revision
        )
        classes, found, title = self.connector()
        self.assertEqual(classes, "dv-art-unresolved")
        self.assertIsNotNone(found)
        self.assertEqual(
            title, "Two or more components: Interface eth1, Console port eth1"
        )

    def test_the_page_has_a_legend_of_every_mark(self):
        self.activate()
        content = self.tab()
        legend = content.split('class="dv-art-legend ', 1)[1].split("</ul>", 1)[0]
        for label in (
            "Cable connected",
            "Cable planned",
            "Cable decommissioning",
            "Cable with another status",
            "Marked connected",
            "Disabled",
            "No component found",
        ):
            self.assertIn(label, legend)
        self.assertEqual(legend.count("<svg"), 7)


def page_face(index):
    return ("front", "rear")[index]


class DamagedArtworkTests(ArtworkTestCase):
    def test_a_damaged_blob_shows_the_part_as_damaged(self):
        self.activate()
        blob = self.revision.files.get(path="front.svg").blob
        (self.media / blob.name).write_text(FRONT_SVG.replace("#555", "#666"))
        with self.assertLogs("netbox_device_view.artwork.render", "WARNING"):
            content = self.tab()
        self.assertIn("Artwork damaged", content)
        self.assertEqual(faces(content), [])

    def test_a_missing_blob_shows_the_part_as_damaged(self):
        self.activate()
        (self.media / self.revision.files.get(path="rear.svg").blob.name).unlink()
        with self.assertLogs("netbox_device_view.artwork.render", "WARNING"):
            self.assertIn("Artwork damaged", self.tab())


class LocalAssetTests(ArtworkTestCase):
    def test_the_artwork_refers_only_to_the_page_and_to_netbox(self):
        self.activate()
        for face in faces(self.tab()):
            for element in face.iter():
                self.assertNotIn(etree.QName(element).localname, ("image", "script"))
                for name, value in element.attrib.items():
                    if name == "href":
                        self.assertRegex(value, r"^(#|/)")
                    self.assertNotRegex(value, r"url\((?!#)")


class VirtualChassisArtworkTests(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        chassis = VirtualChassis.objects.create(name="stack")
        Device.objects.filter(pk=self.device.pk).update(
            virtual_chassis=chassis, vc_position=1
        )
        self.device.refresh_from_db()
        self.member = self.make_device("sw2", virtual_chassis=chassis, vc_position=2)
        Interface.objects.create(device=self.member, name="eth1", type="1000base-t")

    def test_two_devices_of_one_type_share_no_id_and_no_reference(self):
        self.activate()
        content = self.tab()
        page_faces = faces(content)
        self.assertEqual(len(page_faces), 4)
        all_ids = [i for face in page_faces for i in ids(face)]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        for face in page_faces:
            self.assertLessEqual(set(references(face)), set(ids(face)))
        hrefs = {
            link.get("href")
            for face in page_faces
            for link in face.iter(f"{{{SVG_NS}}}a")
        }
        member_eth1 = Interface.objects.get(device=self.member, name="eth1")
        self.assertTrue(
            {self.eth1.get_absolute_url(), member_eth1.get_absolute_url()} <= hrefs
        )
        self.assertEqual(content.count('class="dv-art-part active-device"'), 1)

    def test_a_member_without_artwork_keeps_the_layout_view(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )
        Device.objects.filter(pk=self.member.pk).update(device_type=other)
        for device_type in (self.device_type, other):
            DeviceView.objects.create(device_type=device_type, grid_template_area=GRID)
        grant(self.user, DeviceView, "view")
        self.activate()
        content = self.tab()
        self.assertEqual(faces(content), [])
        self.assertIn('class="deviceview area', content)


class DevicePagePanelTests(ArtworkTestCase):
    def test_the_device_page_panel_shows_the_artwork(self):
        self.activate()
        config = copy.deepcopy(settings.PLUGINS_CONFIG)
        config["netbox_device_view"]["show_on_device_tab"] = True
        with override_settings(PLUGINS_CONFIG=config):
            content = self.client.get(self.device.get_absolute_url()).content.decode()
        self.assertEqual(len(faces(content)), 2)
        self.assertIn("device_view_artwork.js", content)


def apply(matrix, x, y):
    a, b, c, d, e, f = matrix
    return (round(a * x + c * y + e, 6), round(b * x + d * y + f, 6))


@pytest.mark.parametrize(
    ("transform", "point", "expected"),
    [
        ("translate(400 0)", (-20, 10), (380, 10)),
        ("scale(0.25 0.5)", (40, 20), (10, 10)),
        ("rotate(90 10 0)", (10, 0), (10, 0)),
        ("rotate(90)", (1, 0), (0, 1)),
        ("skewX(45)", (0, 2), (2, 2)),
        ("skewY(45)", (2, 0), (2, 2)),
        ("matrix(1 2 3 4 5 6)", (1, 1), (9, 12)),
        # The functions of a list apply right to left.
        ("translate(10 0) scale(2)", (1, 1), (12, 2)),
        ("scale(2) translate(10 0)", (1, 1), (22, 2)),
    ],
)
def test_transform_matrix_maps_a_point_as_svg_does(transform, point, expected):
    assert apply(transform_matrix(transform), *point) == expected
