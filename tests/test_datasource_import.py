"""
End-to-end tests for importing layouts from a NetBox Data Source.

They use a real local DataSource synchronized by NetBox itself, real
object permissions, and the plugin's UI request paths.
"""

import hashlib
import re
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

from core.models import DataSource, ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.core import signing
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view import datasource_import
from netbox_device_view.models import DeviceView, RenderMode

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_YAML = EXAMPLES / "yaml" / "Cisco" / "C9300-24T.yaml"
EXAMPLE_PATH = "examples/yaml/Cisco/C9300-24T.yaml"

START_URL = "plugins:netbox_device_view:deviceview_datasource_import"
REVIEW_URL = "plugins:netbox_device_view:deviceview_datasource_import_review"
APPLY_URL = "plugins:netbox_device_view:deviceview_datasource_import_apply"


class DataSourceImportTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manufacturer = Manufacturer.objects.create(name="Cisco", slug="cisco")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="C9300-24T", slug="c9300-24t"
        )
        site = Site.objects.create(name="Site 1", slug="site-1")
        role = DeviceRole.objects.create(name="Switch", slug="switch")
        cls.device = Device.objects.create(
            name="sw1", device_type=cls.device_type, role=role, site=site
        )

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.write_file(EXAMPLE_PATH, EXAMPLE_YAML.read_bytes())
        self.source = DataSource.objects.create(
            name="Layouts", type="local", source_url=f"file://{self.repo}"
        )
        self.source.sync()

        self.user = User.objects.create_user(username="importer")
        self.client.force_login(self.user)
        self.grant(DataSource, "view")
        self.grant(DeviceType, "view")
        self.grant(Device, "view")
        self.grant(DeviceView, "view")
        self.grant(DeviceView, "add")

    def write_file(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def grant(self, model, action, constraints=None):
        perm = ObjectPermission.objects.create(
            name=f"{model._meta.model_name}-{action}-{ObjectPermission.objects.count()}",
            actions=[action],
            constraints=constraints,
        )
        perm.users.add(self.user)
        perm.object_types.add(ObjectType.objects.get_for_model(model))

    def datafile(self, path=EXAMPLE_PATH):
        return self.source.datafiles.get(path=path)

    def review(self, selections, source=None):
        data = {
            "source": (source or self.source).pk,
            "prefix": "examples/",
            "step": "review",
        }
        data["file"] = [datafile.pk for datafile, _ in selections]
        for datafile, device_type in selections:
            data[f"device_type_{datafile.pk}"] = device_type.pk
        return self.client.post(reverse(REVIEW_URL), data)

    def review_token(self, selections):
        response = self.review(selections)
        self.assertEqual(response.status_code, 200)
        token = response.context["review"].token
        self.assertTrue(token, response.context["review"].rows)
        return token

    def apply(self, token):
        return self.client.post(reverse(APPLY_URL), {"token": token})


class ImportWorkflowTests(DataSourceImportTestBase):
    def test_import_one_yaml_layout_end_to_end(self):
        list_page = self.client.get(
            reverse("plugins:netbox_device_view:deviceview_list")
        )
        self.assertContains(list_page, reverse(START_URL))

        start = self.client.get(reverse(START_URL))
        self.assertContains(start, "Layouts")
        self.assertContains(start, 'value="examples/"')

        catalog = self.client.get(
            reverse(START_URL), {"source": self.source.pk, "prefix": "examples/"}
        )
        self.assertContains(catalog, EXAMPLE_PATH)
        datafile = self.datafile()
        self.assertContains(catalog, f'value="{datafile.pk}"')

        review = self.review([(datafile, self.device_type)])
        self.assertContains(review, "C9300-24T")
        self.assertContains(review, "Create")
        token = review.context["review"].token
        self.assertTrue(token)
        self.assertFalse(DeviceView.objects.exists())

        result = self.apply(token)
        self.assertEqual(result.status_code, 200)
        device_view = DeviceView.objects.get(device_type=self.device_type)
        self.assertContains(result, device_view.get_absolute_url())
        self.assertEqual(device_view.yaml_layout, EXAMPLE_YAML.read_text())
        self.assertEqual(device_view.render_mode, RenderMode.CSS)
        self.assertIn("grid-template-areas", device_view.grid_template_area)
        self.assertTrue(
            ObjectChange.objects.filter(
                changed_object_id=device_view.pk, action="create"
            ).exists()
        )

        tab = self.client.get(
            reverse("dcim:device_deviceview", kwargs={"pk": self.device.pk})
        )
        self.assertEqual(tab.status_code, 200)
        first_row = re.search(
            r'grid-template-areas:\s*"([^"]+)"', device_view.grid_template_area
        )
        self.assertContains(tab, first_row.group(1))


class SourceSelectionTests(DataSourceImportTestBase):
    def source_choices(self):
        form = self.client.get(reverse(START_URL)).context["form"]
        return set(form.fields["source"].queryset)

    def test_only_visible_synchronized_enabled_sources_are_offered(self):
        never_synced = DataSource.objects.create(
            name="New", type="local", source_url=f"file://{self.repo}"
        )
        disabled = DataSource.objects.create(
            name="Disabled", type="local", source_url=f"file://{self.repo}"
        )
        disabled.sync()
        DataSource.objects.filter(pk=disabled.pk).update(enabled=False)
        self.assertEqual(self.source_choices(), {self.source})

        catalog = self.client.get(
            reverse(START_URL), {"source": never_synced.pk, "prefix": ""}
        )
        self.assertIsNone(catalog.context.get("page"))

    def test_source_hidden_by_permission_constraint_is_not_offered(self):
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DataSource)
        ).update(constraints={"name": "Other"})
        self.assertEqual(self.source_choices(), set())

    def test_source_parameters_are_not_exposed(self):
        DataSource.objects.filter(pk=self.source.pk).update(
            parameters={"password": "s3cr3t-value"}
        )
        catalog = self.client.get(
            reverse(START_URL), {"source": self.source.pk, "prefix": "examples/"}
        )
        self.assertNotContains(catalog, "s3cr3t-value")
        self.assertNotContains(catalog, str(self.repo))


