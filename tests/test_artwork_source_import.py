"""Artwork import from a synchronized native Data Source, through the UI and the API."""

import io
import shutil
import tempfile
import uuid
from pathlib import Path

import django_rq
from core.choices import DataSourceStatusChoices
from core.models import DataFile, DataSource
from django.conf import settings
from django.db.models import ProtectedError
from django.test import override_settings
from django.urls import reverse
from users.models import User

from netbox_device_view.artwork import capture
from netbox_device_view.artwork.bundle import read_bundle
from netbox_device_view.artwork.limits import MAX_SVG_BYTES
from netbox_device_view.artwork.storage import read_blob
from netbox_device_view.models import (
    DefinitionRevision,
    RevisionImport,
    SourceSyncRun,
    VisualDefinition,
)

from .artwork_samples import DEVICE_FILES, FRONT_SVG, bundle, definition
from .test_artwork_import import DEVICE_DIR, ArtworkTestCase, expected_digest

SOURCE_IMPORT_URL = "plugins:netbox_device_view:definitionrevision_source_import"
API_SOURCE_IMPORT = "plugins-api:netbox_device_view-api:artwork_source_import"


def private_sync_queue(test):
    """Send Data Source sync jobs to a private RQ queue that the test removes."""
    name = f"netbox_device_view-test-{uuid.uuid4().hex}"
    test.enterContext(
        override_settings(
            RQ_QUEUES={**settings.RQ_QUEUES, name: settings.RQ_QUEUES["default"]},
            QUEUE_MAPPINGS={"datasource": name},
        )
    )
    queue = django_rq.get_queue(name)
    test.addCleanup(queue.delete, delete_jobs=True)
    return queue


