"""The review of an activation: impact, blockers, pins, atomic activation, rollback, visibility and the token."""

import io
import shutil
import tempfile
import time
from unittest import mock

from dcim.models import (
    ConsoleServerPort,
    Device,
    DeviceType,
    Interface,
    Module,
    ModuleBay,
    ModuleType,
    PowerOutlet,
    PowerPort,
)
from django.contrib.messages import get_messages
from django.core import signing
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view.artwork import pins, review
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import (
    Association,
    BindingOverride,
    DefinitionRevision,
    InstancePin,
    active_revisions,
)

from .activation import activate
from .artwork_samples import MODULE_FILES, MODULE_SVG, bundle, definition
from .module_samples import NM_MANIFEST, NM_SVG, Inventory, import_definitions
from .permissions import grant
from .test_artwork_activation import (
    ACTIVATE_URL,
    ASSOCIATION_URL,
    LIST_URL,
    NEWER,
    REVIEW_AGAIN,
    REVIEW_URL,
    ActivationTestCase,
    review_url,
    token_of,
)
from .test_artwork_device_view import FILES, FRONT_SVG, MANIFEST
from .test_binding_override_activation import WITHOUT_ETH1

ETH1_BINDING = "      - {component: interface, name: eth1}\n"
assert ETH1_BINDING in MANIFEST
# con-eth1 binds a name that no component has.
BROKEN = {
    **FILES,
    "manifest.yaml": MANIFEST.replace(
        ETH1_BINDING, ETH1_BINDING.replace("eth1", "eth9")
    ),
}
BAY = "bays:\n  bay-nm:\n    face: front\n    module_bay: Network Module\n"
assert MANIFEST.endswith(BAY)
BAY_RECT = '<rect id="bay-nm" x="300" y="5" width="100"'
assert BAY_RECT in FRONT_SVG
# The device draws no module bay.
WITHOUT_BAY = {
    **FILES,
    "manifest.yaml": MANIFEST.replace(BAY, "bays: {}\n"),
    "front.svg": FRONT_SVG.replace('id="bay-nm" ', ""),
}
# The bay is 90 mm wide; the module artwork is 100 mm wide.
SMALLER_BAY = {
    **FILES,
    "front.svg": FRONT_SVG.replace(BAY_RECT, BAY_RECT.replace("100", "90")),
}
NEW_MODULE = {**MODULE_FILES, "installed.svg": MODULE_SVG.replace("#444", "#555")}
SLOT = '  bay-slot: {face: installed, module_bay: "Slot {module}"}'
assert SLOT in NM_MANIFEST
# A network module that draws no slot for its transceiver.
NM_WITHOUT_SLOT = {
    "manifest.yaml": NM_MANIFEST.replace(SLOT, "  {}"),
    "installed.svg": NM_SVG.replace('id="bay-slot" ', ""),
}
REVIEWER_MODELS = (
    Module,
    ModuleBay,
    ModuleType,
    ConsoleServerPort,
    PowerOutlet,
    BindingOverride,
    InstancePin,
)


class ReviewTestCase(ActivationTestCase):
    """The device type SW-24 shows the revision of the test case; the reviewer can view every object."""

    def setUp(self):
        super().setUp()
        for model in REVIEWER_MODELS:
            grant(self.user, model, "view")
        grant(self.user, Association, "view", "add", "change")
        self.association = Association.objects.create(
            device_type=self.device_type, revision=self.revision
        )
        self.module_revision = self.import_files(MODULE_FILES, "ACME/NM-2X")
        self.module_type = ModuleType.objects.create(
            manufacturer=self.manufacturer, model="NM-2X"
        )

    def reviewed(self, *pairs):
        response = self.review(*pairs)
        self.assertEqual(response.status_code, 200)
        return response, response.context["review"]

    def install(self, device=None, name="Network Module", position="1"):
        """Install an NM-2X module, with the interface that its connector binds."""
        device = device or self.device
        module = Module.objects.create(
            device=device,
            module_bay=ModuleBay.objects.create(
                device=device, name=name, position=position
            ),
            module_type=self.module_type,
        )
        Interface.objects.create(
            device=device,
            module=module,
            name=f"TenGigabitEthernet1/{position}/1",
            type="10gbase-x-sfpp",
        )
        return module

    def activate_module_type(self):
        return Association.objects.create(
            module_type=self.module_type, revision=self.module_revision
        )


