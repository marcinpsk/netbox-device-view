"""The artwork API lists each placed module from the same resolver as the Device View tab."""

from django.urls import reverse
from lxml import etree
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token

from .module_samples import CHASSIS_SVG
from .test_api_artwork import API_URL
from .test_artwork_device_view import SVG_NS, faces, links
from .test_module_composition import CompositionTestCase, activate_alias_revision


class ModuleApiTestCase(CompositionTestCase):
    def setUp(self):
        super().setUp()
        token = Token.objects.create(user=self.user)
        self.api = APIClient()
        self.api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )

    def artwork(self):
        response = self.api.get(reverse(API_URL, kwargs={"pk": self.device.pk}))
        self.assertEqual(response.status_code, 200, response.content[:500])
        [member] = response.json()["members"]
        return member["artwork"]

    def modules(self):
        return {entry["module_id"]: entry for entry in self.artwork()["modules"]}


class ModuleEntryTests(ModuleApiTestCase):
    def test_each_placed_module_is_listed_with_its_bay_revision_and_connectors(self):
        inventory = self.inventory
        artwork = self.artwork()
        self.assertEqual(
            [entry["module_id"] for entry in artwork["modules"]],
            [
                m.pk
                for m in (
                    inventory.fan,
                    inventory.nm1,
                    inventory.sfp,
                    inventory.nm2,
                    inventory.psu,
                )
            ],
        )
        nm1 = self.modules()[inventory.nm1.pk]
        revision = inventory.revisions["acme/nm-2x"]
        part = f"dv-a-d{self.device.pk}-m{inventory.nm1.pk}"
        interface = inventory.interface(inventory.nm1, "TenGigabitEthernet1/1/1")
        self.assertEqual(
            {key: value for key, value in nm1.items() if key != "connectors"},
            {
                "module_id": inventory.nm1.pk,
                "module_bay_id": inventory.nm1.module_bay_id,
                "parent_module_id": None,
                "bay_key": "bay-nm1",
                "face": "front",
                "representation": "artwork",
                "artwork": {
                    "definition_key": "acme/nm-2x",
                    "revision_id": revision.pk,
                    "digest": revision.digest,
                },
                "placement": {
                    "x": 200,
                    "y": 7,
                    "width": 100,
                    "height": 30,
                    "rotate": 0,
                },
                "element_id": part,
                "warnings": [],
            },
        )
        [p1] = nm1["connectors"]
        self.assertEqual(
            {key: p1[key] for key in ("key", "face", "label", "element_id")},
            {
                "key": "con-p1",
                "face": "installed",
                "label": "P1",
                "element_id": f"{part}-installed-con-p1",
            },
        )
        self.assertEqual(
            [(b["status"], b["object_id"], b["name"]) for b in p1["bindings"]],
            [("found", interface.pk, "TenGigabitEthernet1/1/1")],
        )

    def test_a_nested_module_has_its_parent_and_its_bay_in_device_face_coordinates(
        self,
    ):
        modules = self.modules()
        sfp = modules[self.inventory.sfp.pk]
        self.assertEqual(sfp["parent_module_id"], self.inventory.nm1.pk)
        self.assertEqual(
            sfp["placement"],
            {"x": 240, "y": 17, "width": 14, "height": 10, "rotate": 0},
        )
        self.assertEqual(
            modules[self.inventory.psu.pk]["placement"],
            {"x": 120, "y": 2, "width": 30, "height": 40, "rotate": 90},
        )

    def test_a_module_without_artwork_is_an_outline_without_connectors(self):
        fan = self.modules()[self.inventory.fan.pk]
        self.assertEqual(
            (fan["representation"], fan["artwork"], fan["connectors"]),
            ("outline", None, []),
        )
        self.assertEqual(
            fan["element_id"], f"dv-a-d{self.device.pk}-m{self.inventory.fan.pk}"
        )


