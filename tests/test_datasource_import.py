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
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from core.models import DataFile, DataSource, ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.conf import settings
from django.core import signing
from django.core.management import call_command
from django.db import connection
from django.db.models.signals import post_save, pre_save
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.text import slugify
from users.models import ObjectPermission, User

from netbox_device_view import datasource_import
from netbox_device_view.models import DeviceView, RenderMode

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_YAML = EXAMPLES / "yaml" / "Cisco" / "C9300-24T.yaml"
EXAMPLE_PATH = "examples/yaml/Cisco/C9300-24T.yaml"
EXAMPLE_CSS = EXAMPLES / "Cisco" / "C9300-24T.css"
EXAMPLE_CSS_PATH = "examples/Cisco/C9300-24T.css"

START_URL = "plugins:netbox_device_view:deviceview_datasource_import"
REVIEW_URL = "plugins:netbox_device_view:deviceview_datasource_import_review"
APPLY_URL = "plugins:netbox_device_view:deviceview_datasource_import_apply"

RECORDED_EVENTS = []


def record_events(events):
    RECORDED_EVENTS.extend(events)


class _FormFields(HTMLParser):
    """Collect the values a browser submits for the first form posting to one action."""

    def __init__(self, action):
        super().__init__()
        self.action = action
        self.state = "before"
        self.select = None
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and self.state == "before":
            self.state = "inside" if attrs.get("action") == self.action else "before"
        if self.state != "inside":
            return
        if tag == "input" and attrs.get("name"):
            if attrs.get("type") == "checkbox" and "checked" not in attrs:
                return
            self.fields.setdefault(attrs["name"], []).append(attrs.get("value", ""))
        elif tag == "select":
            self.select = attrs["name"]
        elif tag == "option" and self.select and "selected" in attrs:
            self.fields.setdefault(self.select, []).append(attrs["value"])

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None
        elif tag == "form" and self.state == "inside":
            self.state = "after"


def submitted(response, url_name):
    """Return what the page's form for url_name would submit, without unchecked boxes."""
    parser = _FormFields(reverse(url_name))
    parser.feed(response.content.decode())
    assert parser.state == "after", f"no form posts to {url_name}"
    return parser.fields


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
        self.grant(DataFile, "view")
        self.grant(Manufacturer, "view")
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

    def match(self, datafiles):
        return self.client.post(
            reverse(REVIEW_URL),
            {
                "source": self.source.pk,
                "prefix": "examples/",
                "step": "match",
                "file": [datafile.pk for datafile in datafiles],
            },
        )

    def review_token(self, selections):
        response = self.review(selections)
        self.assertEqual(response.status_code, 200)
        token = response.context["review"].token
        self.assertTrue(token, response.context["review"].rows)
        return token

    def apply(self, token):
        return self.client.post(reverse(APPLY_URL), {"token": token})

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "No records were changed", status_code=400)
        self.assertFalse(DeviceView.objects.exists())
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(DeviceView)
            ).exists()
        )


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

        assign = self.client.post(
            reverse(REVIEW_URL),
            {
                "source": self.source.pk,
                "prefix": "examples/",
                "step": "match",
                "file": [datafile.pk],
            },
        )
        self.assertContains(assign, f'name="device_type_{datafile.pk}"')
        self.assertIsNone(assign.context["review"])

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

    def test_empty_catalog_says_so(self):
        response = self.client.get(
            reverse(START_URL), {"source": self.source.pk, "prefix": "nothing/"}
        )
        self.assertContains(response, "No layout files below this directory.")
        self.assertNotContains(response, "Assign device types")

    def test_datafile_hidden_by_permission_constraint_is_not_readable(self):
        other_path = "examples/yaml/Cisco/Other.yaml"
        self.write_file(other_path, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        hidden = self.datafile()
        token = self.review_token([(hidden, self.device_type)])
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DataFile)
        ).update(constraints={"path": other_path})

        self.assertEqual(self.catalog_paths("examples/"), [other_path])
        match = self.client.post(
            reverse(REVIEW_URL),
            {
                "source": self.source.pk,
                "prefix": "examples/",
                "step": "match",
                "file": [hidden.pk],
            },
        )
        self.assertEqual(match.status_code, 302)
        selection = datasource_import.Selection(
            datafile_id=hidden.pk, device_type_id=self.device_type.pk
        )
        review = datasource_import.review(self.user, self.source, [selection])
        self.assertEqual(review.rows[0].error, datasource_import.FILE_UNAVAILABLE)
        self.assertEqual(review.rows[0].proposed_fields, {})
        self.assertEqual(self.apply(token).status_code, 400)
        self.assertFalse(DeviceView.objects.exists())

    def test_catalog_does_not_load_file_content(self):
        with CaptureQueriesContext(connection) as queries:
            self.catalog_paths("examples/")
        datafile_queries = [q["sql"] for q in queries if '"core_datafile"' in q["sql"]]
        self.assertTrue(datafile_queries)
        for sql in datafile_queries:
            self.assertNotIn('"core_datafile"."data"', sql)


