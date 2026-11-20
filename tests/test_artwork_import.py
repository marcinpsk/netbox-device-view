"""Offline bundle upload through the UI and the API, as a candidate definition revision."""

import hashlib
import shutil
import tempfile
from pathlib import Path

from core.models import ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape
from extras.validators import CustomValidator
from rest_framework.test import APIClient
from users.models import User

from netbox_device_view.artwork.storage import DamagedBlob, read_blob
from netbox_device_view.models import (
    ArtworkBlob,
    DefinitionRevision,
    DeviceView,
    ImmutableModelError,
    RevisionFile,
    RevisionImport,
    VisualDefinition,
)
from netbox_device_view.views import ArtworkBundleImportView

from .artwork_samples import (
    DEVICE_FILES,
    FRONT_SVG,
    MODULE_FILES,
    bundle,
    definition,
)
from .permissions import grant

IMPORT_URL = "plugins:netbox_device_view:definitionrevision_import"
LIST_URL = "plugins:netbox_device_view:definitionrevision_list"
API_UPLOAD = "plugins-api:netbox_device_view-api:artwork_bundle_upload"
API_LIST = "plugins-api:netbox_device_view-api:definitionrevision-list"
DEVICE_DIR = "artwork/ACME/SW-24"
UNSAFE = definition(
    DEVICE_DIR,
    dict(DEVICE_FILES, **{"front.svg": FRONT_SVG.replace("</svg>", "<script/></svg>")}),
)


def expected_digest(files):
    lines = "".join(
        f"{path}\0{hashlib.sha256(content.encode()).hexdigest()}\n"
        for path, content in sorted(files.items())
    )
    return hashlib.sha256(lines.encode()).hexdigest()


