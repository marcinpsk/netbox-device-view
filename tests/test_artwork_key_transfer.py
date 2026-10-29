"""Transfer of a definition key to another source, through the UI and the API."""

import io
import shutil
import tempfile
import threading
import time

from core.models import DataSource, ObjectChange, ObjectType
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from users.models import User

from netbox_device_view.artwork.errors import ArtworkError
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import DefinitionRevision, VisualDefinition

from .artwork_samples import DEVICE_FILES, FRONT_SVG, bundle, definition
from .test_artwork_import import DEVICE_DIR
from .test_artwork_source_import import SourceTestCase

EDIT_URL = "plugins:netbox_device_view:visualdefinition_edit"
DETAIL_URL = "plugins:netbox_device_view:visualdefinition"
LIST_URL = "plugins:netbox_device_view:visualdefinition_list"
CHANGELOG_URL = "plugins:netbox_device_view:visualdefinition_changelog"
REVISION_URL = "plugins:netbox_device_view:definitionrevision"
API_DETAIL = "plugins-api:netbox_device_view-api:visualdefinition-detail"
API_LIST = "plugins-api:netbox_device_view-api:visualdefinition-list"

CHANGED_FILES = {**DEVICE_FILES, "front.svg": FRONT_SVG.replace("#333", "#222")}
SOURCE_KEY = "The key acme/sw-24 belongs to the Data Source Artwork."
OFFLINE_KEY = "The key acme/sw-24 belongs to the offline uploads."


class TransferTestCase(SourceTestCase):
    def setUp(self):
        super().setUp()
        self.grant(VisualDefinition, "view", "change")
        self.upload()
        self.visual = VisualDefinition.objects.get()

    def transfer(self, owner):
        return self.client.post(
            reverse(EDIT_URL, args=[self.visual.pk]),
            {"key_owner": owner.pk if owner else ""},
        )

    def patch(self, owner):
        return self.api().patch(
            reverse(API_DETAIL, args=[self.visual.pk]),
            {"key_owner": owner.pk if owner else None},
            format="json",
        )

    def limited_user(self, *grants):
        self.user = User.objects.create_user(username="limited")
        self.client.force_login(self.user)
        for model, actions, constraints in grants:
            self.grant(model, *actions, constraints=constraints)

    def owner(self):
        self.visual.refresh_from_db()
        return self.visual.key_owner


class UiTransferTests(TransferTestCase):
    def test_a_transfer_moves_the_key_to_the_data_source(self):
        response = self.transfer(self.source)

        self.assertRedirects(response, reverse(DETAIL_URL, args=[self.visual.pk]))
        self.assertEqual(self.owner(), self.source)
        self.assertContains(
            self.upload(bundle(definition(DEVICE_DIR, CHANGED_FILES))),
            SOURCE_KEY,
            status_code=400,
        )
        self.write(definition(DEVICE_DIR, CHANGED_FILES))
        self.sync()
        self.assertContains(self.import_source(), "Imported")
        self.assertEqual(self.visual.revisions.count(), 2)

    def test_a_transfer_with_no_owner_returns_the_key_to_the_offline_uploads(self):
        self.transfer(self.source)

        response = self.transfer(None)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.owner())
        self.sync()
        self.assertContains(self.import_source(), OFFLINE_KEY, status_code=400)
        self.assertEqual(
            self.upload(bundle(definition(DEVICE_DIR, CHANGED_FILES))).status_code,
            200,
        )
        self.assertEqual(self.visual.revisions.count(), 2)

    def test_the_changelog_records_the_transfer(self):
        self.grant(ObjectChange, "view")

        self.transfer(self.source)

        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(VisualDefinition),
            changed_object_id=self.visual.pk,
            action="update",
        )
        self.assertIsNone(change.prechange_data["key_owner"])
        self.assertEqual(change.postchange_data["key_owner"], self.source.pk)
        self.assertEqual(change.user, self.user)
        page = self.client.get(reverse(CHANGELOG_URL, args=[self.visual.pk]))
        self.assertContains(page, change.get_absolute_url())

    def test_the_form_only_changes_the_owner(self):
        self.client.post(
            reverse(EDIT_URL, args=[self.visual.pk]),
            {"key_owner": self.source.pk, "key": "acme/other", "kind": "module"},
        )

        self.visual.refresh_from_db()
        self.assertEqual(
            (self.visual.key, self.visual.kind, self.visual.key_owner),
            ("acme/sw-24", "device", self.source),
        )