class SourceTestCase(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.write(definition(DEVICE_DIR, DEVICE_FILES))
        self.source = self.create_source("Artwork", self.root)
        self.grant(DefinitionRevision, "add", "view")
        self.grant(DataSource, "view")
        self.grant(DataFile, "view")

    def create_source(self, name, root):
        return DataSource.objects.create(
            name=name, type="local", source_url=f"file://{root}", enabled=True
        )

    def write(self, entries, root=None):
        for path, content in entries.items():
            target = (root or self.root) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def sync(self, source=None):
        source = source or self.source
        source.sync()
        source.refresh_from_db()
        return source

    def import_source(self, source=None, prefix=""):
        source = source or self.source
        return self.client.post(
            reverse(SOURCE_IMPORT_URL), {"data_source": source.pk, "prefix": prefix}
        )


class SourceImportTests(SourceTestCase):
    def test_import_from_a_synced_source_creates_a_candidate_revision(self):
        self.sync()

        response = self.import_source(prefix="artwork/")

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.digest, expected_digest(DEVICE_FILES))
        self.assertEqual(revision.definition.key, "acme/sw-24")
        self.assertEqual(revision.definition.owner, self.source)
        event = RevisionImport.objects.get()
        self.assertEqual(event.data_source, self.source)
        self.assertEqual(event.source_name, "Artwork")
        self.assertEqual(event.path, DEVICE_DIR)
        self.assertEqual(event.last_synced, self.source.last_synced)
        self.assertEqual(event.user, self.user)
        self.assertEqual(self.source.status, DataSourceStatusChoices.COMPLETED)
        self.assertFalse(VisualDefinition.objects.filter(owner=None).exists())

    def test_a_sync_changes_no_revision(self):
        self.sync()
        self.assertFalse(DefinitionRevision.objects.exists())
        self.import_source()
        revision = DefinitionRevision.objects.get()
        files = list(revision.files.values_list("path", "blob"))

        self.write({f"{DEVICE_DIR}/front.svg": FRONT_SVG.replace("#333", "#222")})
        self.sync()

        self.assertEqual(list(DefinitionRevision.objects.all()), [revision])
        self.assertEqual(list(revision.files.values_list("path", "blob")), files)
        self.assertEqual(RevisionImport.objects.count(), 1)

    def test_the_same_files_give_the_same_revision_as_an_offline_bundle(self):
        entries = definition(DEVICE_DIR, DEVICE_FILES)
        [offline] = read_bundle(io.BytesIO(bundle(entries)))
        self.sync()

        self.import_source()

        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.digest, offline.digest)
        self.assertEqual(revision.manifest, offline.manifest.data)
        self.assertEqual(
            {f.path: read_blob(f.blob) for f in revision.files.select_related("blob")},
            {path: content.encode() for path, content in DEVICE_FILES.items()},
        )

    def test_a_missing_dependency_fails_before_import(self):
        (self.root / DEVICE_DIR / "rear.svg").unlink()
        self.sync()

        response = self.import_source()

        self.assertContains(
            response,
            f"{DEVICE_DIR}/rear.svg: missing: the manifest names this file.",
            status_code=400,
        )
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(VisualDefinition.objects.exists())

    def test_a_file_that_changed_during_the_sync_fails_before_import(self):
        self.sync()
        # refresh_from_disk() reads the file twice: once for the hash, once for the data.
        DataFile.objects.filter(path=f"{DEVICE_DIR}/front.svg").update(
            data=FRONT_SVG.replace("#333", "#222").encode()
        )

        response = self.import_source()

        self.assertContains(
            response, f"{DEVICE_DIR}/front.svg: {capture.CHANGED_FILE}", status_code=400
        )
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_the_next_sync_repairs_a_file_that_changed_while_a_sync_read_it(self):
        # The hash matches the file on disk, so NetBox's own sync skips the row.
        torn = {
            "data": {"data": FRONT_SVG.replace("#333", "#222").encode()},
            "size": {"size": len(FRONT_SVG.encode()) + 1},
        }
        for name, fields in torn.items():
            with self.subTest(name):
                self.sync()
                DataFile.objects.filter(path=f"{DEVICE_DIR}/front.svg").update(**fields)
                self.assertContains(
                    self.import_source(),
                    f"{DEVICE_DIR}/front.svg: {capture.CHANGED_FILE}",
                    status_code=400,
                )

                self.sync()

                self.import_source()
                self.assertTrue(DefinitionRevision.objects.exists())
                stored = DataFile.objects.get(path=f"{DEVICE_DIR}/front.svg")
                self.assertEqual(bytes(stored.data), FRONT_SVG.encode())
                DefinitionRevision.objects.all().delete()

    def test_a_sync_leaves_consistent_files_alone(self):
        self.sync()
        before = dict(DataFile.objects.values_list("path", "last_updated"))

        self.sync()

        self.assertEqual(
            dict(DataFile.objects.values_list("path", "last_updated")), before
        )

    def test_a_sync_in_progress_or_a_failed_sync_blocks_the_import(self):
        self.sync()
        for status in (
            DataSourceStatusChoices.SYNCING,
            DataSourceStatusChoices.FAILED,
            DataSourceStatusChoices.QUEUED,
        ):
            with self.subTest(status=status):
                DataSource.objects.filter(pk=self.source.pk).update(status=status)
                response = self.import_source()
                self.assertContains(response, f"status is {status}.", status_code=400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_an_unfinished_guarded_sync_blocks_the_import(self):
        self.sync()
        # A worker that stopped after pre_sync leaves its run unfinished.
        SourceSyncRun.objects.filter(source=self.source).update(finished=None)

        response = self.import_source()

        self.assertContains(response, capture.SYNC_RUNNING, status_code=400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_a_source_synced_before_the_plugin_needs_one_guarded_sync(self):
        self.sync()
        SourceSyncRun.objects.all().delete()

        response = self.import_source()
        self.assertContains(response, capture.NO_GUARDED_SYNC, status_code=400)

        self.sync()
        self.assertContains(self.import_source(), "Imported")

    def test_files_outside_a_definition_directory_fail_the_import(self):
        self.write({"README.md": "Artwork\n"})
        self.sync()

        response = self.import_source()

        self.assertContains(
            response, "README.md: not inside a definition directory.", status_code=400
        )
        self.assertContains(self.import_source(prefix="artwork/"), "Imported")

    def test_an_oversized_file_fails_the_import(self):
        self.write({f"{DEVICE_DIR}/front.svg": "x" * (MAX_SVG_BYTES + 1)})
        self.sync()

        response = self.import_source()

        self.assertContains(
            response, f"larger than {MAX_SVG_BYTES} bytes.", status_code=400
        )

    def test_a_file_larger_than_its_recorded_size_is_not_read(self):
        self.sync()
        DataFile.objects.filter(path=f"{DEVICE_DIR}/front.svg").update(
            data=b"x" * (MAX_SVG_BYTES + 1)
        )

        response = self.import_source()

        self.assertContains(
            response,
            f"{DEVICE_DIR}/front.svg: larger than {MAX_SVG_BYTES} bytes.",
            status_code=400,
        )

    def test_the_page_shows_the_source_state_and_a_check_for_updates_button(self):
        queue = private_sync_queue(self)
        self.grant(DataSource, "sync")
        page = self.client.get(
            reverse(SOURCE_IMPORT_URL), {"data_source": self.source.pk}
        )
        self.assertContains(page, "None since the plugin was installed")
        sync_url = reverse("core:datasource_sync", kwargs={"pk": self.source.pk})
        self.assertContains(page, f'action="{sync_url}?return_url=')

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(page.context["sync_url"])

        self.assertRedirects(
            response,
            f"{reverse(SOURCE_IMPORT_URL)}?data_source={self.source.pk}&prefix=",
            fetch_redirect_response=False,
        )
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, DataSourceStatusChoices.QUEUED)
        self.assertEqual(queue.count, 1)

    def test_check_for_updates_follows_the_sync_permission_constraints(self):
        self.grant(DataSource, "sync", constraints={"name": "Other"})
        page = self.client.get(
            reverse(SOURCE_IMPORT_URL), {"data_source": self.source.pk}
        )
        self.assertNotContains(page, "mdi-sync")

    def test_check_for_updates_needs_the_native_sync_permission(self):
        page = self.client.get(
            reverse(SOURCE_IMPORT_URL), {"data_source": self.source.pk}
        )
        self.assertNotContains(page, "mdi-sync")
        self.assertEqual(self.client.post(page.context["sync_url"]).status_code, 403)

    def test_the_revision_page_shows_source_provenance_without_a_commit(self):
        self.sync()
        self.import_source()
        revision = DefinitionRevision.objects.get()

        page = self.client.get(revision.get_absolute_url())

        self.assertContains(
            page, f'<a href="{self.source.get_absolute_url()}">Artwork</a>'
        )
        self.assertContains(page, DEVICE_DIR)
        self.assertContains(page, "Key owner")
        self.assertNotContains(page, "ommit")


class SourceIdTests(SourceTestCase):
    def test_a_source_id_with_a_non_decimal_digit_is_a_form_error(self):
        url = reverse(SOURCE_IMPORT_URL)
        self.assertEqual(
            self.client.get(url, {"data_source": "\u00b2"}).status_code, 200
        )

        response = self.client.post(url, {"data_source": "\u00b2", "prefix": ""})

        self.assertEqual(response.status_code, 400)
        self.assertIn("data_source", response.context["form"].errors)


class OwnershipTests(SourceTestCase):
    def setUp(self):
        super().setUp()
        self.other_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.other_root)
        self.write(definition(DEVICE_DIR, DEVICE_FILES), root=self.other_root)
        self.other = self.create_source("Fork", self.other_root)

    def test_the_first_source_owns_the_key(self):
        self.sync()
        self.import_source()
        self.sync(self.other)

        response = self.import_source(self.other)

        self.assertContains(
            response,
            "The key acme/sw-24 belongs to the Data Source Artwork.",
            status_code=400,
        )
        self.assertEqual(RevisionImport.objects.count(), 1)

    def test_an_offline_upload_cannot_add_to_a_source_key(self):
        self.sync()
        self.import_source()

        response = self.upload(bundle(definition(DEVICE_DIR, DEVICE_FILES)))

        self.assertContains(
            response,
            "The key acme/sw-24 belongs to the Data Source Artwork.",
            status_code=400,
        )

    def test_a_source_cannot_add_to_an_offline_key(self):
        self.upload()
        self.sync()

        response = self.import_source()

        self.assertContains(
            response,
            "The key acme/sw-24 belongs to the offline uploads.",
            status_code=400,
        )
        self.assertEqual(VisualDefinition.objects.get().owner, None)

    def test_a_source_that_owns_revisions_cannot_be_deleted(self):
        self.sync()
        self.import_source()
        with self.assertRaises(ProtectedError):
            self.source.delete()

        DefinitionRevision.objects.get().delete()
        self.source.delete()

        self.assertFalse(VisualDefinition.objects.exists())


