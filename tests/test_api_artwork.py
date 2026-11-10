"""The artwork API gives each visible member of a device its artwork, layout panels or reason."""

import re
import time
from unittest import mock

from dcim.models import (
    Cable,
    Device,
    DeviceType,
    Interface,
    Module,
    PowerPort,
    VirtualChassis,
)
from django.apps import apps
from django.urls import reverse
from lxml import etree
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import ObjectPermission, Token

from netbox_device_view.artwork.render import (
    _FOCUS,
    _MARKS,
    API,
    ARTWORK,
    INSTALLED,
    NOT_RECORDED,
    OVERVIEW,
    SCHEMATIC,
)
from netbox_device_view.models import DeviceView, RenderMode

from .permissions import grant
from .test_api_rendered_layout import SVG_YAML
from .test_artwork_device_view import (
    FRONT_SVG,
    GRID,
    SVG_NS,
    ArtworkTestCase,
    faces,
    ids,
    links,
)

API_URL = "plugins-api:netbox_device_view-api:device_artwork"
LAYOUT_URL = "plugins-api:netbox_device_view-api:device_rendered_layout"
# The outer mark class of a connector with one found binding, from its cable status.
CABLE_CLASSES = {
    status: f"dv-art-cable-{status}"
    for status in ("connected", "planned", "decommissioning")
}


def token_client(user):
    """Return an API client that authenticates as ``user`` with a new token."""
    token = Token.objects.create(user=user)
    api = APIClient()
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
    )
    return api


def device_members(test, device, user):
    """Return the members of ``device`` that ``user`` gets from the artwork API."""
    response = token_client(user).get(reverse(API_URL, kwargs={"pk": device.pk}))
    test.assertEqual(response.status_code, 200, response.content[:500])
    return response.json()["members"]


def member_artwork(test, device, user):
    """Return the artwork of the one member of ``device`` that ``user`` gets from the API."""
    [member] = device_members(test, device, user)
    return member["artwork"]


def api_id(page_id):
    """Return the API id of an id of the page overview."""
    return page_id.replace(f"dv-{OVERVIEW}-", f"dv-{API}-", 1)


def class_names(element):
    return set((element.get("class") or "").split())


def page_agreement(content):
    """Return the facts of the page overview that the artwork API also gives, with the ids of the API.

    The facts are the ids of each face, {connector id: (href, mark classes)},
    {bay element id: (state, module href)} and {module element id: (href, representation)}.
    """
    drawn = faces(content)
    # The page adds the mark and the focus ring filters of its connectors.
    face_ids = [
        [api_id(i) for i in ids(face) if _MARKS not in i and _FOCUS not in i]
        for face in drawn
    ]
    connectors, bays, modules = {}, {}, {}
    part = re.compile(rf"dv-{OVERVIEW}-d\d+(-m\d+)+")
    for face in drawn:
        for element in face.iter(etree.Element):
            ident = element.get("id")
            if "dv-art-connector" in class_names(element):
                [child] = [child for child in element if child.get("id")]
                connectors[api_id(child.get("id"))] = (
                    element.get("href"),
                    class_names(element) - {"dv-art-connector"},
                )
            elif ident is None:
                continue
            elif part.fullmatch(ident):
                # The first body in the group is the body of the module, before its parts and child modules.
                body = next(
                    a
                    for a in element.iter(f"{{{SVG_NS}}}a")
                    if "dv-art-module-body" in class_names(a)
                )
                kind = SCHEMATIC if "dv-art-schematic" in class_names(body) else ARTWORK
                bays[api_id(ident)] = (INSTALLED, body.get("href"))
                modules[api_id(ident)] = (body.get("href"), kind)
            # A turned parent draws the mark in an upright group of the bay group.
            elif any(
                "dv-art-not-recorded" in class_names(e)
                for e in (element, *element.iterchildren(etree.Element))
            ):
                bays[api_id(ident)] = (NOT_RECORDED, None)
    return face_ids, connectors, bays, modules


def artwork_connectors(artwork):
    """Return the connectors of the device and of each module of an API artwork."""
    return [
        *artwork["connectors"],
        *(c for module in artwork["modules"] for c in module["connectors"]),
    ]


