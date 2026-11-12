"""Explicit activation of a definition revision for a device type, through the review on the plugin pages."""

import io
import shutil
import tempfile
import threading
import time

from core.models import ObjectChange, ObjectType
from dcim.models import DeviceType, Manufacturer, ModuleType
from django.contrib.messages import get_messages
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from users.constants import TOKEN_PREFIX
from users.models import ObjectPermission, Token, User

from netbox_device_view import forms
from netbox_device_view.artwork import review
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import Association, DefinitionRevision, DeviceView

from .artwork_samples import DEVICE_FILES, MODULE_FILES, bundle, definition
from .permissions import grant
from .test_artwork_device_view import FILES, GRID, ArtworkTestCase, faces
from .test_artwork_key_transfer import waiting_row_locks

ACTIVATE_URL = "plugins:netbox_device_view:definitionrevision_activate"
REVIEW_URL = "plugins:netbox_device_view:association_review"
REVISION_URL = "plugins:netbox_device_view:definitionrevision"
ASSOCIATION_URL = "plugins:netbox_device_view:association"
LIST_URL = "plugins:netbox_device_view:association_list"
DELETE_URL = "plugins:netbox_device_view:association_delete"
# A revision of the key of FILES that changes no connector and no bay.
NEWER = {**FILES, "front.svg": FILES["front.svg"].replace("#555", "#666")}
REVIEW_AGAIN = "Review the activation again."


def review_url(*pairs, **params):
    """Return the URL of the review of (revision, type) pairs."""
    return forms.review_url([(t, revision) for revision, t in pairs], **params)


def token_of(response):
    """Return the token of a review page, or None."""
    reviewed = (response.context or {}).get("review")
    return None if reviewed is None else reviewed.token


def activate_through_review(client, *pairs, **params):
    """Review (revision, type) pairs on the review page and activate them; return the response of the activation."""
    url = review_url(*pairs, **params)
    token = token_of(client.get(url))
    assert token, "the review has no token"
    return client.post(url, {"token": token, **params})