class CatalogTests(DataSourceImportTestBase):
    def catalog_paths(self, prefix):
        response = self.client.get(
            reverse(START_URL), {"source": self.source.pk, "prefix": prefix}
        )
        self.assertEqual(response.status_code, 200)
        page = response.context.get("page")
        return None if page is None else [datafile.path for datafile in page]

    def test_prefix_matches_path_components(self):
        self.write_file("examples-old/Cisco/C9300-24T.yaml", EXAMPLE_YAML.read_bytes())
        self.write_file("examples/notes.txt", b"not a layout")
        self.source.sync()
        self.assertEqual(self.catalog_paths("examples/"), [EXAMPLE_PATH])
        self.assertEqual(self.catalog_paths("examples"), [EXAMPLE_PATH])
        self.assertEqual(
            self.catalog_paths(""),
            ["examples-old/Cisco/C9300-24T.yaml", EXAMPLE_PATH],
        )

    def test_unsafe_prefixes_are_rejected(self):
        for prefix in ("/etc", "../examples", "examples/../..", "exam\x00ples"):
            with self.subTest(prefix=prefix):
                self.assertIsNone(self.catalog_paths(prefix))

    def test_catalog_does_not_load_file_content(self):
        with CaptureQueriesContext(connection) as queries:
            self.catalog_paths("examples/")
        datafile_queries = [q["sql"] for q in queries if '"core_datafile"' in q["sql"]]
        self.assertTrue(datafile_queries)
        for sql in datafile_queries:
            self.assertNotIn('"core_datafile"."data"', sql)