def api_agreement(artwork):
    """Return the facts of page_agreement() that the artwork API gives."""
    face_ids = [ids(etree.fromstring(face["svg"])) for face in artwork["faces"]]
    connectors = {
        connector["element_id"]: binding_state(connector["bindings"])
        for connector in artwork_connectors(artwork)
        if connector["element_id"] is not None
    }
    links = {
        module.pk: module.get_absolute_url()
        for module in Module.objects.filter(
            pk__in=[m["module_id"] for m in artwork["modules"]]
        )
    }
    bays = {
        bay["element_id"]: (bay["state"], links.get(bay["module_id"]))
        for bay in artwork["bays"]
        if bay["element_id"] is not None
    }
    modules = {
        module["element_id"]: (links[module["module_id"]], module["representation"])
        for module in artwork["modules"]
        if module["element_id"] is not None
    }
    return face_ids, connectors, bays, modules


def binding_state(bindings):
    """Return the link and the mark classes that the page shows for ``bindings``."""
    if not bindings or any(b["status"] != "found" for b in bindings):
        return None, {"dv-art-unresolved"}
    if len(bindings) > 1:
        return None, {"dv-art-several"}
    [binding] = bindings
    component = apps.get_model(binding["object_type"]).objects.get(
        pk=binding["object_id"]
    )
    marks = set()
    if binding["cable_status"] in CABLE_CLASSES:
        marks.add(CABLE_CLASSES[binding["cable_status"]])
    elif binding["marked_connected"]:
        marks.add("dv-art-marked")
    if binding["enabled"] is False:
        marks.add("dv-art-disabled")
    return component.get_absolute_url(), marks


