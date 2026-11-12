"""Each connector and layout key has details: its bindings, with the objects that the user can view."""

import copy
import html
import re
from pathlib import Path

import lxml.html
from dcim.models import (
    Cable,
    ConsolePort,
    Device,
    DeviceType,
    FrontPort,
    Interface,
    Module,
    ModuleBay,
    ModuleType,
    PowerPort,
    RearPort,
)
from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import override_settings
from django.urls import reverse
from users.models import ObjectPermission

from netbox_device_view.components import BINDABLE_MODELS
from netbox_device_view.details import details_id
from netbox_device_view.models import DeviceView, RenderMode

from .permissions import grant
from .test_artwork_device_view import (
    ArtworkTestCase,
    activate_second_binding,
    faces,
    wrapper,
)
from .test_component_resolver import ResolverTestBase, page_cells


def details(content, ident):
    """Return (title, fragment) of the details template ``ident`` of a page."""
    match = re.search(
        rf'<template id="{re.escape(ident)}" data-title="([^"]*)">(.*?)</template>',
        content,
        re.DOTALL,
    )
    assert match, f"no details template {ident}"
    return html.unescape(match[1]), lxml.html.fragment_fromstring(
        match[2], create_parent="div"
    )


def links(fragment):
    return [
        (" ".join(a.text_content().split()), a.get("href")) for a in fragment.iter("a")
    ]


def facts(fragment):
    return {
        dt.text_content().strip(): " ".join(dd.text_content().split())
        for dt, dd in zip(fragment.iter("dt"), fragment.iter("dd"), strict=True)
    }


def text(fragment):
    return " ".join(fragment.text_content().split())


class ArtworkDetailsTests(ArtworkTestCase):
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

    def connector(self, key="con-eth1", face=0):
        """Return (wrapper, details title, details fragment) of a connector."""
        content = self.tab()
        name = ("front", "rear")[face]
        wrapped = wrapper(faces(content)[face], f"dv-o-d{self.device.pk}-{name}-{key}")
        return wrapped, *details(content, wrapped.get("data-dv-details"))

    def test_a_linked_connector_has_details_with_each_object_and_fact(self):
        self.activate()
        cable = Cable(a_terminations=[self.eth1], b_terminations=[self.peer_eth0])
        cable.save()
        wrapped, title, fragment = self.connector()
        url = self.eth1.get_absolute_url()
        self.assertEqual(wrapped.get("href"), url)
        self.assertEqual(title, "Connector 1")
        self.assertEqual(
            links(fragment),
            [
                ("Interface eth1", url),
                (str(cable), cable.get_absolute_url()),
                ("server1 | eth0", self.peer_eth0.get_absolute_url()),
                ("Trace", f"{url}trace/"),
            ],
        )
        self.assertEqual(
            facts(fragment),
            {
                "Enabled": "Yes",
                "Cable": f"{cable} (Connected)",
                "Path": "Complete",
                "Peers": "server1 | eth0",
            },
        )
        self.assertEqual(self.client.get(f"{url}trace/").status_code, 200)

    def test_every_bindable_component_type_has_a_trace_page(self):
        for model in BINDABLE_MODELS:
            with self.subTest(model._meta.model_name):
                reverse(f"dcim:{model._meta.model_name}_trace", kwargs={"pk": 1})

    def test_the_details_leave_out_a_cable_and_a_peer_that_the_user_cannot_view(self):
        self.activate()
        Cable(a_terminations=[self.eth1], b_terminations=[self.peer_eth0]).save()
        ObjectPermission.objects.all().delete()
        grant(self.user, Device, "view", constraints={"name": "sw1"})
        grant(self.user, Interface, "view", constraints={"device__name": "sw1"})
        for model in (PowerPort, ConsolePort, FrontPort, RearPort):
            grant(self.user, model, "view")
        _, _, fragment = self.connector()
        self.assertEqual(facts(fragment)["Cable"], "No cable")
        self.assertNotIn("Peers", facts(fragment))
        self.assertNotIn("server1", text(fragment))
        self.assertEqual([label for label, _ in links(fragment)], ["Interface eth1"])

    def test_the_trace_link_needs_the_view_permission_on_cables(self):
        self.activate()
        trace = f"{self.eth1.get_absolute_url()}trace/"
        config = copy.deepcopy(settings.PLUGINS_CONFIG)
        config["netbox_device_view"]["show_on_device_tab"] = True
        for cables in (True, False):
            if not cables:
                ObjectPermission.objects.filter(name__startswith="dcim.cable").delete()
            with override_settings(PLUGINS_CONFIG=config):
                page = self.client.get(self.device.get_absolute_url()).content.decode()
            for surface, content in (("tab", self.tab()), ("device page", page)):
                with self.subTest(surface=surface, cables=cables):
                    ident = details_id(f"dv-o-d{self.device.pk}-front-", "con-eth1")
                    _, fragment = details(content, ident)
                    self.assertEqual(("Trace", trace) in links(fragment), cables)

    def test_two_found_bindings_open_a_chooser_of_both_components(self):
        activate_second_binding(self, "{component: consoleport, name: eth1}")
        wrapped, _, fragment = self.connector()
        self.assertEqual(wrapped.get("role"), "button")
        self.assertIsNone(wrapped.get("href"))
        self.assertIn("Several components. Choose one.", text(fragment))
        self.assertEqual(
            [link for link in links(fragment) if link[0] != "Trace"],
            [
                ("Interface eth1", self.eth1.get_absolute_url()),
                ("Console port eth1", self.console.get_absolute_url()),
            ],
        )

    def test_a_found_and_a_missing_binding_open_the_unresolved_details(self):
        activate_second_binding(self, "{component: powerport, name: PSU9}")
        wrapped, _, fragment = self.connector()
        self.assertEqual(wrapped.get("role"), "button")
        self.assertIn("dv-art-unresolved", wrapped.get("class"))
        entries = fragment.find_class("dv-details-entry")
        self.assertEqual(
            [entry.find("h3").text_content().strip() for entry in entries],
            ["Interface eth1", "Power port PSU9"],
        )
        self.assertIn("No single component", text(fragment))
        self.assertIn("No component found", text(entries[1]))

    def test_each_face_has_a_details_toggle_and_each_connector_its_details(self):
        self.activate()
        content = self.tab()
        self.assertEqual(content.count('data-dv-details-mode aria-pressed="false"'), 2)
        self.assertEqual(content.count('<dialog class="dv-details"'), 1)
        referenced = set(re.findall(r'data-dv-details="([^"]+)"', content))
        templates = set(re.findall(r'<template id="(dv-o-[^"]+)"', content))
        self.assertEqual(len(referenced), 4)
        self.assertEqual(referenced, templates)