class ArtworkTestCase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media)
        media_override = override_settings(MEDIA_ROOT=self.media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="importer")
        self.client.force_login(self.user)

    def upload(self, data=None, name="sw24.zip", follow=True):
        return self.client.post(
            reverse(IMPORT_URL),
            {"bundle": SimpleUploadedFile(name, bundle() if data is None else data)},
            follow=follow,
        )

    def api(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def stored_files(self):
        return sorted(
            str(path.relative_to(self.media))
            for path in Path(self.media).rglob("*")
            if path.is_file()
        )


class UploadTests(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, DefinitionRevision, "view", "add")

    def test_a_valid_bundle_becomes_a_candidate_revision(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.digest, expected_digest(DEVICE_FILES))
        self.assertEqual(revision.definition.key, "acme/sw-24")
        self.assertEqual(revision.definition.kind, "device")
        self.assertEqual(revision.manifest["license"], "MIT")
        self.assertContains(response, revision.get_absolute_url())
        self.assertContains(response, "Imported")
        files = {f.path: f.blob for f in revision.files.all()}
        self.assertEqual(set(files), set(DEVICE_FILES))
        for path, content in DEVICE_FILES.items():
            with self.subTest(path=path):
                self.assertEqual(read_blob(files[path]), content.encode())
                stored = Path(self.media) / files[path].name
                self.assertEqual(stored.read_bytes(), content.encode())
        event = revision.imports.get()
        self.assertEqual(
            (event.user, event.user_name, event.source_name, event.path),
            (self.user, "importer", "sw24.zip", DEVICE_DIR),
        )

    def test_an_import_redirects_to_a_receipt_that_a_reload_does_not_repeat(self):
        response = self.upload(follow=False)
        self.assertEqual(response.status_code, 302)
        receipt = response["Location"]
        self.assertTrue(receipt.startswith(f"{reverse(IMPORT_URL)}?receipt="), receipt)
        revision = DefinitionRevision.objects.get()
        for _reload in range(2):
            page = self.client.get(receipt)
            self.assertContains(page, "Imported revisions")
            self.assertContains(page, revision.get_absolute_url())
            self.assertContains(page, revision.digest)
        self.assertEqual(RevisionImport.objects.count(), 1)
        for url in (reverse(IMPORT_URL), f"{reverse(IMPORT_URL)}?receipt=other"):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), "Imported revisions")

    def test_the_receipt_redirect_does_not_use_the_request_path(self):
        request = RequestFactory().post(
            reverse(IMPORT_URL),
            {"bundle": SimpleUploadedFile("sw24.zip", bundle())},
            PATH_INFO="//evil.example/",
        )
        request.user, request.session = self.user, SessionStore()
        response = ArtworkBundleImportView.as_view()(request)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith(f"{reverse(IMPORT_URL)}?receipt="),
            response["Location"],
        )

    def test_list_and_detail_pages_show_identity_provenance_and_licensing(self):
        self.upload()
        revision = DefinitionRevision.objects.get()
        listing = self.client.get(reverse(LIST_URL))
        self.assertEqual(listing.status_code, 200)
        for text in (
            "acme/sw-24",
            "ACME SW-24",
            "SW-24-AC",
            "MIT",
            revision.digest[:12],
            "importer",
            "sw24.zip",
        ):
            self.assertContains(listing, text)
        detail = self.client.get(revision.get_absolute_url())
        self.assertEqual(detail.status_code, 200)
        for text in (
            revision.digest,
            "acme/sw-24",
            "ACME SW-24 Hardware Guide",
            "Rev B",
            "1.2",
            'href="https://example.com/sw-24.pdf"',
            "MIT",
            "Test Author",
            "sw24.zip",
            DEVICE_DIR,
            "importer",
            "front.svg",
            "rear.svg",
            "manifest.yaml",
            "front, rear",
        ):
            with self.subTest(text=text):
                self.assertContains(detail, text)

    def test_the_change_log_lists_the_creation_within_both_view_restrictions(self):
        self.upload()
        revision = DefinitionRevision.objects.get()
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(DefinitionRevision),
            changed_object_id=revision.pk,
        )
        url = reverse(
            "plugins:netbox_device_view:definitionrevision_changelog",
            args=[revision.pk],
        )
        self.assertNotContains(self.client.get(url), change.get_absolute_url())
        grant(self.user, ObjectChange, "view")
        self.assertContains(self.client.get(url), change.get_absolute_url())
        self.user = User.objects.create_user(username="outsider")
        self.client.force_login(self.user)
        grant(self.user, ObjectChange, "view")
        grant(
            self.user,
            DefinitionRevision,
            "view",
            constraints={"definition__key": "acme/other"},
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_provenance_table_scrolls_in_its_card(self):
        # Without the wrapper, the table widens a 390 px page.
        self.upload()
        detail = self.client.get(DefinitionRevision.objects.get().get_absolute_url())
        self.assertRegex(
            detail.content.decode(),
            r'Provenance</h2>\s*<div class="table-responsive">\s*<table',
        )

    def test_quick_search_filters_the_list(self):
        entries = {
            **definition(DEVICE_DIR, DEVICE_FILES),
            **definition("artwork/ACME/NM-2X", MODULE_FILES),
        }
        self.upload(bundle(entries))
        module = DefinitionRevision.objects.get(definition__key="acme/nm-2x")
        for query in ("nm-2x", "NM-2X", module.digest[:16]):
            with self.subTest(query=query):
                listing = self.client.get(reverse(LIST_URL), {"q": query})
                self.assertContains(listing, module.get_absolute_url())
                self.assertNotContains(listing, "acme/sw-24")

    def test_identical_content_returns_the_existing_revision_with_a_new_import_event(
        self,
    ):
        self.upload()
        moved = bundle(definition("other/place", DEVICE_FILES))
        response = self.upload(moved, name="again.zip")
        self.assertContains(response, "Already imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(
            list(revision.imports.values_list("source_name", "path")),
            [("again.zip", "other/place"), ("sw24.zip", DEVICE_DIR)],
        )
        self.assertEqual(ArtworkBlob.objects.count(), 3)

    def test_a_multi_definition_bundle_imports_every_definition(self):
        entries = {
            **definition(DEVICE_DIR, DEVICE_FILES),
            **definition("artwork/ACME/NM-2X", MODULE_FILES),
        }
        self.upload(bundle(entries))
        self.assertEqual(
            set(VisualDefinition.objects.values_list("key", "kind")),
            {("acme/sw-24", "device"), ("acme/nm-2x", "module")},
        )

    def test_unsafe_input_fails_before_anything_is_stored(self):
        response = self.upload(bundle(UNSAFE))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, f"{DEVICE_DIR}/front.svg:5", status_code=400)
        self.assertContains(response, "script", status_code=400)
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(VisualDefinition.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_a_member_name_that_is_not_utf8_fails(self):
        # zipfile decodes a name with the UTF-8 flag in the directory and again in the local header.
        data = bundle({**definition(DEVICE_DIR, DEVICE_FILES), "é.svg": "x"})
        local = data.replace("é".encode(), b"\xff\xfe", 1)
        for damaged in (data.replace("é".encode(), b"\xff\xfe"), local):
            with self.subTest(everywhere=damaged != local):
                self.assertEqual(self.upload(damaged).status_code, 400)
                response = self.api().post(
                    reverse(API_UPLOAD),
                    {"bundle": SimpleUploadedFile("api.zip", damaged)},
                    format="multipart",
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_one_bad_definition_fails_the_whole_bundle(self):
        entries = {**UNSAFE, **definition("artwork/ACME/NM-2X", MODULE_FILES)}
        self.assertEqual(self.upload(bundle(entries)).status_code, 400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_a_key_cannot_change_its_kind(self):
        self.upload()
        module = dict(MODULE_FILES)
        module["manifest.yaml"] = module["manifest.yaml"].replace(
            "key: acme/nm-2x", "key: acme/sw-24"
        )
        response = self.upload(bundle(definition("m", module)))
        self.assertContains(
            response,
            "m/manifest.yaml:3: The key acme/sw-24 is a device",
            status_code=400,
        )
        self.assertEqual(DefinitionRevision.objects.count(), 1)

    def test_custom_validation_rejects_a_new_revision_before_any_write(self):
        validators = {
            "netbox_device_view.definitionrevision": [
                CustomValidator({"digest": {"neq": expected_digest(DEVICE_FILES)}})
            ]
        }
        with override_settings(CUSTOM_VALIDATORS=validators):
            ui = self.upload()
            api = self.api().post(
                reverse(API_UPLOAD),
                {"bundle": SimpleUploadedFile("api.zip", bundle())},
                format="multipart",
            )
        failed = "Custom validation failed for digest"
        self.assertContains(
            ui, f"{DEVICE_DIR}/manifest.yaml: {failed}", status_code=400
        )
        self.assertEqual(api.status_code, 400, api.content)
        [error] = api.json()["errors"]
        self.assertEqual(error["path"], f"{DEVICE_DIR}/manifest.yaml")
        self.assertIn(failed, error["message"])
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(VisualDefinition.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_custom_validation_does_not_block_the_reuse_of_a_revision(self):
        self.upload()
        validators = {
            "netbox_device_view.definitionrevision": [
                CustomValidator({"digest": {"neq": expected_digest(DEVICE_FILES)}})
            ]
        }
        with override_settings(CUSTOM_VALIDATORS=validators):
            response = self.upload(name="again.zip")
        self.assertContains(response, "Already imported")
        self.assertEqual(DefinitionRevision.objects.get().imports.count(), 2)

    def test_import_repairs_a_damaged_blob(self):
        self.upload()
        blob = RevisionFile.objects.get(path="front.svg").blob
        Path(self.media, blob.name).write_bytes(b"tampered")
        with self.assertRaises(DamagedBlob):
            read_blob(blob)
        self.upload()
        self.assertEqual(read_blob(blob), DEVICE_FILES["front.svg"].encode())

    def test_import_leaves_device_views_unchanged(self):
        manufacturer = Manufacturer.objects.create(name="ACME", slug="acme")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="SW-24", slug="sw-24"
        )
        device = Device.objects.create(
            name="sw1",
            device_type=device_type,
            site=Site.objects.create(name="Site", slug="site"),
            role=DeviceRole.objects.create(name="Role", slug="role"),
        )
        DeviceView.objects.create(
            device_type=device_type,
            grid_template_area='.deviceview.area{grid-template-areas:"eth1";}',
        )
        admin = User.objects.create_user(username="admin", is_superuser=True)
        tab = reverse("dcim:device_deviceview", kwargs={"pk": device.pk})
        self.client.force_login(admin)
        before = self.device_view_card(tab)
        self.client.force_login(self.user)
        self.assertContains(self.upload(), "Imported")
        self.assertEqual(DefinitionRevision.objects.get().definition.key, "acme/sw-24")
        self.client.force_login(admin)
        self.assertEqual(self.device_view_card(tab), before)

    def device_view_card(self, url):
        content = self.client.get(url).content.decode()
        start = content.index('<div class="card-body htmx-container">')
        # End before the footer: its clock changes between the two requests.
        return content[start : content.index("<footer", start)]


class ApiTests(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        grant(self.user, DefinitionRevision, "view", "add", "delete")

    def post(self, data):
        return self.api().post(
            reverse(API_UPLOAD),
            {"bundle": SimpleUploadedFile("api.zip", data)},
            format="multipart",
        )

    def test_api_upload_creates_and_lists_the_revision(self):
        response = self.post(bundle())
        self.assertEqual(response.status_code, 201, response.content)
        [result] = response.json()["revisions"]
        self.assertTrue(result["created"])
        self.assertEqual(result["revision"]["digest"], expected_digest(DEVICE_FILES))
        self.assertEqual(result["revision"]["key"], "acme/sw-24")
        again = self.post(bundle())
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["revisions"][0]["created"])
        listing = self.api().get(reverse(API_LIST)).json()
        self.assertEqual(listing["count"], 1)
        detail = listing["results"][0]
        self.assertEqual(detail["kind"], "device")
        self.assertEqual(detail["manifest"]["hardware"]["sku"], "SW-24-AC")
        self.assertEqual(
            sorted(f["path"] for f in detail["files"]), sorted(DEVICE_FILES)
        )
        self.assertEqual(
            [i["source_name"] for i in detail["imports"]], ["api.zip", "api.zip"]
        )

    def test_the_response_reads_files_and_imports_once_for_all_revisions(self):
        entries = {
            **definition(DEVICE_DIR, DEVICE_FILES),
            **definition("artwork/ACME/NM-2X", MODULE_FILES),
        }
        with CaptureQueriesContext(connection) as queries:
            response = self.post(bundle(entries))
        self.assertEqual(response.status_code, 201, response.content)
        revisions = [r["revision"] for r in response.json()["revisions"]]
        self.assertEqual([len(r["files"]) for r in revisions], [2, 3])
        self.assertEqual([len(r["imports"]) for r in revisions], [1, 1])
        # The import only inserts into these tables; the response selects from them.
        for table in ("revisionfile", "revisionimport"):
            selects = [
                q["sql"]
                for q in queries.captured_queries
                if q["sql"].startswith("SELECT")
                and f'FROM "netbox_device_view_{table}"' in q["sql"]
            ]
            with self.subTest(table=table):
                self.assertEqual(len(selects), 1, selects)

    def test_a_reimport_does_not_show_provenance_the_user_cannot_view(self):
        self.post(bundle())
        self.user = User.objects.create_user(username="adder")
        grant(self.user, DefinitionRevision, "add")
        response = self.post(bundle())
        self.assertEqual(response.status_code, 200)
        [result] = response.json()["revisions"]
        self.assertFalse(result["created"])
        self.assertEqual(
            set(result["revision"]), {"key", "digest"}, "no provenance without view"
        )
        self.assertNotContains(response, "importer")
        self.assertNotContains(response, "api.zip")

    def test_ui_and_api_report_the_same_errors(self):
        response = self.post(bundle(UNSAFE))
        self.assertEqual(response.status_code, 400)
        errors = response.json()["errors"]
        self.assertEqual(errors[0]["path"], f"{DEVICE_DIR}/front.svg")
        self.assertEqual(errors[0]["line"], 5)
        ui = self.upload(bundle(UNSAFE))
        for error in errors:
            self.assertContains(ui, escape(error["message"]), status_code=400)
        self.assertFalse(DefinitionRevision.objects.exists())

    def test_a_revision_cannot_be_changed_through_the_api(self):
        grant(self.user, DefinitionRevision, "change")
        self.post(bundle())
        url = reverse(
            "plugins-api:netbox_device_view-api:definitionrevision-detail",
            args=[DefinitionRevision.objects.get().pk],
        )
        for method in ("put", "patch"):
            with self.subTest(method=method):
                response = getattr(self.api(), method)(url, {"digest": "0" * 64})
                self.assertEqual(response.status_code, 405)

    def test_the_api_has_no_create_or_bulk_write_of_revisions(self):
        grant(self.user, DefinitionRevision, "add", "change", "delete")
        self.post(bundle())
        revision = DefinitionRevision.objects.get()
        url = reverse("plugins-api:netbox_device_view-api:definitionrevision-list")
        body = [{"id": revision.pk, "digest": "0" * 64}]
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.api(), method)(url, body, format="json")
                self.assertEqual(response.status_code, 405)
        self.assertEqual(DefinitionRevision.objects.get().digest, revision.digest)

    def test_api_delete_removes_the_revision_and_keeps_the_blobs(self):
        self.post(bundle())
        revision = DefinitionRevision.objects.get()
        url = reverse(
            "plugins-api:netbox_device_view-api:definitionrevision-detail",
            args=[revision.pk],
        )
        self.assertEqual(self.api().delete(url).status_code, 204)
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(RevisionFile.objects.exists())
        self.assertFalse(RevisionImport.objects.exists())
        self.assertEqual(ArtworkBlob.objects.count(), 3)

    def test_api_delete_reports_a_native_protection_rule_rejection(self):
        self.post(bundle())
        revision = DefinitionRevision.objects.get()
        url = reverse(
            "plugins-api:netbox_device_view-api:definitionrevision-detail",
            args=[revision.pk],
        )
        rules = {
            "netbox_device_view.definitionrevision": [
                CustomValidator({"digest": {"eq": "protected"}})
            ]
        }
        # NetBox 4.5 deletes without a savepoint; this block acts as the request transaction.
        with override_settings(PROTECTION_RULES=rules), transaction.atomic():
            response = self.api().delete(url)
        self.assertEqual(response.status_code, 400)
        self.assertIn("protection rule", response.json()["detail"])
        self.assertTrue(DefinitionRevision.objects.filter(pk=revision.pk).exists())


class ImmutabilityTests(ArtworkTestCase):
    def test_stored_records_refuse_every_save(self):
        grant(self.user, DefinitionRevision, "view", "add")
        self.upload()
        revision = DefinitionRevision.objects.get()
        records = [
            revision,
            revision.files.first(),
            revision.files.first().blob,
            revision.imports.first(),
        ]
        for record in records:
            with (
                self.subTest(record=type(record).__name__),
                self.assertRaises(ImmutableModelError),
            ):
                record.save()
        with self.assertRaises(NoReverseMatch):
            reverse(
                "plugins:netbox_device_view:definitionrevision_edit", args=[revision.pk]
            )

    def test_a_new_record_with_a_stored_key_does_not_overwrite_it(self):
        grant(self.user, DefinitionRevision, "view", "add")
        self.upload()
        stored = ArtworkBlob.objects.first()
        revision = DefinitionRevision.objects.get()
        copies = [
            ArtworkBlob(digest=stored.digest, size=stored.size + 1, extension="png"),
            DefinitionRevision(
                pk=revision.pk,
                definition=revision.definition,
                digest="0" * 64,
                manifest={},
            ),
        ]
        for copy in copies:
            with (
                self.subTest(record=type(copy).__name__),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                copy.save()
        stored_again = ArtworkBlob.objects.get(digest=stored.digest)
        self.assertEqual(
            (stored_again.size, stored_again.extension), (stored.size, stored.extension)
        )
        self.assertEqual(DefinitionRevision.objects.get().digest, revision.digest)

    def test_a_save_cannot_update_fields(self):
        blob = ArtworkBlob(digest="0" * 64, size=1, extension="svg")
        for options in ({"force_update": True}, {"update_fields": ["size"]}):
            with self.subTest(options=options), self.assertRaises(ImmutableModelError):
                blob.save(**options)
        self.assertFalse(ArtworkBlob.objects.filter(digest="0" * 64).exists())

    def test_ui_delete_removes_the_revision(self):
        grant(self.user, DefinitionRevision, "view", "add", "delete")
        self.upload()
        revision = DefinitionRevision.objects.get()
        url = reverse(
            "plugins:netbox_device_view:definitionrevision_delete", args=[revision.pk]
        )
        self.assertEqual(self.client.post(url, {"confirm": True}).status_code, 302)
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertEqual(ArtworkBlob.objects.count(), 3)


class PermissionTests(ArtworkTestCase):
    def test_viewing_a_device_does_not_allow_an_import(self):
        grant(self.user, Device, "view")
        grant(self.user, DefinitionRevision, "view")
        self.assertEqual(self.client.get(reverse(IMPORT_URL)).status_code, 403)
        self.assertEqual(self.upload().status_code, 403)
        response = self.api().post(
            reverse(API_UPLOAD),
            {"bundle": SimpleUploadedFile("api.zip", bundle())},
            format="multipart",
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(self.client.get(reverse(LIST_URL)), reverse(IMPORT_URL))
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_an_importer_sees_the_import_button(self):
        grant(self.user, DefinitionRevision, "view", "add")
        self.assertEqual(self.client.get(reverse(IMPORT_URL)).status_code, 200)
        # The navigation menu links the same URL, so match the list action markup.
        self.assertContains(
            self.client.get(reverse(LIST_URL)),
            f'<a href="{reverse(IMPORT_URL)}" class="btn btn-primary">',
            count=1,
        )

    def test_an_importer_without_view_finds_the_import_in_the_menu(self):
        grant(self.user, DefinitionRevision, "add")
        self.assertEqual(self.client.get(reverse(IMPORT_URL)).status_code, 200)
        self.assertContains(self.client.get(reverse("home")), reverse(IMPORT_URL))

    def test_an_importer_without_view_gets_no_revision_links(self):
        grant(self.user, DefinitionRevision, "add")
        form = self.client.get(reverse(IMPORT_URL))
        self.assertNotContains(form, f'href="{reverse(LIST_URL)}"')
        receipt = self.upload()
        revision = DefinitionRevision.objects.get()
        self.assertNotContains(receipt, f'href="{reverse(LIST_URL)}"')
        self.assertNotContains(receipt, revision.get_absolute_url())
        for text in ("acme/sw-24", revision.digest, "Imported"):
            self.assertContains(receipt, text)

    def test_a_constrained_viewer_gets_no_link_to_a_revision_outside_the_view(self):
        grant(self.user, DefinitionRevision, "add")
        grant(
            self.user,
            DefinitionRevision,
            "view",
            constraints={"definition__key": "acme/other"},
        )
        receipt = self.upload()
        revision = DefinitionRevision.objects.get()
        self.assertContains(receipt, f'href="{reverse(LIST_URL)}"')
        self.assertNotContains(receipt, revision.get_absolute_url())
        self.assertContains(receipt, "acme/sw-24")

    def test_add_constraints_apply_to_the_imported_revisions(self):
        grant(self.user, DefinitionRevision, "view")
        grant(
            self.user,
            DefinitionRevision,
            "add",
            constraints={"definition__key": "acme/other"},
        )
        self.assertEqual(self.upload().status_code, 403)
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(VisualDefinition.objects.exists())
        # A rollback does not remove media files, so the check runs before the writes.
        self.assertEqual(self.stored_files(), [])