class SuggestionTests(DataSourceImportTestBase):
    def catalog(self, **params):
        response = self.client.get(
            reverse(START_URL),
            {"source": self.source.pk, "prefix": "examples/", **params},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def rows(self, **params):
        return {
            row.datafile.path: row for row in self.catalog(**params).context["rows"]
        }

    def assert_preselected(self, response, datafile, device_type):
        field = response.context["target_form"][f"device_type_{datafile.pk}"]
        self.assertEqual(field.value(), device_type.pk if device_type else None)

    def test_bundled_examples_are_suggested_by_exact_path_match(self):
        expected = {}
        for example in sorted(EXAMPLES.glob("yaml/*/*.yaml")):
            path = f"examples/{example.relative_to(EXAMPLES)}"
            self.write_file(path, example.read_bytes())
            manufacturer, _ = Manufacturer.objects.get_or_create(
                name=example.parent.name,
                defaults={"slug": slugify(example.parent.name)},
            )
            expected[path], _ = DeviceType.objects.get_or_create(
                manufacturer=manufacturer,
                model=example.stem,
                defaults={"slug": slugify(example.stem)},
            )
        self.source.sync()
        self.assertGreater(len(expected), 10)

        response = self.catalog(per_page=100)
        rows = {row.datafile.path: row for row in response.context["rows"]}
        self.assertEqual(set(rows), set(expected))
        for path, device_type in expected.items():
            with self.subTest(path=path):
                row = rows[path]
                self.assertEqual(row.suggestion, device_type)
                self.assertEqual(row.format, datasource_import.Format.YAML)
                self.assertEqual(
                    row.state, datasource_import.TargetState.NO_DEVICE_VIEW
                )
                self.assertEqual(row.problems, [])
                self.assertTrue(row.is_selectable)
        self.assertContains(response, "No device view")
        self.assertContains(
            response, reverse("dcim:devicetype", kwargs={"pk": self.device_type.pk})
        )

        datafiles = [self.datafile(path) for path in expected]
        match = self.match(datafiles)
        for datafile in datafiles:
            self.assert_preselected(match, datafile, expected[datafile.path])

    def test_only_exact_matches_are_suggested(self):
        Manufacturer.objects.create(name="Generic", slug="generic")
        paths = [
            "examples/cisco/C9300-24T.yaml",
            "examples/Cisco/c9300-24t.yaml",
            "examples/Cisco/C9300_24T.yaml",
            "examples/Cisco /C9300-24T.yaml",
            "examples/Generic/C9300-24T.yaml",
            "examples/C9300-24T.yaml",
            "examples/Cisco/C9300-24T/layout.yaml",
        ]
        for path in paths:
            self.write_file(path, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        rows = self.rows()
        for path in paths:
            with self.subTest(path=path):
                self.assertIsNone(rows[path].suggestion)
                self.assertEqual(rows[path].problems, [datasource_import.NO_MATCH])
                self.assertFalse(rows[path].is_selectable)
        self.assertEqual(rows[EXAMPLE_PATH].suggestion, self.device_type)
        self.assertFalse(Manufacturer.objects.filter(name="cisco").exists())

    def test_unmatched_file_is_manually_assignable(self):
        path = "examples/custom/my-layout.yaml"
        self.write_file(path, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        datafile = self.datafile(path)
        self.assert_preselected(self.match([datafile]), datafile, None)
        self.review_token([(datafile, self.device_type)])

    def test_suggestion_can_be_corrected_manually(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="Other", slug="other"
        )
        datafile = self.datafile()
        self.assert_preselected(self.match([datafile]), datafile, self.device_type)
        response = self.review([(datafile, other)])
        review = response.context["review"]
        self.assertEqual(review.rows[0].device_type, other)
        self.assertTrue(review.token)
        self.apply(review.token)
        self.assertTrue(DeviceView.objects.filter(device_type=other).exists())
        self.assertFalse(
            DeviceView.objects.filter(device_type=self.device_type).exists()
        )

    def test_ambiguous_matches_are_flagged_across_the_prefix(self):
        other_path = "examples/Cisco/C9300-24T.yml"
        self.write_file(other_path, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        for params in ({}, {"q": ".yml"}):
            with self.subTest(params=params):
                for row in self.rows(**params).values():
                    self.assertEqual(row.suggestion, self.device_type)
                    self.assertEqual(row.problems, [datasource_import.AMBIGUOUS_MATCH])
                    self.assertFalse(row.is_selectable)
        datafiles = [self.datafile(), self.datafile(other_path)]
        match = self.match(datafiles)
        for datafile in datafiles:
            self.assert_preselected(match, datafile, None)

    def test_hidden_manufacturer_or_device_type_is_not_suggested(self):
        for model, constraints in (
            (Manufacturer, {"name": "Other"}),
            (DeviceType, {"model": "Other"}),
        ):
            with self.subTest(model=model.__name__):
                permissions = ObjectPermission.objects.filter(
                    object_types=ObjectType.objects.get_for_model(model)
                )
                permissions.update(constraints=constraints)
                try:
                    row = self.rows()[EXAMPLE_PATH]
                    self.assertIsNone(row.suggestion)
                    self.assertFalse(row.is_selectable)
                finally:
                    permissions.update(constraints=None)

    def test_visible_existing_device_view_is_shown_and_not_preselected(self):
        device_view = DeviceView.objects.create(
            device_type=self.device_type, grid_template_area="x"
        )
        response = self.catalog()
        row = response.context["rows"][0]
        self.assertEqual(row.state, datasource_import.TargetState.EXISTING)
        self.assertEqual(row.device_view, device_view)
        self.assertFalse(row.is_selectable)
        self.assertContains(response, device_view.get_absolute_url())
        self.assert_preselected(self.match([self.datafile()]), self.datafile(), None)

    def test_hidden_device_view_is_only_unavailable(self):
        device_view = DeviceView.objects.create(
            device_type=self.device_type, grid_template_area="hidden-area"
        )
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DeviceView)
        ).update(constraints={"device_type__model": "Other"})
        response = self.catalog()
        row = response.context["rows"][0]
        self.assertEqual(row.state, datasource_import.TargetState.UNAVAILABLE)
        self.assertIsNone(row.device_view)
        self.assertEqual(row.problems, [datasource_import.TARGET_UNAVAILABLE])
        self.assertFalse(row.is_selectable)
        self.assertNotContains(response, device_view.get_absolute_url())
        self.assertNotContains(response, "hidden-area")
        review = self.review([(self.datafile(), self.device_type)]).context["review"]
        self.assertEqual(review.rows[0].error, datasource_import.TARGET_UNAVAILABLE)
        self.assert_preselected(self.match([self.datafile()]), self.datafile(), None)

    def test_file_named_only_by_its_suffix_is_listed(self):
        # Native sync skips dotfiles; other DataFile writers may not.
        for path in ("examples/Cisco/.yaml", "examples/.yml"):
            self.write_file(path, EXAMPLE_YAML.read_bytes())
            datafile = DataFile(source=self.source, path=path)
            datafile.refresh_from_disk(self.repo)
            datafile.save()
        rows = self.rows()
        for path in ("examples/Cisco/.yaml", "examples/.yml"):
            with self.subTest(path=path):
                self.assertEqual(rows[path].format, datasource_import.Format.YAML)
                self.assertIsNone(rows[path].suggestion)

    def test_device_type_of_hidden_manufacturer_is_not_a_permitted_target(self):
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(Manufacturer)
        ).update(constraints={"name": "Other"})
        self.assertIsNone(self.rows()[EXAMPLE_PATH].suggestion)
        response = self.review([(self.datafile(), self.device_type)])
        self.assertIsNone(response.context["review"])
        self.assertTrue(response.context["target_form"].errors)
        selection = datasource_import.Selection(
            datafile_id=self.datafile().pk, device_type_id=self.device_type.pk
        )
        review = datasource_import.review(self.user, self.source, [selection])
        self.assertEqual(review.rows[0].error, datasource_import.TARGET_UNAVAILABLE)

    def test_search_filters_paths_and_keeps_pagination(self):
        # The paginator keeps up to five orphans on the last page.
        paths = [f"examples/Ubiquiti/USW-{index:02}.yaml" for index in range(12)]
        for path in paths:
            self.write_file(path, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        self.assertEqual(set(self.rows(q="ubiquiti")), set(paths))
        first_page = self.catalog(q="ubiquiti", per_page=6)
        self.assertEqual(
            [row.datafile.path for row in first_page.context["rows"]], paths[:6]
        )
        self.assertContains(first_page, "q=ubiquiti")
        self.assertContains(first_page, 'value="ubiquiti"')
        empty = self.catalog(q="nothing-matches")
        self.assertContains(empty, "No layout files match the search.")
        self.assertNotContains(empty, "Assign device types")


class StrictYamlImportTests(DataSourceImportTestBase):
    INJECTION = 'x" onmouseover="alert(1)'

    def review_yaml(self, path, content):
        self.write_file(path, content.encode())
        self.source.sync()
        return self.review([(self.datafile(path), self.device_type)])

    def test_bundled_yaml_examples_pass_review_unchanged(self):
        selections = []
        for example in sorted(EXAMPLES.glob("yaml/*/*.yaml")):
            path = f"examples/{example.relative_to(EXAMPLES)}"
            self.write_file(path, example.read_bytes())
            manufacturer, _ = Manufacturer.objects.get_or_create(
                name=example.parent.name,
                defaults={"slug": slugify(example.parent.name)},
            )
            device_type, _ = DeviceType.objects.get_or_create(
                manufacturer=manufacturer,
                model=example.stem,
                defaults={"slug": slugify(example.stem)},
            )
            selections.append((path, device_type))
        self.source.sync()
        self.assertGreater(len(selections), 10)
        review = self.review(
            [(self.datafile(path), device_type) for path, device_type in selections]
        ).context["review"]
        self.assertEqual([row.error for row in review.rows], [""] * len(selections))
        self.assertTrue(review.token)

    def test_quoted_attribute_injection_is_rejected_before_any_write(self):
        base = EXAMPLE_YAML.read_text().replace(
            "views:\n  front:\n    rows:\n",
            "views:\n  front:\n    rows:\n"
            "      - elements:\n"
            f"          - {{key: mgmt0, css_classes: ['{self.INJECTION}']}}\n",
        )
        variant = EXAMPLE_YAML.read_text().replace(
            "    match: module\n    rows:\n",
            "    match: module\n    rows:\n"
            "      - elements:\n"
            f"          - {{key: mgmt0, css_classes: ['{self.INJECTION}']}}\n",
        )
        self.assertNotEqual(base, EXAMPLE_YAML.read_text())
        self.assertNotEqual(variant, EXAMPLE_YAML.read_text())
        for name, content in (("base", base), ("variant", variant)):
            with self.subTest(name):
                response = self.review_yaml(f"examples/{name}.yaml", content)
                review = response.context["review"]
                self.assertIn("css_classes", review.rows[0].error)
                self.assertEqual(review.token, "")
                self.assertNotContains(response, self.INJECTION)
                self.assertNotContains(response, "Apply import")
        self.assertFalse(DeviceView.objects.exists())
        self.assertFalse(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(DeviceView)
            ).exists()
        )

    def test_malformed_yaml_error_is_escaped(self):
        response = self.review_yaml(
            "examples/malformed.yaml", "views: [<script>alert(1)</script>"
        )
        error = response.context["review"].rows[0].error
        self.assertIn("<script>", error)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)")

    def test_unbounded_sequence_is_rejected(self):
        content = EXAMPLE_YAML.read_text().replace("count: 12", "count: 1000000000", 1)
        started = time.monotonic()
        review = self.review_yaml("examples/huge.yaml", content).context["review"]
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("10000 elements", review.rows[0].error)

    def test_review_rejects_shapes_the_lenient_parser_cannot_read(self):
        huge = "1" + "0" * 3000
        contents = {
            "views-list": ("version: 1\nviews: [front]\n", "views"),
            "duplicate-key": (
                "views:\n  front: {elements: [{key: p1}]}\n  front: {elements: [{key: p2}]}\n",
                "duplicate key",
            ),
            "huge-key-number": (
                (
                    "views:\n  front:\n    rows:\n      - sequence: "
                    f"{{prefix: p, count: {huge}, step: {huge}}}\n"
                ),
                "prefix",
            ),
        }
        wide = ", ".join(
            f"{{key: p{i}, span: {{cols: {'9' * 4299}}}}}" for i in range(12)
        )
        contents["huge-span"] = (
            f"views:\n  front:\n    rows:\n      - elements: [{wide}]\n      - blank: 1\n",
            "span.cols",
        )
        for name, (content, message) in contents.items():
            with self.subTest(name):
                response = self.review_yaml(f"examples/{name}.yaml", content)
                self.assertEqual(response.status_code, 200)
                review = response.context["review"]
                self.assertIn(message, review.rows[0].error)
                self.assertEqual(review.token, "")

    def test_local_definitions_keep_the_lenient_parser(self):
        # Rendering reads stored YAML with the lenient parser; only imports are strict.
        DeviceView.objects.create(
            device_type=self.device_type,
            yaml_layout="views:\n  front:\n    elements:\n      - {key: p1, extra: 1}\n",
        )
        tab = self.client.get(
            reverse("dcim:device_deviceview", kwargs={"pk": self.device.pk})
        )
        self.assertEqual(tab.status_code, 200)