class ArtworkApiTestCase(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        self.api = token_client(self.user)

    def get(self, device=None, url=API_URL, status=200):
        response = self.api.get(reverse(url, kwargs={"pk": (device or self.device).pk}))
        self.assertEqual(response.status_code, status, response.content[:500])
        return response.json()

    def member(self, device=None):
        [member] = self.get(device)["members"]
        return member

    def connectors(self, member):
        return {item["key"]: item for item in member["artwork"]["connectors"]}


class ArtworkResponseTests(ArtworkApiTestCase):
    def test_a_device_with_active_artwork_returns_its_faces_and_connectors(self):
        self.activate()
        body = self.get()
        self.assertEqual(
            (body["schema_version"], body["device_id"]), (1, self.device.pk)
        )
        [member] = body["members"]
        self.assertEqual(
            {key: member[key] for key in member if key not in ("artwork",)},
            {
                "device_id": self.device.pk,
                "label": "sw1",
                "position": None,
                "current": True,
                "representation": "artwork",
                "reason": None,
                "panels": [],
            },
        )
        artwork = member["artwork"]
        self.assertEqual(
            (artwork["definition_key"], artwork["revision_id"], artwork["digest"]),
            ("acme/sw-24", self.revision.pk, self.revision.digest),
        )
        self.assertEqual(
            [(face["name"], face["label"]) for face in artwork["faces"]],
            [("front", "Front"), ("rear", "Rear")],
        )
        eth1 = self.connectors(member)["con-eth1"]
        self.assertEqual(
            {key: eth1[key] for key in ("face", "label", "element_id")},
            {
                "face": "front",
                "label": "1",
                "element_id": f"dv-a-d{self.device.pk}-front-con-eth1",
            },
        )
        self.assertEqual(
            eth1["bindings"],
            [
                {
                    "status": "found",
                    "object_type": "dcim.interface",
                    "object_id": self.eth1.pk,
                    "name": "eth1",
                    "enabled": True,
                    "cable_status": None,
                    "cable_color": None,
                    "marked_connected": False,
                    "path_complete": False,
                    "peer_labels": [],
                    "trace_supported": True,
                }
            ],
        )
        self.assertEqual(
            {
                key: [(b["object_type"], b["object_id"]) for b in item["bindings"]]
                for key, item in self.connectors(member).items()
            },
            {
                "con-eth1": [("dcim.interface", self.eth1.pk)],
                "con-psu": [("dcim.powerport", self.psu.pk)],
                "con-r1": [("dcim.rearport", self.rear_port.pk)],
                "con-f1": [("dcim.frontport", self.front_port.pk)],
            },
        )

    def test_each_connector_element_is_in_its_face(self):
        self.activate()
        member = self.member()
        svg = {
            face["name"]: etree.fromstring(face["svg"])
            for face in member["artwork"]["faces"]
        }
        for key, connector in self.connectors(member).items():
            with self.subTest(key):
                self.assertIn(connector["element_id"], ids(svg[connector["face"]]))

    def test_a_face_is_plain_svg_without_links_outlines_or_classes(self):
        self.activate()
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[
                Interface.objects.create(
                    device=self.make_device("peer"), name="eth0", type="1000base-t"
                )
            ],
        ).save()
        for face in self.member()["artwork"]["faces"]:
            root = etree.fromstring(face["svg"])
            with self.subTest(face["name"]):
                self.assertEqual(root.nsmap, {None: SVG_NS})
                self.assertIsNone(root.get("class"))
                for element in root.iter():
                    self.assertNotIn(
                        etree.QName(element).localname, ("a", "filter", "title")
                    )
                    self.assertNotIn("filter", element.attrib)

    def test_a_missing_component_is_a_missing_binding_with_its_manifest_name(self):
        self.activate()
        self.psu.delete()
        self.assertEqual(
            self.connectors(self.member())["con-psu"]["bindings"],
            [
                {
                    "status": "missing",
                    "object_type": "dcim.powerport",
                    "object_id": None,
                    "name": "PSU1",
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

    def test_a_cabled_connector_gives_its_cable_and_peer(self):
        self.activate()
        peer = self.make_device("server1")
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[
                Interface.objects.create(device=peer, name="eth0", type="1000base-t")
            ],
            color="ff0000",
        ).save()
        [binding] = self.connectors(self.member())["con-eth1"]["bindings"]
        self.assertEqual(
            {
                key: binding[key]
                for key in (
                    "cable_status",
                    "cable_color",
                    "path_complete",
                    "peer_labels",
                )
            },
            {
                "cable_status": "connected",
                "cable_color": "ff0000",
                "path_complete": True,
                "peer_labels": ["server1 | eth0"],
            },
        )

    def test_each_fact_is_its_own_field(self):
        self.activate()
        peer = self.make_device("server1")
        Interface.objects.filter(pk=self.eth1.pk).update(enabled=False)
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[
                Interface.objects.create(device=peer, name="eth0", type="1000base-t")
            ],
            status="planned",
        ).save()
        PowerPort.objects.filter(pk=self.psu.pk).update(mark_connected=True)
        connectors = self.connectors(self.member())
        [eth1] = connectors["con-eth1"]["bindings"]
        [psu] = connectors["con-psu"]["bindings"]
        facts = ("enabled", "cable_status", "marked_connected", "path_complete")
        self.assertEqual(
            {key: eth1[key] for key in facts},
            {
                "enabled": False,
                "cable_status": "planned",
                "marked_connected": False,
                "path_complete": True,
            },
        )
        # A power port has no enabled field; its path ends at the marked connection.
        self.assertEqual(
            {key: psu[key] for key in facts},
            {
                "enabled": None,
                "cable_status": None,
                "marked_connected": True,
                "path_complete": False,
            },
        )

    def test_a_connector_that_the_face_does_not_draw_has_no_element_id(self):
        # The import rejects this face now; an earlier import could store it.
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-eth1" width="12" height="10" style="fill:url(#metal);stroke:#000"/>',
                '<desc><rect id="con-eth1" width="12" height="10"/></desc>',
            ),
        )
        self.activate()
        connectors = self.connectors(self.member())
        self.assertIsNone(connectors["con-eth1"]["element_id"])
        self.assertEqual(
            connectors["con-r1"]["element_id"], f"dv-a-d{self.device.pk}-front-con-r1"
        )

    def test_a_found_and_a_missing_binding_have_the_same_fields(self):
        self.activate()
        self.psu.delete()
        connectors = self.connectors(self.member())
        [found] = connectors["con-eth1"]["bindings"]
        [missing] = connectors["con-psu"]["bindings"]
        self.assertEqual(list(found), list(missing))

    def test_damaged_artwork_is_unavailable(self):
        self.activate()
        blob = self.revision.files.get(path="front.svg").blob
        (self.media / blob.name).write_text("<svg/>")
        with self.assertLogs("netbox_device_view.artwork.render", "WARNING"):
            member = self.member()
        self.assertEqual(
            (member["representation"], member["reason"], member["artwork"]),
            ("unavailable", "damaged_artwork", None),
        )