class ReviewTests(DataSourceImportTestBase):
    def test_review_is_read_only_and_token_holds_no_content(self):
        datafile = self.datafile()
        token = self.review_token([(datafile, self.device_type)])
        self.assertFalse(DeviceView.objects.exists())

        payload = signing.loads(token, salt=datasource_import.SIGNING_SALT)
        self.assertEqual(payload["user"], self.user.pk)
        self.assertEqual(payload["source"], self.source.pk)
        self.assertEqual(
            payload["rows"],
            [
                {
                    "file": datafile.pk,
                    "path": EXAMPLE_PATH,
                    "sha256": hashlib.sha256(EXAMPLE_YAML.read_bytes()).hexdigest(),
                    "device_type": self.device_type.pk,
                    "action": "create",
                    "render_mode": "css",
                }
            ],
        )
        self.assertNotIn(
            str(self.repo), signing.b64_decode(token.split(":")[0].encode()).decode()
        )

    def test_hidden_device_type_cannot_be_selected(self):
        hidden = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="Hidden", slug="hidden"
        )
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DeviceType)
        ).update(constraints={"model": "C9300-24T"})
        response = self.review([(self.datafile(), hidden)])
        self.assertIsNone(response.context["review"])
        self.assertTrue(response.context["target_form"].errors)

    def test_device_type_with_hidden_device_view_is_not_distinguished(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="Other", slug="other"
        )
        DeviceView.objects.create(device_type=self.device_type, grid_template_area="x")
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DeviceView)
        ).update(constraints={"device_type__model": "Other"})
        hidden_target = self.review([(self.datafile(), self.device_type)])
        self.assertNotContains(hidden_target, "grid_template_area")
        rows = hidden_target.context["review"].rows
        self.assertEqual(
            rows[0].error, "The device type is not available as an import target."
        )
        self.assertEqual(hidden_target.context["review"].token, "")

        DeviceView.objects.create(device_type=other, grid_template_area="y")
        visible_target = self.review([(self.datafile(), other)])
        self.assertEqual(visible_target.context["review"].rows[0].error, rows[0].error)

    def test_invalid_yaml_is_reported_without_token(self):
        self.write_file("examples/broken.yaml", b"version: 1\nviews: [not closed")
        self.write_file("examples/latin1.yaml", "meta: caf\xe9".encode("latin-1"))
        self.source.sync()
        for path in ("examples/broken.yaml", "examples/latin1.yaml"):
            with self.subTest(path=path):
                response = self.review([(self.datafile(path), self.device_type)])
                review = response.context["review"]
                self.assertTrue(review.rows[0].error)
                self.assertEqual(review.token, "")
                self.assertNotContains(response, "Apply import")

    def test_utf8_bom_is_accepted(self):
        self.write_file(
            "examples/bom.yaml", b"\xef\xbb\xbf" + EXAMPLE_YAML.read_bytes()
        )
        self.source.sync()
        self.review_token([(self.datafile("examples/bom.yaml"), self.device_type)])

    def test_review_and_apply_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        token = self.review_token([(self.datafile(), self.device_type)])
        self.assertEqual(
            client.post(reverse(REVIEW_URL), {"source": self.source.pk}).status_code,
            403,
        )
        self.assertEqual(
            client.post(reverse(APPLY_URL), {"token": token}).status_code, 403
        )
        self.assertFalse(DeviceView.objects.exists())


class ApplyTests(DataSourceImportTestBase):
    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "No records were changed", status_code=400)
        self.assertFalse(DeviceView.objects.exists())
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(DeviceView)
            ).exists()
        )

    def test_tampered_token_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        self.assert_rejected(
            self.apply(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1])
        )

    def test_expired_token_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        later = time.time() + 15 * 60 + 1
        with mock.patch("django.core.signing.time.time", return_value=later):
            self.assert_rejected(self.apply(token))

    def test_token_from_another_user_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        other = User.objects.create_user(username="other", is_superuser=True)
        self.client.force_login(other)
        self.assert_rejected(self.apply(token))

    def test_add_constraint_is_checked_after_save(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DeviceView), actions=["add"]
        ).update(constraints={"device_type__model": "Other"})
        self.assert_rejected(self.apply(token))

    def test_file_changed_after_review_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        self.write_file(EXAMPLE_PATH, EXAMPLE_YAML.read_bytes() + b"\n# edited\n")
        self.source.sync()
        self.assert_rejected(self.apply(token))

    def test_pages_require_add_permission(self):
        ObjectPermission.objects.filter(actions=["add"]).delete()
        self.assertEqual(self.client.get(reverse(START_URL)).status_code, 403)
        self.assertEqual(self.client.post(reverse(REVIEW_URL)).status_code, 403)
        self.assertEqual(self.client.post(reverse(APPLY_URL)).status_code, 403)

    def test_anonymous_user_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse(START_URL))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class MigrationTests(TestCase):
    def test_import_needs_no_migration(self):
        call_command("makemigrations", "netbox_device_view", check=True, dry_run=True)