class CssImportTests(DataSourceImportTestBase):
    def setUp(self):
        super().setUp()
        self.write_file(EXAMPLE_CSS_PATH, EXAMPLE_CSS.read_bytes())
        self.source.sync()

    def test_import_one_css_layout_end_to_end(self):
        css = EXAMPLE_CSS.read_text()
        catalog = self.client.get(
            reverse(START_URL),
            {"source": self.source.pk, "prefix": "examples/", "q": ".css"},
        )
        (row,) = catalog.context["rows"]
        self.assertEqual(row.datafile.path, EXAMPLE_CSS_PATH)
        self.assertEqual(row.format, datasource_import.Format.CSS)
        self.assertContains(catalog, "<td>CSS</td>", html=True)

        datafile = self.datafile(EXAMPLE_CSS_PATH)
        review = self.review([(datafile, self.device_type)])
        (reviewed,) = review.context["review"].rows
        self.assertEqual(reviewed.error, "")
        self.assertEqual(reviewed.format, datasource_import.Format.CSS)
        self.assertEqual(reviewed.render_mode_label, RenderMode.CSS.label)
        self.assertContains(review, "<td>CSS</td>", html=True)
        self.assertEqual(self.apply(review.context["review"].token).status_code, 200)

        device_view = DeviceView.objects.get(device_type=self.device_type)
        self.assertEqual(device_view.grid_template_area, css)
        self.assertEqual(device_view.yaml_layout, "")
        self.assertEqual(device_view.render_mode, RenderMode.CSS)
        tab = self.client.get(
            reverse("dcim:device_deviceview", kwargs={"pk": self.device.pk})
        )
        self.assertEqual(tab.status_code, 200)
        first_row = re.search(r'grid-template-areas:\s*"([^"]+)"', css)
        self.assertContains(tab, first_row.group(1))

    def test_css_whitespace_is_normalized(self):
        raw = '.deviceview.area {\n  grid-template-areas: "a\u00a0b\u2009c";\n}\n'
        self.write_file("examples/Cisco/spaces.css", raw.encode())
        self.source.sync()
        token = self.review_token(
            [(self.datafile("examples/Cisco/spaces.css"), self.device_type)]
        )
        self.apply(token)
        self.assertEqual(
            DeviceView.objects.get().grid_template_area,
            '.deviceview.area {\n  grid-template-areas: "a b c";\n}\n',
        )

    def test_imported_css_cannot_close_the_style_element(self):
        raw = (
            '.deviceview.area { grid-template-areas: "a<b"; }\n'
            "</style><script>alert(1)</script>\n"
        )
        self.write_file("examples/Cisco/inject.css", raw.encode())
        self.source.sync()
        self.apply(
            self.review_token(
                [(self.datafile("examples/Cisco/inject.css"), self.device_type)]
            )
        )
        plugin_config = {
            **settings.PLUGINS_CONFIG["netbox_device_view"],
            "show_on_device_tab": True,
        }
        with override_settings(
            PLUGINS_CONFIG={
                **settings.PLUGINS_CONFIG,
                "netbox_device_view": plugin_config,
            }
        ):
            pages = [
                self.client.get(
                    reverse("dcim:device_deviceview", kwargs={"pk": self.device.pk})
                ),
                self.client.get(self.device.get_absolute_url()),
            ]
        for page in pages:
            with self.subTest(url=page.request["PATH_INFO"]):
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, "<script>alert(1)")
                self.assertContains(page, '"a\\3c b"')
                self.assertContains(page, "\\3c /style>\\3c script>")

    def test_empty_or_non_utf8_css_is_rejected(self):
        self.write_file("examples/empty.css", b" \n\t\n")
        self.write_file(
            "examples/latin1.css", ".deviceview.area { caf\xe9 }".encode("latin-1")
        )
        self.source.sync()
        for path in ("examples/empty.css", "examples/latin1.css"):
            with self.subTest(path=path):
                review = self.review([(self.datafile(path), self.device_type)])
                self.assertTrue(review.context["review"].rows[0].error)
                self.assertEqual(review.context["review"].token, "")

    def test_bundled_css_examples_pass_review(self):
        selections = []
        for example in sorted(EXAMPLES.glob("*/*.css")):
            path = f"examples/{example.relative_to(EXAMPLES)}"
            self.write_file(path, example.read_bytes())
            manufacturer, _ = Manufacturer.objects.get_or_create(
                name=example.parent.name,
                defaults={"slug": slugify(example.parent.name)},
            )
            device_type, _ = DeviceType.objects.get_or_create(
                manufacturer=manufacturer,
                model=example.stem,
                defaults={"slug": slugify(example.stem)},
            )
            selections.append((path, device_type))
        self.source.sync()
        self.assertGreater(len(selections), 10)
        review = self.review(
            [(self.datafile(path), device_type) for path, device_type in selections]
        ).context["review"]
        self.assertEqual([row.error for row in review.rows], [""] * len(selections))
        self.assertTrue(review.token)

    def test_mixed_yaml_and_css_selection(self):
        other = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="Other", slug="other"
        )
        token = self.review_token(
            [
                (self.datafile(), self.device_type),
                (self.datafile(EXAMPLE_CSS_PATH), other),
            ]
        )
        self.assertEqual(self.apply(token).status_code, 200)
        from_yaml = DeviceView.objects.get(device_type=self.device_type)
        from_css = DeviceView.objects.get(device_type=other)
        self.assertEqual(from_yaml.yaml_layout, EXAMPLE_YAML.read_text())
        self.assertEqual(from_css.yaml_layout, "")
        self.assertEqual(from_css.grid_template_area, EXAMPLE_CSS.read_text())

    def test_yaml_and_css_for_one_model_are_ambiguous(self):
        response = self.client.get(
            reverse(START_URL), {"source": self.source.pk, "prefix": "examples/"}
        )
        rows = {row.datafile.path: row for row in response.context["rows"]}
        for path in (EXAMPLE_PATH, EXAMPLE_CSS_PATH):
            with self.subTest(path=path):
                self.assertEqual(rows[path].suggestion, self.device_type)
                self.assertEqual(
                    rows[path].problems, [datasource_import.AMBIGUOUS_MATCH]
                )
        review = self.review(
            [
                (self.datafile(), self.device_type),
                (self.datafile(EXAMPLE_CSS_PATH), self.device_type),
            ]
        ).context["review"]
        self.assertEqual(
            [row.error for row in review.rows], [datasource_import.DUPLICATE_TARGET] * 2
        )
        self.assertEqual(review.token, "")
        self.review_token([(self.datafile(EXAMPLE_CSS_PATH), self.device_type)])


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
                    "datafile_id": datafile.pk,
                    "path": EXAMPLE_PATH,
                    "sha256": hashlib.sha256(EXAMPLE_YAML.read_bytes()).hexdigest(),
                    "device_type_id": self.device_type.pk,
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

    def test_non_numeric_file_selection_is_rejected(self):
        response = self.client.post(
            reverse(REVIEW_URL),
            {
                "source": self.source.pk,
                "prefix": "examples/",
                "step": "match",
                "file": [self.datafile().pk, "x"],
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_non_ascii_or_oversized_file_selection_is_rejected(self):
        for value in ("\u00b2", "\u0661", "9" * 5000):
            with self.subTest(value=value[:8]):
                response = self.client.post(
                    reverse(REVIEW_URL),
                    {
                        "source": self.source.pk,
                        "prefix": "examples/",
                        "step": "match",
                        "file": [value],
                    },
                )
                self.assertEqual(response.status_code, 302)

    def test_two_files_for_one_device_type_are_rejected_until_one_is_kept(self):
        other_path = "examples/yaml/Cisco/Other.yaml"
        self.write_file(other_path, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        response = self.review(
            [
                (self.datafile(), self.device_type),
                (self.datafile(other_path), self.device_type),
            ]
        )
        review = response.context["review"]
        self.assertEqual(
            [row.error for row in review.rows], [datasource_import.DUPLICATE_TARGET] * 2
        )
        self.assertEqual(review.token, "")
        self.assertNotContains(response, "Apply import")

        fields = submitted(response, REVIEW_URL)
        fields["file"].remove(str(self.datafile(other_path).pk))
        kept = self.client.post(reverse(REVIEW_URL), fields).context["review"]
        self.assertEqual([row.path for row in kept.rows], [EXAMPLE_PATH])
        self.assertTrue(kept.token)

    def test_review_rejects_a_source_the_user_cannot_view(self):
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DataSource)
        ).update(constraints={"name": "Other"})
        selection = datasource_import.Selection(
            datafile_id=self.datafile().pk, device_type_id=self.device_type.pk
        )
        with self.assertRaises(datasource_import.ImportRejected):
            datasource_import.review(self.user, self.source, [selection])

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
    def test_tampered_token_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        self.assert_rejected(
            self.apply(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1])
        )

    def test_expired_token_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        later = time.time() + datasource_import.REVIEW_MAX_AGE_SECONDS + 1
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

    @override_settings(EVENTS_PIPELINE=["tests.test_datasource_import.record_events"])
    def test_rejected_apply_sends_no_events(self):
        token = self.review_token([(self.datafile(), self.device_type)])
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DeviceView), actions=["add"]
        ).update(constraints={"device_type__model": "Other"})
        RECORDED_EVENTS.clear()
        self.assert_rejected(self.apply(token))
        self.assertEqual(RECORDED_EVENTS, [])

        ObjectPermission.objects.filter(actions=["add"]).update(constraints=None)
        self.assertEqual(self.apply(token).status_code, 200)
        self.assertEqual(
            [event["object_type"].model for event in RECORDED_EVENTS], ["deviceview"]
        )

    def test_concurrent_device_view_for_the_target_is_rejected(self):
        token = self.review_token([(self.datafile(), self.device_type)])

        def create_competing_view(sender, instance, **kwargs):
            # bulk_create sends no signals, so this receiver runs once.
            DeviceView.objects.bulk_create(
                [DeviceView(device_type=instance.device_type, grid_template_area="x")]
            )

        pre_save.connect(create_competing_view, sender=DeviceView)
        self.addCleanup(pre_save.disconnect, create_competing_view, sender=DeviceView)
        response = self.apply(token)
        self.assert_rejected(response)
        self.assertContains(
            response,
            "a device view already exists for this device type",
            status_code=400,
        )

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