class ActivationTestCase(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.get(username="admin")
        grant(self.user, DefinitionRevision, "view")
        grant(self.user, DeviceType, "view")

    def import_files(self, files, directory="ACME/SW-24"):
        [imported] = import_bundle(
            self.admin, "more.zip", io.BytesIO(bundle(definition(directory, files)))
        )
        return imported.revision

    def review(self, *pairs, **params):
        """Open the review of (revision, type) pairs; the device type of the test case by default."""
        return self.client.get(
            review_url(*(pairs or [(self.revision, self.device_type)]), **params)
        )

    def post(self, revision=None, device_type=None, **params):
        """Review one pair and activate it; return the response of the activation."""
        return activate_through_review(
            self.client,
            (revision or self.revision, device_type or self.device_type),
            **params,
        )


class ActivationTests(ActivationTestCase):
    def test_activation_needs_add_or_change_permission_on_the_association(self):
        self.assertEqual(
            self.client.get(reverse(ACTIVATE_URL, args=[self.revision.pk])).status_code,
            403,
        )
        self.assertEqual(self.review().status_code, 403)
        page = self.client.get(reverse(REVISION_URL, args=[self.revision.pk]))
        self.assertNotContains(page, reverse(ACTIVATE_URL, args=[self.revision.pk]))
        self.assertFalse(Association.objects.exists())

    def test_a_first_activation_needs_add_and_shows_the_artwork(self):
        grant(self.user, Association, "view", "add")
        page = self.client.get(reverse(REVISION_URL, args=[self.revision.pk]))
        self.assertContains(page, reverse(ACTIVATE_URL, args=[self.revision.pk]))

        response = self.post()

        association = Association.objects.get()
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            [f"ACME SW-24 now shows {self.revision}."],
        )
        self.assertRedirects(response, reverse(LIST_URL))
        self.assertEqual(association.device_type, self.device_type)
        self.assertEqual(association.revision, self.revision)
        self.assertEqual(len(faces(self.tab())), 2)
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(Association)
        )
        self.assertEqual(change.action, "create")
        self.assertEqual(change.user, self.user)

    def test_the_activation_returns_to_a_safe_return_url(self):
        grant(self.user, Association, "add")
        revision_page = reverse(REVISION_URL, args=[self.revision.pk])

        response = self.post(return_url=revision_page)

        self.assertRedirects(response, revision_page)
        self.assertTrue(Association.objects.exists())

    def test_an_unsafe_return_url_returns_to_the_association_list(self):
        grant(self.user, Association, "add")
        unsafe = "https://example.com/elsewhere"
        self.assertEqual(
            self.review(return_url=unsafe).context["return_url"], reverse(LIST_URL)
        )

        response = self.post(return_url=unsafe)

        self.assertRedirects(response, reverse(LIST_URL), fetch_redirect_response=False)

    def test_a_later_activation_needs_change_permission(self):
        grant(self.user, Association, "view", "add")
        self.post()
        newer = self.import_files(NEWER)

        response = self.review((newer, self.device_type))
        self.assertContains(response, review.NEEDS_CHANGE)
        self.assertIsNone(token_of(response))
        self.assertEqual(Association.objects.get().revision, self.revision)

        grant(self.user, Association, "change")
        self.assertEqual(self.post(newer).status_code, 302)
        self.assertEqual(Association.objects.get().revision, newer)
        change = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(Association)
        ).latest("time")
        self.assertEqual(change.action, "update")
        self.assertEqual(change.prechange_data["revision"], self.revision.pk)
        self.assertEqual(change.postchange_data["revision"], newer.pk)

    def test_the_same_revision_again_needs_change_permission_and_changes_nothing(self):
        grant(self.user, Association, "view", "add")
        self.post()
        self.assertContains(self.review(), review.NEEDS_CHANGE)
        grant(self.user, Association, "change")

        response = self.review()

        self.assertContains(response, "None: the type already shows this revision")
        self.assertContains(response, "Nothing to activate")
        self.assertIsNone(token_of(response))
        self.assertEqual(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(Association)
            ).count(),
            1,
        )

    def test_change_constraints_apply_to_the_saved_association(self):
        grant(self.user, Association, "view", "add")
        self.post()
        newer = self.import_files(NEWER)
        # The user may change the association only while it keeps the first revision.
        grant(
            self.user, Association, "change", constraints={"revision": self.revision.pk}
        )

        self.assertEqual(self.post(newer).status_code, 403)

        self.assertEqual(Association.objects.get().revision, self.revision)

    def test_permission_constraints_apply_to_the_activated_type(self):
        grant(
            self.user, Association, "add", constraints={"device_type__model": "Other"}
        )
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(Association.objects.exists())

    def test_a_revision_or_a_device_type_that_the_user_cannot_view_is_rejected(self):
        grant(self.user, Association, "view", "add")
        ObjectPermission.objects.filter(name__startswith="dcim.devicetype").update(
            constraints={"model": "Other"}
        )
        self.assertEqual(self.review().status_code, 400)
        ObjectPermission.objects.filter(
            name__startswith="netbox_device_view.definitionrevision"
        ).delete()
        self.assertEqual(self.review().status_code, 400)
        self.assertEqual(
            self.client.get(reverse(ACTIVATE_URL, args=[self.revision.pk])).status_code,
            404,
        )
        self.assertFalse(Association.objects.exists())


