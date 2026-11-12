"""The Device View tab, the device page panel and the rendered-layout API resolve components the same way."""

from html.parser import HTMLParser

from dcim.models import (
    Cable,
    ConsolePort,
    Device,
    DeviceRole,
    DeviceType,
    FrontPort,
    Interface,
    InterfaceTemplate,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleType,
    PowerPort,
    PowerPortTemplate,
    RearPort,
    Site,
    VirtualChassis,
)
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view.components import BINDABLE_MODELS, device_components, find
from netbox_device_view.models import DeviceView, RenderMode

from .permissions import grant

SINGLE_VIEW_PATCH_PANEL = """
version: 1
views:
  front:
    elements: [{key: port-1}, {key: port-2}]
"""
SWITCH = """
version: 1
views:
  front:
    elements: [{key: gi-1}, {key: gi-2}, {key: gi-3}, {key: console}, {key: port-1}]
"""
ONE_ROW_SWITCH = """
version: 1
views:
  front:
    elements:
      - {key: gi-1, at: {row: 1, col: 1}}
      - {key: gi-2, at: {row: 1, col: 2}}
      - {key: gi-3, at: {row: 1, col: 3}}
      - {key: console, at: {row: 1, col: 4}}
"""
PATCH_PANEL = """
version: 1
views:
  front:
    elements: [{key: port-1}, {key: port-2}]
  rear:
    copy_from: front
"""


class _PortCells(HTMLParser):
    """Collect the attributes of each SVG port group and CSS grid port of a page."""

    def __init__(self):
        super().__init__()
        self.cells = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if (tag == "g" and "dv-port" in classes) or (
            tag == "a" and "device-view-port" in classes
        ):
            self.cells.append(attrs)
        elif tag == "rect" and "dv-port-rect" in classes and self.cells:
            self.cells[-1]["rect-fill"] = attrs["fill"]


def page_cells(content):
    parser = _PortCells()
    parser.feed(content)
    return parser.cells