class ImpactTests(ReviewTestCase):
    def test_the_review_lists_every_device_of_the_type_and_the_pinned_ones_as_unchanged(
        self,
    ):
        sw2, sw3 = self.make_device("sw2"), self.make_device("sw3")
        InstancePin.objects.create(device=sw3, revision=self.revision)
        newer = self.import_files(NEWER)

        response, reviewed = self.reviewed((newer, self.device_type))

        [target] = reviewed.targets
        self.assertEqual(target.instances, (self.device, sw2))
        self.assertEqual(target.pinned, (sw3,))
        for device in (self.device, sw2, sw3):
            self.assertContains(response, device.get_absolute_url())
        self.assertContains(response, "Pinned, unchanged", count=1)
        self.assertContains(response, "Shows the candidate", count=2)
        self.assertTrue(token_of(response))

    def test_the_review_lists_every_module_of_the_type_and_the_devices_that_hold_them(
        self,
    ):
        self.activate_module_type()
        sw2 = self.make_device("sw2")
        first, second = self.install(), self.install(sw2)
        InstancePin.objects.create(module=second, revision=self.module_revision)
        newer = self.import_files(NEW_MODULE, "ACME/NM-2X")

        response, reviewed = self.reviewed((newer, self.module_type))

        [target] = reviewed.targets
        self.assertEqual(target.instances, (first,))
        self.assertEqual(target.pinned, (second,))
        self.assertContains(response, "ACME NM-2X in Network Module", count=2)
        self.assertContains(response, sw2.get_absolute_url())
        self.assertContains(response, "Pinned, unchanged", count=1)
        self.assertEqual(reviewed.blockers, ())

    def test_a_review_writes_nothing(self):
        newer = self.import_files(NEWER)

        self.reviewed((newer, self.device_type))

        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)


class StoppingOverrideTests(ReviewTestCase):
    def test_the_overrides_of_the_active_key_stop_applying_under_another_key(self):
        override = BindingOverride.objects.create(
            device_type=self.device_type,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[{"component": "interface", "name": "eth1"}],
        )
        sw2 = self.make_device("sw2")
        InstancePin.objects.create(device=sw2, revision=self.revision)
        # The pinned device keeps its revision, so its override keeps applying.
        BindingOverride.objects.create(
            device=sw2,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[],
        )
        other_key = self.import_files(
            {
                **FILES,
                "manifest.yaml": MANIFEST.replace("key: acme/sw-24", "key: acme/other"),
            },
            "ACME/OTHER",
        )

        response, reviewed = self.reviewed((other_key, self.device_type))

        [target] = reviewed.targets
        self.assertEqual(target.stopping_overrides, (override,))
        self.assertContains(
            response, "These binding overrides name acme/sw-24 and stop applying"
        )
        self.assertContains(response, override.get_absolute_url())
        self.assertEqual(reviewed.blockers, ())
        self.assertTrue(token_of(response))

    def test_no_override_stops_applying_under_a_revision_of_the_same_key(self):
        BindingOverride.objects.create(
            device_type=self.device_type,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=[{"component": "interface", "name": "eth1"}],
        )

        response, reviewed = self.reviewed((self.import_files(NEWER), self.device_type))

        self.assertEqual(reviewed.targets[0].stopping_overrides, ())
        self.assertNotContains(response, "stop applying")