class DefinitionPageTests(TransferTestCase):
    def test_the_pages_show_the_key_its_owner_and_its_revisions(self):
        revision = DefinitionRevision.objects.get()
        self.transfer(self.source)

        listing = self.client.get(reverse(LIST_URL))
        detail = self.client.get(reverse(DETAIL_URL, args=[self.visual.pk]))
        revision_page = self.client.get(reverse(REVISION_URL, args=[revision.pk]))

        self.assertContains(listing, self.visual.get_absolute_url())
        self.assertContains(listing, "Artwork")
        self.assertContains(detail, self.source.get_absolute_url())
        self.assertContains(detail, revision.get_absolute_url())
        self.assertContains(detail, reverse(EDIT_URL, args=[self.visual.pk]))
        self.assertContains(revision_page, self.visual.get_absolute_url())
        # NetBox 4.5+ shows object.owner (a users.Owner) in the page header.
        self.assertNotContains(detail, 'title="Owner"')

    def test_every_page_names_the_offline_uploads_the_same_way(self):
        revision = DefinitionRevision.objects.get()

        pages = [
            self.client.get(reverse(LIST_URL)),
            self.client.get(reverse(DETAIL_URL, args=[self.visual.pk])),
            self.client.get(reverse(REVISION_URL, args=[revision.pk])),
        ]
        edit = self.client.get(reverse(EDIT_URL, args=[self.visual.pk]))

        for page in pages:
            self.assertContains(page, "Offline uploads")
        self.assertContains(edit, "Leave empty for the offline uploads.")


