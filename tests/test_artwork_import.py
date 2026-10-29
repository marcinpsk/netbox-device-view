"""Offline bundle upload through the UI and the API, as a candidate definition revision."""

import hashlib
import shutil
import tempfile
from pathlib import Path

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape
from rest_framework.test import APIClient
from users.models import ObjectPermission, User

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

from .artwork_samples import (
    DEVICE_FILES,
    FRONT_SVG,
    MODULE_FILES,
    bundle,
    definition,
)

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

    def grant(self, model, *actions, constraints=None, user=None):
        permission = ObjectPermission.objects.create(
            name=f"{model._meta.model_name}-{ObjectPermission.objects.count()}",
            actions=list(actions),
            constraints=constraints,
        )
        permission.users.add(user or self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))

    def upload(self, data=None, name="sw24.zip"):
        return self.client.post(
            reverse(IMPORT_URL),
            {"bundle": SimpleUploadedFile(name, bundle() if data is None else data)},
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
        self.grant(DefinitionRevision, "view", "add")

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
            (event.user, event.user_name, event.bundle_name, event.path),
            (self.user, "importer", "sw24.zip", DEVICE_DIR),
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

    def test_identical_content_returns_the_existing_revision_with_a_new_import_event(
        self,
    ):
        self.upload()
        moved = bundle(definition("other/place", DEVICE_FILES))
        response = self.upload(moved, name="again.zip")
        self.assertContains(response, "Already imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(
            list(revision.imports.values_list("bundle_name", "path")),
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
        self.upload()
        self.client.force_login(admin)
        self.assertEqual(self.device_view_card(tab), before)

    def device_view_card(self, url):
        content = self.client.get(url).content.decode()
        start = content.index('<div class="card-body htmx-container">')
        return content[start : content.index("</footer>", start)]


class ApiTests(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        self.grant(DefinitionRevision, "view", "add", "delete")

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
            [i["bundle_name"] for i in detail["imports"]], ["api.zip", "api.zip"]
        )

    def test_a_reimport_does_not_show_provenance_the_user_cannot_view(self):
        self.post(bundle())
        self.user = User.objects.create_user(username="adder")
        self.grant(DefinitionRevision, "add")
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
        self.grant(DefinitionRevision, "change")
        self.post(bundle())
        url = reverse(
            "plugins-api:netbox_device_view-api:definitionrevision-detail",
            args=[DefinitionRevision.objects.get().pk],
        )
        for method in ("put", "patch"):
            with self.subTest(method=method):
                response = getattr(self.api(), method)(url, {"digest": "0" * 64})
                self.assertEqual(response.status_code, 405)

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


class ImmutabilityTests(ArtworkTestCase):
    def test_stored_records_refuse_every_save(self):
        self.grant(DefinitionRevision, "view", "add")
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

    def test_ui_delete_removes_the_revision(self):
        self.grant(DefinitionRevision, "view", "add", "delete")
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
        self.grant(Device, "view")
        self.grant(DefinitionRevision, "view")
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
        self.grant(DefinitionRevision, "view", "add")
        self.assertEqual(self.client.get(reverse(IMPORT_URL)).status_code, 200)
        self.assertContains(self.client.get(reverse(LIST_URL)), reverse(IMPORT_URL))

    def test_add_constraints_apply_to_the_imported_revisions(self):
        self.grant(DefinitionRevision, "view")
        self.grant(
            DefinitionRevision, "add", constraints={"definition__key": "acme/other"}
        )
        self.assertEqual(self.upload().status_code, 403)
        self.assertFalse(DefinitionRevision.objects.exists())
        self.assertFalse(VisualDefinition.objects.exists())