class BlockerTests(ReviewTestCase):
    def assertBlocked(self, response, reviewed):
        self.assertTrue(reviewed.blockers)
        self.assertIsNone(token_of(response))
        self.assertContains(response, "remove each blocker first")
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)

    def test_a_candidate_that_breaks_a_resolved_binding_shows_the_component(self):
        broken = self.import_files(BROKEN)

        response, reviewed = self.reviewed((broken, self.device_type))

        self.assertBlocked(response, reviewed)
        self.assertEqual(
            reviewed.blockers,
            (
                review.Blocker(
                    self.device, "sw1", "con-eth1", "Interface eth1", review.NOT_BOUND
                ),
            ),
        )
        self.assertContains(response, "Interface eth1")
        # The new name finds no component either.
        self.assertIn(
            review.Unresolved(self.device, "sw1", "con-eth1", "Interface eth9"),
            reviewed.unresolved,
        )

    def test_a_removed_connector_blocks(self):
        candidate = self.import_files(WITHOUT_ETH1)

        response, reviewed = self.reviewed((candidate, self.device_type))

        self.assertBlocked(response, reviewed)
        [blocker] = reviewed.blockers
        self.assertEqual(
            (blocker.connector, blocker.component, blocker.reason),
            (
                "con-eth1",
                "Interface eth1",
                "The candidate has no connector con-eth1.",
            ),
        )

    def test_a_module_that_the_candidate_does_not_draw_blocks(self):
        self.activate_module_type()
        self.install()
        candidate = self.import_files(WITHOUT_BAY)

        response, reviewed = self.reviewed((candidate, self.device_type))

        self.assertBlocked(response, reviewed)
        self.assertEqual(
            [(b.part, b.connector, b.component, b.reason) for b in reviewed.blockers],
            [
                (
                    "ACME NM-2X in Network Module",
                    "con-p1",
                    "Interface TenGigabitEthernet1/1/1",
                    review.NOT_ARTWORK,
                ),
                ("ACME NM-2X in Network Module", None, None, review.NOT_DRAWN),
            ],
        )

    def test_a_bay_size_change_is_a_warning_only(self):
        self.activate_module_type()
        self.install()
        candidate = self.import_files(SMALLER_BAY)

        response, reviewed = self.reviewed((candidate, self.device_type))

        self.assertEqual(reviewed.blockers, ())
        [warning] = reviewed.warnings
        self.assertIn("but the bay bay-nm is 90 × 30 mm", warning.message)
        self.assertContains(response, "but the bay bay-nm is 90 × 30 mm")
        self.assertEqual(self.post(candidate).status_code, 302)
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, candidate)

    def test_a_binding_that_is_missing_now_does_not_block_an_unrelated_activation(
        self,
    ):
        self.psu.delete()
        self.install()

        response, reviewed = self.reviewed((self.module_revision, self.module_type))

        self.assertEqual(reviewed.blockers, ())
        self.assertIn(
            review.Unresolved(self.device, "sw1", "con-psu", "Power port PSU1"),
            reviewed.unresolved,
        )
        self.assertContains(response, "Power port PSU1")
        self.assertEqual(
            self.post(self.module_revision, self.module_type).status_code, 302
        )
        self.assertTrue(
            Association.objects.filter(module_type=self.module_type).exists()
        )

    def test_a_first_activation_over_a_schematic_does_not_block_on_bays_that_it_does_not_draw(
        self,
    ):
        self.association.delete()
        self.activate_module_type()
        self.install()
        # The schematic draws this module in a slot; the artwork has no bay for it.
        self.install(name="Bay 2", position="2")

        response, reviewed = self.reviewed((self.revision, self.device_type))

        self.assertEqual(reviewed.blockers, ())
        self.assertTrue(token_of(response))