class BrowserAgreementTests(ArtworkApiTestCase):
    def setUp(self):
        super().setUp()
        self.activate()
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[
                Interface.objects.create(
                    device=self.make_device("server1"), name="eth0", type="1000base-t"
                )
            ],
        ).save()

    def test_the_api_binds_the_connectors_that_the_page_links(self):
        member = self.member()
        by_element = {
            connector["element_id"]: connector
            for connector in member["artwork"]["connectors"]
        }
        linked = 0
        for face in faces(self.tab()):
            for element_id, link in links(face).items():
                connector = by_element[element_id.replace("dv-o-", "dv-a-", 1)]
                [binding] = [b for b in connector["bindings"] if b["status"] == "found"]
                component = {
                    "dcim.interface": self.eth1,
                    "dcim.powerport": self.psu,
                    "dcim.rearport": self.rear_port,
                    "dcim.frontport": self.front_port,
                }[binding["object_type"]]
                self.assertEqual(binding["object_id"], component.pk)
                self.assertEqual(link.get("href"), component.get_absolute_url())
                self.assertEqual(
                    "dv-art-cable-" in link.get("class"),
                    binding["cable_status"] is not None,
                )
                linked += 1
        self.assertEqual(linked, len(by_element))

    def test_the_api_face_has_the_ids_of_the_page_face(self):
        page = [ids(face) for face in faces(self.tab())]
        api = [
            ids(etree.fromstring(face["svg"]))
            for face in self.member()["artwork"]["faces"]
        ]
        # The page adds the mark and the focus ring filters of its connectors.
        page = [
            [i for i in face if "-marks-" not in i and "-focus-" not in i]
            for face in page
        ]
        self.assertEqual(
            [[i.replace("dv-o-", "dv-a-", 1) for i in face] for face in page], api
        )

    def test_a_restricted_component_is_missing_in_the_api_and_unlinked_on_the_page(
        self,
    ):
        ObjectPermission.objects.filter(name__startswith="dcim.interface").update(
            constraints={"name": "other"}
        )
        front = faces(self.tab())[0]
        self.assertNotIn(f"dv-o-d{self.device.pk}-front-con-eth1", links(front))
        [binding] = self.connectors(self.member())["con-eth1"]["bindings"]
        self.assertEqual((binding["status"], binding["object_id"]), ("missing", None))

    def test_a_visible_cable_to_a_hidden_peer_is_occupied_with_no_label(self):
        ObjectPermission.objects.filter(name__startswith="dcim.interface").update(
            constraints={"device__name": "sw1"}
        )
        [binding] = self.connectors(self.member())["con-eth1"]["bindings"]
        self.assertEqual(
            (binding["cable_status"], binding["peer_labels"]), ("connected", [])
        )

    def test_a_hidden_cable_is_no_occupancy_and_a_hidden_peer_no_label(self):
        ObjectPermission.objects.filter(name__startswith="dcim.cable").delete()
        ObjectPermission.objects.filter(name__startswith="dcim.interface").update(
            constraints={"device__name": "sw1"}
        )
        [binding] = self.connectors(self.member())["con-eth1"]["bindings"]
        self.assertEqual(
            (
                binding["cable_status"],
                binding["cable_color"],
                binding["path_complete"],
                binding["peer_labels"],
            ),
            (None, None, False, []),
        )
        link = links(faces(self.tab())[0])[f"dv-o-d{self.device.pk}-front-con-eth1"]
        self.assertEqual(link.get("class"), "dv-art-connector")