class LayoutDetailsTests(ResolverTestBase):
    def test_missing_ambiguous_and_uncabled_keys_look_different(self):
        Interface.objects.create(device=self.device, name="Gi1", type="1000base-t")
        Interface.objects.create(device=self.device, name="Console", type="1000base-t")
        ConsolePort.objects.create(device=self.device, name="Console")
        content = self._tab()
        cells = {cell["data-stylename"]: cell for cell in page_cells(content)}
        uncabled, ambiguous, missing = cells["gi-1"], cells["console"], cells["port-1"]
        self.assertIn("bg-secondary", uncabled["class"].split())
        self.assertIn("dv-ambiguous", ambiguous["class"].split())
        # The stylesheet draws a port without a state as an empty dotted outline.
        self.assertEqual(missing["class"].split(), ["dv-port", "port-1"])
        self.assertNotIn("data-connection-state", missing)
        self.assertIn(
            ".dv-port:not([data-connection-state]) .dv-port-rect { fill: transparent;",
            Path(
                finders.find("netbox_device_view/css/device_view_svg.css")
            ).read_text(),
        )
        # A port without a component opens the generic missing details.
        self.assertNotIn("data-dv-details", missing)
        self.assertIn('<template id="dv-details-missing">', content)
        self.assertEqual(content.count('data-dv-details-mode aria-pressed="false"'), 1)

    def test_a_css_cell_names_its_component_and_its_state(self):
        Interface.objects.create(device=self.device, name="Gi1", type="1000base-t")
        Interface.objects.create(device=self.device, name="Console", type="1000base-t")
        ConsolePort.objects.create(device=self.device, name="Console")
        DeviceView.objects.update(render_mode=RenderMode.CSS)
        cells = {cell["style"].split(";")[0]: cell for cell in page_cells(self._tab())}
        self.assertEqual(
            {area: cell["aria-label"] for area, cell in cells.items()},
            {
                "grid-area: gi-1": "Gi1: not connected",
                "grid-area: console": "console: several components",
            },
        )

    def test_a_module_component_names_its_module(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.create(device=self.device, name="Bay 1"),
            module_type=ModuleType.objects.create(
                manufacturer=self.manufacturer, model="NM-1"
            ),
        )
        interface = Interface.objects.create(
            device=self.device, module=module, name="Gi1", type="1000base-t"
        )
        for render_mode in (RenderMode.CSS, RenderMode.SVG):
            with self.subTest(render_mode):
                DeviceView.objects.update(render_mode=render_mode)
                content = self._tab()
                (cell,) = (
                    cell
                    for cell in page_cells(content)
                    if interface.get_absolute_url()
                    in (cell.get("href"), cell.get("data-href"))
                )
                _, fragment = details(content, cell["data-dv-details"])
                self.assertEqual(facts(fragment)["Module"], str(module))
                self.assertIn((str(module), module.get_absolute_url()), links(fragment))