class AssociationPageTests(ActivationTestCase):
    def setUp(self):
        super().setUp()
        self.association = Association.objects.create(
            device_type=self.device_type, revision=self.revision
        )
        grant(self.user, Association, "view", "delete")

    def test_the_list_and_the_detail_page_show_the_association(self):
        listing = self.client.get(reverse(LIST_URL))
        self.assertContains(listing, "SW-24")
        self.assertContains(listing, self.revision.digest[:10])
        detail = self.client.get(reverse(ASSOCIATION_URL, args=[self.association.pk]))
        self.assertContains(detail, self.revision.digest)
        self.assertContains(detail, self.device_type.get_absolute_url())

    def test_the_list_links_the_module_type_of_a_module_association(self):
        module_type = ModuleType.objects.create(
            manufacturer=self.device_type.manufacturer, model="NM-2X"
        )
        Association.objects.create(
            module_type=module_type,
            revision=self.import_files(MODULE_FILES, "ACME/NM-2X"),
        )
        listing = self.client.get(reverse(LIST_URL))
        self.assertContains(
            listing,
            f'<a href="{module_type.get_absolute_url()}">ACME NM-2X</a>',
            html=True,
        )

    def test_the_pages_name_the_manufacturer_of_the_type(self):
        other_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="Other", slug="other"),
            model="SW-24",
            slug="other-sw-24",
        )
        other = Association.objects.create(
            device_type=other_type, revision=self.revision
        )
        listing = self.client.get(reverse(LIST_URL))
        for device_type in (self.device_type, other_type):
            self.assertContains(
                listing,
                f'<a href="{device_type.get_absolute_url()}">{device_type.full_name}</a>',
                html=True,
            )
        detail = self.client.get(reverse(ASSOCIATION_URL, args=[other.pk]))
        self.assertContains(
            detail,
            f'<a href="{other_type.get_absolute_url()}">Other SW-24</a>',
            html=True,
        )
        confirmation = self.client.get(reverse(DELETE_URL, args=[other.pk]))
        self.assertContains(confirmation, f"Other SW-24 → {self.revision}")

    def test_an_active_revision_cannot_be_deleted(self):
        grant(self.user, DefinitionRevision, "delete")
        response = self.client.post(
            reverse(
                "plugins:netbox_device_view:definitionrevision_delete",
                args=[self.revision.pk],
            ),
            {"confirm": True},
            follow=True,
        )
        self.assertContains(response, "Unable to delete")
        self.assertTrue(DefinitionRevision.objects.filter(pk=self.revision.pk).exists())

    def test_the_api_answers_a_delete_of_an_active_revision_with_409(self):
        grant(self.user, DefinitionRevision, "delete")
        token = Token.objects.create(user=self.user)
        response = self.client.delete(
            reverse(
                "plugins-api:netbox_device_view-api:definitionrevision-detail",
                args=[self.revision.pk],
            ),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("1 dependent objects were found", response.json()["detail"])
        self.assertTrue(DefinitionRevision.objects.filter(pk=self.revision.pk).exists())

    def test_deleting_the_association_returns_the_device_to_its_layout(self):
        DeviceView.objects.create(device_type=self.device_type, grid_template_area=GRID)
        grant(self.user, DeviceView, "view")
        self.assertNotIn('class="deviceview area', self.tab())
        self.client.post(
            reverse(DELETE_URL, args=[self.association.pk]), {"confirm": True}
        )
        self.assertFalse(Association.objects.exists())
        content = self.tab()
        self.assertEqual(faces(content), [])
        self.assertIn('class="deviceview area', content)


class ActivationRaceTests(TransactionTestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(username="admin", is_superuser=True)
        self.client.force_login(self.user)
        [imported] = import_bundle(
            self.user,
            "sw24.zip",
            io.BytesIO(bundle(definition("ACME/SW-24", DEVICE_FILES))),
        )
        self.revision = imported.revision
        self.device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )

    def test_a_first_activation_that_races_another_asks_to_review_again(self):
        url = review_url((self.revision, self.device_type))
        token = token_of(self.client.get(url))
        result = {}

        def run():
            try:
                result["response"] = self.client.post(url, {"token": token})
            finally:
                connection.close()

        with transaction.atomic():
            Association.objects.create(
                device_type=self.device_type, revision=self.revision
            )
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 10
            while waiting_row_locks() < 1 and thread.is_alive():
                self.assertLess(
                    time.monotonic(), deadline, "the activation did not wait"
                )
                time.sleep(0.05)
        thread.join(30)

        self.assertContains(result["response"], REVIEW_AGAIN, status_code=409)
        self.assertEqual(Association.objects.count(), 1)