class KeyOwnerFilterTests(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.owned = VisualDefinition.objects.create(
            key="acme/owned", kind="device", key_owner=self.source
        )

    def listed_keys(self, query):
        response = self.client.get(reverse(LIST_URL), query)
        return [row.record.key for row in response.context["table"].page.object_list]

    def api_keys(self, query):
        response = self.api().get(reverse(API_LIST), query)
        return [item["key"] for item in response.json()["results"]]

    def test_the_list_filters_by_the_key_owner(self):
        self.assertEqual(
            self.listed_keys({"key_owner_id": self.source.pk}), ["acme/owned"]
        )
        self.assertEqual(self.listed_keys({"key_owner_id": "null"}), ["acme/sw-24"])

    def test_the_api_filters_by_the_key_owner(self):
        self.assertEqual(
            self.api_keys({"key_owner_id": self.source.pk}), ["acme/owned"]
        )
        self.assertEqual(self.api_keys({"key_owner_id": "null"}), ["acme/sw-24"])

    def test_the_filter_form_offers_the_offline_uploads(self):
        response = self.client.get(reverse(LIST_URL))

        self.assertContains(response, 'name="key_owner_id"')
        self.assertContains(response, 'data-null-option="Offline uploads"')


class TransferPermissionTests(TransferTestCase):
    def test_a_transfer_needs_the_change_permission(self):
        self.limited_user(
            (VisualDefinition, ["view"], None), (DataSource, ["view"], None)
        )

        self.assertEqual(self.transfer(self.source).status_code, 403)
        self.assertEqual(self.patch(self.source).status_code, 403)
        self.assertIsNone(self.owner())

    def test_a_definition_outside_the_constraints_cannot_be_transferred(self):
        self.limited_user(
            (VisualDefinition, ["view", "change"], {"key": "acme/other"}),
            (DataSource, ["view"], None),
        )

        self.assertEqual(self.transfer(self.source).status_code, 404)
        self.assertEqual(self.patch(self.source).status_code, 404)
        self.assertIsNone(self.owner())

    def test_a_transfer_must_keep_the_definition_inside_the_constraints(self):
        self.limited_user(
            (VisualDefinition, ["view", "change"], {"key_owner__isnull": True}),
            (DataSource, ["view"], None),
        )

        ui = self.transfer(self.source)
        api = self.patch(self.source)

        self.assertEqual(ui.status_code, 200)
        self.assertTrue(ui.context["form"].non_field_errors())
        self.assertEqual(api.status_code, 403)
        self.assertIsNone(self.owner())

    def test_a_source_the_user_cannot_view_is_not_a_valid_owner(self):
        self.limited_user((VisualDefinition, ["view", "change"], None))

        ui = self.transfer(self.source)
        api = self.patch(self.source)

        self.assertEqual(ui.status_code, 200)
        self.assertIn("key_owner", ui.context["form"].errors)
        self.assertEqual(api.status_code, 400, api.content)
        self.assertIn("key_owner", api.json())
        self.assertIsNone(self.owner())


class ApiTransferTests(TransferTestCase):
    def test_a_patch_transfers_the_key_and_back(self):
        response = self.patch(self.source)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["key_owner"]["id"], self.source.pk)
        self.assertEqual(self.owner(), self.source)
        self.assertEqual(self.patch(None).json()["key_owner"], None)
        self.assertIsNone(self.owner())

    def test_after_an_api_transfer_only_the_new_owner_can_import(self):
        self.patch(self.source)

        self.assertContains(
            self.upload(bundle(definition(DEVICE_DIR, CHANGED_FILES))),
            SOURCE_KEY,
            status_code=400,
        )
        self.write(definition(DEVICE_DIR, CHANGED_FILES))
        self.sync()
        self.assertContains(self.import_source(), "Imported")
        self.assertEqual(self.visual.revisions.count(), 2)
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(VisualDefinition),
            action="update",
        )
        self.assertEqual(change.postchange_data["key_owner"], self.source.pk)

    def test_a_put_must_name_the_owner(self):
        response = self.api().put(
            reverse(API_DETAIL, args=[self.visual.pk]), {}, format="json"
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("key_owner", response.json())

    def test_a_bulk_patch_follows_the_same_rules(self):
        hidden = self.create_source("Hidden", self.root)
        self.limited_user(
            (VisualDefinition, ["view", "change"], None),
            (DataSource, ["view"], {"name": "Artwork"}),
        )
        url = reverse(API_LIST)

        refused = self.api().patch(
            url, [{"id": self.visual.pk, "key_owner": hidden.pk}], format="json"
        )
        accepted = self.api().patch(
            url, [{"id": self.visual.pk, "key_owner": self.source.pk}], format="json"
        )

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(self.owner(), self.source)

    def test_the_key_and_the_kind_are_read_only(self):
        response = self.api().patch(
            reverse(API_DETAIL, args=[self.visual.pk]),
            {"key": "acme/other", "kind": "module"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.visual.refresh_from_db()
        self.assertEqual((self.visual.key, self.visual.kind), ("acme/sw-24", "device"))

    def test_definitions_cannot_be_created_or_deleted(self):
        self.grant(VisualDefinition, "add", "delete")
        client = self.api()

        created = client.post(reverse(API_LIST), {"key": "acme/new"}, format="json")
        deleted = client.delete(reverse(API_DETAIL, args=[self.visual.pk]))

        self.assertEqual((created.status_code, deleted.status_code), (405, 405))
        self.assertEqual(VisualDefinition.objects.count(), 1)

    def test_the_list_shows_the_key_the_kind_and_the_owner(self):
        self.patch(self.source)

        [item] = self.api().get(reverse(API_LIST)).json()["results"]

        self.assertEqual(
            (item["key"], item["kind"], item["key_owner"]["id"]),
            ("acme/sw-24", "device", self.source.pk),
        )


def waiting_row_locks():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted"
        )
        return cursor.fetchone()[0]


class TransferRaceTests(TransactionTestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="admin", is_superuser=True)
        self.source = DataSource.objects.create(
            name="Artwork", type="local", source_url="file:///nonexistent"
        )

    def test_an_import_that_waits_for_a_transfer_sees_the_new_owner(self):
        import_bundle(self.user, "first.zip", io.BytesIO(bundle()))
        changed = bundle(definition(DEVICE_DIR, CHANGED_FILES))
        errors = []

        def run_import():
            try:
                import_bundle(self.user, "second.zip", io.BytesIO(changed))
            except ArtworkError as exc:
                errors.extend(str(issue) for issue in exc.issues)
            finally:
                connection.close()

        with transaction.atomic():
            visual = VisualDefinition.objects.get()
            visual.key_owner = self.source
            visual.save()
            thread = threading.Thread(target=run_import)
            thread.start()
            deadline = time.monotonic() + 10
            while waiting_row_locks() < 1 and thread.is_alive():
                self.assertLess(time.monotonic(), deadline, "the import did not wait")
                time.sleep(0.05)
        thread.join(30)

        self.assertEqual(len(errors), 1)
        self.assertIn(SOURCE_KEY, errors[0])
        self.assertEqual(DefinitionRevision.objects.count(), 1)
