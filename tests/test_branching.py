"""Plugin behaviour in an active NetBox Branching branch; skipped when netbox-branching is not enabled."""

import shutil
import tempfile
import unittest

from core.models import ObjectType
from django.apps import apps
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view.artwork.branching import CATALOGUE
from netbox_device_view.models import DefinitionRevision, DeviceView

from .artwork_samples import bundle
from .test_artwork_import import IMPORT_URL as ARTWORK_IMPORT_URL
from .test_datasource_import import APPLY_URL, EXAMPLE_PATH, EXAMPLE_YAML
from .test_datasource_import_concurrency import ConcurrentApplyTestBase

BRANCHING = "netbox_branching" in settings.PLUGINS


def provision_branch(name):
    from netbox_branching.models import Branch

    branch = Branch(name=name)
    branch.save(provision=False)
    branch.provision(user=None)
    branch.refresh_from_db()
    return branch


@unittest.skipUnless(BRANCHING, "netbox-branching is not enabled")
class DataSourceImportInBranchTests(ConcurrentApplyTestBase):
    def setUp(self):
        super().setUp()
        self.branch = provision_branch("Layouts")
        self.addCleanup(self.branch.deprovision)
        self.clients[0].cookies["active_branch"] = self.branch.schema_id

    def test_import_creates_the_device_view_in_the_branch_only(self):
        from netbox_branching.utilities import activate_branch

        response = self.clients[0].post(
            reverse(APPLY_URL), {"token": self.token(EXAMPLE_PATH)}
        )

        self.assertContains(response, "Created: 1")
        self.assertFalse(DeviceView.objects.exists())
        with activate_branch(self.branch):
            self.assertEqual(
                DeviceView.objects.get().yaml_layout, EXAMPLE_YAML.read_text()
            )


class CatalogueModelTests(SimpleTestCase):
    def test_every_plugin_model_except_device_view_is_in_the_catalogue(self):
        labels = {
            model._meta.label_lower
            for model in apps.get_app_config("netbox_device_view").get_models()
        }

        self.assertEqual(labels - CATALOGUE, {"netbox_device_view.deviceview"})


@unittest.skipUnless(BRANCHING, "netbox-branching is not enabled")
class ArtworkImportInBranchTests(TransactionTestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        user = User.objects.create_user(username="importer")
        permission = ObjectPermission.objects.create(
            name="revision-all", actions=["view", "add"]
        )
        permission.users.add(user)
        permission.object_types.add(
            ObjectType.objects.get_for_model(DefinitionRevision)
        )
        self.client.force_login(user)
        self.branch = provision_branch("Artwork")
        self.addCleanup(self.branch.deprovision)
        self.client.cookies["active_branch"] = self.branch.schema_id

    def test_a_bundle_import_in_a_branch_stores_the_revision_in_main(self):
        response = self.client.post(
            reverse(ARTWORK_IMPORT_URL),
            {"bundle": SimpleUploadedFile("sw24.zip", bundle())},
        )

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.definition.key, "acme/sw-24")
        self.assertEqual(
            sorted(revision.files.values_list("path", flat=True)),
            ["front.svg", "manifest.yaml", "rear.svg"],
        )
        self.assertEqual(revision.imports.get().source_name, "sw24.zip")