class SourcePermissionTests(SourceTestCase):
    def test_hidden_files_block_the_import(self):
        self.user = User.objects.create_user(username="limited")
        self.client.force_login(self.user)
        self.grant(DefinitionRevision, "add")
        self.grant(DataSource, "view")
        self.grant(DataFile, "view", constraints={"path__endswith": ".yaml"})
        self.sync()

        response = self.import_source()

        self.assertContains(response, capture.HIDDEN_FILES, status_code=400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_a_source_the_user_cannot_view_is_not_offered(self):
        self.user = User.objects.create_user(username="limited")
        self.client.force_login(self.user)
        self.grant(DefinitionRevision, "add")
        self.grant(DataFile, "view")
        self.sync()

        response = self.import_source()

        self.assertEqual(response.status_code, 400)
        self.assertIn("data_source", response.context["form"].errors)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_the_import_needs_the_add_permission(self):
        self.user = User.objects.create_user(username="viewer")
        self.client.force_login(self.user)
        self.grant(DataSource, "view")
        self.grant(DataFile, "view")
        self.sync()

        self.assertEqual(self.import_source().status_code, 403)


class SourceApiTests(SourceTestCase):
    def test_api_import_creates_the_revision_with_source_provenance(self):
        self.sync()

        response = self.api().post(
            reverse(API_SOURCE_IMPORT),
            {"data_source": self.source.pk, "prefix": "artwork"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        [result] = response.json()["revisions"]
        self.assertTrue(result["created"])
        [event] = result["revision"]["imports"]
        self.assertEqual(event["source_name"], "Artwork")
        self.assertEqual(event["data_source"]["id"], self.source.pk)
        self.assertEqual(event["path"], DEVICE_DIR)
        self.assertIsNotNone(event["last_synced"])

    def test_a_body_that_is_not_an_object_is_a_bad_request(self):
        response = self.api().post(reverse(API_SOURCE_IMPORT), [1], format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_ui_and_api_report_the_same_errors(self):
        (self.root / DEVICE_DIR / "rear.svg").unlink()
        self.sync()

        ui = self.import_source()
        api = self.api().post(
            reverse(API_SOURCE_IMPORT), {"data_source": self.source.pk}, format="json"
        )

        self.assertEqual(api.status_code, 400)
        messages = [f"{e['path']}: {e['message']}" for e in api.json()["errors"]]
        self.assertEqual(messages, [str(issue) for issue in ui.context["issues"]])