class WithoutArtworkTests(ArtworkApiTestCase):
    def test_a_device_without_artwork_or_layout_is_unavailable(self):
        member = self.member()
        self.assertEqual(
            {
                key: member[key]
                for key in ("representation", "reason", "artwork", "panels")
            },
            {
                "representation": "unavailable",
                "reason": "no_layout",
                "artwork": None,
                "panels": [],
            },
        )

    def test_a_device_with_a_layout_gets_the_rendered_layout_panels(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(device_type=self.device_type, yaml_layout=SVG_YAML)
        member = self.member()
        self.assertEqual(
            (member["representation"], member["reason"], member["artwork"]),
            ("layout", None, None),
        )
        self.assertEqual(member["panels"], self.get(url=LAYOUT_URL)["panels"])

    def test_a_css_only_layout_is_unavailable(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(device_type=self.device_type, grid_template_area=GRID)
        member = self.member()
        self.assertEqual(
            (member["representation"], member["reason"]),
            ("unavailable", "svg_layout_required"),
        )

    def test_an_invalid_layout_is_unavailable(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(
            device_type=self.device_type, yaml_layout="version: 1\nviews: [x"
        )
        member = self.member()
        self.assertEqual(
            (member["representation"], member["reason"]),
            ("unavailable", "invalid_layout"),
        )

    def test_active_artwork_comes_before_a_layout(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(device_type=self.device_type, yaml_layout=SVG_YAML)
        self.activate()
        self.assertEqual(self.member()["representation"], "artwork")


class AccessTests(ArtworkApiTestCase):
    def test_without_view_device_the_request_is_403(self):
        ObjectPermission.objects.filter(name__startswith="dcim.device").delete()
        self.get(status=403)

    def test_a_device_outside_the_permission_scope_is_404(self):
        ObjectPermission.objects.filter(name__startswith="dcim.device").update(
            constraints={"name": "other"}
        )
        self.get(status=404)

    def test_the_artwork_needs_no_permission_on_the_association_or_revision(self):
        self.activate()
        self.assertEqual(self.member()["representation"], "artwork")


class ChassisApiTestCase(ArtworkApiTestCase):
    """Two chassis members: sw1 of the artwork type and sw2 of type SW-48."""

    def setUp(self):
        super().setUp()
        self.chassis = VirtualChassis.objects.create(name="stack")
        Device.objects.filter(pk=self.device.pk).update(
            virtual_chassis=self.chassis, vc_position=1
        )
        self.device.refresh_from_db()
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-48", slug="sw-48"
        )
        self.member2 = self.make_device(
            "sw2", other, virtual_chassis=self.chassis, vc_position=2
        )
        PowerPort.objects.create(device=self.member2, name="PSU1")


class VirtualChassisApiTests(ChassisApiTestCase):
    def test_members_resolve_one_by_one(self):
        self.activate()
        body = self.get(self.member2)
        self.assertEqual(body["device_id"], self.member2.pk)
        self.assertEqual(
            [
                (m["device_id"], m["label"], m["position"], m["current"])
                for m in body["members"]
            ],
            [(self.device.pk, "sw1", 1, False), (self.member2.pk, "sw2", 2, True)],
        )
        self.assertEqual(
            [(m["representation"], m["reason"]) for m in body["members"]],
            [("artwork", None), ("schematic", None)],
        )

    def test_a_member_with_a_layout_gets_its_panels_beside_an_artwork_member(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(
            device_type=self.member2.device_type, yaml_layout=SVG_YAML
        )
        self.activate()
        members = self.get()["members"]
        self.assertEqual([m["representation"] for m in members], ["artwork", "layout"])
        self.assertEqual([panel["key"] for panel in members[1]["panels"]], ["front"])

    def test_an_unexpected_layout_error_makes_only_that_member_unavailable(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(
            device_type=self.member2.device_type, yaml_layout=SVG_YAML
        )
        self.activate()
        # No stored layout is known to raise here, so the test injects the error.
        failing = mock.patch(
            "netbox_device_view.api.device_artwork.member_panels",
            side_effect=RuntimeError("render bug"),
        )
        with failing, self.assertLogs("netbox_device_view.api.device_artwork", "ERROR"):
            members = self.get()["members"]
        self.assertEqual(
            [(m["representation"], m["reason"]) for m in members],
            [("artwork", None), ("unavailable", "invalid_layout")],
        )

    def test_an_unreadable_artwork_file_makes_only_that_member_unavailable(self):
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(
            device_type=self.member2.device_type, yaml_layout=SVG_YAML
        )
        self.activate()
        # Storage cannot read a directory, as it cannot read a file without permission.
        stored = self.media / self.revision.files.get(path="front.svg").blob.name
        stored.unlink()
        stored.mkdir()
        with self.assertLogs("netbox_device_view.api.device_artwork", "ERROR"):
            members = self.get()["members"]
        self.assertEqual(
            [(m["representation"], m["reason"]) for m in members],
            [("unavailable", "artwork_error"), ("layout", None)],
        )

    def test_a_member_that_the_user_cannot_view_is_left_out(self):
        self.activate()
        ObjectPermission.objects.filter(name__startswith="dcim.device").update(
            constraints={"name": "sw1"}
        )
        self.assertEqual(
            [m["device_id"] for m in self.get()["members"]], [self.device.pk]
        )

    def test_two_artwork_members_share_no_element_id(self):
        Device.objects.filter(pk=self.member2.pk).update(device_type=self.device_type)
        self.activate()
        members = self.get()["members"]
        all_ids = [
            i
            for member in members
            for face in member["artwork"]["faces"]
            for i in ids(etree.fromstring(face["svg"]))
        ]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertTrue(
            all(re.match(r"dv-a-d\d+-(front|rear)-", i) for i in all_ids), all_ids
        )


# A stored layout from before the canvas rule; rows 0 made a filler fail with ValueError.
ZERO_ROWS_YAML = (
    "version: 1\ncanvas: {rows: 0}\nviews:\n  front:\n    rows:\n      - blank: 1\n"
)


# Lenient parsing once expanded this until the request timed out.
HUGE_YAML = (
    "version: 1\nviews:\n  front:\n    rows:\n"
    "      - sequence: {kind: port, prefix: p, count: 1000000000000}\n"
)


class UnusableCanvasTests(ChassisApiTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, DeviceView, "view")
        DeviceView.objects.create(
            device_type=self.member2.device_type,
            yaml_layout=ZERO_ROWS_YAML,
            render_mode=RenderMode.SVG,
        )

    def test_the_member_is_an_invalid_layout_beside_the_other_members(self):
        self.activate()
        members = self.get()["members"]
        self.assertEqual(
            [(m["representation"], m["reason"]) for m in members],
            [("artwork", None), ("unavailable", "invalid_layout")],
        )

    def test_the_tab_shows_the_layout_error(self):
        Device.objects.filter(pk=self.member2.pk).update(
            virtual_chassis=None, vc_position=None
        )
        content = self.tab(self.member2)
        self.assertIn("canvas.rows", content)

    def test_the_rest_api_does_not_store_the_layout(self):
        grant(self.user, DeviceView, "add")
        DeviceView.objects.all().delete()
        response = self.api.post(
            reverse("plugins-api:netbox_device_view-api:deviceview-list"),
            {
                "device_type": self.member2.device_type.pk,
                "grid_template_area": GRID,
                "yaml_layout": ZERO_ROWS_YAML,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("canvas.rows", str(response.json()))

    def test_a_layout_over_the_element_limit_is_not_stored_or_rendered(self):
        DeviceView.objects.update(yaml_layout=HUGE_YAML)
        started = time.monotonic()
        self.activate()
        members = self.get()["members"]
        self.assertEqual(members[1]["reason"], "invalid_layout")
        grant(self.user, DeviceView, "change")
        response = self.api.patch(
            reverse(
                "plugins-api:netbox_device_view-api:deviceview-detail",
                kwargs={"pk": DeviceView.objects.get().pk},
            ),
            {"yaml_layout": HUGE_YAML},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("elements", str(response.json()))
        self.assertLess(time.monotonic() - started, 5)