class ResolverTestBase(TestCase):
    layout = SWITCH

    @classmethod
    def setUpTestData(cls):
        cls.manufacturer = Manufacturer.objects.create(name="Mfr", slug="mfr")
        cls.site = Site.objects.create(name="Site", slug="site")
        cls.role = DeviceRole.objects.create(name="Role", slug="role")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Switch", slug="switch"
        )
        DeviceView.objects.create(
            device_type=cls.device_type,
            grid_template_area="",
            yaml_layout=cls.layout,
            render_mode=RenderMode.SVG,
        )
        cls.device = cls._device("sw1")
        cls.user = User.objects.create_superuser(username="admin")

    @classmethod
    def _device(cls, name, **kwargs):
        return Device.objects.create(
            name=name,
            device_type=cls.device_type,
            site=cls.site,
            role=cls.role,
            **kwargs,
        )

    def _api(self, device=None):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        response = client.get(
            reverse(
                "plugins-api:netbox_device_view-api:device_rendered_layout",
                kwargs={"pk": (device or self.device).pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _tab(self, device=None, **params):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse(
                "dcim:device_deviceview", kwargs={"pk": (device or self.device).pk}
            ),
            params,
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _device_page(self, device=None):
        plugins_config = {
            **settings.PLUGINS_CONFIG,
            "netbox_device_view": {
                **settings.PLUGINS_CONFIG["netbox_device_view"],
                "show_on_device_tab": True,
            },
        }
        self.client.force_login(self.user)
        with override_settings(PLUGINS_CONFIG=plugins_config):
            response = self.client.get((device or self.device).get_absolute_url())
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _pages(self, device=None):
        return {
            "tab": self._tab(device),
            "device page panel": self._device_page(device),
        }

    @staticmethod
    def _hotspots(body):
        return [hotspot for panel in body["panels"] for hotspot in panel["hotspots"]]


class PageMatchesApiTests(ResolverTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        gi1, gi2, gi3 = (
            Interface.objects.create(device=cls.device, name=name, type="1000base-t")
            for name in ("Gi1", "Gi2", "Gi3")
        )
        gi3.enabled = False
        gi3.save()
        Cable(a_terminations=[gi1], b_terminations=[gi2], color="ff0000").save()

    def test_page_and_api_bind_the_same_components_in_the_same_state(self):
        api = {
            Interface.objects.get(pk=hotspot["object_id"]).get_absolute_url(): hotspot[
                "connection_state"
            ]
            for hotspot in self._hotspots(self._api())
        }
        self.assertEqual(len(api), 3)
        for page, content in self._pages().items():
            with self.subTest(page):
                self.assertEqual(
                    {
                        cell["data-href"]: cell["data-connection-state"]
                        for cell in page_cells(content)
                        if "data-href" in cell
                    },
                    api,
                )

    def test_page_and_api_show_the_same_peers_and_cable_colour(self):
        hotspots = self._hotspots(self._api())
        self.assertEqual(
            {h["name"]: h["peer_labels"] for h in hotspots},
            {"Gi1": ["sw1 | Gi2"], "Gi2": ["sw1 | Gi1"], "Gi3": []},
        )
        cells = {
            cell["data-href"]: cell
            for cell in page_cells(self._tab(cable_colors="on"))
            if "data-href" in cell
        }
        for hotspot in hotspots:
            with self.subTest(hotspot["name"]):
                cell = cells[
                    Interface.objects.get(pk=hotspot["object_id"]).get_absolute_url()
                ]
                self.assertEqual(
                    cell["data-bs-title"],
                    (
                        "<hr>".join(
                            [hotspot["name"], "<br>".join(hotspot["peer_labels"])]
                        )
                        if hotspot["peer_labels"]
                        else hotspot["name"]
                    ),
                )
                if hotspot["cable_color"]:
                    self.assertEqual(cell["rect-fill"], f"#{hotspot['cable_color']}")

    def test_a_malformed_cable_colour_is_not_drawn(self):
        Cable.objects.update(color="zzzzzz")
        body = self._api()
        self.assertNotIn("#zzzzzz", body["panels"][0]["svg"])
        self.assertNotIn("#zzzzzz", self._tab(cable_colors="on"))

    def test_page_sets_the_state_without_javascript(self):
        content = self._tab()
        self.assertNotIn("applyPortStatus", content)
        cells = {cell["data-stylename"]: cell for cell in page_cells(content)}
        self.assertIn("bg-success", cells["gi-1"]["class"].split())
        self.assertIn("bg-danger", cells["gi-3"]["class"].split())

    def test_page_sets_the_cable_colour_without_javascript(self):
        content = self._tab(cable_colors="on")
        self.assertIn('fill="#ff0000"', content)
        self.assertNotIn("cableColor", content)

    def test_cable_colour_mode_stripes_only_a_cable_without_a_colour(self):
        Cable.objects.update(color="")
        cells = {
            cell["data-stylename"]: cell["class"].split()
            for cell in page_cells(self._tab(cable_colors="on"))
        }
        self.assertIn("nocolor", cells["gi-1"])
        self.assertNotIn("nocolor", cells["gi-3"])
        self.assertIn("bg-danger", cells["gi-3"])


class AmbiguousKeyTests(ResolverTestBase):
    layout = ONE_ROW_SWITCH

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.interface = Interface.objects.create(
            device=cls.device, name="Console", type="1000base-t"
        )
        cls.console_port = ConsolePort.objects.create(device=cls.device, name="Console")
        gi1, gi2, gi3 = (
            Interface.objects.create(device=cls.device, name=name, type="1000base-t")
            for name in ("Gi1", "Gi2", "Gi3")
        )
        gi3.enabled = False
        gi3.save()
        Cable(a_terminations=[gi1], b_terminations=[gi2]).save()

    def test_api_lists_the_key_as_ambiguous_with_both_candidates(self):
        (panel,) = self._api()["panels"]
        self.assertEqual(
            [hotspot["name"] for hotspot in panel["hotspots"]], ["Gi1", "Gi2", "Gi3"]
        )
        (entry,) = panel["ambiguous"]
        self.assertEqual(entry["name"], "console")
        # The console element is the next cell after Gi3 in the same row.
        gi2, gi3 = panel["hotspots"][1:]
        self.assertEqual(
            (entry["x"], entry["y"], entry["width"], entry["height"]),
            (2 * gi3["x"] - gi2["x"], gi3["y"], gi3["width"], gi3["height"]),
        )
        self.assertEqual(
            entry["candidates"],
            [
                {
                    "object_type": "dcim.interface",
                    "object_id": self.interface.pk,
                    "name": "Console",
                },
                {
                    "object_type": "dcim.consoleport",
                    "object_id": self.console_port.pk,
                    "name": "Console",
                },
            ],
        )
        self.assertEqual(
            set(entry), {"name", "x", "y", "width", "height", "candidates"}
        )

    def test_api_hotspots_keep_the_schema_1_types(self):
        hotspots = self._hotspots(self._api())
        self.assertEqual(
            {hotspot["connection_state"] for hotspot in hotspots},
            {"connected", "disabled"},
        )
        for hotspot in hotspots:
            with self.subTest(hotspot["name"]):
                self.assertIn(
                    hotspot["object_type"],
                    {
                        "dcim.interface",
                        "dcim.consoleport",
                        "dcim.consoleserverport",
                        "dcim.frontport",
                        "dcim.rearport",
                    },
                )
                self.assertIs(type(hotspot["object_id"]), int)
                self.assertIs(type(hotspot["enabled"]), bool)
                self.assertIn(
                    hotspot["connection_state"],
                    {"connected", "partially_connected", "enabled", "disabled"},
                )
                self.assertIsInstance(hotspot["cable_color"], (str, type(None)))
                self.assertNotIn("candidates", hotspot)

    def test_page_shows_one_neutral_cell_that_lists_both_components(self):
        for page, content in self._pages().items():
            with self.subTest(page):
                (cell,) = (
                    cell
                    for cell in page_cells(content)
                    if cell["data-stylename"] == "console"
                )
                self.assertEqual(cell["data-connection-state"], "ambiguous")
                self.assertNotIn("data-href", cell)
                self.assertIn(
                    self.interface.get_absolute_url(), cell["data-bs-content"]
                )
                self.assertIn(
                    self.console_port.get_absolute_url(), cell["data-bs-content"]
                )
                self.assertIn("</a> (interface)<br><a", cell["data-bs-content"])


class PatchPanelFaceTests(ResolverTestBase):
    layout = PATCH_PANEL

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for name in ("Port 1", "Port 2"):
            RearPort.objects.create(device=cls.device, name=name, type="8p8c")
            FrontPort.objects.create(device=cls.device, name=name, type="8p8c")

    def test_each_face_binds_the_ports_of_that_face(self):
        panels = {panel["key"]: panel for panel in self._api()["panels"]}
        for face, object_type in (
            ("front", "dcim.frontport"),
            ("rear", "dcim.rearport"),
        ):
            with self.subTest(face):
                self.assertEqual(
                    {hotspot["object_type"] for hotspot in panels[face]["hotspots"]},
                    {object_type},
                )

    def test_page_binds_the_ports_of_each_face(self):
        for page, content in self._pages().items():
            with self.subTest(page):
                hrefs = {cell.get("data-href") for cell in page_cells(content)}
                self.assertEqual(
                    hrefs,
                    {
                        port.get_absolute_url()
                        for model in (FrontPort, RearPort)
                        for port in model.objects.filter(device=self.device)
                    },
                )


class SingleViewPatchPanelTests(PatchPanelFaceTests):
    layout = SINGLE_VIEW_PATCH_PANEL

    def test_a_single_front_view_binds_the_front_ports(self):
        hotspots = self._hotspots(self._api())
        self.assertEqual(
            {(h["object_type"], h["name"]) for h in hotspots},
            {("dcim.frontport", "Port 1"), ("dcim.frontport", "Port 2")},
        )
        for page, content in self._pages().items():
            with self.subTest(page):
                self.assertEqual(
                    {cell.get("data-href") for cell in page_cells(content)},
                    {
                        port.get_absolute_url()
                        for port in FrontPort.objects.filter(device=self.device)
                    },
                )

    test_each_face_binds_the_ports_of_that_face = None
    test_page_binds_the_ports_of_each_face = None


class VirtualChassisTests(ResolverTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        chassis = VirtualChassis.objects.create(name="vc1")
        Device.objects.filter(pk=cls.device.pk).update(
            virtual_chassis=chassis, vc_position=1
        )
        cls.device.refresh_from_db()
        cls.member = cls._device("sw2", virtual_chassis=chassis, vc_position=2)
        for member in (cls.device, cls.member):
            Interface.objects.create(device=member, name="Gi1", type="1000base-t")
            FrontPort.objects.create(device=member, name="Port 1", type="8p8c")

    def test_page_and_api_bind_the_front_ports_of_every_member(self):
        api = {
            (hotspot["object_type"], hotspot["object_id"])
            for hotspot in self._hotspots(self._api())
        }
        self.assertEqual(len(api), 4)
        expected = {
            component.get_absolute_url()
            for model in (Interface, FrontPort)
            for component in model.objects.all()
        }
        for page, content in self._pages().items():
            with self.subTest(page):
                self.assertEqual(
                    {cell.get("data-href") for cell in page_cells(content)} - {None},
                    expected,
                )


class TypedLookupTests(ResolverTestBase):
    """find() resolves a binding by component type, exact name and device or module scope."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        module_type = ModuleType.objects.create(
            manufacturer=cls.manufacturer, model="NM-8X"
        )
        InterfaceTemplate.objects.create(
            module_type=module_type, name="Te1/{module}/1", type="10gbase-x-sfpp"
        )
        PowerPortTemplate.objects.create(module_type=module_type, name="PS")
        cls.module = Module.objects.create(
            device=cls.device,
            module_bay=ModuleBay.objects.create(
                device=cls.device, name="Network Module", position="1"
            ),
            module_type=module_type,
        )
        cls.gi1 = Interface.objects.create(
            device=cls.device, name="Gi1", type="1000base-t"
        )
        cls.console = ConsolePort.objects.create(device=cls.device, name="Gi1")

    def _states(self):
        return device_components(self.device, self.user, models=BINDABLE_MODELS)

    def _found(self, model, name, module=None):
        return [state.component for state in find(self._states(), model, name, module)]

    def test_a_device_binding_finds_the_component_of_that_type(self):
        self.assertEqual(self._found(Interface, "Gi1"), [self.gi1])
        self.assertEqual(self._found(ConsolePort, "Gi1"), [self.console])

    def test_a_device_binding_does_not_find_module_components(self):
        self.assertEqual(self._found(Interface, "Te1/1/1"), [])

    def test_a_module_binding_resolves_the_module_token(self):
        self.assertEqual(
            self._found(Interface, "Te1/{module}/1", self.module),
            [Interface.objects.get(module=self.module)],
        )
        self.assertEqual(
            self._found(PowerPort, "PS", self.module),
            [PowerPort.objects.get(module=self.module)],
        )

    def test_a_module_binding_does_not_find_device_components(self):
        self.assertEqual(self._found(Interface, "Gi1", self.module), [])

    def test_a_name_with_more_module_tokens_than_bay_levels_finds_nothing(self):
        # NetBox raises ValueError when the token count does not fit the depth.
        self.assertEqual(self._found(Interface, "Te{module}/{module}", self.module), [])

    def test_a_missing_component_is_not_found(self):
        self.assertEqual(self._found(Interface, "Gi2"), [])


class HiddenObjectTests(ResolverTestBase):
    """A hidden cable, module or peer renders as an absent one."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        gi1, gi2 = (
            Interface.objects.create(device=cls.device, name=name, type="1000base-t")
            for name in ("Gi1", "Gi2")
        )
        Cable(a_terminations=[gi1], b_terminations=[gi2], color="ff0000").save()
        module = Module.objects.create(
            device=cls.device,
            module_bay=ModuleBay.objects.create(device=cls.device, name="NIM 1"),
            module_type=ModuleType.objects.create(
                manufacturer=cls.manufacturer, model="NIM"
            ),
        )
        cls.module_interface = Interface.objects.create(
            device=cls.device, module=module, name="Gi3", type="1000base-t"
        )
        cls.near = cls._device("sw2")
        far = cls._device("far")
        cls.far_cable = Cable(
            a_terminations=[
                Interface.objects.create(device=cls.near, name="Gi1", type="1000base-t")
            ],
            b_terminations=[
                Interface.objects.create(device=far, name="Gi1", type="1000base-t")
            ],
        )
        cls.far_cable.save()
        cls.user = User.objects.create_user(username="viewer")
        grant(cls.user, Device, "view")
        grant(
            cls.user,
            Interface,
            "view",
            constraints={"device_id__in": [cls.device.pk, cls.near.pk]},
        )
        grant(cls.user, Cable, "view", constraints={"id": cls.far_cable.pk})
        grant(cls.user, DeviceView, "view")

    def test_a_hidden_cable_renders_as_no_cable(self):
        self.assertEqual(
            {h["name"]: h["connection_state"] for h in self._hotspots(self._api())},
            {"Gi1": "enabled", "Gi2": "enabled"},
        )
        for page, content in self._pages().items():
            with self.subTest(page):
                self.assertEqual(
                    {
                        cell["data-stylename"]: cell["data-connection-state"]
                        for cell in page_cells(content)
                        if "data-href" in cell
                    },
                    {"gi-1": "enabled", "gi-2": "enabled"},
                )

    def test_a_hidden_far_end_does_not_connect_the_interface(self):
        (hotspot,) = self._hotspots(self._api(self.near))
        self.assertEqual(
            (hotspot["connection_state"], hotspot["peer_labels"]), ("enabled", [])
        )
        for page, content in self._pages(self.near).items():
            with self.subTest(page):
                self.assertEqual(
                    [
                        cell["data-connection-state"]
                        for cell in page_cells(content)
                        if "data-href" in cell
                    ],
                    ["enabled"],
                )

    def test_a_component_of_a_hidden_module_is_absent(self):
        self.assertNotIn(
            self.module_interface.pk,
            {h["object_id"] for h in self._hotspots(self._api())},
        )
        for page, content in self._pages().items():
            with self.subTest(page):
                self.assertNotIn(
                    self.module_interface.get_absolute_url(),
                    {cell.get("data-href") for cell in page_cells(content)},
                )


class PopoverEscapingTests(ResolverTestBase):
    """A component name is popover text, not markup, in both render modes."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Interface.objects.create(
            device=cls.device, name="<b>Gi1</b>", type="1000base-t"
        )

    def test_the_popover_title_escapes_the_name(self):
        for render_mode in (RenderMode.CSS, RenderMode.SVG):
            with self.subTest(render_mode):
                DeviceView.objects.update(
                    render_mode=render_mode,
                    yaml_layout="version: 1\nviews:\n  front:\n    elements: [{key: -b-gi1--b-}]\n",
                )
                (cell,) = (
                    cell
                    for cell in page_cells(self._tab())
                    if "data-href" in cell or cell.get("href")
                )
                self.assertEqual(cell["data-bs-title"], "&lt;b&gt;Gi1&lt;/b&gt;")