class MissingBindingTests(ModuleApiTestCase):
    def test_a_missing_binding_of_a_module_gives_the_name_that_netbox_resolves(self):
        self.inventory.interface(self.inventory.nm2, "TenGigabitEthernet1/2/1").delete()
        [p1] = self.modules()[self.inventory.nm2.pk]["connectors"]
        self.assertEqual(
            [(b["status"], b["name"]) for b in p1["bindings"]],
            [("missing", "TenGigabitEthernet1/2/1")],
        )


class FaceAgreementTests(ModuleApiTestCase):
    def test_each_element_id_is_in_the_plain_face(self):
        artwork = self.artwork()
        [face] = artwork["faces"]
        svg = etree.fromstring(face["svg"])
        drawn = {element.get("id") for element in svg.iter() if element.get("id")}
        for entry in artwork["modules"]:
            self.assertIn(entry["element_id"], drawn)
            for connector in entry["connectors"]:
                self.assertIn(connector["element_id"], drawn)
        self.assertEqual(list(svg.iter(f"{{{SVG_NS}}}a")), [])
        self.assertEqual([e for e in svg.iter() if e.get("class")], [])

    def test_the_api_binds_the_components_that_the_page_links(self):
        page = links(faces(self.tab())[0])
        for entry in self.artwork()["modules"]:
            for connector in entry["connectors"]:
                [binding] = connector["bindings"]
                page_id = "dv-o-" + connector["element_id"].removeprefix("dv-a-")
                self.assertTrue(
                    page[page_id].get("href").endswith(f"/{binding['object_id']}/")
                )


class ModuleWarningTests(ModuleApiTestCase):
    def test_a_size_mismatch_is_a_warning_of_the_module(self):
        self.store_face(
            "acme/ch-4",
            "front.svg",
            CHASSIS_SVG.replace(
                'x="320" y="7" width="100"', 'x="320" y="7" width="110"'
            ),
        )
        nm2 = self.modules()[self.inventory.nm2.pk]
        self.assertEqual(
            nm2["warnings"],
            [
                {
                    "code": "size_mismatch",
                    "message": (
                        "ACME NM-2X in NM 2: the artwork is 100 × 30 mm, but the bay"
                        " bay-nm2 is 110 × 30 mm."
                    ),
                }
            ],
        )
        self.assertEqual(nm2["representation"], "artwork")

    def test_a_finite_bay_with_a_placement_that_is_not_finite_is_an_outline(self):
        self.store_face(
            "acme/ch-4",
            "front.svg",
            CHASSIS_SVG.replace(
                'x="320" y="7" width="100"', 'x="1.6e308" y="7" width="1e308"'
            ),
        )
        nm2 = self.modules()[self.inventory.nm2.pk]
        self.assertEqual(
            (
                nm2["representation"],
                nm2["placement"],
                nm2["element_id"],
                [warning["code"] for warning in nm2["warnings"]],
            ),
            (
                "outline",
                None,
                f"dv-a-d{self.device.pk}-m{self.inventory.nm2.pk}",
                ["placement_not_finite"],
            ),
        )
        # The centre of the label overflows too; the face holds no infinite number.
        [face] = self.artwork()["faces"]
        self.assertNotIn("inf", face["svg"])

    def test_a_module_that_is_not_drawn_has_no_placement_or_element(self):
        self.store_face(
            "acme/ch-4", "front.svg", CHASSIS_SVG.replace('x="320"', 'x="1e999"')
        )
        nm2 = self.modules()[self.inventory.nm2.pk]
        self.assertEqual(
            (nm2["placement"], nm2["element_id"], nm2["warnings"][0]["code"]),
            (None, None, "placement_not_finite"),
        )
        self.assertEqual(
            [connector["element_id"] for connector in nm2["connectors"]], [None]
        )


class DuplicateBayApiTests(ModuleApiTestCase):
    def test_a_module_that_two_bays_name_has_one_entry_with_a_warning(self):
        activate_alias_revision(self)
        entries = [
            entry
            for entry in self.artwork()["modules"]
            if entry["module_id"] == self.inventory.sfp.pk
        ]
        self.assertEqual(
            [
                (entry["bay_key"], [w["code"] for w in entry["warnings"]])
                for entry in entries
            ],
            [("bay-alias", ["duplicate_bay"])],
        )