class DepthTests(TestCase):
    """A chassis with network modules, and a transceiver in the slot of the first network module."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.admin = User.objects.create_user(username="admin", is_superuser=True)
        self.user = User.objects.create_user(username="operator")
        for model in (
            Device,
            Interface,
            PowerPort,
            DeviceType,
            DefinitionRevision,
            *REVIEWER_MODELS,
        ):
            grant(self.user, model, "view")
        grant(self.user, Association, "view", "add", "change")
        self.client.force_login(self.user)

    def candidate(self, files):
        [imported] = import_bundle(
            self.admin, "nm.zip", io.BytesIO(bundle(definition("ACME/NM-2X", files)))
        )
        return imported.revision

    def test_a_module_that_is_not_drawn_at_depth_two_blocks(self):
        inventory = Inventory(self.admin)
        candidate = self.candidate(NM_WITHOUT_SLOT)

        response = self.client.get(review_url((candidate, inventory.nm_type)))

        blockers = response.context["review"].blockers
        self.assertIn(
            review.Blocker(
                inventory.device, "ACME SFP-10G in Slot 1", None, None, review.NOT_DRAWN
            ),
            blockers,
        )
        self.assertIsNone(token_of(response))

    def test_a_first_module_activation_compares_from_an_empty_state_below_the_module(
        self,
    ):
        inventory = Inventory(self.admin, activate=False)
        revisions = import_definitions(self.admin)
        activate(
            self.admin,
            (revisions["acme/ch-4"], inventory.device_type),
            (revisions["acme/sfp-10g"], inventory.sfp_type),
        )
        candidate = self.candidate(NM_WITHOUT_SLOT)

        response = self.client.get(review_url((candidate, inventory.nm_type)))

        # The schematic of the network module draws the transceiver in a slot; the candidate does not.
        self.assertEqual(response.context["review"].blockers, ())
        self.assertTrue(token_of(response))


class PinTests(ReviewTestCase):
    def test_an_instance_pin_stays_after_a_type_association_update(self):
        sw2 = self.make_device("sw2")
        pins.save(self.admin, InstancePin(device=self.device, revision=self.revision))
        newer = self.import_files(NEWER)

        self.assertEqual(self.post(newer).status_code, 302)

        self.assertEqual(
            active_revisions([self.device, sw2]),
            {self.device.pk: self.revision, sw2.pk: newer},
        )

    def test_an_instance_pin_stays_after_a_module_association_update(self):
        self.activate_module_type()
        first, second = self.install(), self.install(name="Bay 2", position="2")
        pins.save(self.admin, InstancePin(module=first, revision=self.module_revision))
        newer = self.import_files(NEW_MODULE, "ACME/NM-2X")

        self.assertEqual(self.post(newer, self.module_type).status_code, 302)

        self.assertEqual(
            active_revisions([first, second]),
            {first.pk: self.module_revision, second.pk: newer},
        )


class AtomicityTests(ReviewTestCase):
    def test_several_associations_apply_all_or_none(self):
        newer = self.import_files(NEWER)
        self.install()
        pairs = ((newer, self.device_type), (self.module_revision, self.module_type))
        url = review_url(*pairs)
        token = token_of(self.client.get(url))
        # The change of the device type association fails its saved-row check.
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.association"
        ).update(actions=["view", "add"])
        grant(
            self.user, Association, "change", constraints={"revision": self.revision.pk}
        )

        self.assertEqual(self.client.post(url, {"token": token}).status_code, 403)

        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)
        self.assertFalse(
            Association.objects.filter(module_type=self.module_type).exists()
        )

        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.association"
        ).update(actions=["view", "add", "change"], constraints=None)
        response = self.client.post(url, {"token": token_of(self.client.get(url))})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(str(m) for m in get_messages(response.wsgi_request)),
            [
                f"ACME NM-2X now shows {self.module_revision}.",
                f"ACME SW-24 now shows {newer}.",
            ],
        )
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, newer)
        self.assertTrue(
            Association.objects.filter(module_type=self.module_type).exists()
        )


class RollbackTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.newer = self.import_files(NEWER)
        self.assertEqual(self.post(self.newer).status_code, 302)
        self.page_url = reverse(ASSOCIATION_URL, args=[self.association.pk])

    def rollback_url(self):
        return self.client.get(self.page_url).context["rollback_url"]

    def test_rollback_restores_the_prior_revision_through_the_review(self):
        page = self.client.get(self.page_url)
        self.assertContains(page, self.revision.digest)
        url = page.context["rollback_url"]
        self.assertContains(page, "Roll back")
        self.assertContains(page, url.replace("&", "&amp;"))

        review_page = self.client.get(url)
        [target] = review_page.context["review"].targets
        self.assertEqual(target.pair.revision, self.revision)
        response = self.client.post(url, {"token": token_of(review_page)})

        self.assertRedirects(response, self.page_url)
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)
        # The previous revision is now the one that the rollback replaced.
        self.assertEqual(
            self.client.get(self.page_url).context["previous_revision"], self.newer
        )

    def test_the_roll_back_button_needs_change_permission(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.association"
        ).update(actions=["view"])

        page = self.client.get(self.page_url)

        self.assertContains(page, self.revision.digest)
        self.assertIsNone(page.context["rollback_url"])
        self.assertNotContains(page, "Roll back")

    def test_no_rollback_after_a_first_activation(self):
        self.association.delete()
        self.assertEqual(self.post().status_code, 302)
        page = self.client.get(
            reverse(ASSOCIATION_URL, args=[Association.objects.get().pk])
        )

        self.assertIsNone(page.context["previous_revision"])
        self.assertNotContains(page, "Roll back")

    def test_no_rollback_to_a_revision_that_was_deleted(self):
        self.revision.delete()

        self.assertIsNone(self.rollback_url())


class VisibilityTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.newer = self.import_files(NEWER)
        self.sw2 = self.make_device("sw2")
        self.install()

    def assertHidden(self, response):
        self.assertEqual(response.status_code, 200)
        reviewed = response.context["review"]
        self.assertTrue(reviewed.hidden)
        # A hidden review holds no names and no counts.
        self.assertEqual(
            (reviewed.targets, reviewed.blockers, reviewed.unresolved), ((), (), ())
        )
        self.assertIsNone(reviewed.token)
        content = response.content.decode()
        self.assertIn(str(review.HIDDEN), content)
        for name in ("sw1", "sw2", "eth1", "Network Module", "ACME SW-24"):
            self.assertNotIn(name, content.split('<div class="page-body', 1)[1])

    def test_a_reviewer_who_cannot_view_every_affected_object_sees_only_the_message(
        self,
    ):
        for model, constraints in (
            (Device, {"name": "sw1"}),
            (Interface, {"name": "eth2"}),
            (Module, {"pk": 0}),
            (ModuleBay, {"pk": 0}),
            (PowerPort, {"name": "other"}),
        ):
            with self.subTest(model=model.__name__):
                permission = ObjectPermission.objects.get(
                    name__startswith=f"{model._meta.label_lower} view"
                )
                permission.constraints = constraints
                permission.save()
                try:
                    self.assertHidden(self.review((self.newer, self.device_type)))
                finally:
                    permission.constraints = None
                    permission.save()

    def test_an_activation_after_a_loss_of_visibility_is_refused(self):
        url = review_url((self.newer, self.device_type))
        token = token_of(self.client.get(url))
        self.assertTrue(token)
        ObjectPermission.objects.filter(name__startswith="dcim.device view").update(
            constraints={"name": "sw1"}
        )

        response = self.client.post(url, {"token": token})

        self.assertContains(response, REVIEW_AGAIN, status_code=409)
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)


class TokenTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.newer = self.import_files(NEWER)
        self.url = review_url((self.newer, self.device_type))
        self.token = token_of(self.client.get(self.url))
        self.assertTrue(self.token)

    def assertRefused(self, response, message):
        self.assertContains(response, message, status_code=409)
        self.assertContains(response, "No association changed.", status_code=409)
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, self.revision)

    def apply(self, token=None):
        return self.client.post(
            self.url, {"token": self.token if token is None else token}
        )

    def test_an_expired_token_is_refused(self):
        later = time.time() + review.REVIEW_MAX_AGE_SECONDS + 1
        # The signer reads the clock: the only way to age a token in a test.
        with mock.patch("django.core.signing.time.time", return_value=later):
            self.assertRefused(self.apply(), review.EXPIRED)

    def test_a_token_of_another_user_is_refused(self):
        other = User.objects.create_user(username="other", is_superuser=True)
        self.client.force_login(other)

        self.assertRefused(self.apply(), review.OTHER_USER)

    def test_a_token_of_another_connection_is_refused(self):
        payload = signing.loads(self.token, salt=review.SIGNING_SALT)
        payload["connection"] = "branch_1"
        token = signing.dumps(payload, salt=review.SIGNING_SALT)

        self.assertRefused(self.apply(token), review.OTHER_BRANCH)

    def test_a_tampered_token_is_refused(self):
        tampered = (
            self.token[:-2] + ("A" if self.token[-2] != "A" else "B") + self.token[-1]
        )

        self.assertRefused(self.apply(tampered), review.INVALID_TOKEN)

    def test_a_type_change_of_a_bound_component_after_the_review_needs_a_new_review(
        self,
    ):
        # A binding does not find a virtual interface.
        Interface.objects.filter(pk=self.eth1.pk).update(type="virtual")

        self.assertRefused(self.apply(), REVIEW_AGAIN)

    def test_a_rename_of_a_pinned_device_after_the_review_needs_a_new_review(self):
        sw2 = self.make_device("sw2")
        InstancePin.objects.create(device=sw2, revision=self.revision)
        self.token = token_of(self.client.get(self.url))
        self.assertTrue(self.token)

        Device.objects.filter(pk=sw2.pk).update(name="sw2-renamed")

        self.assertRefused(self.apply(), REVIEW_AGAIN)

    def test_a_change_after_the_review_needs_a_new_review(self):
        changes = {
            "a component rename": lambda: Interface.objects.filter(
                pk=self.eth1.pk
            ).update(name="eth5"),
            "a new device of the type": lambda: self.make_device("sw9"),
            "a new pin": lambda: InstancePin.objects.create(
                device=self.device, revision=self.revision
            ),
            "a new override": lambda: BindingOverride.objects.create(
                device_type=self.device_type,
                definition=self.revision.definition,
                connector_key="con-psu",
                bindings=[],
            ),
            "an override change": lambda: BindingOverride.objects.filter(
                connector_key="con-psu"
            ).update(bindings=[{"component": "powerport", "name": "PSU2"}]),
            "a pin change": lambda: InstancePin.objects.update(revision=self.newer),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.token = token_of(self.client.get(self.url))
                self.assertTrue(self.token)
                change()

                self.assertRefused(self.apply(), REVIEW_AGAIN)


class EntryTests(ReviewTestCase):
    def test_the_activate_form_of_a_revision_opens_the_review_of_several_types(self):
        other_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="SW-24B", slug="sw-24b"
        )
        newer = self.import_files(NEWER)
        form = self.client.get(reverse(ACTIVATE_URL, args=[newer.pk]))
        self.assertContains(form, f'action="{reverse(REVIEW_URL)}"')
        self.assertContains(form, 'method="get"')
        self.assertContains(form, f'name="r{newer.pk}-device_type"')

        query = {
            "revision": newer.pk,
            f"r{newer.pk}-device_type": [self.device_type.pk, other_type.pk],
            "return_url": newer.get_absolute_url(),
        }
        response = self.client.get(reverse(REVIEW_URL), query)

        self.assertEqual(
            [t.pair.hardware_type for t in response.context["review"].targets],
            [self.device_type, other_type],
        )
        applied = self.client.post(
            f"{reverse(REVIEW_URL)}?{response.wsgi_request.GET.urlencode()}",
            {"token": token_of(response), "return_url": newer.get_absolute_url()},
        )
        self.assertRedirects(applied, newer.get_absolute_url())
        self.assertEqual(
            set(Association.objects.values_list("device_type", "revision")),
            {(self.device_type.pk, newer.pk), (other_type.pk, newer.pk)},
        )

    def test_a_type_selected_twice_is_rejected(self):
        newer = self.import_files(NEWER)

        response = self.review(
            (newer, self.device_type), (self.revision, self.device_type)
        )

        self.assertContains(response, "Select each type once.", status_code=400)

    def test_a_review_holds_a_bounded_number_of_types(self):
        types = [
            DeviceType.objects.create(
                manufacturer=self.manufacturer, model=f"T{n}", slug=f"t{n}"
            )
            for n in range(review.MAX_PAIRS + 1)
        ]

        response = self.review(*((self.revision, t) for t in types))

        self.assertContains(
            response,
            f"Select at most {review.MAX_PAIRS} types in one review.",
            status_code=400,
        )

    def test_the_bulk_review_update_pairs_each_association_with_the_newest_revision(
        self,
    ):
        self.activate_module_type()
        newer = self.import_files(NEWER)
        listing = self.client.get(reverse(LIST_URL))
        update_url = reverse("plugins:netbox_device_view:association_review_update")
        self.assertContains(listing, "Review update")
        self.assertContains(listing, update_url)
        module_association = Association.objects.get(module_type=self.module_type)

        response = self.client.post(
            update_url,
            {
                "pk": [self.association.pk, module_association.pk],
                "return_url": reverse(LIST_URL),
            },
            follow=True,
        )

        reviewed = response.context["review"]
        self.assertEqual(
            [(t.pair.hardware_type, t.pair.revision) for t in reviewed.targets],
            [(self.device_type, newer)],
        )
        # The module association already has the newest revision of its definition.
        self.assertContains(
            response,
            "These associations already have the newest revision of their definition",
        )
        self.assertContains(response, module_association.get_absolute_url())
        applied = self.client.post(
            response.redirect_chain[-1][0], {"token": reviewed.token}
        )
        self.assertRedirects(applied, reverse(LIST_URL))
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, newer)

    def update(self, data, follow=False):
        return self.client.post(
            reverse("plugins:netbox_device_view:association_review_update"),
            {**data, "return_url": reverse(LIST_URL)},
            follow=follow,
        )

    def only_view(self, revision_pk):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.definitionrevision"
        ).update(constraints={"pk": revision_pk})

    def test_an_update_is_not_an_older_revision_that_the_user_can_view(self):
        newer = self.import_files(NEWER)
        Association.objects.filter(pk=self.association.pk).update(revision=newer)
        self.only_view(self.revision.pk)

        response = self.update({"pk": [self.association.pk]}, follow=True)

        # The association has the newest revision of its definition.
        self.assertIsNone(response.context.get("review"))
        self.assertContains(
            response,
            "These associations already have the newest revision of their definition",
        )

    def test_an_update_to_a_revision_that_the_user_cannot_view_is_rejected(self):
        self.import_files(NEWER)
        self.only_view(0)

        response = self.update({"pk": [self.association.pk]}, follow=True)

        self.assertContains(response, review.NOT_VIEWABLE, status_code=400)
        self.assertNotContains(
            response, "already have the newest revision", status_code=400
        )

    def test_an_update_of_too_many_associations_is_rejected_before_any_review_work(
        self,
    ):
        for n in range(review.MAX_PAIRS):
            Association.objects.create(
                device_type=DeviceType.objects.create(
                    manufacturer=self.manufacturer, model=f"T{n}", slug=f"t{n}"
                ),
                revision=self.revision,
            )
        pks = list(Association.objects.values_list("pk", flat=True))
        for data in ({"pk": pks}, {"_all": "on"}):
            with self.subTest(data=list(data)):
                with CaptureQueriesContext(connection) as queries:
                    response = self.update(data)

                self.assertRedirects(
                    response, reverse(LIST_URL), fetch_redirect_response=False
                )
                self.assertIn(
                    f"Select at most {review.MAX_PAIRS} associations in one review.",
                    [str(m) for m in get_messages(response.wsgi_request)],
                )
                sql = [query["sql"] for query in queries]
                self.assertEqual(
                    [q for q in sql if q.startswith('SELECT "netbox_device_view_')], []
                )

    def test_an_update_reads_the_newest_revisions_in_one_query(self):
        self.activate_module_type()
        newer = self.import_files(NEWER)
        self.import_files(NEW_MODULE, "ACME/NM-2X")

        with CaptureQueriesContext(connection) as queries:
            response = self.update(
                {"pk": list(Association.objects.values_list("pk", flat=True))}
            )

        revision_queries = [
            q["sql"]
            for q in queries
            if 'FROM "netbox_device_view_definitionrevision"' in q["sql"]
        ]
        self.assertEqual(len(revision_queries), 1, revision_queries)
        self.assertIn(f"r{newer.pk}-device_type", response.url)

    def test_the_bulk_review_update_needs_add_or_change_permission(self):
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.association"
        ).update(actions=["view"])

        listing = self.client.get(reverse(LIST_URL))

        self.assertNotContains(listing, "Review update")
        response = self.client.post(
            reverse("plugins:netbox_device_view:association_review_update"),
            {"pk": [self.association.pk]},
        )
        self.assertEqual(response.status_code, 403)