class BulkImportTests(DataSourceImportTestBase):
    # In catalog order, which sorts by path.
    BULK_PATHS = (
        "examples/Cisco/C9300-48P.css",
        EXAMPLE_PATH,
        "examples/yaml/Cisco/C9500-24Y4C.yaml",
    )

    def setUp(self):
        super().setUp()
        for path in (self.BULK_PATHS[0], self.BULK_PATHS[2]):
            self.write_file(path, (EXAMPLES.parent / path).read_bytes())
        self.source.sync()
        for model in ("C9300-48P", "C9500-24Y4C"):
            DeviceType.objects.create(
                manufacturer=self.manufacturer, model=model, slug=slugify(model)
            )

    def catalog(self):
        return self.client.get(
            reverse(START_URL), {"source": self.source.pk, "prefix": "examples/"}
        )

    def review_page(self, datafiles):
        """Submit the catalog and the match pages as a browser would."""
        fields = submitted(self.catalog(), REVIEW_URL)
        fields["file"] = [str(datafile.pk) for datafile in datafiles]
        match = self.client.post(reverse(REVIEW_URL), fields)
        self.assertEqual(match.status_code, 200)
        return self.client.post(reverse(REVIEW_URL), submitted(match, REVIEW_URL))

    def write_sized(self, count, size, name="file"):
        for index in range(count):
            self.write_file(f"examples/limits/{name}{index:03}.css", b"\xff" * size)
        self.source.sync()
        return list(
            self.source.datafiles.filter(path__startswith="examples/limits/").order_by(
                "path"
            )
        )

    def test_mixed_bulk_create_end_to_end(self):
        datafiles = [self.datafile(path) for path in self.BULK_PATHS]
        review = self.review_page(datafiles)
        self.assertEqual(
            [row.format for row in review.context["review"].rows],
            [
                datasource_import.Format.CSS,
                datasource_import.Format.YAML,
                datasource_import.Format.YAML,
            ],
        )
        result = self.client.post(reverse(APPLY_URL), submitted(review, APPLY_URL))
        self.assertEqual(result.status_code, 200)
        created = DeviceView.objects.order_by("device_type__model")
        self.assertEqual(
            [device_view.device_type.model for device_view in created],
            ["C9300-24T", "C9300-48P", "C9500-24Y4C"],
        )
        for device_view in created:
            self.assertContains(result, device_view.get_absolute_url())
        self.assertEqual(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(DeviceView),
                action="create",
            ).count(),
            3,
        )

    def test_result_links_only_visible_device_views(self):
        ObjectPermission.objects.filter(
            object_types=ObjectType.objects.get_for_model(DeviceView), actions=["view"]
        ).update(constraints={"device_type__model": "C9300-24T"})
        datafiles = [self.datafile(path) for path in self.BULK_PATHS[:2]]
        result = self.client.post(
            reverse(APPLY_URL), submitted(self.review_page(datafiles), APPLY_URL)
        )
        self.assertEqual(result.status_code, 200)
        visible = DeviceView.objects.get(device_type__model="C9300-24T")
        hidden = DeviceView.objects.get(device_type__model="C9300-48P")
        self.assertContains(result, visible.get_absolute_url())
        self.assertNotContains(result, hidden.get_absolute_url())
        self.assertContains(result, str(hidden))

    def test_file_over_size_limit_is_rejected_before_decoding(self):
        limit = datasource_import.MAX_FILE_BYTES
        padded = EXAMPLE_YAML.read_bytes()
        padded += b"#" * (limit - len(padded) - 1) + b"\n"
        self.write_file(EXAMPLE_PATH, padded)
        self.write_file("examples/limits/big.css", b"\xff" * (limit + 1))
        self.source.sync()
        self.review_token([(self.datafile(), self.device_type)])
        review = self.review(
            [(self.datafile("examples/limits/big.css"), self.device_type)]
        ).context["review"]
        self.assertEqual(review.rows[0].error, datasource_import.FILE_TOO_LARGE)

    def test_batch_over_byte_limit_is_rejected_before_reading(self):
        limit = datasource_import.MAX_BATCH_BYTES
        size = datasource_import.MAX_FILE_BYTES
        datafiles = self.write_sized(limit // size + 1, size)
        at_limit = self.review(
            [(datafile, self.device_type) for datafile in datafiles[:-1]]
        ).context["review"]
        self.assertEqual(at_limit.errors, [])
        response = self.review([(datafile, self.device_type) for datafile in datafiles])
        review = response.context["review"]
        self.assertEqual(review.errors, [datasource_import.BATCH_TOO_LARGE])
        self.assertEqual(review.rows, [])
        self.assertEqual(review.token, "")
        self.assertContains(response, datasource_import.BATCH_TOO_LARGE)

    def test_batch_over_row_limit_is_rejected_before_reading(self):
        limit = datasource_import.MAX_BATCH_ROWS
        datafiles = self.write_sized(limit + 1, 1)
        at_limit = self.review(
            [(datafile, self.device_type) for datafile in datafiles[:-1]]
        ).context["review"]
        self.assertEqual(at_limit.errors, [])
        for step in ("match", "review"):
            with self.subTest(step=step):
                fields = {
                    "source": self.source.pk,
                    "prefix": "examples/",
                    "step": step,
                    "file": [datafile.pk for datafile in datafiles],
                }
                response = self.client.post(reverse(REVIEW_URL), fields, follow=True)
                self.assertEqual(response.redirect_chain[0][1], 302)
                self.assertContains(response, datasource_import.TOO_MANY_ROWS)
        selections = [
            datasource_import.Selection(datafile.pk, self.device_type.pk)
            for datafile in datafiles
        ]
        review = datasource_import.review(self.user, self.source, selections)
        self.assertEqual(review.errors, [datasource_import.TOO_MANY_ROWS])
        self.assertEqual(review.rows, [])

    def test_limits_use_the_stored_bytes_not_the_size_column(self):
        size = datasource_import.MAX_FILE_BYTES
        self.write_file("examples/big.css", b"\xff" * (size + 1))
        datafiles = self.write_sized(
            datasource_import.MAX_BATCH_BYTES // size + 1, size
        )
        big = self.datafile("examples/big.css")
        DataFile.objects.filter(pk__in=[big.pk, *(df.pk for df in datafiles)]).update(
            size=1
        )
        review = self.review(
            [(datafile, self.device_type) for datafile in datafiles]
        ).context["review"]
        self.assertEqual(review.errors, [datasource_import.BATCH_TOO_LARGE])
        single = self.review([(big, self.device_type)]).context["review"]
        self.assertEqual(single.rows[0].error, datasource_import.FILE_TOO_LARGE)

    def test_a_file_selected_twice_is_rejected(self):
        other = DeviceType.objects.get(model="C9500-24Y4C")
        selections = [
            datasource_import.Selection(self.datafile().pk, device_type.pk)
            for device_type in (self.device_type, other)
        ]
        review = datasource_import.review(self.user, self.source, selections)
        self.assertEqual(review.errors, [datasource_import.REPEATED_FILE])
        self.assertEqual(review.token, "")

    def test_invalid_unselected_file_does_not_block_a_valid_batch(self):
        self.write_file("examples/yaml/Cisco/Broken.yaml", b"views: [not closed")
        self.source.sync()
        datafiles = [self.datafile(path) for path in self.BULK_PATHS[:2]]
        review = self.review_page(datafiles)
        self.assertTrue(review.context["review"].token)
        result = self.client.post(reverse(APPLY_URL), submitted(review, APPLY_URL))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(DeviceView.objects.count(), 2)

    def test_mixed_valid_invalid_review_keeps_selections_and_corrections(self):
        broken = "examples/yaml/Cisco/Broken.yaml"
        self.write_file(broken, b"views: [not closed")
        self.source.sync()
        other = DeviceType.objects.get(model="C9500-24Y4C")
        response = self.review(
            [(self.datafile(), self.device_type), (self.datafile(broken), other)]
        )
        review = response.context["review"]
        errors = {row.path: row.error for row in review.rows}
        self.assertEqual(errors[EXAMPLE_PATH], "")
        self.assertTrue(errors[broken])
        self.assertEqual(review.token, "")
        self.assertFalse(DeviceView.objects.exists())

        fields = submitted(response, REVIEW_URL)
        self.assertEqual(
            fields["file"], [str(self.datafile(broken).pk), str(self.datafile().pk)]
        )
        self.assertEqual(
            fields[f"device_type_{self.datafile(broken).pk}"], [str(other.pk)]
        )

    def test_one_invalid_row_rolls_back_the_whole_apply(self):
        last = "examples/yaml/Cisco/C9500-24Y4C.yaml"
        datafiles = [self.datafile(path) for path in self.BULK_PATHS]
        review = self.review_page(datafiles)
        apply_fields = submitted(review, APPLY_URL)
        self.write_file(last, (EXAMPLES.parent / last).read_bytes() + b"\n# edited\n")
        self.source.sync()
        saved = []

        def record_save(sender, instance, created, **kwargs):
            saved.append(instance.device_type.model)

        post_save.connect(record_save, sender=DeviceView)
        self.addCleanup(post_save.disconnect, record_save, sender=DeviceView)
        response = self.client.post(reverse(APPLY_URL), apply_fields)
        self.assertEqual(saved, ["C9300-48P", "C9300-24T"])
        self.assert_rejected(response)
        self.assertContains(response, last, status_code=400)
        self.assertNotContains(response, "Created", status_code=400)

    def test_rejected_apply_offers_a_new_review_with_the_same_selections(self):
        manual = "examples/yaml/Cisco/Manual.yaml"
        self.write_file(manual, EXAMPLE_YAML.read_bytes())
        self.source.sync()
        corrected = DeviceType.objects.get(model="C9500-24Y4C")
        selections = [
            (self.datafile(), self.device_type),
            (self.datafile(manual), corrected),
        ]
        review = self.review(selections)
        apply_fields = submitted(review, APPLY_URL)
        self.write_file(manual, EXAMPLE_YAML.read_bytes() + b"\n# edited\n")
        self.source.sync()
        rejected = self.client.post(reverse(APPLY_URL), apply_fields)
        self.assert_rejected(rejected)

        again = self.client.post(reverse(REVIEW_URL), submitted(rejected, REVIEW_URL))
        rows = again.context["review"].rows
        self.assertEqual(
            [(row.path, row.device_type) for row in rows],
            [(EXAMPLE_PATH, self.device_type), (manual, corrected)],
        )
        self.assertTrue(again.context["review"].token)

    def test_apply_ignores_selection_fields_outside_the_token(self):
        review = self.review([(self.datafile(), self.device_type)])
        fields = submitted(review, APPLY_URL)
        other = DeviceType.objects.get(model="C9500-24Y4C")
        fields[f"device_type_{self.datafile().pk}"] = [str(other.pk)]
        fields["file"].append(str(self.datafile(self.BULK_PATHS[0]).pk))
        self.assertEqual(self.client.post(reverse(APPLY_URL), fields).status_code, 200)
        self.assertEqual(
            list(DeviceView.objects.values_list("device_type", flat=True)),
            [self.device_type.pk],
        )


class MigrationTests(TestCase):
    def test_import_needs_no_migration(self):
        call_command("makemigrations", "netbox_device_view", check=True, dry_run=True)
